"""What a job records, and how its state is explained to the caller."""

from __future__ import annotations

__all__ = ["EXTRA", "FINAL", "KEPT", "JobState", "describe_job", "initial_state"]

from collections.abc import Callable
from typing import Any, TypedDict


class JobState(TypedDict):
    key: str
    fingerprint: str
    proposal: dict[str, Any]
    deadline: float
    cursor: int
    status: str
    results: dict[str, str]
    intent: dict[str, Any]
    dependencies: dict[str, str]
    policy_dependencies: dict[str, str | None]
    revision: int
    decision: dict[str, Any]
    review: dict[str, Any]
    usage: dict[str, Any] | None
    search: dict[str, Any]
    error: str
    error_code: str
    applied: dict[str, str | None]
    prior_results: dict[str, str]
    # Set on a conflict once reconsider has moved it to a new key.
    resolved_by: str
    feedback: list[str]
    review_attempt: int
    model_attempt: int


# Jobs in these states keep one record and no history (see Runtime._discard).
FINAL = {"completed", "deferred", "conflict", "failed", "cancelled", "expired"}
# What each state keeps beyond the result: a summary of what was decided, or
# the instruction and proposal an open conflict needs for reconsider.
EXTRA: dict[str, tuple[str, ...]] = {
    "completed": ("intent", "decision", "review", "search", "usage", "prior_results"),
    "deferred": ("intent", "decision", "review", "search", "usage", "prior_results"),
    "conflict": ("intent", "proposal", "prior_results", "decision", "search"),
}
KEPT = (
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
        {"tool": "issue_key", "note": "get a new key"},
        {"tool": "resolve_conflict", "args": {"choice": choice}},
    ]


def initial_state(request: dict[str, Any]) -> JobState:
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
        "policy_dependencies": request.get("policy_dependencies", {}),
        "revision": 0,
        "decision": {},
        "review": {},
        "usage": None,
        "search": {},
        "error": "",
        "applied": prior.get("applied", {}),
        "resolved_by": "",
    }


def describe_job(
    state: dict[str, Any], health: Callable[[], str | None]
) -> dict[str, Any]:
    """The job state with a message and the tools that make sense next."""
    status = state["status"]
    actions: list[dict[str, Any]]
    if status in {"queued", "planning", "running"}:
        message = health() or state.get("error") or "The job is running."
        actions = [{"tool": "job_status"}, {"tool": "cancel_job"}]
    elif status == "failed":
        message = state.get("error") or "The job failed."
        actions = [
            {"tool": "issue_key", "note": "get a new key"},
            {"tool": "request_change", "note": "send the same request again"},
        ]
    elif status == "conflict" and state.get("resolved_by"):
        message = (
            "This job stopped on a changed note and is being reconsidered "
            "under key " + state["resolved_by"]
        )
        actions = [{"tool": "job_status", "args": {"key": state["resolved_by"]}}]
    elif status == "conflict":
        message = state.get("error") or "The job stopped because a note changed."
        actions = [{"tool": "read_document"}]
        if state.get("intent"):
            actions += _resolution("reconsider")
        else:
            actions += [
                {"tool": "issue_key", "note": "get a new key"},
                {"tool": "request_change", "note": "ask again on current notes"},
            ]
    elif status == "completed":
        changed = len(state.get("results", {}))
        message = (
            f"Done. Files changed: {changed}."
            if changed
            else "Done. No file needed a change."
        )
        actions = [{"tool": "read_document"}]
    elif status == "deferred":
        message = "Held back for lack of evidence: " + state.get("decision", {}).get(
            "reason", "no reason given"
        )
        actions = [{"tool": "read_document"}, {"tool": "request_change"}]
    else:
        message = {
            "cancelled": "The job was cancelled.",
            "expired": "The job stopped because its request key expired.",
        }.get(
            status, "The job stopped."
        ) + " Files already changed are listed in results."
        actions = [{"tool": "read_document"}, {"tool": "request_change"}]
    return {**state, "message": message, "next_actions": actions}
