"""Per-closet ignore rules: which files Hanger never indexes, searches or edits.

The rules live in ``.hangerignore`` at the closet root and use .gitignore
syntax. The text Hanger acts on is the *applied* copy kept in the state
database; a changed file only takes effect after the user approves the change.
"""

from __future__ import annotations

__all__ = ["DEFAULT_IGNORE", "IGNORE_FILE", "IgnoreRules", "summarize_change"]

from pathlib import PurePosixPath

import pathspec

IGNORE_FILE = ".hangerignore"

DEFAULT_IGNORE = """\
# Hanger ignore rules for this closet
#
# Files matched here are left out of the search index and are never read or
# changed by Hanger. Nothing is ignored until you add a pattern.
#
# The syntax is the same as .gitignore: one pattern per line, a trailing
# slash matches folders only, and a leading ! brings a path back in. For
# example:
#
#   .obsidian/
#   drafts/
#   *.private.md
#
# Hanger asks before it applies an edit to this file. Ignoring a folder
# removes its notes from the index, and indexing them again later may not
# give the same keywords and links.
"""


class IgnoreRules:
    """Compiled ignore patterns for one closet."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._spec = pathspec.GitIgnoreSpec.from_lines(text.splitlines())

    def ignored(self, path: str, *, folder: bool = False) -> bool:
        """Return whether a closet-relative path is ignored.

        A path is also ignored when any folder above it is, as with .gitignore.
        """
        parts = PurePosixPath(path).parts
        for depth in range(1, len(parts)):
            if self._spec.match_file("/".join(parts[:depth]) + "/"):
                return True
        return bool(self._spec.match_file(path + ("/" if folder else "")))

    def patterns(self) -> list[str]:
        return [
            line.strip()
            for line in self.text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]


# cost: time O(f*p), heap O(f), stack O(1), io 0
# vars: f = closet files checked, p = patterns
# basis: estimate
def summarize_change(
    before: IgnoreRules, after: IgnoreRules, files: list[str]
) -> dict[str, object]:
    """Describe what an ignore edit would do to the indexed files."""
    removed = [p for p in files if not before.ignored(p) and after.ignored(p)]
    added = [p for p in files if before.ignored(p) and not after.ignored(p)]
    old, new = before.patterns(), after.patterns()
    return {
        "patterns_added": [p for p in new if p not in old],
        "patterns_removed": [p for p in old if p not in new],
        "files_leaving_index": len(removed),
        "files_entering_index": len(added),
        "examples_leaving": removed[:5],
        "examples_entering": added[:5],
    }
