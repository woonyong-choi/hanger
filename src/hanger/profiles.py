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
        "Preserve the author's intent, voice, questions and uncertainty. "
        "Do not turn notes into verified facts or merge different questions.",
    ),
    "wiki": ProfilePolicy(
        "wiki",
        "Explain concepts, prerequisites, conditions and exceptions precisely. "
        "Preserve source citations and version scope. Separate sourced facts from "
        "hypotheses, opinion and observations. Do not invent external verification. "
        "Defer factual changes that require missing evidence.",
        require_evidence=True,
    ),
    "work_record": ProfilePolicy(
        "work_record",
        "Connect problem, hypothesis, alternatives, decision, evidence and "
        "remaining work chronologically. Distinguish planned from executed and "
        "observed from inferred. Never invent measurements or claim an unexecuted "
        "test passed.",
        require_evidence=True,
    ),
    "blog": ProfilePolicy(
        "blog",
        "Keep a coherent reader journey: concrete problem, reasoning, evidence, "
        "result and limitation. Preserve the author's voice and experience without "
        "inventing personal achievements. Avoid promotional claims and repetition.",
    ),
    "creative": ProfilePolicy(
        "creative",
        "Preserve narrative voice, point of view, character identity, timeline and "
        "world rules. Treat fiction as fiction, not factual assertions. Do not merge "
        "distinct scenes because their topics match. Preserve deliberate ambiguity "
        "unless the user explicitly asks to resolve it.",
    ),
}
