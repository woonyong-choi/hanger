from __future__ import annotations

__all__ = ["Limits", "Runtime"]

import json
import logging
import shutil
import sqlite3
import threading
import time
import unicodedata
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from filelock import FileLock, Timeout
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from persistqueue import SQLiteAckQueue
from persistqueue.serializers import json as json_serializer

from hanger.closets import ClosetPolicy
from hanger.config import Settings
from hanger.documents import Documents
from hanger.errors import (
    ConflictError,
    HangerError,
    RequestError,
    ReviewRejectedError,
    SearchUnavailableError,
)
from hanger.model import Intent, Planner
from hanger.requests import (
    KEY_LIFETIME_SECONDS,
    Proposal,
    digest,
    issue_key,
    load_secret,
    validate_key,
)
from hanger.trace import Trace

logger = logging.getLogger(__name__)
# Retry delays for unexpected job errors; the last value repeats.
# Short internal retries; after the last one the caller reports the failure.
_RETRY_SECONDS = 10
_KEY_REUSED = (
    "This request key was already used for a different request. Get a new one "
    "with issue_key."
)
_RECONSIDER_ONLY = (
    "Only a job stopped by a changed note can be reconsidered. For a failed job, "
    "get a new key with issue_key and send the request again."
)


@dataclass(frozen=True)
class Limits:
    pending_jobs: int = 256
    request_bytes: int = 2 * 1024 * 1024
    pending_bytes: int = 64 * 1024 * 1024
    state_bytes: int = 2 * 1024 * 1024 * 1024
    free_bytes: int = 5 * 1024 * 1024 * 1024


class _State(TypedDict):
    key: str
    fingerprint: str
    proposal: dict[str, Any]
    deadline: float
    cursor: int
    status: str
    results: dict[str, str]
    intent: dict[str, Any]
    dependencies: dict[str, str]
    revision: int
    decision: dict[str, Any]
    review: dict[str, Any]
    usage: dict[str, Any] | None
    search: dict[str, Any]
    error: str
    error_code: str
    applied: dict[str, str | None]
    prior_results: dict[str, str]
    # Set on a conflict once reconsider has moved it to a new key.
    resolved_by: str
    feedback: list[str]
    review_attempt: int
    model_attempt: int


