from __future__ import annotations

__all__ = ["Service"]

import functools
import logging
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import anyio
import anyio.to_thread
from mcp.server.fastmcp import Context, FastMCP

from markchain.context import shape
from markchain.errors import RequestError, SearchUnavailableError
from markchain.model import Intent
from markchain.runtime import Runtime
from markchain.sync import Synchronizer
from markchain.watcher import VaultWatcher
from markchain.workflows import collect_file_target, collect_proposal

if TYPE_CHECKING:
    from markchain.elastic import ElasticManager

logger = logging.getLogger(__name__)

# A worker loop that has not reported for this long while idle is stalled.
_IDLE_STALL_SECONDS = 60
# One job may run a model twice per attempt plus file work.
_BUSY_STALL_SECONDS = 1500
_MONITOR_SECONDS = 5
# How long a search waits for a starting search server before failing.
SEARCH_WAIT_SECONDS = 30
_CLEANUP_SECONDS = 30


class Service:
    def __init__(
        self,
        runtime: Runtime,
        synchronizer: Synchronizer,
        engine: ElasticManager | None = None,
    ) -> None:
        self.runtime = runtime
        self.synchronizer = synchronizer
        self.engine = engine
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._targets: dict[str, Callable[[], None]] = {}
        self._beats: dict[str, float] = {}
        self._busy: dict[str, float] = {}
        self.last_activity = time.monotonic()
        runtime.health = self.health
        self.mcp = FastMCP(
            "markchain", instructions="Local canonical Markdown workflows"
        )
        for tool in (
            self.issue_key,
            self.collect,
            self.search_documents,
            self.read_document,
            self.request_change,
            self.job_status,
            self.cancel_job,
            self.resolve_conflict,
        ):
            self.mcp.add_tool(tool)

    def start(self) -> None:
        self.runtime.cleanup()
        self._targets = {
            # A fresh watcher each time: a stopped observer cannot restart.
            "markchain-watch": lambda: VaultWatcher(self.synchronizer).run(self._stop),
            "markchain-worker-1": lambda: self._work("markchain-worker-1"),
            "markchain-worker-2": lambda: self._work("markchain-worker-2"),
            "markchain-monitor": self._maintain,
        }
        for name in self._targets:
            self._spawn(name)

    def _spawn(self, name: str) -> None:
        self._beats[name] = time.monotonic()
        thread = threading.Thread(target=self._targets[name], name=name, daemon=True)
        self._threads[name] = thread
        thread.start()

    def close(self) -> None:
        self._stop.set()
        self.runtime.interrupt()
        for thread in list(self._threads.values()):
            thread.join(timeout=30)

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def idle(self) -> bool:
        return not self.runtime.pending_count() and not self._busy

    # cost: time O(w), heap O(1), stack O(1)
    # vars: w = worker threads
    # basis: estimate
    def health(self) -> str | None:
        """Explain why queued work is not moving, or None when it should be."""
        if self.engine is not None and not self.engine.ready():
            return self.engine.message()
        now = time.monotonic()
        for name, beat in self._beats.items():
            if not name.startswith("markchain-worker"):
                continue
            started = self._busy.get(name)
            if started is not None and now - started > _BUSY_STALL_SECONDS:
                return "a job has been running too long"
            if started is None and now - beat > _IDLE_STALL_SECONDS:
                return "job workers stalled"
        return None

    def issue_key(self) -> str:
        """Issue a request key. Every mutating call needs its own fresh key."""
        self.touch()
        return self.runtime.issue_key()

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = collected text bytes
    # basis: estimate
    def collect(
        self, key: str, text: str | None = None, file: str | None = None
    ) -> dict[str, str]:
        """Save text or any file unchanged under inbox/ (no model).

        Give exactly one of text or file (an absolute path on this computer).
        Names are collection time, title (first heading, first line, or the
        file's own name), then a short content hash.
        """
        self.touch()
        if (text is None) == (file is None):
            raise RequestError("text와 file 중 하나만 지정하십시오.")
        if file is not None:
            return self._collect_file(key, file)
        return self._collect_text(key, text or "")

    def _collect_text(self, key: str, text: str) -> dict[str, str]:
        try:
            size = len(text.encode())
        except UnicodeEncodeError as error:
            raise RequestError(
                "수집할 내용에 올바르지 않은 유니코드 문자가 있습니다."
            ) from error
        if not text.strip() or size > 1024 * 1024:
            raise RequestError("수집할 내용은 비어 있지 않고 1MiB 이하여야 합니다.")
        # The key's issue time names the file, so a resend gets the same name.
        proposal = collect_proposal(text, self.runtime.key_issued(key))
        status = self.runtime.submit(key, proposal)
        return {"key": key, "status": status, "path": proposal.changes[0].path}

    # cost: time O(n), heap O(1), stack O(1), io O(n)
    # vars: n = file bytes
    # basis: estimate
    def _collect_file(self, key: str, file: str) -> dict[str, str]:
        source = Path(file).expanduser()
        if not source.is_absolute():
            raise RequestError("file에는 이 컴퓨터의 절대 경로를 지정하십시오.")
        if not source.is_file():
            raise RequestError(
                f"수집할 파일을 찾을 수 없습니다: {file}", code="not_found"
            )
        issued = self.runtime.key_issued(key)
        target, digest = collect_file_target(source.resolve(), issued)
        if not self.synchronizer.settings.writable(target):
            raise RequestError(
                f"쓰기 허용 범위 밖의 경로입니다: {target}", code="write_denied"
            )
        fingerprint = "file:" + digest + ":" + target
        if self.runtime.seen(key, fingerprint):
            return {"key": key, "status": "duplicate", "path": target}
        try:
            outcome = self.runtime.documents.store_file(target, source, digest)
        except OSError as error:
            raise RequestError(f"파일을 수집하지 못했습니다: {error}") from error
        self.runtime.record_done(key, fingerprint, {target: outcome})
        return {"key": key, "status": "completed", "path": target}

    # cost: time O(n), heap O(k), stack O(d), io O(f)
    # vars: n = searched bytes, k = result bytes, d = directory depth, f = files read
    # basis: estimate
    async def search_documents(
        self,
        query: str,
        limit: int | None = None,
        fallback_reason: Literal["insufficient_evidence", "user_reported_missing"]
        | None = None,
        folders: tuple[str, ...] = (),
        ctx: Context | None = None,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """Search listed vault folders (OR; empty means all).

        Top results come with full text; the rest are listed with matched lines.
        Use read_document for a listed path. fallback_reason re-syncs the scope
        before searching when a document seems to be missing. While the search
        server starts, progress is reported for up to 30 seconds.
        """
        self.touch()
        waited = 0
        while self.engine is not None and not self.engine.ready():
            message = self.engine.message()
            if waited >= SEARCH_WAIT_SECONDS:
                raise SearchUnavailableError(
                    f"{message} {SEARCH_WAIT_SECONDS}초 안에 준비되지 않았습니다. "
                    "잠시 후 다시 검색하십시오."
                )
            if ctx is not None:
                await ctx.report_progress(waited, SEARCH_WAIT_SECONDS, message)
                await ctx.info(message)
            await anyio.sleep(1)
            waited += 1
        return await anyio.to_thread.run_sync(
            functools.partial(self.search, query, limit, fallback_reason, folders)
        )

    # cost: time O(n), heap O(k), stack O(d), io O(f)
    # vars: n = searched bytes, k = result bytes, d = directory depth, f = files read
    # basis: estimate
    def search(
        self,
        query: str,
        limit: int | None = None,
        fallback_reason: Literal["insufficient_evidence", "user_reported_missing"]
        | None = None,
        folders: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Synchronous search used by the CLI and the MCP tool."""
        if self.engine is not None:
            self.engine.require()
        settings = self.synchronizer.settings
        result = self.synchronizer.search(
            query,
            settings.search.candidates if limit is None else limit,
            fallback_reason=fallback_reason,
            folders=folders,
        )
        full, listed = shape(
            result["documents"],
            {},
            settings,
            self.synchronizer.terms(query),
            load=self.synchronizer.read,
            peek=self.synchronizer.peek,
        )
        return {**result, "documents": list(full.values()), "listed": listed}

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = document bytes
    # basis: estimate
    def read_document(self, path: str) -> dict[str, str]:
        """Read one local Markdown document as it is now."""
        self.touch()
        if self.synchronizer.settings.excluded(path):
            raise RequestError(
                f"관리 범위 밖이거나 제외된 문서입니다: {path}", code="excluded"
            )
        try:
            return self.synchronizer.read(path)
        except FileNotFoundError as error:
            raise RequestError(
                f"문서를 찾을 수 없습니다: {path}", code="not_found"
            ) from error

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = intent bytes
    # basis: estimate
    def request_change(self, key: str, intent: Intent) -> dict[str, str]:
        """Queue a write, edit or organize request. Accepted is not completed."""
        self.touch()
        return {"key": key, "status": self.runtime.submit_intent(key, intent)}

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = persisted job state bytes
    # basis: estimate
    def job_status(self, key: str) -> dict[str, Any]:
        """Read progress, result, reason and the next possible actions."""
        self.touch()
        return self.runtime.describe(key)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = queued job bytes
    # basis: estimate
    def cancel_job(self, key: str) -> dict[str, bool]:
        """Cancel remaining work; files already written are not rolled back."""
        self.touch()
        return {"cancelled": self.runtime.cancel(key)}

    # cost: time O(n), heap O(n), stack O(1), io O(f)
    # vars: n = proposal and source bytes, f = modified paths
    # basis: estimate
    def resolve_conflict(
        self, key: str, new_key: str, choice: Literal["reconsider"] = "reconsider"
    ) -> dict[str, str]:
        """Re-plan a job stopped by a source conflict from the current sources.

        Use a fresh key from issue_key as new_key. Saved proposals are never
        forced over newer content.
        """
        self.touch()
        state = self.runtime.status(key)
        if state.get("status") != "conflict":
            raise RequestError(
                "충돌로 중단된 작업만 재검토할 수 있습니다. "
                "실패한 작업은 issue_key로 새 키를 발급받아 다시 요청하십시오."
            )
        if choice != "reconsider":
            raise RequestError("지원하는 처리 방식은 reconsider뿐입니다.")
        return {"key": new_key, "status": self.runtime.reconsider(key, new_key)}

    # cost: time O(n), heap O(n), stack O(1), io O(f), tokens O(n)
    # vars: n = work bytes over service lifetime, f = work I/O count
    # basis: estimate
    def _work(self, name: str) -> None:
        while not self._stop.wait(0.2):
            self._beats[name] = time.monotonic()
            if self.engine is not None and not self.engine.ready():
                continue
            self._busy[name] = time.monotonic()
            try:
                self.runtime.run_next()
            except InterruptedError:
                return
            except Exception:
                logger.exception("%s: job attempt failed", name)
                self._stop.wait(2)
            finally:
                self._busy.pop(name, None)

    # cost: time O(c), heap O(b), stack O(1), io O(c)
    # vars: c = expired checkpoint bytes, b = checkpoint page bytes
    # basis: estimate
    def _maintain(self) -> None:
        next_cleanup = time.monotonic()
        while not self._stop.wait(_MONITOR_SECONDS):
            self._beats["markchain-monitor"] = time.monotonic()
            for name, thread in list(self._threads.items()):
                if not thread.is_alive() and not self._stop.is_set():
                    logger.error("%s stopped unexpectedly, restarting", name)
                    self._spawn(name)
            if time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + _CLEANUP_SECONDS
                try:
                    self.runtime.cleanup()
                except Exception:
                    logger.exception("cleanup failed")
