"""Writing subgraphs.

The runtime finds and manages documents; this module decides what to write.
A router sends each request to one writing subgraph:

* ``inbox`` stores collected input unchanged under a predictable name.
* ``profile_<name>`` drafts, checks and reviews changes for one document
  profile. All profiles share the same steps today; profile-specific steps
  and writing tools are added inside ``_profile_graph``.
"""

from __future__ import annotations

__all__ = ["collect_file_target", "collect_proposal", "inbox_path", "run_workflow"]

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypedDict, cast

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from hanger.context import encoded_size
from hanger.editing import (
    Decision,
    EditPolicy,
    Review,
    describe_changes,
    materialize,
)
from hanger.errors import (
    ConflictError,
    ModelError,
    RequestError,
    ReviewRejectedError,
)
from hanger.profiles import PROFILES, ProfilePolicy
from hanger.prompts import draft_prompt, review_prompt
from hanger.requests import Change, Proposal
from hanger.sync import Synchronizer
from hanger.trace import Trace

if TYPE_CHECKING:
    from hanger.model import CliModel


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
    # "" before the duplicate check, then "checked" or "found".
    similar: str
    # Collected files are copied as they are; the inbox only names them.
    target: str
    digest: str


_INBOX = "inbox"
_TITLE_CHARS = 40
_HASH_CHARS = 8
# Characters that are unsafe or awkward in file names on macOS and Windows.
_UNSAFE = re.compile(r'[\\/:*?"<>|#\[\]{}^`%~\x00-\x1f]+')
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_EXTENSION = re.compile(r"[A-Za-z0-9]{1,10}")