class Runtime:
    # cost: time O(q), heap O(q), stack O(1), io O(q)
    # vars: q = retained queue payload bytes; library materializes queue rows on restart
    # basis: estimate
    def __init__(
        self,
        closet: Path,
        state: Path,
        planner: Planner | None = None,
        *,
        limits: Limits | None = None,
        trace: Trace | None = None,
        settings: Settings | None = None,
        secret: bytes | None = None,
    ) -> None:
        self.documents = Documents(closet, writer_id=str(state.resolve()))
        self.state = state.resolve()
        self._resources = ExitStack()
        self._admission = threading.RLock()
        self._consumer = threading.BoundedSemaphore(2)
        self._interrupted = threading.Event()
        self._planner = planner
        self._trace = trace or Trace()
        if planner is not None:
            planner.trace = self._trace
        self._limits = limits or Limits()
        if settings is None and planner is not None:
            settings = planner.synchronizer.settings
        self.settings = settings or Settings()
        # The closet's ignore and folder rules, shared with search and drafting.
        self.policy = (
            planner.synchronizer.policy
            if planner is not None
            else ClosetPolicy(self.documents.root)
        )
        # Set by the service to report stalled workers or search recovery.
        self.health: Callable[[], str | None] = lambda: None
        self._pending: dict[str, dict[str, Any]] = {}
        self._queue_ids: dict[str, int] = {}
        self._active: set[str] = set()
        if self.state.is_relative_to(self.documents.root):
            raise RequestError("Hanger's state folder must be outside the closet.")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._resources.enter_context(
                FileLock(self.state / "owner.lock", timeout=0)
            )
        except Timeout as error:
            raise RequestError(
                f"The job state is already in use by another process: {self.state}",
                code="state_in_use",
            ) from error
        try:
            self._secret = secret or load_secret(self.state)
            self._open()
        except (OSError, sqlite3.Error, ValueError, HangerError):
            self._resources.close()
            raise

    def __enter__(self) -> Runtime:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._resources.close()

    def interrupt(self) -> None:
        self._interrupted.set()

    def drain(self, timeout: float) -> bool:
        """Wait until no job runs; call after interrupt() before closing."""
        deadline = time.monotonic() + timeout
        acquired = 0
        try:
            for _ in range(2):
                left = max(0.0, deadline - time.monotonic())
                if not self._consumer.acquire(timeout=left):
                    return False
                acquired += 1
            return True
        finally:
            for _ in range(acquired):
                self._consumer.release()

    # cost: time O(c+q), heap O(b+q), stack O(1), io O(c+q)
    # vars: c = checkpoint bytes, q = retained queue bytes, b = checkpoint page bytes
    # basis: estimate
    def cleanup(self) -> dict[str, Any]:
        today = datetime.fromtimestamp(time.time()).date().isoformat()
        marker = self.state / "cleaned.date"
        if marker.exists() and marker.read_text() == today:
            return {"status": "already_cleaned"}
        acquired = 0
        try:
            for _ in range(2):
                if not self._consumer.acquire(blocking=False):
                    return {"status": "busy"}
                acquired += 1
            with self._admission:
                removed = self._clean_expired()
                marker.write_text(today)
                return {"status": "cleaned", "removed": removed}
        finally:
            for _ in range(acquired):
                self._consumer.release()

    # cost: time O(c+q), heap O(b+q), stack O(1), io O(c+q)
    # vars: c = checkpoint bytes, q = queued bytes, b = one checkpoint bytes
    # basis: estimate
    def _clean_expired(self) -> int:
        now = time.time()
        for key, request in list(self._pending.items()):
            if request["deadline"] > now:
                continue
            proposal = request.get("proposal", request.get("prior", {}).get("proposal"))
            if proposal:
                self.documents.discard_pending(Proposal.model_validate(proposal))
            queue_id = self._queue_ids[key]
            self._queue.get(id=queue_id, block=False)
            self._queue.ack(id=queue_id)
            self._pending.pop(key)
            self._queue_ids.pop(key)
        removed = 0
        before = None
        while True:
            page = list(self._saver.list(None, before=before, limit=1))
            if not page:
                break
            checkpoint = page[0]
            before = checkpoint.config
            key = str(checkpoint.config["configurable"]["thread_id"])
            if int(key.split(".")[0]) + KEY_LIFETIME_SECONDS <= now:
                for saved in self._saver.list(_config(key)):
                    proposal = saved.checkpoint["channel_values"].get("proposal")
                    if proposal:
                        self.documents.discard_pending(
                            Proposal.model_validate(proposal)
                        )
                self._saver.delete_thread(key)
                removed += 1
        self._queue.clear_acked_data(keep_latest=0)
        self._queue.shrink_disk_usage()
        with self._saver.lock:
            self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._connection.execute("VACUUM")
        return removed

    def issue_key(self) -> str:
        return issue_key(self._secret)

    def pending_count(self) -> int:
        with self._admission:
            return len(self._pending)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = proposal bytes
    # basis: estimate
    def submit(self, key: str, proposal: Proposal) -> str:
        deadline = validate_key(key, self._secret)
        self._validate_proposal(proposal)
        return self._enqueue(
            key,
            {"proposal": proposal.model_dump(mode="json")},
            proposal.fingerprint,
            deadline,
        )

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = intent bytes
    # basis: estimate
    def submit_intent(self, key: str, intent: Intent) -> str:
        deadline = validate_key(key, self._secret)
        if self._planner is None:
            raise RequestError("No model is configured for this closet.")
        self.policy.check_folders(intent.folders)
        for path in intent.paths:
            self.documents.resolve(path)
            if self.policy.excluded(path):
                raise RequestError(
                    f"This note is ignored or is a rule file: {path}",
                    code="excluded",
                )
        payload = _intent_payload(self._resolved(intent))
        fingerprint = digest(json.dumps(payload, sort_keys=True).encode())
        return self._enqueue(key, {"intent": payload}, fingerprint, deadline)

    def _resolved(self, intent: Intent) -> Intent:
        if intent.mode is not None:
            return intent
        return intent.model_copy(update={"mode": self.settings.jobs.default_mode})

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = prior proposal and observed effects bytes
    # basis: estimate
    def seen(self, key: str, fingerprint: str) -> bool:
        """Return whether this key already carries this request.

        Raises when the key was used for a different request.
        """
        validate_key(key, self._secret)
        previous = self.status(key)
        if not previous:
            return False
        if previous["fingerprint"] != fingerprint:
            raise RequestError(_KEY_REUSED, code="key_reused")
        return True

    def record_done(self, key: str, fingerprint: str, results: dict[str, str]) -> None:
        """Record work finished at once (a collected file) as a completed job."""
        deadline = validate_key(key, self._secret)
        with self._admission:
            if self.status(key):
                return
            self._graph.update_state(
                _config(key),
                {
                    "key": key,
                    "fingerprint": fingerprint,
                    "deadline": deadline,
                    "status": "completed",
                    "results": results,
                    "error": "",
                    "error_code": "",
                },
                as_node="apply",
            )

    def key_issued(self, key: str) -> float:
        """Return when a valid key was issued."""
        return validate_key(key, self._secret) - KEY_LIFETIME_SECONDS

    def reconsider(self, key: str, new_key: str) -> str:
        deadline = validate_key(new_key, self._secret)
        previous = self.status(key)
        if previous.get("status") != "conflict":
            raise RequestError(_RECONSIDER_ONLY, code="not_conflict")
        if previous.get("resolved_by"):
            if previous["resolved_by"] == new_key:
                return "duplicate"
            raise RequestError(
                "This job is already being reconsidered under key "
                f"{previous['resolved_by']}. Check that key's status."
            )
        if not previous.get("intent"):
            raise RequestError(
                "A job without a model instruction cannot be reconsidered. Send a "
                "new request based on the current notes."
            )
        intent = Intent.model_validate(previous["intent"])
        payload = {
            "intent": _intent_payload(intent),
            "prior": {
                "proposal": previous.get("proposal", {}),
                "results": {
                    **previous.get("prior_results", {}),
                    **previous.get("results", {}),
                },
                "applied": previous.get("applied", {}),
            },
        }
        fingerprint = digest(json.dumps(payload, sort_keys=True).encode())
        outcome = self._enqueue(new_key, payload, fingerprint, deadline)
        if outcome == "accepted":
            # The new job carries everything forward; keep only the result here.
            self._discard(key, resolved=new_key)
        return outcome

    def _validate_proposal(self, proposal: Proposal) -> None:
        canonical_paths = set()
        for change in proposal.changes:
            self.documents.resolve(change.path)
            if not self.policy.writable(change.path):
                raise RequestError(
                    "This path may not be written (ignored, a rule file, or "
                    f"write: false in its RULES.md): {change.path}",
                    code="write_denied",
                )
            canonical = unicodedata.normalize("NFC", change.path).casefold()
            if canonical in canonical_paths:
                raise RequestError(f"Two changes point to the same note: {change.path}")
            canonical_paths.add(canonical)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = request bytes
    # basis: estimate
    def _enqueue(
        self, key: str, payload: dict[str, Any], fingerprint: str, deadline: float
    ) -> str:
        with self._admission:
            previous = self.status(key)
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise RequestError(_KEY_REUSED, code="key_reused")
                return "duplicate"
            request = {
                "key": key,
                "fingerprint": fingerprint,
                **payload,
                "deadline": deadline,
                "cancelled": False,
                "size_bytes": len(json.dumps(payload).encode()),
            }
            self._check_capacity(request["size_bytes"])
            queue_id = self._queue.put(request)
            if queue_id is None:
                raise RequestError(
                    "The request could not be queued. Try again shortly."
                )
            self._pending[key] = request
            self._queue_ids[key] = int(queue_id)
        return "accepted"

    # cost: time O(q+f), heap O(f), stack O(d), io O(f)
    # vars: q = pending jobs, f = state files, d = state directory depth
    # basis: estimate
    def _check_capacity(self, incoming_bytes: int) -> None:
        limits = self._limits
        if len(self._pending) >= limits.pending_jobs:
            raise RequestError(
                f"{limits.pending_jobs} jobs are already waiting. Try again after "
                "some of them finish."
            )
        if incoming_bytes > limits.request_bytes:
            raise RequestError("The request is larger than 2 MiB.")
        pending_bytes = sum(
            item.get("size_bytes", limits.request_bytes)
            for item in self._pending.values()
        )
        if pending_bytes + incoming_bytes > limits.pending_bytes:
            raise RequestError(
                "Waiting requests already take 64 MiB. Try again shortly."
            )
        self._check_storage()

    # cost: time O(f), heap O(f), stack O(d), io O(f)
    # vars: f = state files, d = state directory depth
    # basis: estimate
    def _check_storage(self) -> None:
        limits = self._limits
        state_bytes = sum(
            path.stat().st_size for path in self.state.rglob("*") if path.is_file()
        )
        if state_bytes + 8 * limits.request_bytes >= limits.state_bytes:
            raise RequestError(
                "The job history is full. Try again after expired jobs are cleaned up."
            )
        if shutil.disk_usage(self.state).free < limits.free_bytes:
            raise RequestError("Not enough free disk space (5 GiB needed).")

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = saved graph state bytes
    # basis: estimate
    def status(self, key: str) -> dict[str, Any]:
        snapshot = self._graph.get_state(_config(key))
        request = self._pending.get(key)
        record: dict[str, Any]
        if snapshot.values:
            record = dict(snapshot.values)
        elif request is not None:
            status = "cancelled" if request.get("cancelled") else "queued"
            record = {
                "key": key,
                "fingerprint": request["fingerprint"],
                "status": status,
            }
        else:
            return {}
        if (
            request is not None
            and request.get("attempts")
            and not request.get("cancelled")
            and record.get("status") in {"queued", "planning", "running"}
        ):
            # Waiting for an internal retry; the caller relays this to the user.
            record["error"] = (
                f"An error occurred; retrying in {_RETRY_SECONDS}s "
                f"({request['attempts']}/{self.settings.jobs.error_attempts}): "
                f"{request.get('retry_reason', '')}"
            )
        return record

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = saved graph state bytes
    # basis: estimate
    def describe(self, key: str) -> dict[str, Any]:
        state = self.status(key)
        if not state:
            validate_key(key, self._secret)
            return {
                "key": key,
                "status": "not_found",
                "message": "No job was accepted with this key.",
                "next_actions": [{"tool": "request_change"}, {"tool": "collect"}],
            }
        status = state["status"]
        actions: list[dict[str, Any]]
        if status in {"queued", "planning", "running"}:
            message = self.health() or state.get("error") or "The job is running."
            actions = [{"tool": "job_status"}, {"tool": "cancel_job"}]
        elif status == "failed":
            message = state.get("error") or "The job failed."
            actions = [
                {"tool": "issue_key", "note": "get a new key"},
                {"tool": "request_change", "note": "send the same request again"},
            ]
        elif status == "conflict" and state.get("resolved_by"):
            message = (
                "This job stopped on a changed note and is being reconsidered "
                "under key " + state["resolved_by"]
            )
            actions = [{"tool": "job_status", "args": {"key": state["resolved_by"]}}]
        elif status == "conflict":
            message = state.get("error") or "The job stopped because a note changed."
            actions = [{"tool": "read_document"}]
            if state.get("intent"):
                actions += _resolution("reconsider")
            else:
                actions += [
                    {"tool": "issue_key", "note": "get a new key"},
                    {"tool": "request_change", "note": "ask again on current notes"},
                ]
        elif status == "completed":
            changed = len(state.get("results", {}))
            message = (
                f"Done. Files changed: {changed}."
                if changed
                else "Done. No file needed a change."
            )
            actions = [{"tool": "read_document"}]
        elif status == "deferred":
            message = "Held back for lack of evidence: " + state.get(
                "decision", {}
            ).get("reason", "no reason given")
            actions = [{"tool": "read_document"}, {"tool": "request_change"}]
        else:
            message = {
                "cancelled": "The job was cancelled.",
                "expired": "The job stopped because its request key expired.",
            }.get(
                status, "The job stopped."
            ) + " Files already changed are listed in results."
            actions = [{"tool": "read_document"}, {"tool": "request_change"}]
        return {**state, "message": message, "next_actions": actions}

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = queued request bytes
    # basis: estimate
    def cancel(self, key: str) -> bool:
        with self._admission:
            request = self._pending.get(key)
            if request is None:
                return False
            updated = {**request, "cancelled": True}
            self._queue.update(updated, id=self._queue_ids[key])
            self._pending[key] = updated
        return True

    # cost: time O(n*f), heap O(n), stack O(1), io O(f*f)
    # vars: n = proposal and target bytes, f = target count
    # basis: estimate
    def run_next(self) -> dict[str, Any] | None:
        with self._consumer:
            item = self._take_ready()
            if item is None:
                return None
            request = item["data"]
            key = request["key"]
            config = _config(key)
            trace_token = self._trace.begin(key)
            try:
                if self._interrupted.is_set():
                    raise InterruptedError("runtime is stopping; work remains queued")
                snapshot = self._graph.get_state(config)
                if snapshot.values:
                    if snapshot.next:
                        self._graph.invoke(None, config, durability="sync")
                else:
                    self._graph.invoke(_initial(request), config, durability="sync")
                if self.status(key).get("status") in _FINAL:
                    self._discard(key)
                result = self.status(key)
                self._finish(key, item["pqid"])
                return result
            except InterruptedError:
                self._queue.nack(id=item["pqid"])
                raise
            except SearchUnavailableError as error:
                # Search recovery is supervised separately; wait without spending
                # the job's error attempts.
                logger.warning("job=%s waiting for search: %s", _job(key), error)
                self._defer(item, 10)
                return self.status(key)
            except Exception as error:  # noqa: BLE001 - any failure is retried
                return self._retry(item, error)
            finally:
                self._trace.end(trace_token)
                with self._admission:
                    self._active.discard(key)

    # cost: time O(q+n), heap O(n), stack O(1), io O(1)
    # vars: q = bounded pending jobs, n = selected job bytes
    # basis: estimate
    def _take_ready(self) -> dict[str, Any] | None:
        with self._admission:
            for key, request in self._pending.items():
                if key in self._active:
                    continue
                stopped = request["cancelled"] or request["deadline"] <= time.time()
                if not stopped and request.get("retry_at", 0) > time.time():
                    continue
                item = self._queue.get(id=self._queue_ids[key], block=False, raw=True)
                self._active.add(key)
                return dict(item)
        return None

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = persisted request and checkpoint bytes
    # basis: estimate
    def _retry(self, item: dict[str, Any], error: Exception) -> dict[str, Any]:
        key = item["data"]["key"]
        limit = self.settings.jobs.error_attempts
        with self._admission:
            request = dict(self._pending[key])
            attempts = request.get("attempts", 0) + 1
            self._trace.record("retry", {"attempt": attempts})
            reason = f"{type(error).__name__}: {error}"
            if attempts > limit:
                logger.error(
                    "job=%s failed after %d retries: %s", _job(key), limit, reason
                )
                self._graph.update_state(
                    _config(key),
                    {
                        "status": "failed",
                        "error": (f"The job failed after {limit} retries: {reason}"),
                        "error_code": "internal",
                    },
                    as_node="apply",
                )
                self._discard(key)
                result = self.status(key)
                self._finish(key, item["pqid"])
                return result
            delay = _RETRY_SECONDS
            logger.warning(
                "job=%s retrying in %ds (%d/%d): %s",
                _job(key),
                delay,
                attempts,
                limit,
                reason,
            )
            request.update(
                attempts=attempts, retry_at=time.time() + delay, retry_reason=reason
            )
            self._queue.update(request, id=item["pqid"])
            self._pending[key] = request
            self._queue.nack(id=item["pqid"])
        raise error

    def _defer(self, item: dict[str, Any], seconds: float) -> None:
        key = item["data"]["key"]
        with self._admission:
            request = {**self._pending[key], "retry_at": time.time() + seconds}
            self._queue.update(request, id=item["pqid"])
            self._pending[key] = request
            self._queue.nack(id=item["pqid"])

    # cost: time O(1), heap O(1), stack O(1), io O(1)
    # basis: estimate
    def _discard(self, key: str, *, resolved: str = "") -> None:
        """Reduce a finished or stopped job to its result.

        The step-by-step history is deleted and one record remains until the
        key expires: the result for job_status and duplicate detection, plus
        what reconsider needs while a conflict is still open. Unfinished
        temporary files are removed.
        """
        state = self.status(key)
        proposal = state.get("proposal")
        if proposal:
            self.documents.discard_pending(Proposal.model_validate(proposal))
        status = state.get("status", "")
        kept = _KEPT + (() if resolved else _EXTRA.get(status, ()))
        record = {name: state[name] for name in kept if name in state}
        if resolved:
            record["resolved_by"] = resolved
        self._saver.delete_thread(key)
        self._graph.update_state(_config(key), record, as_node="apply")

    def _finish(self, key: str, queue_id: int) -> None:
        with self._admission:
            self._queue.ack(id=queue_id)
            self._pending.pop(key, None)
            self._queue_ids.pop(key, None)

    # cost: time O(q), heap O(q), stack O(1), io O(q)
    # vars: q = retained queue bytes
    # basis: estimate
    def _open(self) -> None:
        self._queue = SQLiteAckQueue(
            str(self.state / "queue"),
            multithreading=True,
            auto_resume=True,
            serializer=json_serializer,
        )
        self._resources.callback(self._queue.close)
        self._queue.clear_acked_data(keep_latest=0)
        self._connection = sqlite3.connect(
            self.state / "graph.sqlite", check_same_thread=False
        )
        self._resources.callback(self._connection.close)
        self._saver = SqliteSaver(self._connection)
        builder = StateGraph(_State)
        builder.add_node("prepare", self._prepare)
        builder.add_node("apply", self._apply)
        builder.add_conditional_edges(START, _route)
        builder.add_conditional_edges("prepare", _route)
        builder.add_conditional_edges("apply", _route)
        self._graph = builder.compile(checkpointer=self._saver)
        for row in self._queue.queue():
            if str(row["status"]) == "5":
                continue
            request = row["data"]
            self._pending[request["key"]] = request
            self._queue_ids[request["key"]] = row["id"]
        self.cleanup()

    # cost: time O(n+t), heap O(n), stack O(d), io O(f), tokens O(n)
    # vars: n = context and model result bytes, t = model latency
    # vars: d = directory depth, f = dependency files
    # basis: estimate
    def _prepare(self, state: _State) -> dict[str, Any]:
        if self._planner is None:
            raise RequestError("No model is configured for this closet.")

        def is_cancelled() -> bool:
            return (
                self._interrupted.is_set()
                or self._pending[state["key"]]["cancelled"]
                or time.time() >= state["deadline"]
            )

        try:
            self._check_storage()
            planned = self._planner.prepare(
                Intent.model_validate(state["intent"]),
                is_cancelled,
                {
                    "proposal": state["proposal"],
                    "observed": {
                        **state.get("prior_results", {}),
                        **state["results"],
                    },
                    "applied": state.get("applied", {}),
                }
                if state["proposal"]
                else None,
                state.get("feedback") or None,
            )
            if self._interrupted.is_set():
                raise InterruptedError("runtime stopped while planning")
            if is_cancelled():
                return {
                    "status": "cancelled"
                    if self._pending[state["key"]]["cancelled"]
                    else "expired"
                }
            if planned["proposal"]:
                if (
                    len(json.dumps(planned["proposal"]).encode())
                    > self._limits.request_bytes
                ):
                    raise RequestError(
                        "The proposal is larger than 2 MiB.",
                        code="proposal_too_large",
                    )
                self._validate_proposal(Proposal.model_validate(planned["proposal"]))
            return {
                **planned,
                "cursor": 0,
                "error": "",
                "error_code": "",
                "feedback": [],
                "status": "running"
                if planned["proposal"]
                else (
                    "deferred"
                    if planned.get("decision", {}).get("action") == "defer"
                    else "completed"
                ),
            }
        except ConflictError as error:
            if self._interrupted.is_set():
                raise InterruptedError("runtime stopped while planning") from None
            if self._pending[state["key"]]["cancelled"]:
                return {"status": "cancelled"}
            if time.time() >= state["deadline"]:
                return {"status": "expired"}
            return self._conflict(state, str(error))
        except ReviewRejectedError as error:
            return self._again(
                state, "review_attempt", self.settings.jobs.review_attempts, error
            )
        except HangerError as error:
            if isinstance(error, SearchUnavailableError):
                raise
            # Model output that breaks an edit rule is re-drafted with the
            # rule violation as feedback, like a review rejection.
            if error.retryable or error.code in _REDRAFT:
                return self._again(
                    state, "model_attempt", self.settings.jobs.error_attempts, error
                )
            logger.info("job=%s failed [%s]: %s", _job(state["key"]), error.code, error)
            return {"status": "failed", "error": str(error), "error_code": error.code}

    def _again(
        self, state: _State, counter: str, limit: int, error: HangerError
    ) -> dict[str, Any]:
        """Re-plan a retryable failure with feedback, up to the configured limit."""
        attempt = int(state.get(counter, 0)) + 1  # type: ignore[call-overload]
        label = (
            "the review rejected the draft"
            if error.code == "review_rejected"
            else "the model output broke an edit rule"
        )
        feedback = list(getattr(error, "issues", [str(error)]))
        if attempt < limit:
            logger.info(
                "job=%s redrafting after %s (%d/%d)",
                _job(state["key"]),
                error.code,
                attempt,
                limit,
            )
            return {
                "status": "planning",
                counter: attempt,
                "feedback": feedback,
                "error": f"Redrafting because {label} ({attempt}/{limit}).",
                "error_code": error.code,
            }
        detail = feedback[-1] if feedback else str(error)
        return {
            "status": "failed",
            counter: attempt,
            "error": f"Failed after {attempt} drafts because {label}: {detail}",
            "error_code": error.code,
        }

    def _conflict(self, state: _State, reason: str) -> dict[str, Any]:
        mode = state["intent"].get("mode") or self.settings.jobs.default_mode
        limit = self.settings.jobs.source_changed_retries
        if mode == "bypass" and state["revision"] < limit:
            revision = state["revision"] + 1
            logger.info(
                "job=%s source changed, replanning (%d/%d)",
                _job(state["key"]),
                revision,
                limit,
            )
            return {
                "status": "planning",
                "revision": revision,
                "error": (
                    f"A note changed; planning again on the current notes "
                    f"({revision}/{limit})."
                ),
                "error_code": "source_changed",
            }
        prefix = (
            "The job stopped because notes kept changing"
            if mode == "bypass"
            else "The job stopped because a note changed"
        )
        return {
            "status": "conflict",
            "error": f"{prefix}: {reason}",
            "error_code": "source_changed",
        }

    # cost: time O(n), heap O(n), stack O(1), io O(f)
    # vars: n = target and proposal bytes, f = target count
    # basis: estimate
    def _apply(self, state: _State) -> dict[str, Any]:
        """Write every remaining file in one step.

        One graph step for all files keeps the checkpoint from storing the
        whole proposal again per file. After a crash the step runs again and
        each file is recognised as already applied, pending or changed.
        """
        with self._admission:
            proposal = Proposal.model_validate(state["proposal"])
            results = dict(state["results"])
            applied = dict(state.get("applied", {}))
            targets = {change.path for change in proposal.changes}
            for cursor in range(state["cursor"], len(proposal.changes)):
                if self._interrupted.is_set():
                    raise InterruptedError(
                        "runtime stopped before document replacement"
                    )
                request = self._pending[state["key"]]
                if request["cancelled"]:
                    return self._stopped(proposal, "cancelled")
                if time.time() >= state["deadline"]:
                    return self._stopped(proposal, "expired")
                try:
                    self._check_storage()
                except RequestError as error:
                    return {
                        **self._stopped(proposal, "failed"),
                        "error": str(error),
                        "error_code": "storage",
                    }
                change = proposal.changes[cursor]
                try:
                    for path, expected in state.get("dependencies", {}).items():
                        if path in targets:
                            continue
                        if self.documents.read_hash(path) != expected:
                            raise ConflictError(
                                f"A note used as a source changed: {path}"
                            )
                    self.documents.check_proposal(proposal, cursor)
                    with self._trace.measure("apply"):
                        outcome = self.documents.apply(change)
                except ConflictError as error:
                    stopped = self._stopped(proposal, "conflict")
                    return {
                        **stopped,
                        **self._conflict(state, str(error)),
                        "cursor": cursor,
                        "applied": {**applied, **stopped["applied"]},
                    }
                except RequestError as error:
                    return {
                        **self._stopped(proposal, "failed"),
                        "error": f"This change cannot be applied: {error}",
                        "error_code": "invalid_target",
                    }
                results[change.path] = outcome
                applied[change.path] = (
                    None if change.after is None else digest(change.after.encode())
                )
            return {
                "cursor": len(proposal.changes),
                "results": results,
                "status": "completed",
                "applied": applied,
            }

    # cost: time O(n), heap O(m+f), stack O(1), io O(f)
    # vars: n = target bytes, m = largest target bytes, f = targets
    # basis: estimate
    def _stopped(self, proposal: Proposal, status: str) -> dict[str, Any]:
        results = {}
        applied: dict[str, str | None] = {}
        for change in proposal.changes:
            try:
                current = self.documents.read_hash(change.path)
            except RequestError:
                results[change.path] = "unreadable"
                continue
            expected = None if change.after is None else digest(change.after.encode())
            if current == expected:
                outcome = "result_present"
                applied[change.path] = expected
            elif current == change.before:
                outcome = "source_present"
            else:
                outcome = "externally_changed"
            results[change.path] = outcome
        return {"status": status, "results": results, "applied": applied}


