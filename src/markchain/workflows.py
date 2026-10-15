from __future__ import annotations

__all__ = ["run_workflow"]

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from markchain.context import encoded_size
from markchain.editing import (
    Decision,
    EditPolicy,
    Review,
    describe_changes,
    materialize,
)
from markchain.errors import (
    ConflictError,
    ModelError,
    RequestError,
    ReviewRejectedError,
)
from markchain.prompts import draft_prompt, review_prompt
from markchain.sync import Synchronizer
from markchain.trace import Trace

if TYPE_CHECKING:
    from markchain.model import ClaudeModel


class _State(TypedDict):
    request: dict[str, Any]
    documents: dict[str, dict[str, str]]
    listed: list[dict[str, Any]]
    reads_left: int
    coverage: Any
    previous: dict[str, Any] | None
    feedback: list[str]
    writable: list[str]
    applied: list[str]
    signatures: dict[str, str]
    decision: dict[str, Any]
    proposal: dict[str, Any]
    review: dict[str, Any]
    usage: dict[str, Any]


# cost: time O(n*r+t), heap O(n), stack O(d), io O(f), tokens O(n)
# vars: n = bounded context/result bytes, r = patches per file, t = model latency
# vars: f = checked files, d = directory depth; drafts <= 1 + extra reads
# basis: estimate
def run_workflow(
    context: dict[str, Any],
    model: ClaudeModel,
    synchronizer: Synchronizer,
    trace: Trace,
    is_cancelled: Callable[[], bool],
) -> dict[str, Any]:
    profile = context["request"]["profile"]
    workflow = _Workflow(model, synchronizer, trace, is_cancelled)
    draft, review = "draft_" + profile, "review_" + profile
    builder = StateGraph(_State)
    builder.add_node(draft, workflow.draft)
    builder.add_node("read_more", workflow.read_more)
    builder.add_node("assemble", workflow.assemble)
    builder.add_node(review, workflow.review)
    builder.add_edge(START, draft)
    builder.add_conditional_edges(draft, workflow.after_draft)
    builder.add_edge("read_more", draft)
    builder.add_conditional_edges(
        "assemble", lambda s: review if s["proposal"] else END
    )
    builder.add_edge(review, END)
    initial = {"decision": {}, "proposal": {}, "review": {}, "usage": {}, **context}
    try:
        result = builder.compile(name=profile).invoke(
            cast(_State, initial), durability="sync"
        )
    except (ValidationError, KeyError) as error:
        raise ModelError("모델 출력이 수정 형식과 맞지 않습니다") from error
    workflow.guard(cast(_State, result))
    check = result.get("review", {})
    if result["proposal"] and (not check["approved"] or check["issues"]):
        raise ReviewRejectedError(list(check["issues"]) or ["승인되지 않음"])
    return {
        "proposal": result["proposal"],
        "decision": result["decision"],
        "review": check,
        "usage": result["usage"],
        "dependencies": {
            path: doc["hash"] for path, doc in result["documents"].items()
        },
        "read": sorted(result["documents"]),
        "listed": [item["path"] for item in result["listed"]],
    }


