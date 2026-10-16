from __future__ import annotations

__all__ = ["Synchronizer"]

import os
import threading
from collections import deque
from collections.abc import Callable, Iterator
from itertools import batched
from pathlib import Path
from typing import Any, Literal

from elastic_transport import TransportError
from elasticsearch import ApiError, BadRequestError
from elasticsearch.helpers import BulkIndexError

from hanger.config import Settings
from hanger.context import snippets
from hanger.documents import Documents
from hanger.errors import ConflictError, RequestError, SearchUnavailableError
from hanger.requests import digest
from hanger.scope import SearchScope
from hanger.search import SearchIndex

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024


class Synchronizer:
    def __init__(
        self,
        documents: Documents,
        index: SearchIndex,
        settings: Settings | None = None,
    ) -> None:
        self.documents = documents
        self.settings = settings or Settings()
        self.index = index
        self.read_count = 0
        self.read_bytes = 0
        self.diagnostics: deque[dict[str, str]] = deque(maxlen=100)
        self._writer = threading.RLock()
        self._retry_paths: set[str] = set()
        # Until one full reconcile finishes, the index may miss files.
        self.reconciled = False

    # cost: time O(f+n), heap O(b), stack O(d), io O(f+n/b)
    # vars: f = file count, n = changed bytes, b = batch bytes, d = directory depth
    # basis: estimate
    def reconcile(
        self, is_cancelled: Callable[[], bool] = lambda: False
    ) -> dict[str, Any]:
        with self._writer:
            self.index.ensure()
            self.diagnostics.clear()
            reads = self.read_count
        checked = 0
        for paths in batched(self.paths(), 256):
            if is_cancelled():
                raise InterruptedError("reconciliation interrupted")
            self.sync_paths(list(paths))
            checked += len(paths)
        for entries in batched(self.index.entries(), 256):
            if is_cancelled():
                raise InterruptedError("reconciliation interrupted")
            with self._writer:
                removed = []
                for entry in entries:
                    path = entry["path"]
                    try:
                        exists = self.documents.resolve(path).is_file()
                    except RequestError:
                        exists = False
                    if not self._included(path) or not exists:
                        removed.append({"path": path})
                self.index.write(removed)
        with self._writer:
            self.index.refresh()
            self.reconciled = True
            return {
                "checked": checked,
                "read": self.read_count - reads,
                "diagnostics": list(self.diagnostics),
            }

    # cost: time O(f+n), heap O(b), stack O(1), io O(f)
    # vars: f = paths, n = changed document bytes, b = bulk bytes
    # basis: estimate
    def sync_paths(self, paths: list[str], *, force: bool = False) -> None:
        with self._writer:
            selected = [p for p in paths if self._included(p)]
            previous = self.index.metadata(selected)
            self.index.write(self._updates(selected, previous, force=force))

    def _included(self, path: str) -> bool:
        return path.lower().endswith(".md") and not self.settings.excluded(path)

    def terms(self, query: str) -> list[str]:
        """Return query terms as the index analyzes them, for matched lines."""
        try:
            return self.index.analyze(query)
        except (TransportError, ApiError):
            return sorted(set(query.casefold().split()), key=len, reverse=True)

    def drain_retries(self) -> list[str]:
        with self._writer:
            paths = list(self._retry_paths)
            self._retry_paths.clear()
            return paths

    # cost: time O(n), heap O(m), stack O(1), io O(k)
    # vars: n = audited bytes, m = largest file bytes, k = audit count
    # basis: estimate
    def audit(self, limit: int = 64) -> dict[str, int]:
        if not 1 <= limit <= 1024:
            raise RequestError("audit limit must be between 1 and 1024")
        with self._writer:
            entries = self.index.audit_entries(limit)
            budget = 8 * 1024 * 1024
            selected: list[dict[str, Any]] = []
            for entry in entries:
                size = int(entry["signature"].split(":")[2])
                if selected and size > budget:
                    break
                selected.append(entry)
                budget -= size
            reads = self.read_count
            self.sync_paths([entry["path"] for entry in selected], force=True)
            self.index.refresh()
            self.index.save_audit_cursor(selected[-1]["path"] if selected else "")
            return {"checked": len(selected), "read": self.read_count - reads}

    # cost: time O(k+f), heap O(k), stack O(d), io O(k+f)
    # vars: k = result bytes, f = scoped files checked when evidence is missing
    # vars: d = directory depth
    # basis: estimate
    def search(
        self,
        query: str,
        limit: int = 20,
        *,
        fallback_reason: Literal["insufficient_evidence", "user_reported_missing"]
        | None = None,
        folders: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if not query.strip() or not 1 <= limit <= 100:
            raise RequestError("검색어가 필요하며 limit은 1에서 100 사이여야 합니다.")
        scope = SearchScope(folders)
        self.settings.check_folders(scope.folders)
        with self._writer:
            try:
                if fallback_reason is not None or not self.reconciled:
                    # Missing evidence or a fresh index: sync the scope first.
                    self.index.ensure()
                    for batch in batched(self.paths(scope.folders), 256):
                        self.sync_paths(list(batch))
                    self.index.refresh()
                results, stale = self._read_hits(
                    self.index.find(query, limit, folders=scope.folders), scope
                )
                if stale:
                    self.sync_paths(stale, force=True)
                    self.index.refresh()
                    results, stale = self._read_hits(
                        self.index.find(query, limit, folders=scope.folders), scope
                    )
            except BadRequestError as error:
                raise RequestError(
                    "검색 서버가 요청을 거부했습니다. 질의나 폴더 범위를 "
                    "좁혀 다시 검색하십시오."
                ) from error
            except (TransportError, ApiError, BulkIndexError) as error:
                raise SearchUnavailableError(
                    "검색 서버가 응답하지 않습니다."
                ) from error
            return {
                "source": "elasticsearch",
                "documents": results,
                "candidate_limit": limit,
                "coverage": "ranked_candidates",
                "reason": fallback_reason or "ranked",
                "unverified": stale,
                "folders": list(scope.folders),
                "excluded_names": list(self.settings.closet.exclude),
            }

    # cost: time O(f), heap O(w), stack O(d), io O(f)
    # vars: f = filesystem entries, w = directory width, d = directory depth
    # basis: estimate
    def paths(self, folders: tuple[str, ...] = ()) -> Iterator[str]:
        scope = SearchScope(folders)
        for parent, directories, files in os.walk(
            self.documents.root, followlinks=False
        ):
            base = Path(parent).relative_to(self.documents.root)
            directories[:] = sorted(
                d
                for d in directories
                if not self.settings.excluded((base / d).as_posix())
                and not (Path(parent) / d).is_symlink()
                and scope.can_descend((base / d).as_posix())
            )
            for name in sorted(files):
                path = (Path(parent) / name).relative_to(self.documents.root).as_posix()
                if (
                    self._included(path)
                    and scope.contains(path)
                    and not (Path(parent) / name).is_symlink()
                ):
                    yield path

    # cost: time O(f), heap O(1), stack O(1), io O(f)
    # vars: f = dependency files
    # basis: estimate
    def matches(self, signatures: dict[str, str]) -> bool:
        for path, signature in signatures.items():
            try:
                if _signature(self.documents.resolve(path)) != signature:
                    return False
            except (OSError, RequestError):
                return False
        return True

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = document bytes, bounded by MAX_DOCUMENT_BYTES
    # basis: estimate
    def read(self, path: str) -> dict[str, str]:
        target = self.documents.resolve(path)
        before = _signature(target)
        if target.stat().st_size > MAX_DOCUMENT_BYTES:
            raise RequestError(f"문서가 크기 한도(2MiB)를 넘습니다: {path}")
        with target.open("rb") as source:
            data = source.read(MAX_DOCUMENT_BYTES + 1)
        self.read_count += 1
        self.read_bytes += len(data)
        if len(data) > MAX_DOCUMENT_BYTES or _signature(target) != before:
            raise ConflictError(f"읽는 도중 문서가 변경되었습니다: {path}")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RequestError(f"UTF-8 문서가 아닙니다: {path}") from error
        title = next(
            (
                line.lstrip("# ")
                for line in content.splitlines()
                if line.startswith("# ")
            ),
            target.stem,
        )
        return {
            "kind": "document",
            "path": path,
            "hash": digest(data),
            "signature": before,
            "title": title,
            "content": content,
        }

    # cost: time O(f+n), heap O(m), stack O(1), io O(f)
    # vars: f = files, n = changed bytes, m = largest changed file
    # basis: estimate
    def _updates(
        self, paths: list[str], previous: dict[str, dict[str, Any]], *, force: bool
    ) -> Iterator[dict[str, Any]]:
        for path in paths:
            self._retry_paths.discard(path)
            try:
                target = self.documents.resolve(path)
            except RequestError:
                yield {"path": path}
                continue
            try:
                signature = _signature(target)
                if not force and previous.get(path, {}).get("signature") == signature:
                    continue
                yield self.read(path)
            except FileNotFoundError:
                yield {"path": path}
            except RequestError as error:
                self.diagnostics.append({"path": path, "reason": str(error)})
                yield {"path": path}
            except (ConflictError, OSError) as error:
                self.diagnostics.append({"path": path, "retry": str(error)})
                if len(self._retry_paths) < 4096:
                    self._retry_paths.add(path)

    # cost: time O(n), heap O(n), stack O(1), io O(k)
    # vars: n = result content bytes, k = result count
    # basis: estimate
    def _read_hits(
        self, hits: list[dict[str, Any]], scope: SearchScope
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Check each hit against the file on disk without reading it.

        A hit is current when the file's identity, size and timestamps match
        what was indexed. Bodies are read later, only for the documents that
        are returned in full, and always from disk.
        """
        results, stale = [], []
        for hit in hits:
            if not scope.contains(hit["path"]):
                raise RequestError("검색 결과에 요청 범위 밖 문서가 포함되었습니다.")
            try:
                target = self.documents.resolve(hit["path"])
                signature = _signature(target)
            except (OSError, RequestError):
                stale.append(hit["path"])
                continue
            if signature != hit.get("signature"):
                stale.append(hit["path"])
                continue
            results.append(
                {
                    "path": hit["path"],
                    "title": hit.get("title") or target.stem,
                    "bytes": target.stat().st_size,
                }
            )
        return results, stale

    # cost: time O(n), heap O(w), stack O(1), io O(n)
    # vars: n = bytes read until enough matched lines, w = longest line
    # basis: estimate
    def peek(self, path: str, terms: list[str]) -> list[str]:
        """Return a few matched lines, reading only as far as needed."""
        target = self.documents.resolve(path)
        with target.open("r", encoding="utf-8", errors="replace") as source:
            return snippets(source, terms)


# cost: time O(1), heap O(1), stack O(1), io 1
# basis: estimate
def _signature(path: Path) -> str:
    stat = path.stat()
    return ":".join(
        str(value)
        for value in (
            stat.st_dev,
            stat.st_ino,
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_ctime_ns,
        )
    )