# Model output that breaks an edit rule is redrafted with the violation.
_REDRAFT = {"model_output", "write_denied", "delete_denied"}
# Jobs in these states keep one record and no history (see Runtime._discard).
_FINAL = {"completed", "deferred", "conflict", "failed", "cancelled", "expired"}
# What each state keeps beyond the result: a summary of what was decided, or
# the instruction and proposal an open conflict needs for reconsider.
_EXTRA: dict[str, tuple[str, ...]] = {
    "completed": ("intent", "decision", "review", "search", "usage", "prior_results"),
    "deferred": ("intent", "decision", "review", "search", "usage", "prior_results"),
    "conflict": ("intent", "proposal", "prior_results", "decision", "search"),
}
_KEPT = (
    "key",
    "fingerprint",
    "deadline",
    "status",
    "error",
    "error_code",
    "results",
    "applied",
)


def _resolution(choice: str) -> list[dict[str, Any]]:
    return [
        {"tool": "issue_key", "note": "get a new key"},
        {"tool": "resolve_conflict", "args": {"choice": choice}},
    ]


def _config(key: str) -> RunnableConfig:
    return {"configurable": {"thread_id": key}, "recursion_limit": 2002}


def _intent_payload(intent: Intent) -> dict[str, Any]:
    payload = intent.model_dump(mode="json")
    # Omitted new defaults preserve identities issued before folder/profile support.
    if not intent.folders:
        payload.pop("folders")
    if intent.profile == "note":
        payload.pop("profile")
    return payload


def _job(key: str) -> str:
    """Short log identifier that does not reveal the signed request key."""
    return digest(key.encode())[:8]


def _initial(request: dict[str, Any]) -> _State:
    prior = request.get("prior", {})
    return {
        "key": request["key"],
        "fingerprint": request["fingerprint"],
        "proposal": request.get("proposal", prior.get("proposal", {})),
        "deadline": request["deadline"],
        "cursor": 0,
        "status": "planning" if "intent" in request else "running",
        "results": {},
        "prior_results": prior.get("results", {}),
        "feedback": [],
        "review_attempt": 0,
        "model_attempt": 0,
        "error_code": "",
        "intent": request.get("intent", {}),
        "dependencies": {},
        "revision": 0,
        "decision": {},
        "review": {},
        "usage": None,
        "search": {},
        "error": "",
        "applied": prior.get("applied", {}),
        "resolved_by": "",
    }


def _route(state: _State) -> str:
    if state["status"] == "planning":
        return "prepare"
    return "apply" if state["status"] == "running" else END
