from __future__ import annotations

__all__ = ["SearchScope"]

from collections.abc import Sequence
from pathlib import PurePosixPath

from markdown_notes_agent.errors import RequestError


class SearchScope:
    # cost: time O(s*(d+log(n))), heap O(s*d), stack O(1)
    # vars: s = path bytes, d = maximum depth, n = supplied folders
    # basis: estimate
    def __init__(self, folders: Sequence[str] = ()) -> None:
        paths = set()
        for folder in folders:
            path = folder.rstrip("/")
            parts = path.split("/")
            if (
                not path
                or "\\" in path
                or "\x00" in path
                or any(part in {"", ".", ".."} for part in parts)
            ):
                raise RequestError(
                    "folders takes folder paths relative to the vault root, "
                    f"such as notes/projects: {folder}"
                )
            paths.add(path)
        self.folders = tuple(
            sorted(
                path
                for path in paths
                if not any(
                    str(parent) in paths for parent in PurePosixPath(path).parents
                )
            )
        )
        self._roots = frozenset(self.folders)
        self._ancestors = frozenset(
            str(parent)
            for path in self.folders
            for parent in PurePosixPath(path).parents
        )

    def contains(self, path: str) -> bool:
        return not self.folders or any(
            str(parent) in self._roots for parent in PurePosixPath(path).parents
        )

    def can_descend(self, folder: str) -> bool:
        return (
            folder in self._roots or folder in self._ancestors or self.contains(folder)
        )
