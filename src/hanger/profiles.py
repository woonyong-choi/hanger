"""Document profiles: what kind of document a request writes.

Each profile is one writing subgraph (see workflows.py). Today they share the
same steps and differ only in the policy text given to the model and in
whether changes must quote their sources.
"""

from __future__ import annotations

__all__ = ["PROFILES", "Profile", "ProfilePolicy"]

from dataclasses import dataclass
from typing import Literal

Profile = Literal["note", "wiki", "work_record", "blog", "creative"]


@dataclass(frozen=True)
class ProfilePolicy:
    name: Profile
    # Guidance for the drafting and reviewing model.
    guidance: str
    # Changes must quote the source text they rely on.
    require_evidence: bool = False


PROFILES: dict[Profile, ProfilePolicy] = {
    "note": ProfilePolicy(
        "note",
        "Preserve intent, voice, questions and uncertainty; "
        "do not promote notes to verified facts.",
    ),
    "wiki": ProfilePolicy(
        "wiki",
        "Explain prerequisites, conditions and exceptions. "
        "Preserve citations/version scope; defer factual changes missing evidence, "
        "but retain explicitly unverified claims as unverified.",
        require_evidence=True,
    ),
    "work_record": ProfilePolicy(
        "work_record",
        "Connect problem, alternatives, decision, evidence and remaining work "
        "chronologically. Separate planned/executed and observed/inferred; "
        "invent no measurements or test results.",
        require_evidence=True,
    ),
    "blog": ProfilePolicy(
        "blog",
        "Keep the reader journey from problem through reasoning/evidence to "
        "result/limits. Preserve voice; invent no achievements.",
    ),
    "creative": ProfilePolicy(
        "creative",
        "Preserve voice, viewpoint, characters, timeline, world rules and ambiguity. "
        "Keep fiction fictional and distinct scenes separate; do not force concepts.",
    ),
}
