from __future__ import annotations

__all__ = ["Profile", "policies"]

from typing import Literal

Profile = Literal["note", "wiki", "work_record", "blog", "creative"]

policies: dict[Profile, str] = {
    "note": "Preserve the author's intent, voice, questions and uncertainty. "
    "Do not turn notes into verified facts or merge different questions.",
    "wiki": "Explain concepts, prerequisites, conditions and exceptions precisely. "
    "Preserve source citations and version scope. Separate sourced facts from "
    "hypotheses, opinion and observations. Do not invent external verification. "
    "Defer factual changes that require missing evidence.",
    "work_record": "Connect problem, hypothesis, alternatives, decision, evidence and "
    "remaining work chronologically. Distinguish planned from executed and observed "
    "from inferred. Never invent measurements or claim an unexecuted test passed.",
    "blog": "Keep a coherent reader journey: concrete problem, reasoning, evidence, "
    "result and limitation. Preserve the author's voice and experience without "
    "inventing personal achievements. Avoid promotional claims and repetition.",
    "creative": "Preserve narrative voice, point of view, character identity, "
    "timeline and world rules. Treat fiction as fiction, not factual assertions. "
    "Do not merge distinct scenes because their topics match. Preserve deliberate "
    "ambiguity unless the user explicitly asks to resolve it.",
}
