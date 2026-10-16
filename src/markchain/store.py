"""State database: the vault catalog, settings, applied ignore rules and decisions.

One SQLite file per state folder (``markchain.db``). Every process that touches
Markchain state — the daemon, terminal commands, MCP calls — reads and writes
it, so each method opens a short transaction and nothing is cached here.
"""

from __future__ import annotations

__all__ = ["Decision", "StateStore", "Vault"]

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

from markchain.config import Settings, flatten, unflatten
from markchain.errors import RequestError

_SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS vaults (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ignores (
    vault_id INTEGER PRIMARY KEY REFERENCES vaults(id) ON DELETE CASCADE,
    applied TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY,
    vault_id INTEGER NOT NULL REFERENCES vaults(id) ON DELETE CASCADE,
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
class Vault:
    id: int
    path: Path

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "path": str(self.path)}


@dataclass(frozen=True)
class Decision:
    id: int
    vault_id: int
    kind: str
    detail: dict[str, Any]
    status: str


class StateStore:
    """Short-lived connections to the shared state database."""

    def __init__(self, state: Path) -> None:
        self.state = state.expanduser().resolve()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.state / "markchain.db"
        with self._connect() as connection:
            connection.executescript(_SCHEMA)
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > _SCHEMA_VERSION:
                raise RequestError(
                    "The state database was created by a newer Markchain. "
                    "Update Markchain to use it.",
                    code="state_version",
                )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
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

    # ---------- vaults ----------

    def vaults(self) -> list[Vault]:
        with self._connect() as connection:
            rows = connection.execute("SELECT id, path FROM vaults ORDER BY id")
            return [Vault(int(i), Path(p)) for i, p in rows]

    def vault(self, reference: str) -> Vault | None:
        """Find a vault by id or by path (the path may also be inside it)."""
        vaults = self.vaults()
        if reference.isdigit():
            return next((v for v in vaults if v.id == int(reference)), None)
        path = Path(reference).expanduser().resolve()
        return next((v for v in vaults if path.is_relative_to(v.path)), None)

    # cost: time O(v), heap O(v), stack O(1), io O(1)
    # vars: v = registered vaults
    # basis: estimate
    def add_vault(self, path: Path, ignore_text: str) -> Vault:
        """Register a folder; refuse duplicates and vaults inside other vaults."""
        root = path.expanduser().resolve()
        if not root.is_dir():
            raise RequestError(f"Not a folder: {root}", code="vault_missing")
        if not os.access(root, os.R_OK | os.W_OK):
            raise RequestError(
                f"Markchain cannot read and write this folder: {root}",
                code="vault_access",
            )
        if self.state.is_relative_to(root):
            raise RequestError(
                f"This folder contains Markchain's own state folder ({self.state}). "
                "Choose a folder that does not.",
                code="vault_nested",
            )
        with self._connect() as connection:
            for (existing,) in connection.execute("SELECT path FROM vaults"):
                other = Path(existing)
                if root == other:
                    raise RequestError(
                        f"This folder is already a vault: {root}",
                        code="vault_exists",
                    )
                if root.is_relative_to(other):
                    raise RequestError(
                        f"This folder is inside a registered vault: {other}. "
                        "Its notes are already covered there.",
                        code="vault_nested",
                    )
                if other.is_relative_to(root):
                    raise RequestError(
                        f"This folder contains a registered vault: {other}. "
                        "Remove that vault first if you want to register the "
                        "larger folder: markchain vault remove "
                        f"{json.dumps(str(other))}",
                        code="vault_nested",
                    )
            cursor = connection.execute(
                "INSERT INTO vaults (path, added_at) VALUES (?, ?)",
                (str(root), time.time()),
            )
            vault_id = int(cursor.lastrowid or 0)
            connection.execute(
                "INSERT INTO ignores (vault_id, applied) VALUES (?, ?)",
                (vault_id, ignore_text),
            )
            self._bump(connection)
        return Vault(vault_id, root)

    def remove_vault(self, vault_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM vaults WHERE id = ?", (vault_id,))
            self._bump(connection)

    # ---------- ignore rules ----------

    def applied_ignore(self, vault_id: int) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT applied FROM ignores WHERE vault_id = ?", (vault_id,)
            ).fetchone()
        return str(row[0]) if row else ""

    def set_applied_ignore(self, vault_id: int, text: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO ignores VALUES (?, ?) "
                "ON CONFLICT(vault_id) DO UPDATE SET applied = excluded.applied",
                (vault_id, text),
            )
            self._bump(connection)

    # ---------- decisions ----------

    def decisions(self, *, pending_only: bool = True) -> list[Decision]:
        query = "SELECT id, vault_id, kind, detail, status FROM decisions"
        if pending_only:
            query += " WHERE status = 'pending'"
        with self._connect() as connection:
            rows = connection.execute(query + " ORDER BY id").fetchall()
        return [Decision(int(i), int(v), k, json.loads(d), s) for i, v, k, d, s in rows]

    def decision(self, decision_id: int) -> Decision | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, vault_id, kind, detail, status FROM decisions WHERE id = ?",
                (decision_id,),
            ).fetchone()
        if row is None:
            return None
        return Decision(int(row[0]), int(row[1]), row[2], json.loads(row[3]), row[4])

    def open_decision(self, vault_id: int, kind: str, detail: dict[str, Any]) -> int:
        """Record a question for the user; a newer one replaces a pending one."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM decisions WHERE vault_id = ? AND status = 'pending'",
                (vault_id,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE decisions SET kind = ?, detail = ? WHERE id = ?",
                    (kind, json.dumps(detail), row[0]),
                )
                decision_id = int(row[0])
            else:
                cursor = connection.execute(
                    "INSERT INTO decisions (vault_id, kind, detail, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (vault_id, kind, json.dumps(detail), time.time()),
                )
                decision_id = int(cursor.lastrowid or 0)
            self._bump(connection)
        return decision_id

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
                f"Unknown setting: {key}. See: markchain config list",
                code="config",
            )
        values = {**self._values(), key: value}
        try:
            stored = flatten(
                Settings.model_validate(unflatten(values)).model_dump(mode="json")
            )[key]
        except ValidationError as error:
            reason = error.errors(include_url=False)[0]["msg"]
            raise RequestError(f"Invalid value for {key}: {reason}", code="config") from error
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

    def import_settings(self, values: dict[str, Any]) -> None:
        """Store settings from an older settings file, skipping invalid ones."""
        for key, value in flatten(values).items():
            try:
                self.set_setting(key, value)
            except RequestError:
                continue
