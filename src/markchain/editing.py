from __future__ import annotations

__all__ = ["Decision", "EditPolicy", "Review", "describe_changes", "materialize"]

import difflib
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from markchain.config import Settings
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
    read_more: list[str] = Field(default_factory=list, max_length=50)


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    issues: list[str]


@dataclass(frozen=True)
class EditPolicy:
    """Mechanical rules for turning a model decision into file changes.

    How much to change, and whether a change fits the document, is judged
    by the model and its review; these rules only keep edits exact and inside
    what the request allows.
    """

    settings: Settings = field(default_factory=Settings)
    allow_delete: bool = False
    # Set by the document profile: changes must quote their sources.
    require_evidence: bool = False


# cost: time O(n*r), heap O(n), stack O(1), io O(f)
# vars: n = source and proposal bytes, r = replacements per file, f = changed files
# basis: estimate
def materialize(
    decision: Decision,
    sources: dict[str, dict[str, str]],
    documents: Documents,
    policy: EditPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or EditPolicy()
    if any(path not in sources for path in decision.sources):
        raise RequestError(
            "읽지 않은 문서를 근거로 인용했습니다. 제공된 문서만 인용하십시오.",
            code="model_output",
        )
    for evidence in decision.evidence:
        if (
            evidence.path not in decision.sources
            or evidence.quote not in sources[evidence.path]["content"]
        ):
            raise RequestError(
                "인용한 문장이 해당 원문에 없습니다. 원문의 문장을 "
                "그대로 인용하십시오.",
                code="model_output",
            )
    if policy.require_evidence and decision.changes and not decision.evidence:
        raise RequestError(
            "이 문서 종류는 변경 근거 인용이 필요합니다. evidence에 근거가 된 원문 "
            "문장을 그대로 넣으십시오.",
            code="model_output",
        )
    if decision.action in {"keep", "defer"} and decision.changes:
        raise RequestError(
            "유지·보류 판정에는 문서 변경을 포함할 수 없습니다.", code="model_output"
        )
    changes = []
    for edit in decision.changes:
        target = documents.resolve(edit.path)
        if not policy.settings.writable(edit.path):
            raise RequestError(
                f"쓰기 허용 범위 밖의 경로입니다: {edit.path}", code="write_denied"
            )
        source = sources.get(edit.path)
        if edit.operation == "create":
            if (
                source is not None
                or target.exists()
                or edit.content is None
                or edit.replacements
            ):
                raise RequestError(
                    "새 문서는 존재하지 않는 경로와 전체 본문이 필요합니다.",
                    code="model_output",
                )
            after = edit.content
        else:
            if (
                source is None
                or edit.path not in decision.sources
                or edit.content is not None
            ):
                raise RequestError(
                    "기존 문서 수정은 읽고 인용한 원문에 대해 구간 "
                    "교체로만 할 수 있습니다.",
                    code="model_output",
                )
            if edit.operation == "delete":
                if not policy.allow_delete or edit.replacements:
                    raise RequestError(
                        f"삭제가 허용되지 않은 요청입니다: {edit.path}. 삭제하려면 "
                        "allow_delete가 필요합니다.",
                        code="delete_denied",
                    )
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
        raise RequestError(
            "수정에는 교체할 원문 구간이 하나 이상 필요합니다.", code="model_output"
        )
    spans = []
    for replacement in replacements:
        start = content.find(replacement.old)
        if start < 0 or content.find(replacement.old, start + 1) >= 0:
            raise RequestError(
                "교체할 원문 구간이 문서에서 정확히 한 번 나타나야 합니다. 앞뒤 "
                "문장을 포함해 구간을 늘리십시오.",
                code="model_output",
            )
        spans.append((start, start + len(replacement.old), replacement.new))
    spans.sort()
    pieces: list[str] = []
    cursor = 0
    for start, end, text in spans:
        if start < cursor:
            raise RequestError(
                "교체 구간이 서로 겹칩니다. 겹치지 않게 나누십시오.",
                code="model_output",
            )
        pieces.extend((content[cursor:start], text))
        cursor = end
    pieces.append(content[cursor:])
    result = "".join(pieces)
    if result == content:
        raise RequestError("교체 결과가 원문과 같습니다.", code="model_output")
    return result


# cost: time O(n), heap O(n), stack O(1)
# vars: n = source and result bytes
# basis: estimate
def describe_changes(
    proposal: dict[str, Any], sources: dict[str, dict[str, str]]
) -> list[dict[str, str]]:
    """Show reviewers only what changed instead of whole result files."""
    described = []
    for change in proposal.get("changes", []):
        path = change["path"]
        before = sources.get(path, {}).get("content")
        after = change.get("after")
        if after is None:
            described.append({"path": path, "operation": "delete"})
        elif before is None:
            described.append({"path": path, "operation": "create", "content": after})
        else:
            diff = difflib.unified_diff(
                before.splitlines(keepends=True),
                after.splitlines(keepends=True),
                fromfile=path,
                tofile=path,
                n=3,
            )
            described.append(
                {"path": path, "operation": "replace", "diff": "".join(diff)}
            )
    return described
