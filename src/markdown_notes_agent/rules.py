"""Folder rules from VAULT.md files.

A VAULT.md applies to its folder and everything below it.
Settings at the top
of the file (between ``---`` lines) follow the nearest file; the written rules
are all passed on, from the vault root down to the nearest folder.
"""

from __future__ import annotations

__all__ = ["DEFAULT_RULES", "RULES_FILE", "rules_for"]

import hashlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from markdown_notes_agent.errors import RequestError

RULES_FILE = "VAULT.md"
# Total rule text passed to the model per request.
_MAX_RULE_BYTES = 32 * 1024

DEFAULT_RULES = """\
---
# Settings for this folder and everything below it. A VAULT.md in a
# subfolder can set the same key again to change it there.
#
# write: false prevents creating, changing or deleting notes here.
write: true
---

# Rules for this folder

Markdown Notes Agent reads this file before it adds or changes notes in this folder or
below. Write here how notes in this folder should be kept, the way you would
explain it to a careful assistant: what belongs here, how notes are named and
laid out, and what must never change.

A VAULT.md in a subfolder adds rules for that folder. When two files
disagree, the one nearer the note wins. Markdown Notes Agent never edits these files.
"""


@dataclass(frozen=True)
class FolderRules:
    """Rules that apply to one vault path."""

    settings: dict[str, str]
    dependencies: dict[str, str | None]
    # (rule file path, written rules) from the vault root down.
    texts: tuple[tuple[str, str], ...]

    @property
    def writable(self) -> bool:
        return self.settings.get("write", "true").lower() not in {"false", "no", "0"}


def _split(text: str) -> tuple[dict[str, str], str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text.strip()
    settings: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return settings, "\n".join(lines[index + 1 :]).strip()
        key, colon, value = line.partition(":")
        if colon and key.strip() and not key.lstrip().startswith("#"):
            settings[key.strip()] = value.split("#")[0].strip().strip("\"'")
    return {}, text.strip()


# cost: time O(d+r), heap O(r), stack O(1), io O(d)
# vars: d = folder depth, r = rule file bytes
# basis: estimate
def rules_for(root: Path, path: str, *, folder: bool = False) -> FolderRules:
    """Collect rule files that apply to a vault-relative path."""
    parts = PurePosixPath(path).parts
    if PurePosixPath(path).is_absolute() or ".." in parts:
        raise RequestError("invalid rule scope", code="invalid_target")
    dependencies: dict[str, str | None] = {}
    depth = len(parts) if folder else len(parts) - 1
    settings: dict[str, str] = {}
    found: list[tuple[str, str]] = []
    for level in range(max(depth, 0) + 1):
        relative = PurePosixPath(*parts[:level], RULES_FILE)
        candidate = root / relative
        if not candidate.resolve().is_relative_to(root.resolve()):
            raise RequestError("rule path escapes vault", code="invalid_target")
        try:
            raw = candidate.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            dependencies[relative.as_posix()] = None
            continue
        dependencies[relative.as_posix()] = hashlib.sha256(raw).hexdigest()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RequestError(f"invalid UTF-8 rules: {relative}") from error
        values, body = _split(text)
        settings.update(values)
        if body:
            found.append((relative.as_posix(), body))
    if sum(len(body.encode()) for _, body in found) > _MAX_RULE_BYTES:
        raise RequestError("folder rules exceed 32 KiB", code="rules_limit")
    return FolderRules(settings, dependencies, tuple(found))
