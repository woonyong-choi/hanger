from __future__ import annotations

__all__ = ["Synchronizer"]

import os
import threading
import time
from collections import deque
from collections.abc import Iterator
from itertools import batched
from pathlib import Path
from typing import Any

from elastic_transport import TransportError
from elasticsearch import ApiError

from markchain.documents import Documents
from markchain.errors import ConflictError, RequestError
from markchain.requests import digest
from markchain.search import SearchIndex

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
_EXCLUDED = {"tooling", "agent", "node_modules", "AGENTS.md", "CLAUDE.md"}


class Synchronizer:
    def __init__(self, documents: Documents, index: SearchIndex) -> None:
        self.documents = documents
        self.index = index
        self.read_count = 0
        self.read_bytes = 0
        self.diagnostics: deque[dict[str, str]] = deque(maxlen=100)
        self._writer = threading.RLock()

    # cost: time O(f+n), heap O(b), stack O(d), io O(f+n/b)
    # vars: f = file count, n = changed bytes, b = batch bytes, d = directory depth
    # basis: estimate
    def reconcile(self) -> dict[str, Any]:
        with self._writer:
            self.index.ensure()
            self.diagnostics.clear()
            reads = self.read_count
            checked = 0
            for paths in batched(self.paths(), 256):
                self.sync_paths(list(paths))
                checked += len(paths)
            for entry in self.index.entries():
                path = entry["path"]
                try:
                    exists = self.documents.resolve(path).exists()
                except RequestError:
                    exists = False
                if not _included(path) or not exists:
                    self.index.write([{"path": path}])
            self.index.refresh()
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
            selected = [p for p in paths if _included(p)]
            previous = self.index.metadata(selected)
            self.index.write(self._updates(selected, previous, force=force))

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

    # cost: time O(n), heap O(k), stack O(d), io O(f)
    # vars: n = selected bytes or fallback corpus bytes, k = result bytes
    # vars: f = files read, d = directory depth
    # basis: estimate
    def search(
        self, query: str, limit: int = 20, *, fallback_offset: int = 0
    ) -> dict[str, Any]:
        if not query.strip() or not 1 <= limit <= 100 or fallback_offset < 0:
            raise RequestError("query and a limit between 1 and 100 are required")
        reason = "no_hits"
        with self._writer:
            try:
                hits = self.index.find(query, limit)
                results, stale = self._read_hits(hits)
                if stale:
                    self.sync_paths(stale, force=True)
                    self.index.refresh()
                    results, stale = self._read_hits(self.index.find(query, limit))
                if results and not stale and fallback_offset == 0:
                    return {
                        "source": "elasticsearch",
                        "documents": results,
                        "candidate_limit": limit,
                        "coverage": "ranked_candidates",
                    }
                reason = "stale_results" if stale else "no_hits"
            except (TransportError, ApiError):
                reason = "search_unavailable"
            fallback = self._local_search(query, limit, fallback_offset)
            results = fallback["documents"]
            if results and reason != "search_unavailable":
                self.sync_paths([r["path"] for r in results], force=True)
                self.index.refresh()
            return {
                "source": "local_fallback",
                "reason": reason,
                **fallback,
                "diagnostics": list(self.diagnostics),
            }

    # cost: time O(f), heap O(w), stack O(d), io O(f)
    # vars: f = filesystem entries, w = directory width, d = directory depth
    # basis: estimate
    def paths(self) -> Iterator[str]:
        for parent, directories, files in os.walk(
            self.documents.root, followlinks=False
        ):
            directories[:] = sorted(
                [
                    d
                    for d in directories
                    if not d.startswith(".")
                    and d not in _EXCLUDED
                    and not (Path(parent) / d).is_symlink()
                ]
            )
            for name in sorted(files):
                path = (Path(parent) / name).relative_to(self.documents.root).as_posix()
                if _included(path) and not (Path(parent) / name).is_symlink():
                    yield path

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = document bytes, bounded by MAX_DOCUMENT_BYTES
    # basis: estimate
    def read(self, path: str) -> dict[str, str]:
        target = self.documents.resolve(path)
        before = _signature(target)
        if target.stat().st_size > MAX_DOCUMENT_BYTES:
            raise RequestError(f"document exceeds size limit: {path}")
        with target.open("rb") as source:
            data = source.read(MAX_DOCUMENT_BYTES + 1)
        self.read_count += 1
        self.read_bytes += len(data)
        if len(data) > MAX_DOCUMENT_BYTES or _signature(target) != before:
            raise ConflictError(f"document changed while reading: {path}")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RequestError(f"document is not utf-8: {path}") from error
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

    # cost: time O(n), heap O(n), stack O(1), io O(k)
    # vars: n = result content bytes, k = result count
    # basis: estimate
    def _read_hits(
        self, hits: list[dict[str, Any]]
    ) -> tuple[list[dict[str, str]], list[str]]:
        results, stale = [], []
        for hit in hits:
            try:
                current = self.read(hit["path"])
                if current["hash"] == hit["hash"]:
                    results.append(current)
                else:
                    stale.append(hit["path"])
            except (OSError, RequestError, ConflictError):
                stale.append(hit["path"])
        return results, stale

    # cost: time O(n), heap O(k), stack O(d), io O(f)
    # vars: n = corpus bytes, k = bounded result bytes, f = files, d = depth
    # basis: estimate
    def _local_search(self, query: str, limit: int, offset: int) -> dict[str, Any]:
        terms = query.casefold().split()
        results: list[dict[str, str]] = []
        deadline, initial_bytes = time.monotonic() + 5, self.read_bytes
        for number, path in enumerate(self.paths()):
            if number < offset:
                continue
            if (
                time.monotonic() >= deadline
                or self.read_bytes - initial_bytes >= 8 * 1024 * 1024
            ):
                return {"documents": results, "complete": False, "next_offset": number}
            try:
                document = self.read(path)
            except (OSError, RequestError, ConflictError) as error:
                self.diagnostics.append({"path": path, "reason": str(error)})
                continue
            if any(term in document["content"].casefold() for term in terms):
                results.append(document)
                if len(results) == limit:
                    return {
                        "documents": results,
                        "complete": False,
                        "next_offset": number + 1,
                    }
        return {
            "documents": results,
            "complete": not self.diagnostics,
            "next_offset": None,
        }


def _included(path: str) -> bool:
    parts = Path(path).parts
    return path.lower().endswith(".md") and not any(
        p.startswith(".") or p in _EXCLUDED for p in parts
    )


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
