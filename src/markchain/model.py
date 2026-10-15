from __future__ import annotations

__all__ = ["ClaudeModel", "Decision", "Intent", "Planner"]

import json
import subprocess
import time
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from markchain.context import shape
from markchain.editing import Decision, Review
from markchain.errors import ConflictError, ModelError, RequestError
from markchain.profiles import Profile
from markchain.scope import SearchScope
from markchain.sync import Synchronizer
from markchain.trace import Trace
from markchain.workflows import run_workflow


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["write", "edit", "organize"]
    instruction: str = Field(min_length=1, max_length=16000)
    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Question or source text for search; not a required document title",
    )
    paths: tuple[str, ...] = Field(
        default=(),
        max_length=100,
        description="Explicit source paths read independently of the search folders",
    )
    folders: tuple[str, ...] = Field(
        default=(),
        description="Vault-relative folder array with OR scope; empty means all",
    )
    profile: Profile = "note"
    mode: Literal["normal", "bypass"] | None = Field(
        default=None,
        description="normal stops on source change; bypass re-plans. "
        "Omitted uses the configured default",
    )
    allow_delete: bool = False
    allow_rewrite: bool = Field(
        default=False,
        description="Allow edits that replace a large share of a document",
    )
    fallback_reason: (
        Literal["insufficient_evidence", "user_reported_missing"] | None
    ) = None

    @field_validator("folders")
    @classmethod
    def validate_folders(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return SearchScope(value).folders


class ClaudeModel:
    def __init__(self, model: str = "sonnet", timeout_seconds: float = 600) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds

    # cost: time O(t+n), heap O(n), stack O(1), io 1, tokens O(n)
    # vars: t = provider latency, n = bounded request and response bytes
    # basis: estimate
    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        response = self._invoke(prompt, is_cancelled, Decision)
        return {"decision": response["value"], "usage": response["usage"]}

    # cost: time O(t+n), heap O(n), stack O(1), io 1, tokens O(n)
    # vars: t = provider latency, n = bounded request and response bytes
    # basis: estimate
    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        response = self._invoke(prompt, is_cancelled, Review)
        return {"review": response["value"], "usage": response["usage"]}

    # cost: time O(t+n), heap O(n), stack O(1), io 1, tokens O(n)
    # vars: t = provider latency, n = bounded request and response bytes
    # basis: estimate
    def _invoke(
        self, prompt: str, is_cancelled: Callable[[], bool], schema: type[BaseModel]
    ) -> dict[str, Any]:
        if is_cancelled():
            raise ConflictError("model call invalidated or cancelled")
        command = [
            "claude",
            "--safe-mode",
            "-p",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema.model_json_schema()),
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
        ]
        deadline = time.monotonic() + self.timeout_seconds
        with subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            pending: str | None = prompt
            try:
                while True:
                    if is_cancelled():
                        raise ConflictError("model call invalidated or cancelled")
                    if time.monotonic() >= deadline:
                        raise ModelError("모델 응답 시간 초과")
                    try:
                        output, _ = process.communicate(pending, timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pending = None
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
        if process.returncode != 0:
            raise ModelError(f"모델 실행 실패 (종료 코드 {process.returncode})")
        try:
            envelope = json.loads(output)
            if envelope.get("is_error"):
                raise ModelError("모델이 오류를 반환했습니다")
            value = schema.model_validate(envelope.get("structured_output"))
        except (ValueError, ValidationError, AttributeError) as error:
            raise ModelError("모델 출력 형식 오류") from error
        return {"value": value.model_dump(), "usage": envelope.get("usage")}


class Planner:
    def __init__(self, synchronizer: Synchronizer, model: ClaudeModel) -> None:
        self.synchronizer = synchronizer
        self.model = model
        self.trace = Trace()

    # cost: time O(n+t), heap O(n), stack O(d), io O(f), tokens O(n)
    # vars: n = bounded context and model output bytes, t = model latency
    # vars: d = filesystem depth, f = documents checked
    # basis: estimate
    def prepare(
        self,
        intent: Intent,
        is_cancelled: Callable[[], bool],
        previous: dict[str, Any] | None = None,
        feedback: list[str] | None = None,
    ) -> dict[str, Any]:
        settings = self.synchronizer.settings
        with self.trace.measure("search") as metrics:
            search = self.synchronizer.search(
                intent.query,
                settings.search.candidates,
                fallback_reason=intent.fallback_reason,
                folders=intent.folders,
            )
            if self.trace.enabled:
                metrics["candidates"] = len(search["documents"])
                metrics["source"] = str(search.get("source", "unknown"))
                metrics["reason"] = str(search.get("reason", "ranked"))
                metrics["source_bytes"] = sum(
                    len(doc["content"].encode()) for doc in search["documents"]
                )
        required: dict[str, dict[str, Any]] = {}
        applied = previous.get("applied", {}) if previous else {}
        for path in intent.paths:
            try:
                required[path] = self.synchronizer.read(path)
            except FileNotFoundError as error:
                if path not in applied or applied[path] is not None:
                    raise RequestError(
                        f"입력 문서가 없습니다: {path}", code="input_missing"
                    ) from error
        if previous:
            for change in previous.get("proposal", {}).get("changes", []):
                try:
                    required[change["path"]] = self.synchronizer.read(change["path"])
                except FileNotFoundError:
                    required.pop(change["path"], None)
        terms = self.synchronizer.terms(intent.query)
        documents, listed = shape(search["documents"], required, settings, terms)
        context = {
            "request": intent.model_dump(mode="json"),
            "documents": documents,
            "listed": listed,
            "reads_left": settings.search.extra_reads,
            "coverage": search.get("coverage"),
            "previous": previous,
            "feedback": feedback or [],
            "writable": list(settings.vault.writable),
            "applied": sorted(applied),
            "signatures": {path: doc["signature"] for path, doc in documents.items()},
        }
        planned = run_workflow(
            context, self.model, self.synchronizer, self.trace, is_cancelled
        )
        return {
            **planned,
            "search": {
                **{k: v for k, v in search.items() if k != "documents"},
                "read": planned.pop("read"),
                "listed": planned.pop("listed"),
            },
        }
