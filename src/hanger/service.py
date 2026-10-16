"""The MCP tools AI clients call, and the background threads behind them.

Every user-facing tool first checks for pending decisions: while the user
has not answered a question (such as an edited ignore file), only the
catalog and decision tools work. Background threads keep running regardless:
file watchers index changes and workers finish queued jobs.
"""

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

from hanger.closets import describe_decision, register_closet, resolve_decision
from hanger.context import shape
from hanger.errors import RequestError, SearchUnavailableError, guidance
from hanger.hub import ClosetHub, OpenCloset
from hanger.model import Intent
from hanger.requests import issue_key as new_key
from hanger.requests import validate_key
from hanger.workflows import collect_file_target, collect_proposal

if TYPE_CHECKING:
    from hanger.elastic import ElasticManager

logger = logging.getLogger(__name__)

# A worker loop that has not reported for this long while idle is stalled.
_IDLE_STALL_SECONDS = 60
# One job may run a model twice per attempt plus file work.
_BUSY_STALL_SECONDS = 1500
_MONITOR_SECONDS = 5
# How long a search waits for a starting search server before failing.
SEARCH_WAIT_SECONDS = 30
_CLEANUP_SECONDS = 30
_WORKERS = ("hanger-worker-1", "hanger-worker-2")

INSTRUCTIONS = """\
Hanger keeps the user's Markdown notes in registered folders called closets.
Start with list_closets. With more than one closet, pass closet (an id, or a
path inside it) to every call. Every request_change or collect needs a fresh
key from issue_key.
Some results and errors carry "Tell the user:" and "Then:". Give the user the
"Tell the user" text with nothing added or left out, translated into the
language the user writes in or has set for you. Keep these words as they are in
every language: closet, Hanger, .hangerignore, HANGER.md, and every command,
path and id. "Then" is for you: do it.
A decision_required error means Hanger waits for the user's own answer. Show
the question and both choices, wait, then call decide. Never answer for them."""


