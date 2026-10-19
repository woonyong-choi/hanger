"""Relay an MCP stdio session to the shared daemon, restarting it when needed."""

from __future__ import annotations

__all__ = ["Launcher", "Relay", "call_tool"]

import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import mcp.types as types
from filelock import FileLock, Timeout
from packaging.version import InvalidVersion, Version

from markdown_notes_agent import __version__
from markdown_notes_agent.daemon import DaemonInfo, socket_path
from markdown_notes_agent.errors import MarkdownNotesAgentError, RequestError

logger = logging.getLogger(__name__)

_CONNECT_SECONDS = 60
_STOP_SECONDS = 90
_PING_SECONDS = 30
_PING_TIMEOUT = 90
_INTERNAL = "markdown-notes-agent-proxy-"
_CALL_SECONDS = 600


# Daemons this process started; polled so exited ones do not linger as zombies.
_children: list[subprocess.Popen[bytes]] = []


def _reap() -> None:
    _children[:] = [child for child in _children if child.poll() is None]


def _older(running: str) -> bool:
    """Return whether a running daemon is older than this client.

    Only older daemons are replaced, so a client left over from an earlier
    version never stops a newer daemon over and over.
    """
    try:
        return Version(running) < Version(__version__)
    except InvalidVersion:
        return running != __version__


def _alive(pid: int) -> bool:
    """Return whether pid runs; an exited process awaiting its parent counts as gone."""
    _reap()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        status = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    except OSError:
        return True
    return bool(status) and not status.startswith("Z")


def _is_daemon(pid: int) -> bool:
    """Guard against signalling an unrelated process that reused the pid."""
    try:
        command = subprocess.run(
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
        ).stdout
    except OSError:
        return False
    return (
        "markdown_notes_agent.cli" in command or "markdown-notes-agent" in command
    ) and "daemon" in command


class Launcher:
    """Find the daemon for a state directory, starting or replacing it."""

    def __init__(self, state: Path, command: list[str]) -> None:
        self.state = state.resolve()
        self.path = socket_path(self.state)
        self.command = command

    def connect(self) -> socket.socket | None:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            client.connect(str(self.path))
        except OSError:
            client.close()
            return None
        return client

    def spawn(self) -> None:
        logs = self.state / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "daemon.out").open("ab") as output:
            _reap()
            child = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            _children.append(child)
        # Collect the exit status at once so a stopped daemon never lingers.
        threading.Thread(target=child.wait, name="daemon-reaper", daemon=True).start()

    # cost: time O(s), heap O(1), stack O(1), io O(s)
    # vars: s = daemon start or replacement time
    # basis: estimate
    def ensure(self) -> socket.socket:
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = DaemonInfo.read(self.state)
        if info is not None and _older(info.version) and _alive(info.pid):
            logger.info("replacing service %s with %s", info.version, __version__)
            self.stop()
        client = self.connect()
        if client is not None:
            return client
        # One client at a time decides whether to start a daemon; setup and
        # stop hold the same lock so nothing restarts the daemon meanwhile.
        try:
            lock = FileLock(self.state / "spawn.lock", timeout=_CONNECT_SECONDS * 2)
            lock.acquire()
        except Timeout as error:
            raise MarkdownNotesAgentError(
                "background service is being set up; try again shortly",
                code="daemon_busy",
            ) from error
        try:
            client = self.connect()
            if client is not None:
                return client
            info = DaemonInfo.read(self.state)
            if info is not None and _alive(info.pid) and _is_daemon(info.pid):
                # Alive but not listening: starting up, shutting down or hung.
                client = self._wait(15)
                if client is not None:
                    return client
                self.kill()
            self.spawn()
            client = self._wait(_CONNECT_SECONDS)
        finally:
            lock.release()
        if client is None:
            raise MarkdownNotesAgentError(
                f"background service did not start; see {self.state / 'logs'}",
                code="daemon_start",
            )
        return client

    def _wait(self, seconds: float) -> socket.socket | None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            client = self.connect()
            if client is not None:
                return client
            time.sleep(0.2)
        return None

    @contextmanager
    def hold(self) -> Iterator[None]:
        """Keep clients from starting a daemon while setup changes things."""
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(self.state / "spawn.lock"):
            yield

    def stop(self) -> bool:
        """Stop the daemon; call inside hold() to keep it from restarting."""
        info = DaemonInfo.read(self.state)
        if info is None or not _alive(info.pid) or not _is_daemon(info.pid):
            return False
        os.kill(info.pid, signal.SIGTERM)
        deadline = time.monotonic() + _STOP_SECONDS
        while time.monotonic() < deadline and _alive(info.pid):
            time.sleep(0.2)
        if _alive(info.pid):
            self.kill()
        return True

    def kill(self) -> None:
        info = DaemonInfo.read(self.state)
        if info is not None and _alive(info.pid) and _is_daemon(info.pid):
            logger.warning("background service did not stop; killed pid %d", info.pid)
            os.kill(info.pid, signal.SIGKILL)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and _alive(info.pid):
                time.sleep(0.1)