class _Workflow:
    def __init__(
        self,
        model: ClaudeModel,
        synchronizer: Synchronizer,
        trace: Trace,
        is_cancelled: Callable[[], bool],
    ) -> None:
        self.model = model
        self.synchronizer = synchronizer
        self.trace = trace
        self.is_cancelled = is_cancelled

    def guard(self, state: _State) -> None:
        if self.is_cancelled() or not self.synchronizer.matches(state["signatures"]):
            raise ConflictError("작업 중 원문이 바뀌었거나 작업이 취소됐습니다")

    def _invalidated(self, state: _State) -> bool:
        return self.is_cancelled() or not self.synchronizer.matches(state["signatures"])

    # cost: time O(n+t), heap O(n), stack O(1), io O(f), tokens O(n)
    # vars: n = prompt/result bytes, t = model latency, f = checked source files
    # basis: estimate
    def draft(self, state: _State) -> dict[str, Any]:
        self.guard(state)
        response = _call(
            self.model.generate,
            draft_prompt(cast(dict[str, Any], state)),
            lambda: self._invalidated(state),
            self.trace,
            "model",
        )
        self.guard(state)
        decision = Decision.model_validate(response["decision"])
        usage = state.get("usage") or {}
        drafts = [*usage.get("drafts", []), response.get("usage")]
        return {
            "decision": decision.model_dump(),
            "usage": {**usage, "draft": response.get("usage"), "drafts": drafts},
        }

    def after_draft(self, state: _State) -> str:
        decision = state["decision"]
        if (
            decision.get("read_more")
            and not decision.get("changes")
            and state["reads_left"] > 0
            and self._readable(state)
        ):
            return "read_more"
        return "assemble"

    def _readable(self, state: _State) -> list[str]:
        listed = {item["path"] for item in state["listed"]}
        wanted = [p for p in state["decision"]["read_more"] if p in listed]
        limit = self.synchronizer.settings.search.extra_read_documents
        return wanted[:limit]

    # cost: time O(n), heap O(n), stack O(1), io O(k)
    # vars: n = added document bytes, k = requested documents
    # basis: estimate
    def read_more(self, state: _State) -> dict[str, Any]:
        """Load listed documents the model asked for, within the context limit."""
        self.guard(state)
        limit = self.synchronizer.settings.context_bytes
        documents = dict(state["documents"])
        used = sum(encoded_size(doc) for doc in documents.values())
        added = []
        for path in self._readable(state):
            document = self.synchronizer.read(path)
            size = encoded_size(document)
            if used + size > limit:
                continue
            documents[path] = document
            used += size
            added.append(path)
        return {
            "documents": documents,
            "signatures": {p: d["signature"] for p, d in documents.items()},
            "listed": [item for item in state["listed"] if item["path"] not in added],
            # No progress means further reads cannot help within this request.
            "reads_left": state["reads_left"] - 1 if added else 0,
        }

    # cost: time O(n*r), heap O(n), stack O(1), io O(f)
    # vars: n = source/result bytes, r = patches per file, f = checked files
    # basis: estimate
    def assemble(self, state: _State) -> dict[str, Any]:
        self.guard(state)
        decision = Decision.model_validate(state["decision"])
        if decision.read_more and not decision.changes and decision.action != "defer":
            decision = decision.model_copy(update={"action": "defer"})
        request = state["request"]
        proposal = materialize(
            decision,
            state["documents"],
            self.synchronizer.documents,
            EditPolicy(
                settings=self.synchronizer.settings,
                allow_delete=request["allow_delete"],
                allow_rewrite=request.get("allow_rewrite", False),
                applied=frozenset(state.get("applied", [])),
            ),
        )
        if len(json.dumps(proposal).encode()) > 2 * 1024 * 1024:
            raise RequestError("수정안이 2MiB를 넘습니다", code="proposal_too_large")
        return {"proposal": proposal, "decision": decision.model_dump()}

    # cost: time O(n+t), heap O(n), stack O(1), io O(f), tokens O(n)
    # vars: n = review context/result bytes, t = model latency, f = checked files
    # basis: estimate
    def review(self, state: _State) -> dict[str, Any]:
        self.guard(state)
        changed = {c["path"] for c in state["proposal"]["changes"]}
        cited = set(state["decision"].get("sources", []))
        sources = {
            path: doc
            for path, doc in state["documents"].items()
            if path in changed | cited
        }
        prompt = review_prompt(
            {**state, "documents": sources},
            describe_changes(state["proposal"], state["documents"]),
        )
        response = _call(
            self.model.review,
            prompt,
            lambda: self._invalidated(state),
            self.trace,
            "review",
        )
        self.guard(state)
        result = Review.model_validate(response["review"])
        return {
            "review": result.model_dump(),
            "usage": {**state["usage"], "review": response.get("usage")},
        }


# cost: time O(n+t), heap O(n), stack O(1), io 1, tokens O(n)
# vars: n = bounded request/result bytes, t = model latency
# basis: estimate
def _call(
    operation: Callable[[str, Callable[[], bool]], dict[str, Any]],
    prompt: str,
    is_cancelled: Callable[[], bool],
    trace: Trace,
    stage: str,
) -> dict[str, Any]:
    if len(prompt.encode()) > 512 * 1024:
        raise RequestError(
            "검색된 문서가 모델 입력 한도(512KiB)를 넘습니다. 폴더를 좁혀 주세요",
            code="context_limit",
        )
    with trace.measure(stage) as metrics:
        response = operation(prompt, is_cancelled)
        if trace.enabled:
            metrics["input_bytes"] = len(prompt.encode())
            usage = response.get("usage") or {}
            for name in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
            ):
                if isinstance(usage.get(name), (int, float)):
                    metrics[name] = usage[name]
    return response
