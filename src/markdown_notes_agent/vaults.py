"""Vaults: the note folders Markdown Notes Agent keeps, and what it may do inside each.

A vault is a folder registered in the state database. Two files at its root
steer Markdown Notes Agent:

* ``.markdown-notes-agentignore`` says which files Markdown Notes Agent leaves alone.
  It acts on the
  copy it last applied; an edited or deleted file becomes a question for the
  user (a *decision*) and takes effect only once the user approves it.
* ``VAULT.md`` files tell the model how notes are kept, folder by folder.
"""

from __future__ import annotations

__all__ = [
    "VaultPolicy",
    "check_ignore",
    "vault_present",
    "describe_decision",
    "register_vault",
    "resolve_decision",
]

import hashlib
import os
import threading
from collections.abc import Iterable, Iterator
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from markdown_notes_agent.errors import ConflictError, RequestError
from markdown_notes_agent.ignore import (
    DEFAULT_IGNORE,
    IGNORE_FILE,
    IgnoreRules,
    summarize_change,
)
from markdown_notes_agent.rules import DEFAULT_RULES, RULES_FILE, rules_for
from markdown_notes_agent.store import Decision, StateStore, Vault

Answer = Literal["approve", "decline"]


class VaultPolicy:
    """What Markdown Notes Agent may index, read and change in one vault.

    Shared by every component of a vault; ``update`` swaps in newly approved
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
        """Return whether a vault-relative path is outside the allowed scope.

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
        """Rule texts for the vault root, the given notes and the given folders."""
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

    def context(
        self,
        paths: Iterable[str],
        folders: Iterable[str] = (),
        targets: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Capture scoped policies and every present or absent rule dependency."""
        catalog: dict[str, str] = {}
        dependencies: dict[str, str | None] = {
            "@applied_ignore": hashlib.sha256(self.ignore_text.encode()).hexdigest(),
            IGNORE_FILE: self._fingerprint(IGNORE_FILE),
        }
        scopes: dict[str, dict[str, list[str]]] = {
            "source_rules": {},
            "target_rules": {},
            "folder_rules": {},
        }
        read_only = []
        folder_writable: dict[str, bool] = {}
        for key, entries, folder in (
            ("source_rules", paths, False),
            ("target_rules", targets, False),
            ("folder_rules", ["", *folders], True),
        ):
            for path in entries:
                rules = rules_for(self.root, path, folder=folder)
                for name, fingerprint in rules.dependencies.items():
                    if name in dependencies and dependencies[name] != fingerprint:
                        raise ConflictError(
                            "folder rules changed while collecting policy"
                        )
                    dependencies[name] = fingerprint
                catalog.update(rules.texts)
                scopes[key][path] = [name for name, _ in rules.texts]
                if folder:
                    folder_writable[path] = rules.writable and not self.excluded(
                        path, folder=True
                    )
                if not folder and (not rules.writable or self.excluded(path)):
                    read_only.append(path)
        if sum(len(text.encode()) for text in catalog.values()) > 32 * 1024:
            raise RequestError("request rules exceed 32 KiB", code="rules_limit")
        return {
            **scopes,
            "rule_catalog": catalog,
            "read_only": sorted(set(read_only)),
            "folder_writable": folder_writable,
            "policy_dependencies": dependencies,
        }

    def matches(self, dependencies: dict[str, str | None]) -> bool:
        """Check rule contents, absence and both disk and applied ignore policy."""
        for path, expected in dependencies.items():
            actual = (
                hashlib.sha256(self.ignore_text.encode()).hexdigest()
                if path == "@applied_ignore"
                else self._fingerprint(path)
            )
            if actual != expected:
                return False
        return True

    def _fingerprint(self, path: str) -> str | None:
        try:
            return hashlib.sha256((self.root / path).read_bytes()).hexdigest()
        except (FileNotFoundError, NotADirectoryError):
            return None


def vault_present(root: Path) -> bool:
    """Whether a vault's folder is there to work on.

    An empty folder counts as absent: it is most likely an unmounted drive's
    mount point, and treating it as a vault would empty its index.
    """
    try:
        return root.is_dir() and any(root.iterdir())
    except OSError:
        return False


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
# vars: f = files under the vault, p = patterns, d = folder depth
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
# vars: f = files under the vault, p = patterns, d = folder depth
# basis: estimate
def check_ignore(store: StateStore, vault: Vault) -> int | None:
    """Compare the ignore file with the applied rules.

    Returns the pending decision number, or None when nothing waits. An edit
    that only touches comments is applied at once; a file put back to the
    applied text withdraws the question.
    """
    if not vault_present(vault.path):
        return None
    applied = store.applied_ignore(vault.id)
    current = _read(ignore_file(vault.path))
    pending = [d for d in store.decisions() if d.vault_id == vault.id]
    if current == applied:
        for decision in pending:
            store.close_decision(decision.id, "withdrawn")
        return None
    for decision in pending:
        # Already asked about exactly this file: no need to walk the vault again.
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
            store.set_applied_ignore(vault.id, current)
            for decision in pending:
                store.close_decision(decision.id, "withdrawn")
            return None
        kind = "ignore_change"
    summary = summarize_change(before, after, sorted(_notes(vault.path, before, after)))
    detail: dict[str, Any] = {"vault": str(vault.path), "summary": summary}
    if current is not None:
        detail["text"] = current
    return store.open_decision(vault.id, kind, detail)


def describe_decision(decision: Decision) -> dict[str, Any]:
    """The question, its consequences and the two answers, ready to show."""
    detail = decision.detail
    summary = detail["summary"]
    leaving = summary["files_leaving_index"]
    entering = summary["files_entering_index"]
    where = f"vault #{decision.vault_id} ({detail['vault']})"
    effect = (
        f"{_notes_phrase(leaving, 'leave')} the search index; "
        f"{_notes_phrase(entering, 'enter')} it."
    )
    if decision.kind == "ignore_deleted":
        question = (
            f"{IGNORE_FILE} was deleted in {where}. Recreate it with the default "
            f"rules? {effect}"
        )
        approve = f"Recreate {IGNORE_FILE} with the default rules"
        decline = f"Put the deleted {IGNORE_FILE} back as it was"
    else:
        changes = []
        if summary["patterns_added"]:
            changes.append(
                "adds " + ", ".join(f"`{p}`" for p in summary["patterns_added"])
            )
        if summary["patterns_removed"]:
            changes.append(
                "removes " + ", ".join(f"`{p}`" for p in summary["patterns_removed"])
            )
        question = (
            f"{IGNORE_FILE} was edited in {where}; the edit {' and '.join(changes)}. "
            f"Apply this edit? {effect}"
        )
        approve = "Apply the edit"
        decline = f"Keep the current rules and restore {IGNORE_FILE}"
    if leaving:
        question += (
            " Notes taken out of the index may not get the same keywords and "
            "links when they are indexed again."
        )
    return {
        "id": decision.id,
        "vault": detail["vault"],
        "question": question,
        "choices": {"approve": approve, "decline": decline},
        "examples_leaving": summary["examples_leaving"],
        "examples_entering": summary["examples_entering"],
        "answer_with": f"markdown-notes-agent decide {decision.id} approve|decline",
    }


def _notes_phrase(value: int, verb: str) -> str:
    if value == 1:
        return f"1 note {verb}s"
    return f"{value:,} notes {verb}" if value else f"no notes {verb}"


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
    vault = next((c for c in store.vaults() if c.id == decision.vault_id), None)
    if vault is None:
        raise RequestError(f"Vault #{decision.vault_id} was removed.", code="not_found")
    if not vault_present(vault.path):
        raise RequestError(
            f"The folder of vault #{vault.id} is missing or empty: {vault.path}. "
            "Reconnect it and answer again.",
            code="vault_unavailable",
        )
    path = ignore_file(vault.path)
    if answer == "approve":
        text = decision.detail.get("text", DEFAULT_IGNORE)
        expected = None if decision.kind == "ignore_deleted" else text
        if _read(path) != expected:
            check_ignore(store, vault)
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
        store.set_applied_ignore(vault.id, text)
        message += " The search index is being updated."
    else:
        _write(path, store.applied_ignore(vault.id))
        message = f"Kept the current rules and restored {IGNORE_FILE}."
    store.close_decision(decision.id, "approved" if answer == "approve" else "declined")
    return {
        "decision": decision.id,
        "answer": answer,
        "vault": str(vault.path),
        "message": message,
    }


def register_vault(store: StateStore, path: Path) -> dict[str, Any]:
    """Register a folder and give it the default ignore and rule files.

    Files already in the folder are kept: an existing ignore file becomes the
    applied rules, since registering it is the user's approval.
    """
    root = path.expanduser().resolve()
    existing = _read(ignore_file(root)) if root.is_dir() else None
    vault = store.add_vault(root, DEFAULT_IGNORE if existing is None else existing)
    created = []
    try:
        if existing is None:
            _write(ignore_file(root), DEFAULT_IGNORE)
            created.append(IGNORE_FILE)
        if not (root / RULES_FILE).exists():
            _write(root / RULES_FILE, DEFAULT_RULES)
            created.append(RULES_FILE)
    except OSError as error:
        store.remove_vault(vault.id)
        raise RequestError(
            f"Could not create the vault files in {root}: {error}",
            code="vault_access",
        ) from error
    return {"vault": vault.as_dict(), "created": created}
