"""Folder rules: HANGER.md files that tell Hanger how to keep each part of a closet.

A HANGER.md applies to its folder and everything below it. Settings at the top
of the file (between ``---`` lines) follow the nearest file; the written rules
are all passed on, from the closet root down to the nearest folder.
"""

from __future__ import annotations

__all__ = ["DEFAULT_RULES", "RULES_FILE", "rules_for"]

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

RULES_FILE = "HANGER.md"
# Total rule text passed to the model per request.
_MAX_RULE_BYTES = 32 * 1024

DEFAULT_RULES = """\
---
# Settings for this folder and everything below it. A HANGER.md in a
# subfolder can set the same key again to change it there.
#
# write: false keeps Hanger from creating, changing or deleting notes here.
write: true
---

# Rules for this folder

Hanger reads this file before it adds or changes notes in this folder or
below. Write here how notes in this folder should be kept, the way you would
explain it to a careful assistant: what belongs here, how notes are named and
laid out, and what must never change.

A HANGER.md in a subfolder adds rules for that folder. When two files
disagree, the one nearer the note wins. Hanger never edits these files.
"""


@dataclass(frozen=True)
class FolderRules:
    """Rules that apply to one closet path."""

    settings: dict[str, str]
    # (rule file path, written rules) from the closet root down.
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
    """Collect the HANGER.md files that apply to a closet-relative path."""
    parts = PurePosixPath(path).parts
    depth = len(parts) if folder else len(parts) - 1
    settings: dict[str, str] = {}
    found: list[tuple[str, str]] = []
    for level in range(max(depth, 0) + 1):
        relative = PurePosixPath(*parts[:level], RULES_FILE)
        try:
            text = (root / relative).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError, UnicodeDecodeError):
            continue
        values, body = _split(text)
        settings.update(values)
        if body:
            found.append((relative.as_posix(), body))
    # Over the size budget, the rules closest to the note are kept.
    kept: list[tuple[str, str]] = []
    size = 0
    for rule_path, body in reversed(found):
        size += len(body.encode())
        if size > _MAX_RULE_BYTES:
            break
        kept.append((rule_path, body))
    return FolderRules(settings, tuple(reversed(kept)))
