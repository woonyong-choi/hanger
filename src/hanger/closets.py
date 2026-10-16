"""Closets: the note folders Hanger keeps, and what it may do inside each.

A closet is a folder registered in the state database. Two files at its root
steer Hanger:

* ``.hangerignore`` says which files Hanger leaves alone. Hanger acts on the
  copy it last applied; an edited or deleted file becomes a question for the
  user (a *decision*) and takes effect only once the user approves it.
* ``RULES.md`` files tell the model how notes are kept, folder by folder.
"""

from __future__ import annotations

__all__ = [
    "ClosetPolicy",
    "check_ignore",
    "describe_decision",
    "register_closet",
    "resolve_decision",
]

import os
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from hanger.errors import RequestError
from hanger.ignore import DEFAULT_IGNORE, IGNORE_FILE, IgnoreRules, summarize_change
from hanger.rules import DEFAULT_RULES, RULES_FILE, rules_for
from hanger.store import Closet, Decision, StateStore

Answer = Literal["approve", "decline"]


class ClosetPolicy:
    """What Hanger may index, read and change in one closet.

    Shared by every component of a closet; ``update`` swaps in newly approved
    ignore rules for all of them at once.
    """

    def __init__(self, root: Path, ignore_text: str = DEFAULT_IGNORE) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._ignore = IgnoreRules(ignore_text)

    @property
    def ignore_text(self) -> str:
        return self._ignore.text

    def update(self, ignore_text: str) -> None:
        with self._lock:
            self._ignore = IgnoreRules(ignore_text)

    def excluded(self, path: str, *, folder: bool = False) -> bool:
        """Return whether a closet-relative path is outside Hanger's reach.

        Rule files are never treated as notes, so the model cannot edit the
        rules it is given.
        """
        if not folder and PurePosixPath(path).name == RULES_FILE:
            return True
        return self._ignore.ignored(path, folder=folder)

    def writable(self, path: str) -> bool:
        return not self.excluded(path) and rules_for(self.root, path).writable

    def check_folders(self, folders: Iterable[str]) -> None:
        for folder in folders:
            if self.excluded(folder, folder=True):
                raise RequestError(
                    f"This folder is ignored in {IGNORE_FILE}: {folder}",
                    code="excluded",
                )

    # cost: time O(p*d+r), heap O(r), stack O(1), io O(p*d)
    # vars: p = paths, d = folder depth, r = rule file bytes
    # basis: estimate
    def rules(
        self, paths: Iterable[str], folders: Iterable[str] = ()
    ) -> list[dict[str, str]]:
        """Rule texts for the closet root, the given notes and the given folders."""
        found: dict[str, str] = {}
        targets = [
            ("", True),
            *((path, False) for path in paths),
            *((folder, True) for folder in folders),
        ]
        for path, folder in targets:
            for rule_path, text in rules_for(self.root, path, folder=folder).texts:
                found.setdefault(rule_path, text)
        # From the root down, so nearer rules come later.
        ordered = sorted(found.items(), key=lambda item: (item[0].count("/"), item[0]))
        return [{"file": path, "rules": text} for path, text in ordered]


def ignore_file(root: Path) -> Path:
    return root / IGNORE_FILE


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError:
        return ""


def _write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


# cost: time O(f*p), heap O(f), stack O(d), io O(f)
# vars: f = files under the closet, p = patterns, d = folder depth
# basis: estimate
def _notes(root: Path, before: IgnoreRules, after: IgnoreRules) -> Iterator[str]:
    """Markdown files whose fate the two rule sets could decide differently."""
    for parent, folders, files in os.walk(root, followlinks=False):
        base = Path(parent).relative_to(root)
        folders[:] = [
            name
            for name in folders
            if not (
                before.ignored((base / name).as_posix(), folder=True)
                and after.ignored((base / name).as_posix(), folder=True)
            )
        ]
        for name in files:
            if name.lower().endswith(".md") and name != RULES_FILE:
                yield (base / name).as_posix()


# cost: time O(f*p), heap O(f), stack O(d), io O(f)
# vars: f = files under the closet, p = patterns, d = folder depth
# basis: estimate
def check_ignore(store: StateStore, closet: Closet) -> int | None:
    """Compare the ignore file with the applied rules.

    Returns the pending decision number, or None when nothing waits. An edit
    that only touches comments is applied at once; a file put back to the
    applied text withdraws the question.
    """
    applied = store.applied_ignore(closet.id)
    current = _read(ignore_file(closet.path))
    pending = [d for d in store.decisions() if d.closet_id == closet.id]
    if current == applied:
        for decision in pending:
            store.close_decision(decision.id, "withdrawn")
        return None
    for decision in pending:
        # Already asked about exactly this file: no need to walk the closet again.
        kind = "ignore_deleted" if current is None else "ignore_change"
        if decision.kind == kind and decision.detail.get("text") == current:
            return decision.id
    before = IgnoreRules(applied)
    if current is None:
        after = IgnoreRules(DEFAULT_IGNORE)
        kind = "ignore_deleted"
    else:
        after = IgnoreRules(current)
        if after.patterns() == before.patterns():
            store.set_applied_ignore(closet.id, current)
            for decision in pending:
                store.close_decision(decision.id, "withdrawn")
            return None
        kind = "ignore_change"
    summary = summarize_change(
        before, after, sorted(_notes(closet.path, before, after))
    )
    detail: dict[str, Any] = {"closet": str(closet.path), "summary": summary}
    if current is not None:
        detail["text"] = current
    return store.open_decision(closet.id, kind, detail)


