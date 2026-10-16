from __future__ import annotations

__all__ = ["Decision", "EditPolicy", "Review", "describe_changes", "materialize"]

import difflib
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from hanger.closets import ClosetPolicy
from hanger.documents import Documents
from hanger.errors import RequestError
from hanger.requests import Change, Proposal


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

    # Where the closet allows writing; None allows every Markdown path.
    closet: ClosetPolicy | None = None
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
    unknown = [path for path in decision.sources if path not in sources]
    if unknown:
        raise RequestError(
            f"You cited notes you were not given: {', '.join(unknown)}. Cite only "
            "the given notes, or 'request' for the user's own text.",
            code="model_output",
        )
    for evidence in decision.evidence:
        if (
            evidence.path not in decision.sources
            or evidence.quote not in sources[evidence.path]["content"]
        ):
            raise RequestError(
                "A quote in evidence is not in its note. Quote the note's text "
                "exactly.",
                code="model_output",
            )
    if policy.require_evidence and decision.changes and not decision.evidence:
        raise RequestError(
            "This kind of note needs evidence for every change. Put the exact "
            "source text each change relies on in evidence.",
            code="model_output",
        )
    if decision.action in {"keep", "defer"} and decision.changes:
        raise RequestError(
            "A keep or defer decision cannot include changes.", code="model_output"
        )
    changes = []
    for edit in decision.changes:
        target = documents.resolve(edit.path)
        if policy.closet is not None and not policy.closet.writable(edit.path):
            raise RequestError(
                f"This path may not be written (ignored, or write: false in its "
                f"HANGER.md): {edit.path}",
                code="write_denied",
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
                    "A new note needs a path that does not exist yet and its full "
                    "content.",
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
                    "An existing note can only be changed by replacing exact "
                    "passages of a note you were given and cited.",
                    code="model_output",
                )
            if edit.operation == "delete":
                if not policy.allow_delete or edit.replacements:
                    raise RequestError(
                        f"This request does not allow deleting {edit.path}. "
                        "Deleting needs allow_delete.",
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
            "A change needs at least one passage to replace.", code="model_output"
        )
    spans = []
    for replacement in replacements:
        start = content.find(replacement.old)
        if start < 0 or content.find(replacement.old, start + 1) >= 0:
            raise RequestError(
                "Each passage to replace must appear exactly once in the note. "
                "Include more of the surrounding text.",
                code="model_output",
            )
        spans.append((start, start + len(replacement.old), replacement.new))
    spans.sort()
    pieces: list[str] = []
    cursor = 0
    for start, end, text in spans:
        if start < cursor:
            raise RequestError(
                "Two passages to replace overlap. Split them so they do not.",
                code="model_output",
            )
        pieces.extend((content[cursor:start], text))
        cursor = end
    pieces.append(content[cursor:])
    result = "".join(pieces)
    if result == content:
        raise RequestError(
            "The replacements leave the note unchanged.", code="model_output"
        )
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
