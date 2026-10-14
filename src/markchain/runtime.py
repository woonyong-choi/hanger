from __future__ import annotations

__all__ = ["Limits", "Runtime"]

import json
import os
import secrets
import shutil
import sqlite3
import threading
import time
import unicodedata
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

from markchain.documents import Documents
from markchain.errors import ConflictError, MarkchainError, RequestError
from markchain.model import Intent, Planner
from markchain.requests import (
    KEY_LIFETIME_SECONDS,
    Proposal,
    digest,
    issue_key,
    validate_key,
)
from markchain.trace import Trace


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
    usage: dict[str, Any] | None
    search: dict[str, Any]
    error: str
    applied: dict[str, str | None]


class Runtime:
    # cost: time O(q), heap O(q), stack O(1), io O(q)
    # vars: q = retained queue payload bytes; library materializes queue rows on restart
    # basis: estimate
    def __init__(
        self,
        vault: Path,
        state: Path,
        planner: Planner | None = None,
        *,
        limits: Limits | None = None,
        trace: Trace | None = None,
    ) -> None:
        self.documents = Documents(vault, writer_id=str(state.resolve()))
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
        self._pending: dict[str, dict[str, Any]] = {}
        self._queue_ids: dict[str, int] = {}
        self._active: set[str] = set()
        if self.state.is_relative_to(self.documents.root):
            raise RequestError("runtime state must be outside vault")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._resources.enter_context(
                FileLock(self.state / "owner.lock", timeout=0)
            )
        except Timeout as error:
            raise RequestError("runtime state is already in use") from error
        try:
            self._open()
        except (OSError, sqlite3.Error, ValueError, MarkchainError):
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
            raise RequestError("model planner is not configured")
        payload = intent.model_dump(mode="json")
        fingerprint = digest(json.dumps(payload, sort_keys=True).encode())
        return self._enqueue(key, {"intent": payload}, fingerprint, deadline)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = prior proposal and observed effects bytes
    # basis: estimate
    def reconsider(self, key: str, new_key: str) -> str:
        deadline = validate_key(new_key, self._secret)
        previous = self.status(key)
        if previous.get("status") not in {"conflict", "failed"}:
            raise RequestError("job must be conflicted or failed")
        intent = Intent.model_validate(previous["intent"])
        payload = {
            "intent": intent.model_dump(mode="json"),
            "prior": {
                name: previous.get(name, {})
                for name in ("proposal", "results", "applied")
            },
        }
        fingerprint = digest(json.dumps(payload, sort_keys=True).encode())
        return self._enqueue(new_key, payload, fingerprint, deadline)

    def _validate_proposal(self, proposal: Proposal) -> None:
        canonical_paths = set()
        for change in proposal.changes:
            self.documents.resolve(change.path)
            canonical = unicodedata.normalize("NFC", change.path).casefold()
            if canonical in canonical_paths:
                raise RequestError("duplicate portable document path")
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
                    raise RequestError("request key reused with different content")
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
                raise RequestError("queue did not confirm persisted request")
            self._pending[key] = request
            self._queue_ids[key] = int(queue_id)
        return "accepted"

    # cost: time O(q+f), heap O(f), stack O(d), io O(f)
    # vars: q = pending jobs, f = state files, d = state directory depth
    # basis: estimate
    def _check_capacity(self, incoming_bytes: int) -> None:
        limits = self._limits
        if len(self._pending) >= limits.pending_jobs:
            raise RequestError("pending job limit reached; retry after completion")
        if incoming_bytes > limits.request_bytes:
            raise RequestError("request byte limit exceeded")
        pending_bytes = sum(
            item.get("size_bytes", limits.request_bytes)
            for item in self._pending.values()
        )
        if pending_bytes + incoming_bytes > limits.pending_bytes:
            raise RequestError("pending byte limit reached")
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
            raise RequestError("state byte limit reached; cleanup is required")
        if shutil.disk_usage(self.state).free < limits.free_bytes:
            raise RequestError("insufficient free disk space")

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = saved graph state bytes
    # basis: estimate
    def status(self, key: str) -> dict[str, Any]:
        snapshot = self._graph.get_state(_config(key))
        if snapshot.values:
            return dict(snapshot.values)
        request = self._pending.get(key)
        if request is None:
            return {}
        return {"key": key, "fingerprint": request["fingerprint"], "status": "queued"}

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
                result = self.status(key)
                self._finish(key, item["pqid"])
                return result
            except InterruptedError:
                self._queue.nack(id=item["pqid"])
                raise
            except (OSError, sqlite3.Error) as error:
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
        with self._admission:
            request = dict(self._pending[key])
            attempts = request.get("attempts", 0) + 1
            self._trace.record("retry", {"attempt": attempts})
            if attempts > 3:
                self._graph.update_state(
                    _config(key),
                    {"status": "failed", "error": "I/O retry limit exhausted"},
                    as_node="apply",
                )
                result = self.status(key)
                self._finish(key, item["pqid"])
                return result
            request.update(
                attempts=attempts, retry_at=time.time() + (2, 10, 30)[attempts - 1]
            )
            self._queue.update(request, id=item["pqid"])
            self._pending[key] = request
            self._queue.nack(id=item["pqid"])
        raise error

    # cost: time O(1), heap O(1), stack O(1), io O(1)
    # basis: estimate
    def _finish(self, key: str, queue_id: int) -> None:
        with self._admission:
            self._queue.ack(id=queue_id)
            self._pending.pop(key, None)
            self._queue_ids.pop(key, None)

    # cost: time O(q), heap O(q), stack O(1), io O(q)
    # vars: q = retained queue bytes
    # basis: estimate
    def _open(self) -> None:
        identity_path = self.state / "vault.path"
        identity = str(self.documents.root)
        if not identity_path.exists():
            with identity_path.open("x") as output:
                output.write(identity)
                output.flush()
                os.fsync(output.fileno())
        if identity_path.read_text() != identity:
            raise RequestError("runtime state belongs to a different vault")
        secret_path = self.state / "request.key"
        if not secret_path.exists():
            with secret_path.open("xb") as output:
                output.write(secrets.token_bytes(32))
                output.flush()
                os.fsync(output.fileno())
            secret_path.chmod(0o600)
        self._secret = secret_path.read_bytes()
        if len(self._secret) != 32:
            raise RequestError("invalid request signing key")
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
            raise RequestError("model planner is not configured")

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
                    "observed": state["results"],
                    "applied": state.get("applied", {}),
                }
                if state["proposal"]
                else None,
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
                    raise RequestError("model proposal byte limit exceeded")
                self._validate_proposal(Proposal.model_validate(planned["proposal"]))
            return {
                **planned,
                "cursor": 0,
                "status": "running" if planned["proposal"] else "completed",
            }
        except ConflictError:
            if self._interrupted.is_set():
                raise InterruptedError("runtime stopped while planning") from None
            if self._pending[state["key"]]["cancelled"]:
                return {"status": "cancelled"}
            if time.time() >= state["deadline"]:
                return {"status": "expired"}
            return self._conflict(state)
        except RequestError as error:
            return {"status": "failed", "error": str(error)}

    def _conflict(self, state: _State) -> dict[str, Any]:
        if state["intent"].get("mode") == "bypass" and state["revision"] < 2:
            return {"status": "planning", "revision": state["revision"] + 1}
        return {"status": "conflict"}

    # cost: time O(n), heap O(n), stack O(1), io O(f)
    # vars: n = target and proposal bytes, f = target count
    # basis: estimate
    def _apply(self, state: _State) -> dict[str, Any]:
        with self._admission:
            if self._interrupted.is_set():
                raise InterruptedError("runtime stopped before document replacement")
            request = self._pending[state["key"]]
            proposal = Proposal.model_validate(state["proposal"])
            if request["cancelled"]:
                return self._stopped(proposal, "cancelled")
            if time.time() >= state["deadline"]:
                return self._stopped(proposal, "expired")
            try:
                self._check_storage()
            except RequestError as error:
                return {"status": "failed", "error": str(error)}
            change = proposal.changes[state["cursor"]]
            try:
                targets = {change.path for change in proposal.changes}
                for path, expected in state.get("dependencies", {}).items():
                    if path in targets:
                        continue
                    if self.documents.read_hash(path) != expected:
                        raise ConflictError("supporting source changed")
                self.documents.check_proposal(proposal, state["cursor"])
                with self._trace.measure("apply"):
                    outcome = self.documents.apply(change)
            except ConflictError:
                stopped = self._stopped(proposal, "conflict")
                return {
                    **stopped,
                    **self._conflict(state),
                    "applied": {**state.get("applied", {}), **stopped["applied"]},
                }
            except RequestError:
                return self._stopped(proposal, "invalid_target")
            cursor = state["cursor"] + 1
            status = "completed" if cursor == len(proposal.changes) else "running"
            return {
                "cursor": cursor,
                "results": {**state["results"], change.path: outcome},
                "status": status,
                "applied": {
                    **state.get("applied", {}),
                    change.path: None
                    if change.after is None
                    else digest(change.after.encode()),
                },
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


def _config(key: str) -> RunnableConfig:
    return {"configurable": {"thread_id": key}, "recursion_limit": 2002}


def _initial(request: dict[str, Any]) -> _State:
    prior = request.get("prior", {})
    return {
        "key": request["key"],
        "fingerprint": request["fingerprint"],
        "proposal": request.get("proposal", prior.get("proposal", {})),
        "deadline": request["deadline"],
        "cursor": 0,
        "status": "planning" if "intent" in request else "running",
        "results": prior.get("results", {}),
        "intent": request.get("intent", {}),
        "dependencies": {},
        "revision": 0,
        "decision": {},
        "usage": None,
        "search": {},
        "error": "",
        "applied": prior.get("applied", {}),
    }


def _route(state: _State) -> str:
    if state["status"] == "planning":
        return "prepare"
    return "apply" if state["status"] == "running" else END
