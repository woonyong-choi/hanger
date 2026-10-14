from __future__ import annotations

__all__ = ["Service"]

import logging
import sqlite3
import threading
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from markchain.errors import MarkchainError, RequestError
from markchain.model import Intent
from markchain.requests import Change, Proposal
from markchain.runtime import Runtime
from markchain.sync import Synchronizer
from markchain.watcher import VaultWatcher

logger = logging.getLogger(__name__)


class Service:
    def __init__(self, runtime: Runtime, synchronizer: Synchronizer) -> None:
        self.runtime = runtime
        self.synchronizer = synchronizer
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.mcp = FastMCP(
            "markchain", instructions="Local canonical Markdown workflows"
        )
        self.mcp.add_tool(self.issue_key)
        self.mcp.add_tool(self.collect)
        self.mcp.add_tool(self.search_documents)
        self.mcp.add_tool(self.read_document)
        self.mcp.add_tool(self.request_change)
        self.mcp.add_tool(self.job_status)
        self.mcp.add_tool(self.cancel_job)
        self.mcp.add_tool(self.resolve_conflict)

    def start(self) -> None:
        self.runtime.cleanup()
        watcher = VaultWatcher(self.synchronizer)
        self._threads = [
            threading.Thread(
                target=watcher.run, args=(self._stop,), name="markchain-watch"
            ),
            threading.Thread(target=self._work, name="markchain-worker-1"),
            threading.Thread(target=self._work, name="markchain-worker-2"),
            threading.Thread(target=self._maintain, name="markchain-cleanup"),
        ]
        for thread in self._threads:
            thread.start()

    def close(self) -> None:
        self._stop.set()
        self.runtime.interrupt()
        for thread in self._threads:
            thread.join()

    def issue_key(self) -> str:
        """Issue a request identity before sending a mutating operation."""
        return self.runtime.issue_key()

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = collected text bytes
    # basis: estimate
    def collect(self, key: str, text: str) -> dict[str, str]:
        """Preserve a memo, link or conversation in inbox without model rewriting."""
        if not text.strip() or len(text.encode()) > 1024 * 1024:
            raise RequestError("collection must contain 1 byte to 1 MiB of text")
        path = f"inbox/{key.split('.')[1]}.md" if "." in key else "inbox/invalid.md"
        status = self.runtime.submit(
            key, Proposal(changes=(Change(path=path, after=text),))
        )
        return {"key": key, "status": status, "path": path}

    # cost: time O(n), heap O(k), stack O(d), io O(f)
    # vars: n = searched bytes, k = result bytes, d = directory depth, f = files read
    # basis: estimate
    def search_documents(
        self,
        query: str,
        limit: int = 20,
        fallback_offset: int = 0,
        fallback_reason: Literal["insufficient_evidence", "user_reported_missing"]
        | None = None,
    ) -> dict[str, Any]:
        """Search Elasticsearch first and return verified local source text."""
        return self.synchronizer.search(
            query,
            limit,
            fallback_offset=fallback_offset,
            fallback_reason=fallback_reason,
        )

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = document bytes
    # basis: estimate
    def read_document(self, path: str) -> dict[str, str]:
        """Read an explicitly selected local Markdown source."""
        return self.synchronizer.read(path)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = intent bytes
    # basis: estimate
    def request_change(self, key: str, intent: Intent) -> dict[str, str]:
        """Queue a write, edit or organization request; never publish remotely."""
        return {"key": key, "status": self.runtime.submit_intent(key, intent)}

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = persisted job state bytes
    # basis: estimate
    def job_status(self, key: str) -> dict[str, Any]:
        """Read the proposal, evidence, progress and any conflict from durable state."""
        return self.runtime.status(key)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = queued job bytes
    # basis: estimate
    def cancel_job(self, key: str) -> dict[str, bool]:
        """Cancel remaining work; already applied user files are not rolled back."""
        return {"cancelled": self.runtime.cancel(key)}

    # cost: time O(n), heap O(n), stack O(1), io O(f)
    # vars: n = proposal and source bytes, f = modified paths
    # basis: estimate
    def resolve_conflict(
        self, key: str, new_key: str, choice: Literal["reconsider", "overwrite"]
    ) -> dict[str, str]:
        """Reconsider current sources or explicitly approve a saved replacement."""
        state = self.runtime.status(key)
        if state.get("status") not in {"conflict", "failed"}:
            raise RequestError("job must be conflicted or failed")
        if choice == "reconsider":
            return {"key": new_key, "status": self.runtime.reconsider(key, new_key)}
        proposal = Proposal.model_validate(state["proposal"])
        changes = tuple(
            Change(
                path=c.path,
                before=self.runtime.documents.read_hash(c.path),
                after=c.after,
            )
            for c in proposal.changes
        )
        return {
            "key": new_key,
            "status": self.runtime.submit(new_key, Proposal(changes=changes)),
        }

    # cost: time O(n), heap O(n), stack O(1), io O(f), tokens O(n)
    # vars: n = work bytes over service lifetime, f = work I/O count
    # basis: estimate
    def _work(self) -> None:
        while not self._stop.wait(0.2):
            try:
                self.runtime.run_next()
            except (OSError, sqlite3.Error, MarkchainError) as error:
                logger.error("work attempt failed: %s", error)
                self._stop.wait(2)

    # cost: time O(c), heap O(b), stack O(1), io O(c)
    # vars: c = expired checkpoint bytes, b = checkpoint page bytes
    # basis: estimate
    def _maintain(self) -> None:
        while not self._stop.wait(30):
            try:
                self.runtime.cleanup()
            except (OSError, sqlite3.Error, MarkchainError) as error:
                logger.error("cleanup failed: %s", error)
