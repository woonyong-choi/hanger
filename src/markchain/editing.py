from __future__ import annotations

__all__ = ["Decision", "Review", "materialize"]

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from markchain.documents import Documents
from markchain.errors import RequestError
from markchain.requests import Change, Proposal


class _Replacement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old: str = Field(min_length=1)
    new: str


class _Edit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    operation: Literal["replace", "create", "delete"]
    content: str | None = None
    replacements: list[_Replacement] = Field(default_factory=list)


class _Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    quote: str = Field(min_length=1)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["keep", "link", "supplement", "merge", "new", "defer"]
    reason: str = Field(min_length=1)
    sources: list[str]
    uncertainties: list[str]
    evidence: list[_Evidence] = Field(default_factory=list)
    changes: list[_Edit] = Field(max_length=100)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    issues: list[str]


# cost: time O(n*r), heap O(n), stack O(1), io O(f)
# vars: n = source and proposal bytes, r = replacements per file, f = changed files
# basis: estimate
def materialize(
    decision: Decision,
    sources: dict[str, dict[str, str]],
    documents: Documents,
    *,
    allow_delete: bool = False,
) -> dict[str, Any]:
    if any(path not in sources for path in decision.sources):
        raise RequestError("decision cited an unread source")
    for evidence in decision.evidence:
        if (
            evidence.path not in decision.sources
            or evidence.quote not in sources[evidence.path]["content"]
        ):
            raise RequestError("evidence does not match its cited source")
    if decision.action in {"keep", "defer"} and decision.changes:
        raise RequestError("keep or defer decision cannot mutate documents")
    changes = []
    for edit in decision.changes:
        target = documents.resolve(edit.path)
        if (
            not edit.path.startswith(("docs/", "inbox/", "legacy/", "worktree/"))
            or "/agent/" in edit.path
        ):
            raise RequestError("model write target is outside document workspaces")
        source = sources.get(edit.path)
        if edit.operation == "create":
            if (
                source is not None
                or target.exists()
                or edit.content is None
                or edit.replacements
            ):
                raise RequestError("create requires a new path and complete content")
            after = edit.content
        else:
            if (
                source is None
                or edit.path not in decision.sources
                or edit.content is not None
            ):
                raise RequestError(
                    "change requires a cited current source and no full replacement"
                )
            if edit.operation == "delete":
                if not allow_delete or edit.replacements:
                    raise RequestError("deletion requires explicit request permission")
                after = None
            else:
                after = _replace(source["content"], edit.replacements)
        changes.append(
            Change(
                path=edit.path, before=source["hash"] if source else None, after=after
            )
        )
    return Proposal(changes=tuple(changes)).model_dump(mode="json") if changes else {}


def _replace(content: str, replacements: list[_Replacement]) -> str:
    if not replacements:
        raise RequestError("replace requires at least one anchored change")
    spans = []
    for replacement in replacements:
        start = content.find(replacement.old)
        if start < 0 or content.find(replacement.old, start + 1) >= 0:
            raise RequestError("replacement anchor must match exactly once")
        spans.append((start, start + len(replacement.old), replacement.new))
    spans.sort()
    pieces: list[str] = []
    cursor = 0
    for start, end, text in spans:
        if start < cursor:
            raise RequestError("replacement ranges overlap")
        pieces.extend((content[cursor:start], text))
        cursor = end
    pieces.append(content[cursor:])
    result = "".join(pieces)
    if result == content:
        raise RequestError("replacement must change the document")
    return result
