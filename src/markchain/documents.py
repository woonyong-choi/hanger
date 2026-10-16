from __future__ import annotations

__all__ = ["Documents"]

import hashlib
import logging
import os
import shutil
from pathlib import Path

from markchain.errors import ConflictError, RequestError
from markchain.requests import Change, Proposal, digest

logger = logging.getLogger(__name__)

_DELETING = ".markchain-deleting"
# Space kept free on the disk after collecting a file.
_FREE_MARGIN = 1024 * 1024 * 1024


class Documents:
    def __init__(self, root: Path, writer_id: str = "direct") -> None:
        self.root = root.resolve(strict=True)
        self._writer_id = writer_id

    def resolve(self, relative: str) -> Path:
        if Path(relative).suffix.lower() != ".md":
            raise RequestError("Markdown(.md) 문서만 다룰 수 있습니다.")
        return self._resolve_any(relative)

    def _resolve_any(self, relative: str) -> Path:
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise RequestError(
                "문서 경로가 올바르지 않습니다. Vault 기준 상대 경로를 사용하십시오."
            )
        if path.as_posix() != relative:
            raise RequestError(
                "문서 경로가 정규 형식이 아닙니다. ./ 나 // 없이 지정하십시오."
            )
        if any(part.startswith(".") for part in path.parts):
            raise RequestError("숨김 경로의 문서는 다룰 수 없습니다.")
        target = self.root / path
        if not target.resolve().is_relative_to(self.root):
            raise RequestError("문서 경로가 Vault 밖을 가리킵니다.")
        if any(p.is_symlink() for p in [target, *target.parents] if p != self.root):
            raise RequestError("심볼릭 링크를 거치는 경로는 다룰 수 없습니다.")
        return target

    # cost: time O(n), heap O(1), stack O(1), io O(n/b)
    # vars: n = document bytes
    # vars: b = 64 KiB read block
    # basis: estimate
    def read_hash(self, relative: str) -> str | None:
        target = self.resolve(relative)
        try:
            hashed = hashlib.sha256()
            with target.open("rb") as source:
                while chunk := source.read(65536):
                    hashed.update(chunk)
            return hashed.hexdigest()
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
                raise ConflictError(f"원문이 변경되었습니다: {change.path}")

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = source and result bytes
    # basis: estimate
    def apply(self, change: Change) -> str:
        target = self.resolve(change.path)
        result = None if change.after is None else digest(change.after.encode())
        if change.after is None:
            # A delete interrupted after its rename is finished or undone first.
            self._settle_deleting(change, target)
        current = self.read_hash(change.path)
        if current == result:
            return "already_applied"
        if current != change.before:
            raise ConflictError(f"원문이 변경되었습니다: {change.path}")
        if change.after is None:
            return self._delete(change, target)
        target.parent.mkdir(parents=True, exist_ok=True)
        pending = self._pending_path(change.path)
        pending.unlink(missing_ok=True)
        try:
            with pending.open("xb") as output:
                output.write(change.after.encode())
                output.flush()
                os.fsync(output.fileno())
            # External writers can still race the final compare and replacement.
            if self.read_hash(change.path) != change.before:
                raise ConflictError(f"원문이 변경되었습니다: {change.path}")
            if target.exists():
                pending.chmod(target.stat().st_mode & 0o777)
            os.replace(pending, target)
            _sync_directory(target.parent)
        finally:
            pending.unlink(missing_ok=True)
        return "applied"

    # cost: time O(n), heap O(1), stack O(1), io O(n/b)
    # vars: n = file bytes, b = 1 MiB copy block
    # basis: estimate
    def store_file(self, relative: str, source: Path, expected: str) -> str:
        """Copy any file into the vault unchanged, never replacing a file.

        The copy is written to a hidden temporary name, checked against the
        expected SHA-256, then linked into place. Nothing is left behind if
        any step fails.
        """
        target = self._resolve_any(relative)
        if target.exists():
            if _hash_file(target) == expected:
                return "already_applied"
            raise ConflictError(f"같은 이름의 다른 파일이 이미 있습니다: {relative}")
        size = source.stat().st_size
        target.parent.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(target.parent).free < size + _FREE_MARGIN:
            raise RequestError("디스크 여유 공간이 부족해 파일을 수집할 수 없습니다.")
        pending = self._pending_path(relative)
        pending.unlink(missing_ok=True)
        try:
            hashed = hashlib.sha256()
            with source.open("rb") as reader, pending.open("xb") as output:
                while chunk := reader.read(1024 * 1024):
                    hashed.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if hashed.hexdigest() != expected:
                raise ConflictError(
                    f"수집하는 동안 원본 파일이 변경되었습니다: {source}"
                )
            try:
                os.link(pending, target)  # fails instead of overwriting
            except FileExistsError:
                if _hash_file(target) == expected:
                    return "already_applied"
                raise ConflictError(
                    f"같은 이름의 다른 파일이 이미 있습니다: {relative}"
                ) from None
            except OSError:
                if target.exists():
                    raise
                os.rename(pending, target)
            _sync_directory(target.parent)
        finally:
            pending.unlink(missing_ok=True)
        return "applied"

    # cost: time O(f), heap O(1), stack O(1), io O(f)
    # vars: f = proposal targets
    # basis: estimate
    def discard_pending(self, proposal: Proposal) -> None:
        for change in proposal.changes:
            try:
                self._pending_path(change.path).unlink(missing_ok=True)
                if change.after is None:
                    self._settle_deleting(change, self.root / change.path)
            except RequestError:
                logger.warning("candidate parent moved outside its owned location")

    # cost: time O(n), heap O(1), stack O(1), io O(n/b)
    # vars: n = document bytes, b = 64 KiB read block
    # basis: estimate
    def _delete(self, change: Change, target: Path) -> str:
        """Delete only the exact content that was planned for deletion.

        The file is first renamed to a hidden name in the same folder, which
        is atomic, and then hashed again. If someone saved it in between, it
        is put back and the change becomes a conflict. Nothing hidden is left
        behind: every path out of this method removes or restores it.
        """
        deleting = self._deleting_path(change.path)
        try:
            os.rename(target, deleting)
        except FileNotFoundError:
            if self.read_hash(change.path) is None:
                return "already_applied"
            raise ConflictError(f"원문이 변경되었습니다: {change.path}") from None
        try:
            moved = _hash_file(deleting)
        except OSError:
            self._restore(deleting, target)
            raise
        if moved != change.before:
            self._restore(deleting, target)
            raise ConflictError(f"원문이 변경되었습니다: {change.path}")
        deleting.unlink()
        _sync_directory(target.parent)
        return "applied"

    def _settle_deleting(self, change: Change, target: Path) -> None:
        """Finish or undo a delete that stopped between rename and unlink."""
        deleting = self._deleting_path(change.path)
        try:
            moved = _hash_file(deleting)
        except FileNotFoundError:
            return
        if moved == change.before:
            deleting.unlink(missing_ok=True)  # the planned content; finish it
        else:
            self._restore(deleting, target)
        _sync_directory(target.parent)

    def _restore(self, deleting: Path, target: Path) -> None:
        """Put a moved file back; never overwrite a file saved meanwhile.

        If the original name was taken again in the same instant, the moved
        content is kept next to it under a visible name instead.
        """
        candidates = [target] + [
            target.with_name(f"{target.stem}.markchain-recovered-{number}.md")
            for number in range(1, 1000)
        ]
        for destination in candidates:
            if destination.exists():
                continue
            try:
                os.link(deleting, destination)  # fails instead of overwriting
            except FileExistsError:
                continue
            except OSError:
                # No hard links on this filesystem: rename after the check.
                os.rename(deleting, destination)
            else:
                deleting.unlink()
            if destination != target:
                logger.warning("kept a concurrently saved copy as %s", destination.name)
            _sync_directory(target.parent)
            return
        raise ConflictError(
            f"삭제를 되돌릴 파일 이름을 확보하지 못했습니다: {target.name}"
        )

    def _deleting_path(self, relative: str) -> Path:
        self._pending_path(relative)  # same ownership checks
        target = self.root / relative
        return target.with_name(f".{target.name}{_DELETING}")

    # cost: time O(f), heap O(d), stack O(1), io O(f)
    # vars: f = files in the vault, d = directory width
    # basis: estimate
    def release(self) -> int:
        """Remove Markchain's leftovers before this vault stops being managed.

        Unfinished writes are deleted. An unfinished delete is put back,
        because without its job the content cannot be confirmed as planned.
        """
        cleaned = 0
        for directory, folders, files in os.walk(self.root):
            folders[:] = [f for f in folders if not os.path.islink(Path(directory) / f)]
            for name in files:
                path = Path(directory) / name
                if name.startswith(".markchain-") and name.endswith(".pending"):
                    path.unlink(missing_ok=True)
                    cleaned += 1
                elif name.startswith(".") and name.endswith(_DELETING):
                    target = path.with_name(name[1 : -len(_DELETING)])
                    self._restore(path, target)
                    cleaned += 1
        return cleaned

    def _pending_path(self, relative: str) -> Path:
        target = self.root / relative
        parent = target.parent.resolve()
        if parent != target.parent or not parent.is_relative_to(self.root):
            raise RequestError("문서가 있던 폴더가 바뀌어 안전하게 쓸 수 없습니다.")
        identity = digest((self._writer_id + ":" + relative).encode())[:32]
        return target.with_name(f".markchain-{identity}.pending")


def _hash_file(path: Path) -> str:
    hashed = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(65536):
            hashed.update(chunk)
    return hashed.hexdigest()


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