class Service:
    def __init__(self, hub: ClosetHub, engine: ElasticManager | None = None) -> None:
        self.hub = hub
        self.engine = engine
        self._stop = threading.Event()
        self._threads: dict[str, threading.Thread] = {}
        self._targets: dict[str, Callable[[], None]] = {}
        self._beats: dict[str, float] = {}
        self._busy: dict[str, float] = {}
        self.last_activity = time.monotonic()
        hub.health = self.health
        self.mcp = FastMCP("hanger", instructions=INSTRUCTIONS)
        for tool in (
            self.list_closets,
            self.register_closet,
            self.remove_closet,
            self.list_decisions,
            self.decide,
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

    # ---------- lifecycle ----------

    def start(self) -> None:
        self.hub.refresh()
        self.hub.each(lambda opened: opened.runtime.cleanup())
        self.hub.watch()
        self._targets = {name: functools.partial(self._work, name) for name in _WORKERS}
        self._targets["hanger-monitor"] = self._maintain
        for name in self._targets:
            self._spawn(name)

    def _spawn(self, name: str) -> None:
        self._beats[name] = time.monotonic()
        thread = threading.Thread(target=self._targets[name], name=name, daemon=True)
        self._threads[name] = thread
        thread.start()

    def close(self) -> None:
        self._stop.set()
        for opened in self.hub.opened():
            opened.runtime.interrupt()
        for thread in list(self._threads.values()):
            thread.join(timeout=30)
        self.hub.close()

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def idle(self) -> bool:
        pending = sum(o.runtime.pending_count() for o in self.hub.opened())
        return not pending and not self._busy

    # cost: time O(w), heap O(1), stack O(1)
    # vars: w = worker threads
    # basis: estimate
    def health(self) -> str | None:
        """Explain why queued work is not moving, or None when it should be."""
        if self.engine is not None and not self.engine.ready():
            return self.engine.message()
        now = time.monotonic()
        for name in _WORKERS:
            beat = self._beats.get(name, now)
            started = self._busy.get(name)
            if started is not None and now - started > _BUSY_STALL_SECONDS:
                return "A job has been running for more than 25 minutes."
            if started is None and now - beat > _IDLE_STALL_SECONDS:
                return "A job worker has not responded for more than 60 seconds."
        return None

    # ---------- gate ----------

    def ready(self) -> None:
        """Refuse every request while a decision waits for the user."""
        self.touch()
        self.hub.refresh()
        pending = self.hub.store.decisions()
        if not pending:
            return
        lines = ["Hanger is waiting for your answer before it continues."]
        for decision in pending:
            described = describe_decision(decision)
            lines += [
                f"Decision #{described['id']}: {described['question']}",
                f"- approve: {described['choices']['approve']}",
                f"- decline: {described['choices']['decline']}",
            ]
        message = guidance(
            "\n".join(lines),
            "Wait for the user's choice, then call decide with it. Never choose "
            "for them. In a terminal the user can also run: hanger decide <id> "
            "approve|decline",
        )
        raise RequestError(message, code="decision_required")

    def _closet(self, closet: str | None, key: str | None = None) -> OpenCloset:
        """The closet a request names; a key already used elsewhere is refused."""
        opened = self.hub.get(closet)
        if key is not None:
            holder = self.hub.holding(key)
            if holder is not None and holder.id != opened.id:
                raise RequestError(
                    f"This request key was already used in closet #{holder.id}. "
                    "Get a new one with issue_key.",
                    code="key_reused",
                )
        return opened

    # ---------- catalog and decisions ----------

    def list_closets(self) -> dict[str, Any]:
        """List registered closets and any decision waiting for the user."""
        self.touch()
        self.hub.refresh()
        broken = self.hub.broken()
        closets = [
            {
                **closet.as_dict(),
                "available": closet.id not in broken,
                **({"problem": broken[closet.id]} if closet.id in broken else {}),
            }
            for closet in self.hub.store.closets()
        ]
        result: dict[str, Any] = {
            "closets": closets,
            "decisions": [describe_decision(d) for d in self.hub.store.decisions()],
        }
        if not closets:
            result["tell_user"] = (
                "No closet is set up yet. A closet is a folder of Markdown notes "
                "that Hanger keeps. Which folder should be the closet? Send its "
                "absolute path."
            )
            result["next"] = (
                "Call register_closet with the path the user gives. Never guess "
                "a folder."
            )
        return result

    def register_closet(self, path: str) -> dict[str, Any]:
        """Register a folder of Markdown notes as a closet (absolute path).

        Creates .hangerignore and HANGER.md at its root when they are missing.
        Refuses a folder inside, or containing, a registered closet.
        """
        self.touch()
        folder = Path(path).expanduser()
        if not folder.is_absolute():
            raise RequestError(f"Give an absolute folder path: {path}")
        result = register_closet(self.hub.store, folder)
        self.hub.refresh()
        closet = result["closet"]
        tell = (
            f"Closet #{closet['id']} is set up: {closet['path']}. Hanger is "
            "indexing its notes in the background."
        )
        if result["created"]:
            tell += " Created at its root: " + ", ".join(result["created"]) + "."
        return {**result, "tell_user": tell}

    def remove_closet(self, closet: str) -> dict[str, Any]:
        """Removing a closet is only possible from the user's terminal."""
        self.touch()
        found = self.hub.store.closet(closet)
        target = str(found.path) if found else closet
        raise RequestError(
            guidance(
                "A closet can only be removed from a terminal, so its search "
                "index is never deleted by accident. To remove it, run: "
                f'hanger closet remove "{target}"'
            ),
            code="terminal_only",
        )

    def list_decisions(self) -> dict[str, Any]:
        """List decisions waiting for the user's answer."""
        self.touch()
        self.hub.refresh()
        return {"decisions": [describe_decision(d) for d in self.hub.store.decisions()]}

    def decide(
        self, decision_id: int, answer: Literal["approve", "decline"]
    ) -> dict[str, Any]:
        """Apply the user's own answer to a pending decision."""
        self.touch()
        result = resolve_decision(self.hub.store, decision_id, answer)
        self.hub.refresh()
        return result

    # ---------- requests ----------

    def issue_key(self) -> str:
        """Issue a request key. Every mutating call needs its own fresh key."""
        self.ready()
        return new_key(self.hub.secret)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = collected text bytes
    # basis: estimate
    def collect(
        self,
        key: str,
        text: str | None = None,
        file: str | None = None,
        closet: str | None = None,
    ) -> dict[str, str]:
        """Save text or any file unchanged under inbox/ (no model).

        Give exactly one of text or file (an absolute path on this computer).
        Names are collection time, title (first heading, first line, or the
        file's own name), then a short content hash.
        """
        self.ready()
        opened = self._closet(closet, key)
        if (text is None) == (file is None):
            raise RequestError("Give exactly one of text or file.")
        if file is not None:
            return self._collect_file(opened, key, file)
        return self._collect_text(opened, key, text or "")

    def _collect_text(self, opened: OpenCloset, key: str, text: str) -> dict[str, str]:
        try:
            size = len(text.encode())
        except UnicodeEncodeError as error:
            raise RequestError("The text contains invalid Unicode.") from error
        if not text.strip() or size > 1024 * 1024:
            raise RequestError("The text must not be empty and at most 1 MiB.")
        # The key's issue time names the file, so a resend gets the same name.
        proposal = collect_proposal(text, opened.runtime.key_issued(key))
        status = opened.runtime.submit(key, proposal)
        return {"key": key, "status": status, "path": proposal.changes[0].path}

    # cost: time O(n), heap O(1), stack O(1), io O(n)
    # vars: n = file bytes
    # basis: estimate
    def _collect_file(self, opened: OpenCloset, key: str, file: str) -> dict[str, str]:
        source = Path(file).expanduser()
        if not source.is_absolute():
            raise RequestError(f"Give an absolute file path: {file}")
        if not source.is_file():
            raise RequestError(f"No such file: {file}", code="not_found")
        runtime = opened.runtime
        target, digest = collect_file_target(source.resolve(), runtime.key_issued(key))
        if not opened.policy.writable(target):
            raise RequestError(
                f"This closet does not allow writing here: {target}",
                code="write_denied",
            )
        fingerprint = "file:" + digest + ":" + target
        if runtime.seen(key, fingerprint):
            return {"key": key, "status": "duplicate", "path": target}
        try:
            outcome = runtime.documents.store_file(target, source, digest)
        except OSError as error:
            raise RequestError(f"Could not collect the file: {error}") from error
        runtime.record_done(key, fingerprint, {target: outcome})
        return {"key": key, "status": "completed", "path": target}

    # cost: time O(n), heap O(k), stack O(d), io O(f)
    # vars: n = searched bytes, k = result bytes, d = directory depth, f = files read
    # basis: estimate
    async def search_documents(
        self,
        query: str,
        closet: str | None = None,
        limit: int | None = None,
        fallback_reason: Literal["insufficient_evidence", "user_reported_missing"]
        | None = None,
        folders: tuple[str, ...] = (),
        ctx: Context | None = None,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """Search notes in listed closet folders (OR; empty means all).

        Top results come with full text; the rest are listed with matched lines.
        Use read_document for a listed path. fallback_reason re-syncs the scope
        before searching when a note seems to be missing. While the search
        server starts, progress is reported for up to 30 seconds.
        """
        await anyio.to_thread.run_sync(self.ready)
        waited = 0
        while self.engine is not None and not self.engine.ready():
            message = self.engine.message()
            if waited >= SEARCH_WAIT_SECONDS:
                raise SearchUnavailableError(
                    f"{message} It was not ready within {SEARCH_WAIT_SECONDS}s. "
                    "Search again shortly."
                )
            if ctx is not None:
                await ctx.report_progress(waited, SEARCH_WAIT_SECONDS, message)
                await ctx.info(message)
            await anyio.sleep(1)
            waited += 1
        return await anyio.to_thread.run_sync(
            functools.partial(
                self.search, query, closet, limit, fallback_reason, folders
            )
        )

    # cost: time O(n), heap O(k), stack O(d), io O(f)
    # vars: n = searched bytes, k = result bytes, d = directory depth, f = files read
    # basis: estimate
    def search(
        self,
        query: str,
        closet: str | None = None,
        limit: int | None = None,
        fallback_reason: Literal["insufficient_evidence", "user_reported_missing"]
        | None = None,
        folders: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Synchronous search used by the CLI and the MCP tool."""
        self.ready()
        opened = self._closet(closet)
        if self.engine is not None:
            self.engine.require()
        synchronizer = opened.synchronizer
        settings = synchronizer.settings
        result = synchronizer.search(
            query,
            settings.search.candidates if limit is None else limit,
            fallback_reason=fallback_reason,
            folders=folders,
        )
        full, listed = shape(
            result["documents"],
            {},
            settings,
            synchronizer.terms(query),
            load=synchronizer.read,
            peek=synchronizer.peek,
        )
        return {
            **result,
            "closet": str(opened.root),
            "documents": list(full.values()),
            "listed": listed,
        }

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = document bytes
    # basis: estimate
    def read_document(self, path: str, closet: str | None = None) -> dict[str, str]:
        """Read one Markdown note as it is now (path relative to the closet)."""
        self.ready()
        opened = self._closet(closet)
        if opened.policy.excluded(path):
            raise RequestError(
                f"This note is ignored or is a rule file: {path}", code="excluded"
            )
        try:
            return opened.synchronizer.read(path)
        except FileNotFoundError as error:
            raise RequestError(f"No such note: {path}", code="not_found") from error

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = intent bytes
    # basis: estimate
    def request_change(
        self, key: str, intent: Intent, closet: str | None = None
    ) -> dict[str, str]:
        """Queue a write, edit or organize request. Accepted is not completed."""
        self.ready()
        opened = self._closet(closet, key)
        return {"key": key, "status": opened.runtime.submit_intent(key, intent)}

    # cost: time O(c*n), heap O(n), stack O(1), io O(c)
    # vars: c = closets, n = persisted job state bytes
    # basis: estimate
    def job_status(self, key: str) -> dict[str, Any]:
        """Read progress, result, reason and the next possible actions."""
        self.ready()
        opened = self.hub.holding(key)
        if opened is None:
            validate_key(key, self.hub.secret)
            return {
                "key": key,
                "status": "not_found",
                "message": "No job was accepted with this key.",
                "next_actions": [{"tool": "request_change"}, {"tool": "collect"}],
            }
        return opened.runtime.describe(key)

    def cancel_job(self, key: str) -> dict[str, bool]:
        """Cancel remaining work; files already written are not rolled back."""
        self.ready()
        opened = self.hub.holding(key)
        return {"cancelled": opened is not None and opened.runtime.cancel(key)}

    # cost: time O(n), heap O(n), stack O(1), io O(f)
    # vars: n = proposal and source bytes, f = modified paths
    # basis: estimate
    def resolve_conflict(
        self, key: str, new_key: str, choice: Literal["reconsider"] = "reconsider"
    ) -> dict[str, str]:
        """Re-plan a job stopped by a changed note, from the current notes.

        Use a fresh key from issue_key as new_key. Saved proposals are never
        forced over newer content.
        """
        self.ready()
        opened = self.hub.holding(key)
        if opened is None or opened.runtime.status(key).get("status") != "conflict":
            raise RequestError(
                "Only a job stopped by a changed note can be reconsidered. For a "
                "failed job, get a new key with issue_key and send it again.",
                code="not_conflict",
            )
        if choice != "reconsider":
            raise RequestError("The only supported choice is reconsider.")
        return {"key": new_key, "status": opened.runtime.reconsider(key, new_key)}

    # ---------- background ----------

    # cost: time O(n), heap O(n), stack O(1), io O(f), tokens O(n)
    # vars: n = work bytes over service lifetime, f = work I/O count
    # basis: estimate
    def _work(self, name: str) -> None:
        while not self._stop.wait(0.2):
            self._beats[name] = time.monotonic()
            if self.engine is not None and not self.engine.ready():
                continue
            for opened in self.hub.opened():
                self._busy[name] = time.monotonic()
                try:
                    opened.runtime.run_next()
                except InterruptedError:
                    if self._stop.is_set():
                        return
                except Exception:
                    logger.exception("%s: job attempt failed", name)
                    self._stop.wait(2)
                finally:
                    self._busy.pop(name, None)
                self._beats[name] = time.monotonic()

    # cost: time O(c), heap O(b), stack O(1), io O(c)
    # vars: c = expired checkpoint bytes, b = checkpoint page bytes
    # basis: estimate
    def _maintain(self) -> None:
        next_cleanup = time.monotonic()
        while not self._stop.wait(_MONITOR_SECONDS):
            self._beats["hanger-monitor"] = time.monotonic()
            for name, thread in list(self._threads.items()):
                if not thread.is_alive() and not self._stop.is_set():
                    logger.error("%s stopped unexpectedly, restarting", name)
                    self._spawn(name)
            try:
                self.hub.refresh()
                self.hub.restart_watchers()
            except Exception:
                logger.exception("closet refresh failed")
            if time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + _CLEANUP_SECONDS
                self.hub.each(lambda opened: opened.runtime.cleanup())
