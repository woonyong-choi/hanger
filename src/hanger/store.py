"""State database: the closet catalog, settings, applied ignore rules and decisions.

One SQLite file per state folder (``hanger.db``). Every process that touches
Hanger state — the daemon, terminal commands, MCP calls — reads and writes
it, so each method opens a short transaction and nothing is cached here.
"""

from __future__ import annotations

__all__ = ["Decision", "StateStore", "Closet"]

import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from hanger.config import Settings, flatten, unflatten
from hanger.errors import RequestError

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS closets (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ignores (
    closet_id INTEGER PRIMARY KEY REFERENCES closets(id) ON DELETE CASCADE,
    applied TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    closet_id INTEGER NOT NULL REFERENCES closets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
"""


@dataclass(frozen=True)
class Closet:
    id: int
    path: Path

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": str(self.path)}


@dataclass(frozen=True)
class Decision:
    id: int
    closet_id: int
    kind: str
    detail: dict[str, Any]
    status: str


class StateStore:
    """Short-lived connections to the shared state database."""

    def __init__(self, state: Path) -> None:
        self.state = state.expanduser().resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state / "hanger.db"
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise RequestError(
                    "The state database was created by a newer Hanger. "
                    "Update Hanger to use it.",
                    code="state_version",
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
            connection.commit()
        finally:
            connection.close()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @staticmethod
    def _bump(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO meta VALUES ('revision', 1) "
            "ON CONFLICT(key) DO UPDATE SET value = value + 1"
        )

    def revision(self) -> int:
        """A counter that changes whenever the catalog, rules or settings change."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM meta WHERE key = 'revision'"
            ).fetchone()
        return int(row[0]) if row else 0

    # ---------- closets ----------

    def closets(self) -> list[Closet]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id, path FROM closets ORDER BY id")
            return [Closet(int(i), Path(p)) for i, p in rows]

    def closet(self, reference: str) -> Closet | None:
        """Find a closet by id or by path (the path may also be inside it)."""
        closets = self.closets()
        if reference.isdigit():
            return next((v for v in closets if v.id == int(reference)), None)
        path = Path(reference).expanduser().resolve()
        return next((v for v in closets if path.is_relative_to(v.path)), None)

    # cost: time O(v), heap O(v), stack O(1), io O(1)
    # vars: v = registered closets
    # basis: estimate
    def add_closet(self, path: Path, ignore_text: str) -> Closet:
        """Register a folder; refuse duplicates and closets inside other closets."""
        root = path.expanduser().resolve()
        if not root.is_dir():
            raise RequestError(f"Not a folder: {root}", code="closet_missing")
        if not os.access(root, os.R_OK | os.W_OK):
            raise RequestError(
                f"Hanger cannot read and write this folder: {root}",
                code="closet_access",
            )
        if self.state.is_relative_to(root):
            raise RequestError(
                f"This folder contains Hanger's own state folder ({self.state}). "
                "Choose a folder that does not.",
                code="closet_nested",
            )
        with self._connect() as connection:
            for (existing,) in connection.execute("SELECT path FROM closets"):
                other = Path(existing)
                if root == other:
                    raise RequestError(
                        f"This folder is already a closet: {root}",
                        code="closet_exists",
                    )
                if root.is_relative_to(other):
                    raise RequestError(
                        f"This folder is inside a registered closet: {other}. "
                        "Its notes are already covered there.",
                        code="closet_nested",
                    )
                if other.is_relative_to(root):
                    raise RequestError(
                        f"This folder contains a registered closet: {other}. "
                        "Remove that closet first if you want to register the "
                        "larger folder: hanger closet remove "
                        f"{json.dumps(str(other))}",
                        code="closet_nested",
                    )
            cursor = connection.execute(
                "INSERT INTO closets (path, added_at) VALUES (?, ?)",
                (str(root), time.time()),
            )
            closet_id = int(cursor.lastrowid or 0)
            connection.execute(
                "INSERT INTO ignores (closet_id, applied) VALUES (?, ?)",
                (closet_id, ignore_text),
            )
            self._bump(connection)
        return Closet(closet_id, root)

    def remove_closet(self, closet_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM closets WHERE id = ?", (closet_id,))
            self._bump(connection)

    # ---------- ignore rules ----------

    def applied_ignore(self, closet_id: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT applied FROM ignores WHERE closet_id = ?", (closet_id,)
            ).fetchone()
        return str(row[0]) if row else ""

    def set_applied_ignore(self, closet_id: int, text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ignores VALUES (?, ?) "
                "ON CONFLICT(closet_id) DO UPDATE SET applied = excluded.applied",
                (closet_id, text),
            )
            self._bump(connection)

    # ---------- decisions ----------

    def decisions(self, *, pending_only: bool = True) -> list[Decision]:
        query = "SELECT id, closet_id, kind, detail, status FROM decisions"
        if pending_only:
            query += " WHERE status = 'pending'"
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY id").fetchall()
        return [Decision(int(i), int(v), k, json.loads(d), s) for i, v, k, d, s in rows]

    def decision(self, decision_id: int) -> Decision | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, closet_id, kind, detail, status FROM decisions "
                "WHERE id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return Decision(int(row[0]), int(row[1]), row[2], json.loads(row[3]), row[4])

    def open_decision(self, closet_id: int, kind: str, detail: dict[str, Any]) -> int:
        """Record a question for the user, one pending question per closet.

        The same question keeps its number. A different one withdraws the
        pending question and gets a new number, so an answer always refers to
        the question the user actually saw.
        """
        encoded = json.dumps(detail, sort_keys=True)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, kind, detail FROM decisions "
                "WHERE closet_id = ? AND status = 'pending'",
                (closet_id,),
            ).fetchone()
            if row is not None:
                if row[1] == kind and row[2] == encoded:
                    return int(row[0])
                connection.execute(
                    "UPDATE decisions SET status = 'withdrawn', resolved_at = ? "
                    "WHERE id = ?",
                    (time.time(), row[0]),
                )
            cursor = connection.execute(
                "INSERT INTO decisions (closet_id, kind, detail, created_at) "
                "VALUES (?, ?, ?, ?)",
                (closet_id, kind, encoded, time.time()),
            )
            self._bump(connection)
        return int(cursor.lastrowid or 0)

    def close_decision(self, decision_id: int, status: str) -> None:
        """Mark a decision approved, declined or withdrawn (no longer needed)."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE decisions SET status = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (status, time.time(), decision_id),
            )
            self._bump(connection)

    # ---------- settings ----------

    def settings(self) -> Settings:
        return Settings.model_validate(unflatten(self._values()))

    def setting_values(self) -> dict[str, Any]:
        """Every setting key with its current value, defaults included."""
        return flatten(self.settings().model_dump(mode="json"))

    def changed_settings(self) -> dict[str, Any]:
        return self._values()

    def _values(self) -> dict[str, Any]:
        with self._connect() as connection:
            rows = connection.execute("SELECT key, value FROM settings").fetchall()
        return {key: json.loads(value) for key, value in rows}

    # cost: time O(s), heap O(s), stack O(1), io O(1)
    # vars: s = settings count
    # basis: estimate
    def set_setting(self, key: str, value: Any) -> Any:
        """Validate and store one setting; returns the stored value."""
        known = flatten(Settings().model_dump(mode="json"))
        if key not in known:
            raise RequestError(
                f"Unknown setting: {key}. See: hanger config list",
                code="config",
            )
        values = {**self._values(), key: value}
        try:
            stored = flatten(
                Settings.model_validate(unflatten(values)).model_dump(mode="json")
            )[key]
        except ValidationError as error:
            reason = error.errors(include_url=False)[0]["msg"]
            raise RequestError(
                f"Invalid value for {key}: {reason}", code="config"
            ) from error
        with self._connect() as connection:
            if stored == known[key]:
                connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            else:
                connection.execute(
                    "INSERT INTO settings VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(stored)),
                )
            self._bump(connection)
        return stored

    def unset_setting(self, key: str) -> None:
        """Return one setting to its default."""
        with self._connect() as connection:
            connection.execute("DELETE FROM settings WHERE key = ?", (key,))
            self._bump(connection)
