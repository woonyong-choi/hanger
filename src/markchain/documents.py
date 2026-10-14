from __future__ import annotations

__all__ = ["Documents"]

import os
from pathlib import Path
from uuid import uuid4

from markchain.errors import ConflictError, RequestError
from markchain.requests import Change, Proposal, digest


class Documents:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve(strict=True)

    def resolve(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise RequestError("invalid document path")
        if path.as_posix() != relative:
            raise RequestError("document path must be canonical")
        if any(part.startswith(".") for part in path.parts):
            raise RequestError("hidden document path is not writable")
        if path.suffix.lower() != ".md":
            raise RequestError("only markdown documents are writable")
        target = self.root / path
        if not target.resolve().is_relative_to(self.root):
            raise RequestError("document path escapes vault")
        if any(p.is_symlink() for p in [target, *target.parents] if p != self.root):
            raise RequestError("symlink document paths are not writable")
        return target

    # cost: time O(n), heap O(n), stack O(1), io 1
    # vars: n = document bytes
    # basis: estimate
    def read_hash(self, relative: str) -> str | None:
        target = self.resolve(relative)
        try:
            return digest(target.read_bytes())
        except FileNotFoundError:
            return None

    # cost: time O(n), heap O(m), stack O(1), io O(f)
    # vars: n = total target bytes, m = largest target bytes, f = targets
    # basis: estimate
    def check_proposal(self, proposal: Proposal, cursor: int) -> None:
        for index, change in enumerate(proposal.changes):
            result = None if change.after is None else digest(change.after.encode())
            expected = {result} if index < cursor else {change.before, result}
            if self.read_hash(change.path) not in expected:
                raise ConflictError(f"document changed: {change.path}")

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = source and result bytes
    # basis: estimate
    def apply(self, change: Change) -> str:
        target = self.resolve(change.path)
        result = None if change.after is None else digest(change.after.encode())
        current = self.read_hash(change.path)
        if current == result:
            return "already_applied"
        if current != change.before:
            raise ConflictError(f"document changed: {change.path}")
        if change.after is None:
            target.unlink()
            _sync_directory(target.parent)
            return "applied"
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = target.with_name(f".{target.name}.{uuid4().hex}.pending")
        try:
            with pending.open("xb") as output:
                output.write(change.after.encode())
                output.flush()
                os.fsync(output.fileno())
            # External writers can still race the final compare and replacement.
            if self.read_hash(change.path) != change.before:
                raise ConflictError(f"document changed: {change.path}")
            if target.exists():
                pending.chmod(target.stat().st_mode & 0o777)
            os.replace(pending, target)
            _sync_directory(target.parent)
        finally:
            pending.unlink(missing_ok=True)
        return "applied"


# cost: time O(1), heap O(1), stack O(1), io O(1)
# basis: estimate
def _sync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