def _lines(connection: socket.socket) -> Any:
    with connection.makefile("rb") as reader:
        while True:
            line = reader.readline()
            if not line:
                return
            yield line


class Relay:
    """Copy JSON-RPC lines between stdio and the daemon, surviving restarts.

    The client's initialize handshake and unanswered requests are replayed
    after a reconnect. Request keys make replayed mutations idempotent.
    After a disconnect the relay reconnects only when the client sends
    something, so a stopped daemon stays stopped until it is needed.
    """

    def __init__(self, launcher: Launcher, source: BinaryIO, sink: BinaryIO) -> None:
        self.launcher = launcher
        self.source = source
        self.sink = sink
        self._socket: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._sink_lock = threading.Lock()
        self._initialize: dict[str, Any] | None = None
        self._initialized: bytes | None = None
        self._pending: dict[str, bytes] = {}
        self._closing = threading.Event()
        self._connected = threading.Event()
        self._pong = threading.Event()
        self._demand = threading.Event()
        self._sequence = 0
        self._generation = 0

    def run(self) -> None:
        self._socket = self.launcher.ensure()
        self._connected.set()
        reader = threading.Thread(target=self._read, name="relay-read", daemon=True)
        pinger = threading.Thread(target=self._ping, name="relay-ping", daemon=True)
        reader.start()
        pinger.start()
        try:
            for line in iter(self.source.readline, b""):
                self._forward(line)
        finally:
            self._closing.set()
            with self._send_lock:
                if self._socket is not None:
                    self._socket.close()
            reader.join(timeout=5)

    def _forward(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if isinstance(message, dict):
            if message.get("method") == "initialize":
                self._initialize = message
            elif message.get("method") == "notifications/initialized":
                self._initialized = line
            if "id" in message and "method" in message:
                self._pending[json.dumps(message["id"])] = line
        if not self._connected.is_set():
            self._demand.set()
        self._send(line)

    def _send(self, line: bytes) -> None:
        with self._send_lock:
            try:
                if self._socket is not None:
                    self._socket.sendall(_terminated(line))
            except OSError:
                # The reader notices the broken connection and replays pending work.
                pass

    def _emit(self, line: bytes) -> None:
        with self._sink_lock:
            self.sink.write(line)
            self.sink.flush()

    def _read(self) -> None:
        while not self._closing.is_set():
            connection = self._socket
            if connection is not None:
                try:
                    for line in _lines(connection):
                        self._receive(line)
                except OSError:
                    pass
            if self._closing.is_set():
                return
            logger.info("service connection closed")
            self._disconnect()
            while not self._pending and not self._closing.is_set():
                if self._demand.wait(1):
                    break
            self._demand.clear()
            if self._closing.is_set():
                return
            self._reconnect()

    def _receive(self, line: bytes) -> None:
        try:
            message = json.loads(line)
        except ValueError:
            self._emit(line)
            return
        identifier = message.get("id") if isinstance(message, dict) else None
        if isinstance(identifier, str) and identifier.startswith(_INTERNAL):
            self._pong.set()
            return
        if identifier is not None and ("result" in message or "error" in message):
            self._pending.pop(json.dumps(identifier), None)
        self._emit(line)

    def _disconnect(self) -> None:
        self._connected.clear()
        with self._send_lock:
            if self._socket is not None:
                self._socket.close()
                self._socket = None

    def _reconnect(self) -> None:
        self._disconnect()
        while not self._closing.is_set():
            connection: socket.socket | None = None
            try:
                connection = self.launcher.ensure()
                if self._initialize is not None:
                    replay = {**self._initialize, "id": self._internal("init")}
                    connection.sendall(_terminated(json.dumps(replay).encode()))
                    for line in _lines(connection):
                        if json.loads(line).get("id") == replay["id"]:
                            break
                    if self._initialized is not None:
                        connection.sendall(_terminated(self._initialized))
                with self._send_lock:
                    self._socket = connection
                    for line in list(self._pending.values()):
                        connection.sendall(_terminated(line))
                self._generation += 1
                self._connected.set()
                return
            except (OSError, ValueError, MarkdownNotesAgentError) as error:
                logger.error("service reconnect failed: %s", error)
                if connection is not None:
                    connection.close()
                time.sleep(2)

    def _internal(self, kind: str) -> str:
        self._sequence += 1
        return f"{_INTERNAL}{kind}-{self._sequence}"

    def _ping(self) -> None:
        while not self._closing.wait(_PING_SECONDS):
            try:
                self._check_alive()
            except OSError as error:
                logger.warning("service health check failed: %s", error)

    def _check_alive(self) -> None:
        if self._initialize is None or not self._connected.is_set():
            return
        generation = self._generation
        self._pong.clear()
        ping = {"jsonrpc": "2.0", "id": self._internal("ping"), "method": "ping"}
        self._send(json.dumps(ping).encode())
        if self._pong.wait(_PING_TIMEOUT) or self._closing.is_set():
            return
        if generation != self._generation or not self._connected.is_set():
            return  # the connection changed meanwhile; ping the new daemon next
        logger.error("service unresponsive for %ds, restarting", _PING_TIMEOUT)
        self.launcher.kill()
        with self._send_lock:
            if self._socket is not None:
                self._socket.shutdown(socket.SHUT_RDWR)


def _terminated(line: bytes) -> bytes:
    return line if line.endswith(b"\n") else line + b"\n"


# cost: time O(n), heap O(n), stack O(1), io 3
# vars: n = response bytes
# basis: estimate
def call_tool(launcher: Launcher, name: str, arguments: dict[str, Any]) -> Any:
    """Call one daemon tool from the CLI and return its structured result."""
    connection = launcher.ensure()
    connection.settimeout(_CALL_SECONDS)
    with connection:
        stream = _lines(connection)

        def request(identifier: int, method: str, params: dict[str, Any]) -> Any:
            message = {"jsonrpc": "2.0", "id": identifier, "method": method}
            connection.sendall(
                json.dumps({**message, "params": params}).encode() + b"\n"
            )
            for line in stream:
                reply = json.loads(line)
                if reply.get("id") == identifier:
                    if "error" in reply:
                        raise RequestError(
                            reply["error"].get("message", "request failed")
                        )
                    return reply["result"]
            raise MarkdownNotesAgentError(
                "background service connection lost", code="daemon_lost"
            )

        request(
            1,
            "initialize",
            {
                "protocolVersion": types.LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {
                    "name": "markdown-notes-agent-cli",
                    "version": __version__,
                },
            },
        )
        connection.sendall(
            b'{"jsonrpc": "2.0", "method": "notifications/initialized"}\n'
        )
        result = request(2, "tools/call", {"name": name, "arguments": arguments})
    text = " ".join(
        item.get("text", "")
        for item in result.get("content", [])
        if isinstance(item, dict)
    )
    if result.get("isError"):
        prefix = f"Error executing tool {name}: "
        raise RequestError(text.removeprefix(prefix) or "request failed")
    if "structuredContent" in result:
        structured = result["structuredContent"]
        if isinstance(structured, dict) and set(structured) == {"result"}:
            return structured["result"]
        return structured
    return text