def describe_decision(decision: Decision) -> dict[str, Any]:
    """The question, its consequences and the two answers, ready to show."""
    detail = decision.detail
    summary = detail["summary"]
    leaving = summary["files_leaving_index"]
    entering = summary["files_entering_index"]
    effect = (
        f"{_count(leaving, 'note')} would leave the search index and "
        f"{_count(entering, 'note')} would be indexed."
    )
    if decision.kind == "ignore_deleted":
        question = (
            f"The {IGNORE_FILE} file in {detail['closet']} was deleted. "
            f"Recreate it with the default rules? {effect}"
        )
        approve = "Recreate the file with the default rules"
        decline = "Restore the file as it was"
    else:
        changes = []
        if summary["patterns_added"]:
            changes.append("adds " + ", ".join(summary["patterns_added"]))
        if summary["patterns_removed"]:
            changes.append("removes " + ", ".join(summary["patterns_removed"]))
        question = (
            f"The {IGNORE_FILE} file in {detail['closet']} was edited; the edit "
            f"{' and '.join(changes)}. Apply it? {effect}"
        )
        approve = "Apply the edit"
        decline = "Keep the current rules and restore the file"
    if leaving:
        question += (
            " Notes removed from the index may not get the same keywords and "
            "links when they are indexed again."
        )
    return {
        "id": decision.id,
        "closet": detail["closet"],
        "question": question,
        "choices": {"approve": approve, "decline": decline},
        "examples_leaving": summary["examples_leaving"],
        "examples_entering": summary["examples_entering"],
        "answer_with": f"hanger decide {decision.id} approve|decline",
    }


def _count(value: int, word: str) -> str:
    return f"{value:,} {word}" + ("" if value == 1 else "s")


def resolve_decision(
    store: StateStore, decision_id: int, answer: Answer
) -> dict[str, Any]:
    """Carry out the user's answer to a pending decision."""
    decision = store.decision(decision_id)
    if decision is None:
        raise RequestError(f"No decision #{decision_id}.", code="not_found")
    if decision.status == "withdrawn":
        raise RequestError(
            f"Decision #{decision_id} was replaced by a newer question. List the "
            "pending decisions and answer the current one.",
            code="decision_withdrawn",
        )
    if decision.status != "pending":
        raise RequestError(
            f"Decision #{decision_id} was already answered ({decision.status}).",
            code="decision_closed",
        )
    closet = next(c for c in store.closets() if c.id == decision.closet_id)
    path = ignore_file(closet.path)
    if answer == "approve":
        text = decision.detail.get("text", DEFAULT_IGNORE)
        expected = None if decision.kind == "ignore_deleted" else text
        if _read(path) != expected:
            check_ignore(store, closet)
            raise RequestError(
                f"The {IGNORE_FILE} file changed again before the answer. List the "
                "pending decisions and answer the current one.",
                code="decision_withdrawn",
            )
        if decision.kind == "ignore_deleted":
            _write(path, text)
            message = f"Recreated {IGNORE_FILE} with the default rules."
        else:
            message = "Applied the edit."
        store.set_applied_ignore(closet.id, text)
        message += " The search index is being updated."
    else:
        _write(path, store.applied_ignore(closet.id))
        message = f"Kept the current rules and restored {IGNORE_FILE}."
    store.close_decision(decision.id, "approved" if answer == "approve" else "declined")
    return {
        "decision": decision.id,
        "answer": answer,
        "closet": str(closet.path),
        "message": message,
    }


def register_closet(store: StateStore, path: Path) -> dict[str, Any]:
    """Register a folder and give it the default ignore and rule files.

    Files already in the folder are kept: an existing ignore file becomes the
    applied rules, since registering it is the user's approval.
    """
    root = path.expanduser().resolve()
    existing = _read(ignore_file(root)) if root.is_dir() else None
    closet = store.add_closet(root, DEFAULT_IGNORE if existing is None else existing)
    created = []
    try:
        if existing is None:
            _write(ignore_file(root), DEFAULT_IGNORE)
            created.append(IGNORE_FILE)
        if not (root / RULES_FILE).exists():
            _write(root / RULES_FILE, DEFAULT_RULES)
            created.append(RULES_FILE)
    except OSError as error:
        store.remove_closet(closet.id)
        raise RequestError(
            f"Could not create the closet files in {root}: {error}",
            code="closet_access",
        ) from error
    return {"closet": closet.as_dict(), "created": created}