def inbox_path(text: str, collected_at: float) -> str:
    """Name collected text: collection time, title, short content hash.

    The title is the first Markdown heading, or else the first non-empty line
    (a link loses its scheme), cut to a fixed length.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    heading = next(
        (line.lstrip("#").strip() for line in lines if re.match(r"#{1,6}\s", line)),
        None,
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    return _inbox_name(heading or (lines[0] if lines else ""), digest, collected_at)


def inbox_file_path(name: str, digest: str, collected_at: float) -> str:
    """Name a collected file by the same rule; its own name is the title."""
    stem, dot, extension = name.rpartition(".")
    if not stem:  # no extension, or a name like ".env"
        stem, dot, extension = name, "", ""
    suffix = (dot + extension.lower()) if _EXTENSION.fullmatch(extension) else ""
    return _inbox_name(stem, digest, collected_at, suffix)


def _inbox_name(
    title: str, digest: str, collected_at: float, suffix: str = ".md"
) -> str:
    stamp = datetime.fromtimestamp(collected_at).strftime("%Y-%m-%d-%H%M%S")
    return f"{_INBOX}/{stamp}-{_title(title)}-{digest[:_HASH_CHARS]}{suffix}"


def _title(raw: str) -> str:
    raw = _SCHEME.sub("", raw).removeprefix("www.")
    words = _UNSAFE.sub(" ", unicodedata.normalize("NFC", raw)).split()
    title = "-".join(words).lstrip("-.")[:_TITLE_CHARS].rstrip("-.")
    return title or "untitled"


def collect_file_target(source: Path, collected_at: float) -> tuple[str, str]:
    """Run the writing router for a collected file; return (path, sha256)."""
    request = {"kind": "collect", "file": str(source), "collected_at": collected_at}
    result = _writing_graph(None).invoke(cast(_State, {**_EMPTY, "request": request}))
    return result["target"], result["digest"]


def collect_proposal(text: str, collected_at: float) -> Proposal:
    """Run the writing router for a collect request (no model is involved)."""
    request = {"kind": "collect", "text": text, "collected_at": collected_at}
    graph = _writing_graph(None)
    result = graph.invoke(cast(_State, {**_EMPTY, "request": request}))
    return Proposal.model_validate(result["proposal"])


_EMPTY: dict[str, Any] = {
    "documents": {},
    "listed": [],
    "reads_left": 0,
    "coverage": None,
    "previous": None,
    "feedback": [],
    "writable": [],
    "applied": [],
    "signatures": {},
    "decision": {},
    "proposal": {},
    "review": {},
    "usage": {},
    "similar": "",
    "target": "",
    "digest": "",
}


def _route(state: _State) -> str:
    request = state["request"]
    if request["kind"] == "collect":
        return _INBOX
    return "profile_" + str(request["profile"])


def _writing_graph(workflow: _Workflow | None) -> Any:
    builder = StateGraph(_State)
    builder.add_node(_INBOX, _inbox_graph())
    for policy in PROFILES.values():
        node = "profile_" + policy.name
        if workflow is not None:
            builder.add_node(node, _profile_graph(policy, workflow))
        else:
            builder.add_node(node, _unavailable)
        builder.add_edge(node, END)
    builder.add_conditional_edges(START, _route)
    builder.add_edge(_INBOX, END)
    return builder.compile(name="writing")


def _unavailable(state: _State) -> dict[str, Any]:
    raise RequestError("모델 실행기가 설정되지 않았습니다.")


def _inbox_graph() -> Any:
    """Store input exactly as received; organizing it is a separate request."""

    def store_as_received(state: _State) -> dict[str, Any]:
        request = state["request"]
        if "file" in request:
            source = Path(request["file"])
            hashed = hashlib.sha256()
            with source.open("rb") as reader:
                while chunk := reader.read(1024 * 1024):
                    hashed.update(chunk)
            digest = hashed.hexdigest()
            target = inbox_file_path(source.name, digest, request["collected_at"])
            return {"target": target, "digest": digest}
        path = inbox_path(request["text"], request["collected_at"])
        proposal = Proposal(changes=(Change(path=path, after=request["text"]),))
        return {"proposal": proposal.model_dump(mode="json")}

    builder = StateGraph(_State)
    builder.add_node("store_as_received", store_as_received)
    builder.add_edge(START, "store_as_received")
    builder.add_edge("store_as_received", END)
    return builder.compile(name=_INBOX)


def _profile_graph(policy: ProfilePolicy, workflow: _Workflow) -> Any:
    """Writing subgraph for one document profile.

    Shared steps: draft, optional extra reads, mechanical assembly, a check
    for similar existing documents before creating a new one, and review.
    Profile-specific steps belong here.
    """
    builder = StateGraph(_State)
    builder.add_node("draft", workflow.draft)
    builder.add_node("read_more", workflow.read_more)
    builder.add_node("assemble", lambda state: workflow.assemble(state, policy))
    builder.add_node("similar", workflow.similar)
    builder.add_node("review", workflow.review)
    builder.add_edge(START, "draft")
    builder.add_conditional_edges("draft", workflow.after_draft)
    builder.add_edge("read_more", "draft")
    builder.add_conditional_edges("assemble", workflow.after_assemble)
    builder.add_conditional_edges(
        "similar", lambda state: "draft" if state["similar"] == "found" else "review"
    )
    builder.add_edge("review", END)
    return builder.compile(name="profile_" + policy.name)


# cost: time O(n*r+t), heap O(n), stack O(d), io O(f), tokens O(n)
# vars: n = bounded context/result bytes, r = patches per file, t = model latency
# vars: f = checked files, d = directory depth; drafts <= 1 + extra reads
# basis: estimate
def run_workflow(
    context: dict[str, Any],
    model: CliModel,
    synchronizer: Synchronizer,
    trace: Trace,
    is_cancelled: Callable[[], bool],
) -> dict[str, Any]:
    workflow = _Workflow(model, synchronizer, trace, is_cancelled)
    initial = {**_EMPTY, **context}
    try:
        result = _writing_graph(workflow).invoke(
            cast(_State, initial), durability="sync"
        )
    except (ValidationError, KeyError) as error:
        raise ModelError("모델 출력이 수정안 형식과 맞지 않습니다.") from error
    workflow.guard(cast(_State, result))
    check = result.get("review", {})
    if result["proposal"] and (not check["approved"] or check["issues"]):
        raise ReviewRejectedError(
            list(check["issues"]) or ["검토에서 승인되지 않았습니다."]
        )
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
        model: CliModel,
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
            raise ConflictError("작업 중 원문이 변경되었거나 작업이 취소되었습니다.")

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
    def assemble(self, state: _State, policy: ProfilePolicy) -> dict[str, Any]:
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
                require_evidence=policy.require_evidence,
            ),
        )
        if len(json.dumps(proposal).encode()) > 2 * 1024 * 1024:
            raise RequestError(
                "수정안이 크기 한도(2MiB)를 넘습니다.", code="proposal_too_large"
            )
        return {"proposal": proposal, "decision": decision.model_dump()}

    def after_assemble(self, state: _State) -> str:
        if not state["proposal"]:
            return END
        if state["similar"] == "" and _created(state["proposal"]):
            return "similar"
        return "review"

    # cost: time O(s+n), heap O(n), stack O(1), io O(k)
    # vars: s = search cost per created document, n = loaded bytes, k = loaded files
    # basis: estimate
    def similar(self, state: _State) -> dict[str, Any]:
        """Before creating a document, look once for an existing one like it.

        Found documents are added to the sources and the model decides again
        whether to supplement them instead. Only one such pass is made.
        """
        self.guard(state)
        settings = self.synchronizer.settings
        limit = settings.context_bytes
        documents = dict(state["documents"])
        used = sum(encoded_size(doc) for doc in documents.values())
        created = _created(state["proposal"])
        found: list[str] = []
        for content in created.values():
            query = _summary(content)
            if not query:
                continue
            hits = self.synchronizer.search(query, settings.search.full_text)
            for hit in hits["documents"]:
                path = hit["path"]
                if path in documents or path in created or path in found:
                    continue
                if len(found) >= settings.search.extra_read_documents:
                    break
                try:
                    document = self.synchronizer.read(path)
                except (OSError, RequestError, ConflictError):
                    continue
                size = encoded_size(document)
                if used + size > limit:
                    continue
                documents[path] = document
                used += size
                found.append(path)
        if not found:
            return {"similar": "checked"}
        notice = (
            "새로 만들려는 문서와 비슷한 기존 문서가 있습니다: "
            + ", ".join(found)
            + ". 새 문서 대신 기존 문서를 보완하거나 병합하는 것이 맞는지 다시 "
            "판단하십시오."
        )
        return {
            "similar": "found",
            "documents": documents,
            "signatures": {p: d["signature"] for p, d in documents.items()},
            "feedback": [*state["feedback"], notice],
        }

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


def _created(proposal: dict[str, Any]) -> dict[str, str]:
    return {
        change["path"]: change["after"]
        for change in proposal.get("changes", [])
        if change.get("before") is None and change.get("after") is not None
    }


def _summary(content: str) -> str:
    """Title and first sentence of a new document, as a search query."""
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    heading = next((ln.lstrip("#").strip() for ln in lines if ln.startswith("#")), "")
    body = next((ln for ln in lines if not ln.startswith("#")), "")
    return f"{heading} {body}".strip()[:1000]


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
            "검색된 문서가 모델 입력 한도(512KiB)를 넘습니다. 폴더 "
            "범위를 좁혀 다시 요청하십시오.",
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
