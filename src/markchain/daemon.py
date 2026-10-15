"""One background Markchain process per state directory, shared over a socket.

Every MCP client (Codex, Claude Code, ...) starts ``markchain serve``. That
command is a thin relay: it connects to this daemon, starting it when absent.
The daemon owns the queue, the LangGraph state, the file watcher and the
managed Elasticsearch process, and exits by itself after an idle period.
"""

from __future__ import annotations

__all__ = ["Daemon", "DaemonInfo", "socket_path"]

import functools
import hashlib
import json
import logging
import os
import signal
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
import anyio.abc
import mcp.types as types
from anyio.streams.buffered import BufferedByteReceiveStream
from mcp.shared.message import SessionMessage

from markchain import __version__
from markchain.errors import RequestError
from markchain.service import Service

logger = logging.getLogger(__name__)

# Large enough for a 2 MiB request or a full search response line.
_MAX_LINE = 32 * 1024 * 1024
_IDLE_CHECK_SECONDS = 15
_SOCKET_LIMIT = 100  # macOS sun_path is 104 bytes including the terminator


def socket_path(state: Path) -> Path:
    """Socket inside the state folder, or a short temp path when that is too long."""
    path = state.resolve() / "markchain.sock"
    if len(str(path).encode()) < _SOCKET_LIMIT:
        return path
    name = "markchain-" + hashlib.sha256(str(state.resolve()).encode()).hexdigest()[:16]
    for folder in (Path(tempfile.gettempdir()), Path("/tmp")):
        candidate = folder / f"{name}.sock"
        if len(str(candidate).encode()) < _SOCKET_LIMIT:
            return candidate
    raise RequestError(f"소켓 경로를 만들 수 없습니다: {path}")


@dataclass(frozen=True)
class DaemonInfo:
    pid: int
    version: str

    @staticmethod
    def read(state: Path) -> DaemonInfo | None:
        try:
            data = json.loads((state / "daemon.json").read_text())
            return DaemonInfo(int(data["pid"]), str(data["version"]))
        except (OSError, ValueError, KeyError):
            return None

    def write(self, state: Path) -> None:
        temporary = state / f"daemon.json.{os.getpid()}"
        temporary.write_text(json.dumps({"pid": self.pid, "version": self.version}))
        os.replace(temporary, state / "daemon.json")


def _threaded(function: Callable[..., Any]) -> Callable[..., Awaitable[Any]]:
    """Run a blocking tool in a worker thread so other sessions stay responsive."""

    @functools.wraps(function)
    async def wrapper(**arguments: Any) -> Any:
        return await anyio.to_thread.run_sync(functools.partial(function, **arguments))

    return wrapper


class Daemon:
    def __init__(self, service: Service, state: Path, idle_minutes: float) -> None:
        self.service = service
        self.state = state.resolve()
        self.path = socket_path(self.state)
        self.idle_seconds = idle_minutes * 60
        self._connections = 0
        self._shutdown = threading.Event()
        server = service.mcp._mcp_server
        self._server = server
        # Re-register tools as non-blocking coroutines for the shared event loop.
        tools = service.mcp._tool_manager
        for name, tool in list(tools._tools.items()):
            if tool.is_async:
                continue
            tools._tools.pop(name)
            service.mcp.add_tool(
                _threaded(tool.fn), name=name, description=tool.description
            )

    def request_shutdown(self, *_: object) -> None:
        self._shutdown.set()

    # cost: time O(lifetime), heap O(sessions), stack O(1), io O(messages)
    # basis: estimate
    def run(self) -> None:
        signal.signal(signal.SIGTERM, self.request_shutdown)
        signal.signal(signal.SIGINT, self.request_shutdown)
        self.path.unlink(missing_ok=True)
        try:
            anyio.run(self._serve)
        finally:
            self.path.unlink(missing_ok=True)
            (self.state / "daemon.json").unlink(missing_ok=True)

    async def _serve(self) -> None:
        # Publish identity before listening so a connected client can find it.
        DaemonInfo(os.getpid(), __version__).write(self.state)
        listener = await anyio.create_unix_listener(self.path)
        os.chmod(self.path, 0o600)
        logger.info("본체 시작 pid=%d socket=%s", os.getpid(), self.path)
        async with anyio.create_task_group() as group:
            group.start_soon(self._accept, listener)
            await self._wait_for_exit()
            # Stop accepting first so a new client starts a fresh daemon.
            await listener.aclose()
            self.path.unlink(missing_ok=True)
            group.cancel_scope.cancel()
        logger.info("본체 종료")

    async def _accept(self, listener: anyio.abc.Listener[Any]) -> None:
        try:
            await listener.serve(self._session)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            return

    async def _wait_for_exit(self) -> None:
        while not self._shutdown.is_set():
            await anyio.sleep(1)
            if self.idle_seconds <= 0 or self._connections:
                continue
            quiet = time.monotonic() - self.service.last_activity
            if quiet >= self.idle_seconds and self.service.idle():
                logger.info("유휴 %d초, 본체 종료", int(quiet))
                return

    async def _session(self, stream: anyio.abc.SocketStream) -> None:
        self._connections += 1
        self.service.touch()
        try:
            await self._run_session(stream)
        except Exception:
            logger.exception("세션 오류")
        finally:
            self._connections -= 1
            self.service.touch()
            await stream.aclose()

    async def _run_session(self, stream: anyio.abc.SocketStream) -> None:
        incoming_writer, incoming = anyio.create_memory_object_stream[
            SessionMessage | Exception
        ](0)
        outgoing, outgoing_reader = anyio.create_memory_object_stream[SessionMessage](0)
        buffered = BufferedByteReceiveStream(stream)

        async def read() -> None:
            async with incoming_writer:
                while True:
                    try:
                        line = await buffered.receive_until(b"\n", _MAX_LINE)
                    except (anyio.EndOfStream, anyio.IncompleteRead):
                        return
                    if not line.strip():
                        continue
                    try:
                        message = types.JSONRPCMessage.model_validate_json(line)
                    except ValueError as error:
                        await incoming_writer.send(error)
                        continue
                    await incoming_writer.send(SessionMessage(message))

        async def write() -> None:
            async with outgoing_reader:
                async for item in outgoing_reader:
                    data = item.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await stream.send(data.encode() + b"\n")

        async with anyio.create_task_group() as group:
            group.start_soon(read)
            group.start_soon(write)
            await self._server.run(
                incoming, outgoing, self._server.create_initialization_options()
            )
            group.cancel_scope.cancel()
