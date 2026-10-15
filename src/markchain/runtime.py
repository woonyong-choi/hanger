from __future__ import annotations

__all__ = ["Limits", "Runtime"]

import json
import logging
import os
import secrets
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

from markchain.config import Settings
from markchain.documents import Documents
from markchain.errors import (
    ConflictError,
    MarkchainError,
    RequestError,
    ReviewRejectedError,
    SearchUnavailableError,
)
from markchain.model import Intent, Planner
from markchain.requests import (
    KEY_LIFETIME_SECONDS,
    Proposal,
    digest,
    issue_key,
    validate_key,
)
from markchain.trace import Trace

logger = logging.getLogger(__name__)
# Retry delays for unexpected job errors; the last value repeats.
# Short internal retries; after the last one the caller reports the failure.
_RETRY_SECONDS = 10


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
    feedback: list[str]
    review_attempt: int
    model_attempt: int


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
        settings: Settings | None = None,
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
        if settings is None and planner is not None:
            settings = planner.synchronizer.settings
        self.settings = settings or Settings()
        # Set by the service to report stalled workers or search recovery.
        self.health: Callable[[], str | None] = lambda: None
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
            raise RequestError("모델 실행기가 설정되지 않았습니다.")
        self.settings.check_folders(intent.folders)
        for path in intent.paths:
            self.documents.resolve(path)
            if self.settings.excluded(path):
                raise RequestError(
                    f"관리 범위 밖이거나 제외된 경로입니다: {path}", code="excluded"
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
    def reconsider(self, key: str, new_key: str) -> str:
        deadline = validate_key(new_key, self._secret)
        previous = self.status(key)
        if previous.get("status") != "conflict":
            raise RequestError(
                "충돌로 중단된 작업만 재검토할 수 있습니다. "
                "실패한 작업은 issue_key로 새 키를 발급받아 다시 요청하십시오."
            )
        if not previous.get("intent"):
            raise RequestError(
                "모델 지시가 없는 작업은 재검토할 수 없습니다. 현재 원문 "
                "기준으로 새로 요청하십시오."
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
        return self._enqueue(new_key, payload, fingerprint, deadline)

    def _validate_proposal(self, proposal: Proposal) -> None:
        canonical_paths = set()
        for change in proposal.changes:
            self.documents.resolve(change.path)
            if not self.settings.writable(change.path):
                raise RequestError(
                    f"쓰기 허용 범위 밖의 경로입니다: {change.path}",
                    code="write_denied",
                )
            canonical = unicodedata.normalize("NFC", change.path).casefold()
            if canonical in canonical_paths:
                raise RequestError("같은 문서를 가리키는 경로가 중복되었습니다.")
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
                    raise RequestError(
                        "이미 다른 내용으로 사용된 요청 키입니다. "
                        "issue_key로 새 키를 발급받으십시오."
                    )
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
                    "요청을 큐에 저장하지 못했습니다. 잠시 후 다시 요청하십시오."
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
                "대기 중인 작업이 한도(256건)에 도달했습니다. 진행 중인 "
                "작업이 끝난 뒤 다시 요청하십시오."
            )
        if incoming_bytes > limits.request_bytes:
            raise RequestError("요청 크기가 한도(2MiB)를 넘습니다.")
        pending_bytes = sum(
            item.get("size_bytes", limits.request_bytes)
            for item in self._pending.values()
        )
        if pending_bytes + incoming_bytes > limits.pending_bytes:
            raise RequestError(
                "대기 중인 요청의 총량이 한도(64MiB)에 도달했습니다. "
                "잠시 후 다시 요청하십시오."
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
                "작업 기록 저장 공간이 한도에 도달했습니다. 만료 기록 "
                "정리 후 다시 요청하십시오."
            )
        if shutil.disk_usage(self.state).free < limits.free_bytes:
            raise RequestError("디스크 여유 공간이 부족합니다(5GiB 이상 필요).")

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
                f"처리 중 오류가 발생해 {_RETRY_SECONDS}초 후 다시 시도합니다 "
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
                "message": "이 키로 접수된 작업이 없습니다.",
                "next_actions": [{"tool": "request_change"}, {"tool": "collect"}],
            }
        status = state["status"]
        actions: list[dict[str, Any]]
        if status in {"queued", "planning", "running"}:
            message = self.health() or state.get("error") or "작업을 처리하고 있습니다."
            actions = [{"tool": "job_status"}, {"tool": "cancel_job"}]
        elif status == "failed":
            message = state.get("error") or "작업이 실패했습니다."
            actions = [
                {"tool": "issue_key", "note": "새 키 발급"},
                {"tool": "request_change", "note": "같은 내용으로 다시 요청"},
            ]
        elif status == "conflict":
            message = state.get("error") or "원문이 변경되어 작업을 중단했습니다."
            actions = [{"tool": "read_document"}]
            if state.get("intent"):
                actions += _resolution("reconsider")
            else:
                actions += [
                    {"tool": "issue_key", "note": "새 키 발급"},
                    {"tool": "request_change", "note": "현재 원문 기준으로 다시 요청"},
                ]
        elif status == "completed":
            changed = len(state.get("results", {}))
            message = (
                f"작업을 완료했습니다. 반영한 파일: {changed}개."
                if changed
                else "작업을 완료했습니다. 변경한 파일은 없습니다."
            )
            actions = [{"tool": "read_document"}]
        elif status == "deferred":
            message = "근거가 부족해 반영을 보류했습니다: " + state.get(
                "decision", {}
            ).get("reason", "사유 미기재")
            actions = [{"tool": "read_document"}, {"tool": "request_change"}]
        else:
            message = {
                "cancelled": "작업이 취소되었습니다.",
                "expired": "요청 키의 유효 기간이 지나 작업을 중단했습니다.",
            }.get(
                status, "작업이 중단되었습니다."
            ) + " 이미 반영된 파일은 results에서 확인하십시오."
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
                if self.status(key).get("status") in _DISCARDED:
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
                        "error": (
                            f"처리 중 오류가 발생해 {limit}회 재시도했으나 "
                            f"실패했습니다: {reason}"
                        ),
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
    def _discard(self, key: str) -> None:
        """Drop the working data of a job that will not continue.

        Only a small record stays for job_status and duplicate detection until
        the key expires. Unfinished temporary files are removed.
        """
        state = self.status(key)
        proposal = state.get("proposal")
        if proposal:
            self.documents.discard_pending(Proposal.model_validate(proposal))
        record = {name: state[name] for name in _KEPT if name in state}
        self._saver.delete_thread(key)
        self._graph.update_state(_config(key), record, as_node="apply")

    def _rebind(self, previous: str, identity: str) -> None:
        """Start over for a new vault root; nothing of the old one is kept.

        Jobs of the old vault cannot apply to the new one, so the queue and
        job history are removed, and Markchain's unfinished files in the old
        vault are cleaned up. The search index is replaced on first use.
        """
        logger.warning(
            "vault changed from %s to %s; resetting jobs", previous, identity
        )
        old = Path(previous)
        if old.is_dir():
            try:
                Documents(old).release()
            except OSError as error:
                logger.warning("could not clean the previous vault: %s", error)
        shutil.rmtree(self.state / "queue", ignore_errors=True)
        for name in ("graph.sqlite", "graph.sqlite-wal", "graph.sqlite-shm"):
            (self.state / name).unlink(missing_ok=True)
        (self.state / "cleaned.date").unlink(missing_ok=True)
        temporary = self.state / "vault.path.tmp"
        temporary.write_text(identity)
        os.replace(temporary, self.state / "vault.path")

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
        previous = identity_path.read_text()
        if previous != identity:
            self._rebind(previous, identity)
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
            raise RequestError("모델 실행기가 설정되지 않았습니다.")

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
                        "수정안이 요청 크기 한도(2MiB)를 넘습니다.",
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
        except MarkchainError as error:
            if isinstance(error, SearchUnavailableError):
                raise
            # Model output that breaks an edit rule is re-drafted with the
            # rule violation as feedback, like a review rejection.
            if error.retryable or error.code == "model_output":
                return self._again(
                    state, "model_attempt", self.settings.jobs.error_attempts, error
                )
            logger.info("job=%s failed [%s]: %s", _job(state["key"]), error.code, error)
            return {"status": "failed", "error": str(error), "error_code": error.code}

    def _again(
        self, state: _State, counter: str, limit: int, error: MarkchainError
    ) -> dict[str, Any]:
        """Re-plan a retryable failure with feedback, up to the configured limit."""
        attempt = int(state.get(counter, 0)) + 1  # type: ignore[call-overload]
        label = "검토 거절" if error.code == "review_rejected" else "모델 출력 오류"
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
                "error": (
                    f"{label}로 수정안을 다시 작성하고 있습니다 ({attempt}/{limit})."
                ),
                "error_code": error.code,
            }
        detail = feedback[-1] if feedback else str(error)
        return {
            "status": "failed",
            counter: attempt,
            "error": f"{label}로 {attempt}회 다시 작성했으나 실패했습니다: {detail}",
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
                    "원문이 변경되어 최신 원문으로 다시 계획하고 있습니다 "
                    f"({revision}/{limit})."
                ),
                "error_code": "source_changed",
            }
        prefix = (
            "원문이 계속 변경되어 작업을 중단했습니다"
            if mode == "bypass"
            else "원문이 변경되어 작업을 중단했습니다"
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
                                f"근거로 사용한 문서가 변경되었습니다: {path}"
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
                        "error": f"반영할 수 없는 대상입니다: {error}",
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


# Jobs that end here keep only a small record; their working data is dropped.
_DISCARDED = {"failed", "cancelled", "expired"}
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
        {"tool": "issue_key", "note": "새 키 발급"},
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
    if not intent.allow_rewrite:
        payload.pop("allow_rewrite")
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
    }


def _route(state: _State) -> str:
    if state["status"] == "planning":
        return "prepare"
    return "apply" if state["status"] == "running" else END
