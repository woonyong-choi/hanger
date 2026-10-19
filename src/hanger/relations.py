from __future__ import annotations

__all__ = ["RelationProjection"]

import json
import sqlite3
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hanger.errors import HangerError, RequestError
from hanger.scope import SearchScope
from hanger.semantics import (
    DocumentSemanticSnapshot,
    RelationAssertion,
    SemanticError,
    parse_semantics,
    verify_evidence,
)

_MAX_HOPS = 2
_MAX_DOCUMENTS = 64
_MAX_BYTES = 512 * 1024
_MAX_ASSERTIONS = 128
_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents(
    path TEXT PRIMARY KEY, hash TEXT NOT NULL, identity TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS document_identity ON documents(identity);
CREATE TABLE IF NOT EXISTS entities(
    identity TEXT NOT NULL, owner TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY(identity, owner));
CREATE INDEX IF NOT EXISTS entity_owner ON entities(owner);
CREATE TABLE IF NOT EXISTS assertions(
    owner TEXT NOT NULL, ordinal INTEGER NOT NULL,
    subject TEXT NOT NULL, object TEXT NOT NULL,
    PRIMARY KEY(owner, ordinal));
CREATE INDEX IF NOT EXISTS assertion_subject ON assertions(subject);
CREATE INDEX IF NOT EXISTS assertion_object ON assertions(object);
"""


@dataclass
class _Traversal:
    read: Callable[[str, int], dict[str, str]]
    allowed: Callable[[str], bool]
    scope: SearchScope
    snapshots: dict[str, DocumentSemanticSnapshot | None] = field(default_factory=dict)
    omissions: set[str] = field(default_factory=set)
    used_bytes: int = 0
    checked_assertions: int = 0
    output_bytes: int = 0
    documents: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add_documents(
        self, paths: Iterable[str], seed: str, trail: list[dict[str, Any]]
    ) -> None:
        evidence = [item for edge in trail for item in edge["evidence"]]
        for path in dict.fromkeys(paths):
            snapshot = self.snapshots.get(path)
            if path in self.documents or snapshot is None:
                continue
            document = {
                "path": path,
                "content": snapshot.text,
                "hash": snapshot.sha256,
                "matched_by": ["relation"],
                "seed": seed,
                "relation_path": trail,
                "evidence": evidence,
            }
            size = len(json.dumps(document, ensure_ascii=False).encode())
            if self.output_bytes + size > _MAX_BYTES:
                self.omissions.add("response_byte_budget")
                continue
            self.documents[path] = document
            self.output_bytes += size

    def load(self, path: str) -> DocumentSemanticSnapshot | None:
        if path in self.snapshots:
            return self.snapshots[path]
        if not self.scope.contains(path) or not self.allowed(path):
            self.omissions.add("outside_scope_or_ignored")
            return None
        if len(self.snapshots) >= _MAX_DOCUMENTS:
            self.omissions.add("document_budget")
            return None
        self.snapshots[path] = None
        try:
            document = self.read(path, _MAX_BYTES - self.used_bytes)
            size = len(document["content"].encode())
            if self.used_bytes + size > _MAX_BYTES:
                self.omissions.add("byte_budget")
                return None
            self.used_bytes += size
            snapshot = parse_semantics(document["content"], path=path)
        except RequestError as error:
            self.omissions.add(
                "byte_budget"
                if error.code == "read_budget"
                else "missing_changed_or_invalid_document"
            )
            return None
        except (OSError, HangerError):
            self.omissions.add("missing_changed_or_invalid_document")
            return None
        self.snapshots[path] = snapshot
        return snapshot


class RelationProjection:
    """Rebuildable closet-local identities and assertions, never canonical text."""

    def __init__(self, database: Path) -> None:
        self._lock = threading.RLock()
        self._db = sqlite3.connect(database, check_same_thread=False, timeout=30)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def observe(self, snapshot: DocumentSemanticSnapshot) -> None:
        with self._lock, self._db:
            self._remove(snapshot.path)
            self._db.execute(
                "INSERT INTO documents VALUES(?,?,?)",
                (snapshot.path, snapshot.sha256, snapshot.document_id),
            )
            self._db.executemany(
                "INSERT INTO entities VALUES(?,?,?)",
                [
                    (
                        entity.id,
                        snapshot.path,
                        entity.model_dump_json(exclude_unset=True),
                    )
                    for entity in snapshot.metadata.entities
                ],
            )
            self._db.executemany(
                "INSERT INTO assertions VALUES(?,?,?,?)",
                [
                    (
                        snapshot.path,
                        ordinal,
                        assertion.subject,
                        assertion.object,
                    )
                    for ordinal, assertion in enumerate(snapshot.metadata.assertions)
                ],
            )

    def forget(self, path: str) -> None:
        with self._lock, self._db:
            self._remove(path)

    def missing(self, paths: Iterable[str]) -> set[str]:
        with self._lock:
            known = {row[0] for row in self._db.execute("SELECT path FROM documents")}
        present = set(paths)
        for path in known - present:
            self.forget(path)
        return present - known

    def expand(
        self,
        seeds: list[str],
        *,
        read: Callable[[str, int], dict[str, str]],
        allowed: Callable[[str], bool],
        scope: SearchScope,
    ) -> dict[str, Any]:
        state = _Traversal(read, allowed, scope)
        with self._lock:
            documents = self._expand(seeds, state)
        return {
            "documents": documents,
            "coverage": "bounded_verified_relations",
            "limits": {
                "hops": _MAX_HOPS,
                "documents": _MAX_DOCUMENTS,
                "bytes": _MAX_BYTES,
                "assertions": _MAX_ASSERTIONS,
            },
            "checked_documents": len(state.snapshots),
            "read_bytes": state.used_bytes,
            "output_bytes": state.output_bytes,
            "omitted": sorted(state.omissions),
            "unverified_scope": [
                "unindexed_documents",
                "relations_beyond_limits",
                "semantic_support_of_quotes",
            ],
        }

    def _remove(self, path: str) -> None:
        for table, column in (
            ("documents", "path"),
            ("entities", "owner"),
            ("assertions", "owner"),
        ):
            self._db.execute(f"DELETE FROM {table} WHERE {column}=?", (path,))

    def _current(self, path: str, state: _Traversal) -> DocumentSemanticSnapshot | None:
        snapshot = state.load(path)
        if snapshot is None:
            return None
        row = self._db.execute(
            "SELECT hash FROM documents WHERE path=?", (path,)
        ).fetchone()
        if row is None or row[0] != snapshot.sha256:
            state.omissions.add("projection_stale_or_missing")
            return None
        return snapshot

    def _identity_paths(self, identity: str, state: _Traversal) -> list[str]:
        documents = self._db.execute(
            "SELECT path FROM documents WHERE identity=? LIMIT ?",
            (identity, _MAX_DOCUMENTS + 1),
        ).fetchall()
        rows = self._db.execute(
            "SELECT owner, payload FROM entities WHERE identity=? LIMIT ?",
            (identity, _MAX_DOCUMENTS + 1),
        ).fetchall()
        if len(rows) > _MAX_DOCUMENTS:
            state.omissions.add("document_budget")
            return []
        if len(documents) > 1:
            state.omissions.add("identity_conflict")
            return []
        values: dict[str, Any] = {"kind": "document"} if documents else {}
        for _, payload in rows:
            for key, value in json.loads(payload).items():
                if key in values and values[key] != value:
                    state.omissions.add("identity_conflict")
                    return []
                values[key] = value
        paths = sorted({row[0] for row in [*documents, *rows]})
        if documents:
            paths = [documents[0][0]]
        elif values.get("kind") == "document":
            state.omissions.add("unresolved_document")
            return []
        current = []
        for path in paths:
            if self._current(path, state) is not None:
                current.append(path)
        return current

    def _verified_edge(
        self, owner: str, ordinal: int, state: _Traversal
    ) -> tuple[RelationAssertion, list[DocumentSemanticSnapshot]] | None:
        snapshot = self._current(owner, state)
        if snapshot is None or ordinal >= len(snapshot.metadata.assertions):
            return None
        assertion = snapshot.metadata.assertions[ordinal]
        if assertion.status != "accepted" or assertion.origin != "explicit":
            state.omissions.add("proposed_rejected_or_inferred")
            return None
        sources: dict[str, DocumentSemanticSnapshot] = {}
        for evidence in assertion.evidence:
            paths = self._identity_paths(evidence.source.id, state)
            if len(paths) != 1:
                state.omissions.add("unverified_evidence")
                return None
            source = self._current(paths[0], state)
            if source is None:
                return None
            sources[source.document_id] = source
        isolated = snapshot.model_copy(
            update={
                "metadata": snapshot.metadata.model_copy(
                    update={"assertions": [assertion]}
                )
            }
        )
        try:
            checks = verify_evidence(
                isolated,
                sources=[s for s in sources.values() if s.path != owner],
            )
        except SemanticError:
            state.omissions.add("unverified_evidence")
            return None
        if not checks or any(check.status != "verified" for check in checks):
            state.omissions.add("unverified_evidence")
            return None
        return assertion, list(sources.values())

    def _describes(self, path: str, identity: str, state: _Traversal) -> bool:
        snapshot = state.snapshots.get(path)
        if snapshot is None:
            return False
        if snapshot.document_id == identity:
            return True
        return any(
            entity.id == identity
            and (
                entity.label
                or entity.aliases
                or entity.definition
                or entity.central_question
            )
            for entity in snapshot.metadata.entities
        )

    def _expand(self, seeds: list[str], state: _Traversal) -> list[dict[str, Any]]:
        queue: deque[tuple[str, list[dict[str, Any]], str]] = deque()
        visited: set[str] = set()
        for path in seeds:
            snapshot = self._current(path, state)
            if snapshot is None:
                continue
            for identity in [
                snapshot.document_id,
                *(e.id for e in snapshot.metadata.entities),
            ]:
                if identity not in visited and self._identity_paths(identity, state):
                    visited.add(identity)
                    queue.append((identity, [], path))
        checked: set[tuple[str, int]] = set()
        while queue and state.checked_assertions < _MAX_ASSERTIONS:
            identity, trail, seed = queue.popleft()
            if len(trail) >= _MAX_HOPS:
                continue
            rows = self._db.execute(
                "SELECT owner, ordinal FROM assertions WHERE subject=? OR object=? "
                "ORDER BY owner, ordinal LIMIT ?",
                (identity, identity, _MAX_ASSERTIONS + 1),
            ).fetchall()
            for owner, ordinal in rows:
                if (owner, ordinal) in checked:
                    continue
                if state.checked_assertions >= _MAX_ASSERTIONS:
                    state.omissions.add("assertion_budget")
                    break
                checked.add((owner, ordinal))
                state.checked_assertions += 1
                verified = self._verified_edge(owner, ordinal, state)
                if verified is None:
                    continue
                assertion, sources = verified
                other = (
                    assertion.object
                    if assertion.subject == identity
                    else assertion.subject
                )
                target_paths = self._identity_paths(other, state)
                if not target_paths:
                    state.omissions.add("unresolved_endpoint")
                    continue
                owner_snapshot = state.snapshots.get(owner)
                if owner_snapshot is None:
                    continue
                edge = {
                    "owner": owner,
                    "owner_sha256": owner_snapshot.sha256,
                    **assertion.model_dump(mode="json", exclude_none=True),
                    "direction": "forward"
                    if assertion.subject == identity
                    else "reverse",
                }
                next_trail = [*trail, edge]
                source_ids = {source.document_id: source for source in sources}
                edge["evidence"] = [
                    {
                        "source": {
                            "id": item.source.id,
                            "path": source_ids[item.source.id].path,
                            "sha256": source_ids[item.source.id].sha256,
                        },
                        "quote": item.quote,
                        "locator": item.locator,
                    }
                    for item in assertion.evidence
                ]
                state.add_documents(
                    (
                        p
                        for p in [
                            owner,
                            *(
                                p
                                for p in target_paths
                                if self._describes(p, other, state)
                            ),
                            *(s.path for s in sources),
                        ]
                        if p not in seeds
                    ),
                    seed,
                    next_trail,
                )
                if other not in visited:
                    visited.add(other)
                    queue.append((other, next_trail, seed))
        if queue:
            state.omissions.add("assertion_budget")
        return list(state.documents.values())
