"""Keyword tree: each note is a concept with names, a definition and relations.

A note's place comes first from what the user wrote: a ``parent`` in its
front matter, or a list line in another note that starts with a link to it
(that note is an index of its children). Where the user wrote nothing, the
model reads the note with its nearest search matches and proposes a name,
other names, a definition, broader concepts, related concepts and concepts to
read first. The program checks and stores the proposal:

* broader concepts must exist, sit in the same tree and not form a cycle;
* a new broader name joins a note or generated concept of the same name, or
  waits until a second note proposes it, so one guess never makes a node;
* a concept left with one child or none goes back to waiting;
* a concept with too many children is split into narrower groups;
* two notes that answer the same central question are reported, and merged
  when the folder's HANGER.md sets ``merge: auto``.

The graph lives in SQLite next to the closet's job store. It is derived from
the notes and the model's answers, so deleting it only costs re-extraction.
"""

from __future__ import annotations

__all__ = [
    "PROMPT_VERSION",
    "Extraction",
    "KeywordBuilder",
    "KeywordGraph",
    "Split",
    "norm",
]

import json
import logging
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from hanger.closets import ClosetPolicy
from hanger.errors import HangerError, ModelError, ModelOutputError, RequestError
from hanger.keyword_metadata import keyword_metadata
from hanger.prompts import extract_prompt, split_prompt
from hanger.relations import RelationProjection
from hanger.rules import rules_for
from hanger.semantics import SemanticError, parse_semantics

logger = logging.getLogger(__name__)

# Raise when the extraction prompt or its meaning changes: every note is then
# extracted again, after the notes that changed.
PROMPT_VERSION = 2
# A concept with more direct children than this is split into groups.
SPLIT_LIMIT = 15
# Attempts per note before it is reported as failed.
MAX_ATTEMPTS = 3
# Times a note is placed again because a neighbour changed, until it changes.
MAX_RECHECKS = 3
# Search candidates shown to the model.
CANDIDATES = 20
# Wait after a model call fails for a reason outside the note (CLI missing,
# time out, usage limit) before trying again.
PAUSE_SECONDS = 60
# Prefix of generated concepts (those that are not notes).
NODE = "node:"
MULTIPLE = "[multiple topics]"

# Raise when the tables below change.
_SCHEMA_VERSION = 2
_SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
    path TEXT PRIMARY KEY, hash TEXT NOT NULL, title TEXT NOT NULL,
    slug TEXT NOT NULL, stem TEXT NOT NULL, tree TEXT NOT NULL,
    enabled INTEGER NOT NULL, merge INTEGER NOT NULL,
    extracted TEXT, version INTEGER, name TEXT, aliases TEXT,
    question TEXT, definition TEXT, differentia TEXT,
    title_key TEXT NOT NULL DEFAULT '', name_key TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 0, failure TEXT NOT NULL DEFAULT '',
    rechecks INTEGER NOT NULL DEFAULT 0,
    explicit TEXT NOT NULL DEFAULT '{}', concept INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS notes_slug ON notes(slug);
CREATE INDEX IF NOT EXISTS notes_title_key ON notes(tree, title_key);
CREATE INDEX IF NOT EXISTS notes_name_key ON notes(tree, name_key);
CREATE INDEX IF NOT EXISTS notes_stem ON notes(stem);
-- Links the user wrote. kind: parent (front matter), list (list line that
-- starts with the link: owner is the parent), prose (related).
CREATE TABLE IF NOT EXISTS links(
    owner TEXT NOT NULL, raw TEXT NOT NULL, key TEXT NOT NULL, kind TEXT NOT NULL,
    anchor TEXT NOT NULL, target TEXT, PRIMARY KEY(owner, raw, kind));
CREATE INDEX IF NOT EXISTS links_target ON links(target);
CREATE INDEX IF NOT EXISTS links_key ON links(key);
-- Relations the model proposed. kind: broader, related, requires, same.
CREATE TABLE IF NOT EXISTS edges(
    owner TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
    PRIMARY KEY(owner, target, kind));
CREATE INDEX IF NOT EXISTS edges_target ON edges(target, kind);
CREATE TABLE IF NOT EXISTS nodes(id TEXT PRIMARY KEY, name TEXT NOT NULL,
    tree TEXT NOT NULL, definition TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS pending(key TEXT NOT NULL, name TEXT NOT NULL,
    tree TEXT NOT NULL, supporter TEXT NOT NULL, PRIMARY KEY(key, supporter));
CREATE TABLE IF NOT EXISTS pending_under(key TEXT NOT NULL, under TEXT NOT NULL,
    supporter TEXT NOT NULL, PRIMARY KEY(key, supporter));
CREATE TABLE IF NOT EXISTS tombstones(key TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS splits(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS merges(pair TEXT PRIMARY KEY);
-- A note said it answers the same question as one not extracted yet.
CREATE TABLE IF NOT EXISTS same_wait(path TEXT NOT NULL, other TEXT NOT NULL,
    name TEXT NOT NULL, PRIMARY KEY(path, other));
"""


def norm(text: str) -> str:
    """Compare names without spacing, punctuation or case."""
    return re.sub(r"[\s_\-·/()\[\]`'\".,:]+", "", text).casefold()


class _Broader(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(default="", description="Candidate id, or empty")
    new: str = Field(default="", description="New broader name, or empty")
    under: str = Field(
        default="", description="Candidate id the new name sits under, or empty"
    )


class Extraction(BaseModel):
    """What the model says one note is about."""

    model_config = ConfigDict(extra="forbid")
    central_question: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list, max_length=8)
    definition: str
    differentia: str
    broader: list[_Broader] = Field(default_factory=list, max_length=3)
    same_as: str = Field(default="", description="Candidate id, or empty")
    related: list[str] = Field(default_factory=list, max_length=6)
    requires: list[str] = Field(default_factory=list, max_length=4)

    def problems(self) -> list[str]:
        found = []
        for item in self.broader:
            if bool(item.id) == bool(item.new):
                found.append("each broader item needs exactly one of id or new")
            if item.under and not item.new:
                found.append("under goes only with new")
        if self.central_question.startswith(MULTIPLE) and (
            self.broader or self.same_as
        ):
            found.append("a note with multiple topics leaves broader and same_as empty")
        return found


class _Group(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    definition: str
    children: list[str]


class Split(BaseModel):
    """Narrower groups for a concept with too many children."""

    model_config = ConfigDict(extra="forbid")
    groups: list[_Group] = Field(default_factory=list, max_length=20)
    unassigned: list[str] = Field(default_factory=list)


class Asker(Protocol):
    def ask(
        self,
        prompt: str,
        schema: type[BaseModel],
        is_cancelled: Callable[[], bool],
        role: str = ...,
    ) -> dict[str, Any]: ...


# ---------- note text ----------


def _front_matter(text: str) -> tuple[dict[str, str], str]:
    """Top-level ``key: value`` pairs of the front matter, and the body."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    values: dict[str, str] = {}
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            return values, "\n".join(lines[index + 1 :])
        if line[:1].isspace():
            continue
        key, colon, value = line.partition(":")
        if colon and key.strip():
            values[key.strip()] = value.strip().strip("\"'")
    return {}, text


_WIKI_LINK = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_MD_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)\)")
_LIST_LINK = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+(?:\[\[|\[[^\]]+\]\()")
_SITE_LINK = re.compile(r"^/(?:wiki|docs)/([^/#?]+)/?(?:[#?].*)?$")


def _links(text: str) -> Iterator[tuple[str, str]]:
    """(target, anchor) for each link to another note."""
    for match in _WIKI_LINK.finditer(text):
        yield match.group(1).strip(), (match.group(2) or "").strip()
    for match in _MD_LINK.finditer(text):
        target = match.group(2).strip()
        if re.match(r"^[a-z][a-z0-9+.-]*:", target):
            continue  # http:, mailto: and the like
        if target.split("#")[0].endswith(".md") or _SITE_LINK.match(target):
            yield target, match.group(1).strip()


def _link_key(raw: str) -> str:
    """The name a link refers to: a slug or a file name without .md."""
    raw = raw.split("#")[0].strip()
    site = _SITE_LINK.match(raw)
    if site:
        return norm(site.group(1))
    stem = PurePosixPath(raw).name
    return norm(stem[:-3] if stem.endswith(".md") else stem)


def note_title(text: str, fallback: str, *, path: str = "document.md") -> str:
    """The front matter title, else the first heading, else the file name."""
    try:
        snapshot = parse_semantics(text, path=path)
        title, body = snapshot.frontmatter.get("title"), snapshot.body
    except SemanticError:
        meta, body = _front_matter(text)
        title = meta.get("title")
    heading = re.search(r"^#\s+(.+)$", body, re.M)
    return (title if isinstance(title, str) and title else None) or (
        heading.group(1).strip() if heading else fallback
    )


def parse_note(
    text: str, fallback: str, *, path: str = "document.md"
) -> dict[str, Any]:
    """Title, slug and user-written links of a note."""
    snapshot = parse_semantics(text, path=path)
    meta, body = snapshot.frontmatter, snapshot.body
    title = note_title(text, fallback, path=path)
    explicit = keyword_metadata(snapshot)
    links: list[tuple[str, str, str]] = []
    for field, kind in (
        ("parent", "parent"),
        ("broader", "parent"),
        ("related", "related"),
        ("requires", "requires"),
    ):
        for value in explicit.values.get(field, []):
            found = list(_links(value))
            if found:
                links += [(raw, kind, anchor) for raw, anchor in found]
            elif value.strip():
                links.append((value, kind, ""))
    for line in body.splitlines():
        kind = "list" if _LIST_LINK.match(line) else "prose"
        for raw, anchor in _links(line):
            links.append((raw, kind, anchor))
            kind = "prose"  # only the link that starts a list line is a child
    return {"title": title, "slug": meta.get("slug", ""), "links": links}


def plain_text(text: str) -> str:
    """Note body with link syntax reduced to the words a reader sees."""
    _, body = _front_matter(text)
    body = re.sub(r"\[\[[^\]|]+\|([^\]]+)\]\]", r"\1", body)
    body = re.sub(r"\[\[([^\]]+)\]\]", lambda m: m.group(1).split("/")[-1], body)
    return re.sub(r"(?<!!)\[([^\]]+)\]\([^)]+\)", r"\1", body)


# ---------- the graph ----------


class KeywordGraph:
    """Concepts and relations of one closet, kept in step with its notes."""

    def __init__(self, root: Path, database: Path, policy: ClosetPolicy) -> None:
        self.root = root
        self.policy = policy
        self._lock = threading.RLock()
        database.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(database)) as setup:
            setup.execute("PRAGMA journal_mode=WAL")
            (version,) = setup.execute("PRAGMA user_version").fetchone()
            if version != _SCHEMA_VERSION:
                # The graph is derived from the notes: a new layout starts over.
                for (table,) in setup.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall():
                    setup.execute(f'DROP TABLE IF EXISTS "{table}"')
            setup.executescript(_SCHEMA)
            setup.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        self._db = sqlite3.connect(
            database, check_same_thread=False, isolation_level=None, timeout=30
        )
        # Called with (note, same note, concept name) when two notes answer
        # the same question and both folders allow merging.
        self.on_same: Callable[[str, str, str], None] | None = None
        self.relations = RelationProjection(database.with_name("relations.sqlite"))

    def close(self) -> None:
        with self._lock:
            self.relations.close()
            self._db.close()

    def _rows(self, query: str, *args: Any) -> list[tuple[Any, ...]]:
        return self._db.execute(query, args).fetchall()

    def _one(self, query: str, *args: Any) -> tuple[Any, ...] | None:
        row: tuple[Any, ...] | None = self._db.execute(query, args).fetchone()
        return row

    def _transaction(self) -> Any:
        return _Transaction(self._db, self._lock)

    # ---------- keeping in step with the notes ----------

    def _folder_rules(self, path: str) -> tuple[str, bool, bool]:
        settings = rules_for(self.root, path).settings
        enabled = settings.get("keywords", "true").lower() not in {"false", "no", "0"}
        return settings.get("tree", ""), enabled, settings.get("merge", "") == "auto"

    # cost: time O(l+k), heap O(n), stack O(1), io O(d)
    # vars: n = note bytes, l = links in the note, k = links naming it, d = depth
    # basis: estimate
    def observe(self, path: str, text: str, digest: str) -> set[str]:
        """Record a note's text. Returns notes whose link names changed."""
        try:
            snapshot = parse_semantics(text, path=path)
            parsed = parse_note(text, PurePosixPath(path).stem, path=path)
            explicit = keyword_metadata(snapshot)
        except SemanticError:
            self.forget(path)
            raise
        with self._transaction():
            row = self._one("SELECT hash, slug, tree FROM notes WHERE path=?", path)
            if row and row[0] == digest:
                return set()
            self.relations.observe(snapshot)
            tree, enabled, merge = self._folder_rules(path)
            slug = norm(parsed["slug"])
            values = (
                digest,
                parsed["title"],
                slug,
                norm(PurePosixPath(path).stem),
                tree,
                int(enabled),
                int(merge),
                norm(parsed["title"]),
                path,
            )
            if row:
                self._db.execute(
                    "UPDATE notes SET hash=?, title=?, slug=?, stem=?, tree=?, "
                    "enabled=?, merge=?, title_key=?, attempts=0, failure='', "
                    "rechecks=0 WHERE path=?",
                    values,
                )
            else:
                self._db.execute(
                    "INSERT INTO notes(hash, title, slug, stem, tree, enabled, "
                    "merge, title_key, path) VALUES(?,?,?,?,?,?,?,?,?)",
                    values,
                )
            self._db.execute(
                "UPDATE notes SET explicit=?, concept=? WHERE path=?",
                (
                    json.dumps(explicit.values, ensure_ascii=False),
                    int(explicit.concept),
                    path,
                ),
            )
            if not explicit.concept:
                self._leave_tree(path)
            if explicit.complete:
                self._db.execute(
                    "UPDATE notes SET extracted=hash, version=? WHERE path=?",
                    (PROMPT_VERSION, path),
                )
            # A note of this name is back: its name may be used again.
            self._db.execute(
                "DELETE FROM tombstones WHERE key=?",
                (f"{tree}::{norm(parsed['title'])}",),
            )
            before = set(
                self._rows("SELECT target, anchor FROM links WHERE owner=?", path)
            )
            self._db.execute("DELETE FROM links WHERE owner=?", (path,))
            for raw, kind, anchor in parsed["links"]:
                self._db.execute(
                    "INSERT OR REPLACE INTO links VALUES(?,?,?,?,?,?)",
                    (path, raw, _link_key(raw), kind, anchor, self._resolve(raw, path)),
                )
            after = set(
                self._rows("SELECT target, anchor FROM links WHERE owner=?", path)
            )
            # Only notes whose incoming link text changed are indexed again.
            touched = {target for target, _ in before ^ after}
            if not row:
                # Links that name this note by file name or slug may now resolve.
                touched |= self._relink({norm(PurePosixPath(path).stem), slug})
            elif row[1] != slug:
                touched |= self._relink({row[1], slug})
            if row and row[2] != tree:
                self._leave_tree(path)
        return {t for t in touched if t and t != path}

    def _leave_tree(self, path: str) -> None:
        """A note moved to another tree: its model relations no longer hold."""
        for (owner,) in self._rows(
            "SELECT owner FROM edges WHERE target=? AND owner!=?", path, path
        ):
            self._recheck(owner)
        self._db.execute("DELETE FROM edges WHERE owner=? OR target=?", (path, path))
        self._db.execute("DELETE FROM pending WHERE supporter=?", (path,))
        self._db.execute("DELETE FROM pending_under WHERE supporter=?", (path,))
        self._recheck(path)
        self._demote_small_nodes()

    def _recheck(self, path: str) -> None:
        """Place a note again; bounded so two notes cannot bounce forever."""
        self._db.execute(
            "UPDATE notes SET extracted=NULL, rechecks=rechecks+1 "
            "WHERE path=? AND rechecks<?",
            (path, MAX_RECHECKS),
        )

    # cost: time O(k+c), heap O(c), stack O(1), io O(1)
    # vars: k = links naming the note, c = children
    # basis: estimate
    def forget(self, path: str) -> set[str]:
        """A note is gone: its children move up a level and are checked again."""
        with self._transaction():
            self.relations.forget(path)
            row = self._one("SELECT tree FROM notes WHERE path=?", path)
            if not row:
                return set()
            parents = [p for p in self.parents(path)[0] if p != path]
            children = [
                o
                for (o,) in self._rows(
                    "SELECT owner FROM edges WHERE target=? AND kind='broader'", path
                )
            ]
            name = self.display_name(path)
            for child in children:
                self._db.execute(
                    "DELETE FROM edges WHERE owner=? AND target=? AND kind='broader'",
                    (child, path),
                )
                for parent in parents:
                    if parent != child and not self._reaches(parent, child):
                        self._db.execute(
                            "INSERT OR IGNORE INTO edges VALUES(?,?,'broader')",
                            (child, parent),
                        )
                self._recheck(child)
            if children:
                # Its name is not made again as a generated concept.
                self._db.execute(
                    "INSERT OR IGNORE INTO tombstones VALUES(?)",
                    (f"{row[0]}::{norm(name)}",),
                )
            for (owner,) in self._rows(
                "SELECT owner FROM edges WHERE target=? AND kind='same'", path
            ):
                self._recheck(owner)
            touched = {
                t
                for (t,) in self._rows(
                    "SELECT target FROM links WHERE owner=? AND target IS NOT NULL",
                    path,
                )
            }
            self._db.execute("DELETE FROM notes WHERE path=?", (path,))
            self._db.execute("DELETE FROM links WHERE owner=?", (path,))
            self._db.execute("UPDATE links SET target=NULL WHERE target=?", (path,))
            self._db.execute(
                "DELETE FROM edges WHERE owner=? OR target=?", (path, path)
            )
            self._db.execute("DELETE FROM pending WHERE supporter=?", (path,))
            self._db.execute(
                "DELETE FROM pending_under WHERE supporter=? OR under=?", (path, path)
            )
            self._db.execute("DELETE FROM splits WHERE id=?", (path,))
            self._db.execute(
                "DELETE FROM same_wait WHERE path=? OR other=?", (path, path)
            )
            touched |= self._relink({norm(PurePosixPath(path).stem)})
            self._demote_small_nodes()
        return {t for t in touched if t and t != path}

    # cost: time O(g+f), heap O(g), stack O(1), io O(f*d)
    # vars: g = notes in the graph, f = notes on disk, d = folder depth
    # basis: estimate
    def reconcile(
        self, paths: Iterable[str], read: Callable[[str], tuple[str, str]]
    ) -> set[str]:
        """Bring the graph in line with the notes on disk.

        ``read`` returns a note's text and hash. Notes the graph lacks are
        read; notes it has are re-read only when their folder rules moved
        them to another tree or turned keywords on or off.
        """
        present = set(paths)
        missing_relations = self.relations.missing(present)
        touched: set[str] = set()
        with self._lock:
            known = {
                p: (tree, bool(enabled), bool(merge))
                for p, tree, enabled, merge in self._rows(
                    "SELECT path, tree, enabled, merge FROM notes"
                )
            }
        for path in sorted(set(known) - present):
            touched |= self.forget(path)
        for path in sorted(present):
            if path not in known or path in missing_relations:
                try:
                    text, digest = read(path)
                    touched |= self.observe(path, text, digest)
                    if path in missing_relations:
                        self.relations.observe(parse_semantics(text, path=path))
                except (OSError, HangerError) as error:
                    logger.info("keywords: cannot read %s: %s", path, error)
                    continue
                continue
            rules = self._folder_rules(path)
            if rules != known[path]:
                with self._transaction():
                    self._db.execute(
                        "UPDATE notes SET tree=?, enabled=?, merge=? WHERE path=?",
                        (rules[0], int(rules[1]), int(rules[2]), path),
                    )
                    if rules[0] != known[path][0] or not rules[1]:
                        self._leave_tree(path)
                    elif not known[path][1]:
                        self._recheck(path)
        return touched - {""}

    # ---------- links ----------

    def _resolve(self, raw: str, owner: str) -> str | None:
        """The note a user-written link names, when there is exactly one."""
        raw = raw.split("#")[0].strip()
        if not raw:
            return None
        site = _SITE_LINK.match(raw)
        if site:
            return self._unique("slug", norm(site.group(1)))
        candidate = raw.lstrip("/")
        if not candidate.endswith(".md"):
            candidate += ".md"
        base = PurePosixPath(owner).parent
        relative = (base / candidate).as_posix()
        options = (candidate,) if raw.startswith("/") else (relative, candidate)
        for option in options:
            normal = _normalize(option)
            if normal and self._one("SELECT 1 FROM notes WHERE path=?", normal):
                return normal
        if "/" in raw.strip("/"):
            return None  # a path that names no note does not fall back to a name
        key = _link_key(raw)
        return self._unique("slug", key) or self._unique("stem", key)

    def _unique(self, column: str, key: str) -> str | None:
        if not key:
            return None
        rows = self._rows(f"SELECT path FROM notes WHERE {column}=? LIMIT 2", key)  # noqa: S608
        return str(rows[0][0]) if len(rows) == 1 else None

    def _relink(self, keys: set[str]) -> set[str]:
        """Resolve again the links that name one of these keys."""
        changed: set[str] = set()
        for key in keys - {""}:
            for owner, raw, kind, target in self._rows(
                "SELECT owner, raw, kind, target FROM links WHERE key=?", key
            ):
                resolved = self._resolve(raw, owner)
                if resolved != target:
                    self._db.execute(
                        "UPDATE links SET target=? WHERE owner=? AND raw=? AND kind=?",
                        (resolved, owner, raw, kind),
                    )
                    changed |= {t for t in (target, resolved) if t}
        return changed

    # ---------- names and places ----------

    def names(self, path: str) -> list[str]:
        """Every name a note is known by: title, link texts, model names."""
        with self._lock:
            found: list[str] = []
            row = self._one("SELECT title, name, aliases FROM notes WHERE path=?", path)
            if not row:
                return []
            found.append(row[0])
            explicit = self.explicit(path)
            found += explicit.get("aliases", [])
            if explicit.get("name"):
                found.append(explicit["name"])
            found += [
                a
                for (a,) in self._rows(
                    "SELECT DISTINCT anchor FROM links WHERE target=? AND anchor!=''",
                    path,
                )
            ]
            if row[1] and self._tree_of(path) is not None:
                if "name" not in explicit:
                    found.append(row[1])
                if "aliases" not in explicit:
                    found += json.loads(row[2] or "[]")
            seen: set[str] = set()
            unique = []
            for name in found:
                if norm(name) and norm(name) not in seen:
                    seen.add(norm(name))
                    unique.append(name)
            return unique

    def display_name(self, node: str) -> str:
        with self._lock:
            if node.startswith(NODE):
                row = self._one("SELECT name FROM nodes WHERE id=?", node)
                return str(row[0]) if row else node[len(NODE) :]
            row = self._one("SELECT title FROM notes WHERE path=?", node)
            return str(row[0]) if row and row[0] else PurePosixPath(node).stem

    def _tree_of(self, node: str) -> str | None:
        row = self._one(
            "SELECT tree FROM nodes WHERE id=?"
            if node.startswith(NODE)
            else "SELECT tree FROM notes WHERE path=? AND enabled=1 AND concept=1",
            node,
        )
        return None if row is None else str(row[0])

    def parents(self, node: str) -> tuple[list[str], str]:
        """Broader concepts and who set them: user links win over the model."""
        with self._lock:
            if not node.startswith(NODE):
                tree = self._tree_of(node)
                human = [
                    t
                    for (t,) in self._rows(
                        "SELECT target FROM links WHERE owner=? AND kind='parent' "
                        "AND target IS NOT NULL",
                        node,
                    )
                ]
                human += [
                    o
                    for (o,) in self._rows(
                        "SELECT owner FROM links WHERE target=? AND kind='list'", node
                    )
                ]
                human = [h for h in dict.fromkeys(human) if self._tree_of(h) == tree]
                explicit = self.explicit(node)
                if human or "parent" in explicit or "broader" in explicit:
                    return sorted(human), "user"
            model = [
                t
                for (t,) in self._rows(
                    "SELECT target FROM edges WHERE owner=? AND kind='broader' "
                    "ORDER BY target",
                    node,
                )
            ]
            return model, ("model" if model else "")

    def children(self, node: str) -> list[str]:
        with self._lock:
            found = {
                o
                for (o,) in self._rows(
                    "SELECT owner FROM links WHERE target=? AND kind='parent'", node
                )
            }
            found |= {
                t
                for (t,) in self._rows(
                    "SELECT target FROM links WHERE owner=? AND kind='list' "
                    "AND target IS NOT NULL",
                    node,
                )
            }
            found |= {
                o
                for (o,) in self._rows(
                    "SELECT owner FROM edges WHERE target=? AND kind='broader'", node
                )
            }
            return sorted(c for c in found if c != node and node in self.parents(c)[0])

    def position(self, node: str, depth: int = 8) -> list[str]:
        """Names from the root down to the concept just above this one."""
        with self._lock:
            chain: list[str] = []
            seen = {node}
            current = node
            while depth:
                parents = self.parents(current)[0]
                if not parents or parents[0] in seen:
                    break
                current = parents[0]
                seen.add(current)
                chain.append(self.display_name(current))
                depth -= 1
            return list(reversed(chain))

    def _all_parents(self, node: str) -> list[str]:
        """User and model parents together, so no hidden edge closes a cycle."""
        found = [
            t
            for (t,) in self._rows(
                "SELECT target FROM edges WHERE owner=? AND kind='broader'", node
            )
        ]
        if not node.startswith(NODE):
            found += [
                t
                for (t,) in self._rows(
                    "SELECT target FROM links WHERE owner=? AND kind='parent' "
                    "AND target IS NOT NULL",
                    node,
                )
            ]
            found += [
                o
                for (o,) in self._rows(
                    "SELECT owner FROM links WHERE target=? AND kind='list'", node
                )
            ]
        return found

    def _reaches(self, start: str, goal: str) -> bool:
        """Whether goal is start or above it."""
        stack, seen = [start], {start}
        while stack:
            current = stack.pop()
            if current == goal:
                return True
            for parent in self._all_parents(current):
                if parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        return False

    # ---------- the queue ----------

    def next_note(self) -> str | None:
        """The next note to extract: changed notes before version updates."""
        with self._lock:
            row = self._one(
                "SELECT path FROM notes WHERE enabled=1 AND concept=1 "
                "AND attempts<? AND "
                "(extracted IS NULL OR extracted!=hash OR version!=?) "
                "ORDER BY COALESCE(version IS NOT NULL AND extracted=hash, 0), "
                "attempts, path LIMIT 1",
                MAX_ATTEMPTS,
                PROMPT_VERSION,
            )
            return None if row is None else str(row[0])

    def next_split(self) -> str | None:
        with self._lock:
            row = self._one("SELECT id FROM splits ORDER BY id LIMIT 1")
            return None if row is None else str(row[0])

    def failed(self, path: str, reason: str) -> None:
        with self._transaction():
            self._db.execute(
                "UPDATE notes SET attempts=attempts+1, failure=? WHERE path=?",
                (reason, path),
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            (total,) = self._one(
                "SELECT COUNT(*) FROM notes WHERE enabled=1 AND concept=1"
            ) or (0,)
            (done,) = self._one(
                "SELECT COUNT(*) FROM notes WHERE enabled=1 AND concept=1 "
                "AND extracted=hash "
                "AND version=?",
                PROMPT_VERSION,
            ) or (0,)
            failures = [
                {"path": p, "reason": r}
                for p, r in self._rows(
                    "SELECT path, failure FROM notes WHERE enabled=1 AND concept=1 "
                    "AND attempts>=? "
                    "ORDER BY path LIMIT 20",
                    MAX_ATTEMPTS,
                )
            ]
            (failed,) = self._one(
                "SELECT COUNT(*) FROM notes WHERE enabled=1 AND concept=1 "
                "AND attempts>=? "
                "AND NOT (extracted=hash AND version=?)",
                MAX_ATTEMPTS,
                PROMPT_VERSION,
            ) or (0,)
            # Notes that answer the same question; merged ones are gone.
            same = [
                {"notes": [a, b], "merge_queued": bool(queued)}
                for a, b, queued in self._rows(
                    "SELECT owner, target, EXISTS(SELECT 1 FROM merges WHERE pair="
                    "CASE WHEN owner<target THEN owner||char(10)||target "
                    "ELSE target||char(10)||owner END) FROM edges "
                    "WHERE kind='same' ORDER BY owner LIMIT 20"
                )
            ]
            (nodes,) = self._one("SELECT COUNT(*) FROM nodes") or (0,)
            (waiting,) = self._one("SELECT COUNT(DISTINCT key) FROM pending") or (0,)
            return {
                "notes": total,
                "extracted": done,
                "queued": total - done - failed,
                "failed_count": failed,
                "failed": failures,
                "same_question": same,
                "generated_concepts": nodes,
                "waiting_names": waiting,
            }

    def busy(self) -> bool:
        return self.next_note() is not None or self.next_split() is not None

    # ---------- the model's input ----------

    def explicit(self, path: str) -> dict[str, Any]:
        with self._lock:
            row = self._one("SELECT explicit FROM notes WHERE path=?", path)
            return dict(json.loads(row[0])) if row else {}

    def note(self, path: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._one(
                "SELECT hash, tree, name, definition, differentia, question, merge "
                "FROM notes WHERE path=?",
                path,
            )
            if row is None:
                return None
            explicit = self.explicit(path)
            return {
                "hash": row[0],
                "tree": row[1],
                "name": explicit.get("name", row[2] or ""),
                "definition": explicit.get("definition", row[3] or ""),
                "differentia": explicit.get("differentia", row[4] or ""),
                "question": explicit.get("central_question", row[5] or ""),
                "merge": bool(row[6]) and self._tree_of(path) is not None,
            }

    def candidates(
        self, path: str, hits: Iterable[str], query: str
    ) -> list[dict[str, Any]]:
        """Search matches in the same tree, plus generated and waiting concepts."""
        with self._lock:
            tree = self._tree_of(path)
            found: list[dict[str, Any]] = []
            for hit in hits:
                if hit == path or self._tree_of(hit) != tree:
                    continue
                info = self.note(hit) or {}
                found.append(
                    {
                        "id": hit,
                        "name": self.display_name(hit),
                        "other_names": self.names(hit)[1:5],
                        "position": " > ".join(self.position(hit, 4)),
                        "children": len(self.children(hit)),
                        "definition": info.get("definition", "")[:160],
                    }
                )
                if len(found) >= CANDIDATES:
                    break
            words = set(re.findall(r"\w{2,}", query.casefold()))
            for node, name, definition in self._rows(
                "SELECT id, name, definition FROM nodes WHERE tree=?", tree
            ):
                if words & set(re.findall(r"\w{2,}", name.casefold())):
                    found.append(
                        {
                            "id": node,
                            "name": name,
                            "other_names": [],
                            "position": " > ".join(self.position(node, 4)),
                            "children": len(self.children(node)),
                            "definition": definition[:160],
                        }
                    )
            waiting = [
                {"waiting_name": name, "supporters": count}
                for name, count in self._rows(
                    "SELECT name, COUNT(*) FROM pending WHERE tree=? GROUP BY key",
                    tree,
                )
                if words & set(re.findall(r"\w{2,}", name.casefold()))
            ]
            return found + waiting

    # ---------- the model's answer ----------

    # cost: time O(c*h), heap O(c), stack O(1), io O(1)
    # vars: c = relations in the answer, h = tree height
    # basis: estimate
    def apply(
        self, path: str, expected: str, answer: Extraction, ids: dict[str, str]
    ) -> dict[str, Any]:
        """Store an extraction. ``ids`` maps the short ids in the prompt."""
        report: dict[str, Any] = {"created": [], "ignored": [], "same_as": None}
        merges: list[tuple[str, str, str]] = []
        with self._transaction():
            row = self._one("SELECT hash, tree, name FROM notes WHERE path=?", path)
            if row is None or row[0] != expected or self._tree_of(path) is None:
                return {"stale": True}
            explicit = self.explicit(path)
            tree, old_name = row[1], row[2]
            self._db.execute(
                "UPDATE notes SET extracted=hash, version=?, name=?, aliases=?, "
                "question=?, definition=?, differentia=?, attempts=0, failure='', "
                "name_key=? WHERE path=?",
                (
                    PROMPT_VERSION,
                    answer.name.strip(),
                    json.dumps(
                        [a.strip() for a in answer.aliases if a.strip()],
                        ensure_ascii=False,
                    ),
                    answer.central_question,
                    answer.definition,
                    explicit.get("differentia", answer.differentia),
                    norm(explicit.get("name", answer.name)),
                    path,
                ),
            )
            if (
                old_name
                and norm(old_name) != norm(answer.name)
                and norm(old_name) not in {norm(a) for a in answer.aliases}
            ):
                # Notes placed under the old name are checked against the new.
                for (owner,) in self._rows(
                    "SELECT owner FROM edges WHERE target=? AND kind='broader'", path
                ):
                    self._recheck(owner)
            self._db.execute("DELETE FROM edges WHERE owner=?", (path,))
            self._db.execute("DELETE FROM pending WHERE supporter=?", (path,))
            self._db.execute("DELETE FROM pending_under WHERE supporter=?", (path,))

            def target(short: str) -> str | None:
                node = ids.get(short.strip(), "")
                if not node or node == path or self._tree_of(node) != tree:
                    report["ignored"].append(short)
                    return None
                return node

            multiple = answer.central_question.startswith(MULTIPLE)
            for item in (
                []
                if multiple or {"parent", "broader"} & explicit.keys()
                else answer.broader
            ):
                if item.id:
                    node = target(item.id)
                    if node and not self._reaches(node, path):
                        self._db.execute(
                            "INSERT OR IGNORE INTO edges VALUES(?,?,'broader')",
                            (path, node),
                        )
                    elif node:
                        report["ignored"].append(item.id)
                elif item.new.strip():
                    under = target(item.under) if item.under else None
                    self._propose(path, tree, item.new.strip(), under, report)
            broader = {
                t
                for (t,) in self._rows(
                    "SELECT target FROM edges WHERE owner=? AND kind='broader'", path
                )
            }
            # Parents the user set are not related or prerequisite either.
            above = broader | set(self.parents(path)[0])
            requires = {
                t
                for t in map(target, [] if "requires" in explicit else answer.requires)
                if t
            } - above
            for node in requires:
                self._db.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,'requires')", (path, node)
                )
            for node in {
                t
                for t in map(target, [] if "related" in explicit else answer.related)
                if t
            }:
                if node not in above | requires:
                    self._db.execute(
                        "INSERT OR IGNORE INTO edges VALUES(?,?,'related')",
                        (path, node),
                    )
            self._db.execute("DELETE FROM same_wait WHERE path=?", (path,))
            if answer.same_as and not multiple and "same_as" not in explicit:
                same = target(answer.same_as)
                if same and not same.startswith(NODE):
                    report["same_as"] = self._same(path, same, answer.name, merges)
            # Notes that waited for this one to be placed are judged now.
            for waiting, name in self._rows(
                "SELECT path, name FROM same_wait WHERE other=?", path
            ):
                self._db.execute("DELETE FROM same_wait WHERE path=?", (waiting,))
                self._same(waiting, path, name, merges)
            for node in broader:
                if len(self.split_input(node)) > SPLIT_LIMIT:
                    self._db.execute("INSERT OR IGNORE INTO splits VALUES(?)", (node,))
            self._demote_small_nodes()
        # Queued only after the graph is saved, so a merge is never asked twice.
        for merge in merges:
            if self.on_same is not None:
                self.on_same(*merge)
        return report

    def _same(
        self, path: str, other: str, name: str, merges: list[tuple[str, str, str]]
    ) -> str:
        """Two notes answer the same question: record it, and merge if allowed."""
        if self._tree_of(path) is None or self._tree_of(other) is None:
            return "ignored"
        if "same_as" in self.explicit(path) or "same_as" in self.explicit(other):
            return "ignored"
        info = self.note(other)
        if not info or not info["definition"]:
            # The other note is judged when it is placed.
            self._db.execute(
                "INSERT OR REPLACE INTO same_wait VALUES(?,?,?)", (path, other, name)
            )
            return "waiting"
        self._db.execute(
            "INSERT OR IGNORE INTO edges VALUES(?,?,'same')", (path, other)
        )
        pair = "\n".join(sorted((path, other)))
        mine = self.note(path)
        allowed = (
            bool(mine and mine["merge"] and info["merge"])
            and self.policy.writable(path)
            and self.policy.writable(other)
        )
        if not allowed:
            return "reported"
        if self._one("SELECT 1 FROM merges WHERE pair=?", pair):
            return "merge_queued"
        self._db.execute("INSERT INTO merges VALUES(?)", (pair,))
        merges.append((path, other, name))
        return "merge_queued"

    def _propose(
        self, path: str, tree: str, name: str, under: str | None, report: dict[str, Any]
    ) -> None:
        """A broader name that is not a candidate: join it or let it wait."""
        key = f"{tree}::{norm(name)}"
        if not norm(name):
            report["ignored"].append(name)
            return
        existing = [
            n for (n,) in self._rows("SELECT id FROM nodes WHERE id=?", NODE + key)
        ] + [
            p
            for (p,) in self._rows(
                "SELECT path FROM notes WHERE tree=? AND enabled=1 AND path!=? "
                "AND concept=1 AND (title_key=? OR name_key=?) "
                "ORDER BY title_key!=?, path LIMIT 2",
                tree,
                path,
                norm(name),
                norm(name),
                norm(name),
            )
        ]
        if not existing and self._one("SELECT 1 FROM tombstones WHERE key=?", key):
            report["ignored"].append(name)
            return
        if len(existing) > 1:
            report["ignored"].append(name)
            return
        if existing:
            if not self._reaches(existing[0], path):
                self._db.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,'broader')",
                    (path, existing[0]),
                )
            return
        node = NODE + key
        if under and not self._reaches(under, path):
            self._db.execute(
                "INSERT OR REPLACE INTO pending_under VALUES(?,?,?)", (key, under, path)
            )
        self._db.execute(
            "INSERT OR IGNORE INTO pending VALUES(?,?,?,?)", (key, name, tree, path)
        )
        supporters = [
            s for (s,) in self._rows("SELECT supporter FROM pending WHERE key=?", key)
        ]
        if len(supporters) < 2:
            return
        self._db.execute(
            "INSERT OR IGNORE INTO nodes(id, name, tree) VALUES(?,?,?)",
            (node, name, tree),
        )
        for supporter in supporters:
            if not self._reaches(node, supporter):
                self._db.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,'broader')",
                    (supporter, node),
                )
        self._db.execute("DELETE FROM pending WHERE key=?", (key,))
        # The concept sits under the broader candidate two supporters named.
        for (parent,) in self._rows(
            "SELECT under FROM pending_under WHERE key=? GROUP BY under "
            "HAVING COUNT(*)>=2 ORDER BY MIN(rowid) LIMIT 1",
            key,
        ):
            if not self._reaches(parent, node):
                self._db.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,'broader')", (node, parent)
                )
        self._db.execute("DELETE FROM pending_under WHERE key=?", (key,))
        report["created"].append(name)

    def _demote_small_nodes(self) -> None:
        """A generated concept with one child or none goes back to waiting."""
        for node, name, tree in self._rows("SELECT id, name, tree FROM nodes"):
            kids = [
                o
                for (o,) in self._rows(
                    "SELECT owner FROM edges WHERE target=? AND kind='broader'", node
                )
            ]
            if len(kids) > 1:
                continue
            self._db.execute("DELETE FROM nodes WHERE id=?", (node,))
            self._db.execute(
                "DELETE FROM edges WHERE owner=? OR target=?", (node, node)
            )
            self._db.execute("DELETE FROM splits WHERE id=?", (node,))
            self._db.execute("DELETE FROM pending WHERE supporter=?", (node,))
            self._db.execute(
                "DELETE FROM pending_under WHERE supporter=? OR under=?", (node, node)
            )
            for kid in kids:
                if kid.startswith(NODE):
                    continue  # only notes wait for a broader name
                self._db.execute(
                    "INSERT OR IGNORE INTO pending VALUES(?,?,?,?)",
                    (node[len(NODE) :], name, tree, kid),
                )

    # ---------- splitting large concepts ----------

    def split_input(self, node: str) -> list[dict[str, str]]:
        """Children the model placed here, without their bodies."""
        with self._lock:
            found = []
            for child in self.children(node):
                if self.parents(child)[1] != "model":
                    continue
                info = self.note(child) or {}
                found.append(
                    {
                        "id": child,
                        "name": self.display_name(child),
                        "definition": info.get("definition", ""),
                        "differentia": info.get("differentia", ""),
                    }
                )
            return found

    def apply_split(self, node: str, answer: Split, ids: dict[str, str]) -> list[str]:
        """Put children under the narrower groups the model proposed."""
        created: list[str] = []
        with self._transaction():
            self._db.execute("DELETE FROM splits WHERE id=?", (node,))
            tree = self._tree_of(node)
            if tree is None:
                return created
            used: set[str] = set()
            for group in answer.groups:
                members = [
                    ids[c]
                    for c in dict.fromkeys(group.children)
                    if c in ids and ids[c] not in used
                ]
                key = f"{tree}::{norm(group.name)}"
                if len(members) < 2 or not norm(group.name):
                    continue
                if self._one("SELECT 1 FROM tombstones WHERE key=?", key):
                    continue
                group_id = NODE + key
                if group_id == node or self._reaches(node, group_id):
                    continue
                self._db.execute(
                    "INSERT OR IGNORE INTO nodes VALUES(?,?,?,?)",
                    (group_id, group.name, tree, group.definition),
                )
                self._db.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,'broader')",
                    (group_id, node),
                )
                for member in members:
                    self._db.execute(
                        "DELETE FROM edges WHERE owner=? AND target=? "
                        "AND kind='broader'",
                        (member, node),
                    )
                    self._db.execute(
                        "INSERT OR IGNORE INTO edges VALUES(?,?,'broader')",
                        (member, group_id),
                    )
                    used.add(member)
                created.append(group.name)
            self._demote_small_nodes()
        return created

    # ---------- reading the tree ----------

    def describe(self, node: str) -> dict[str, Any] | None:
        """A concept with its names, place and relations."""
        with self._lock:
            if node.startswith(NODE):
                row = self._one("SELECT name, definition FROM nodes WHERE id=?", node)
                if row is None:
                    return None
                info: dict[str, Any] = {
                    "id": node,
                    "name": row[0],
                    "kind": "generated",
                    "definition": row[1],
                }
            else:
                note = self.note(node)
                if note is None:
                    return None
                info = {
                    "id": node,
                    "name": self.display_name(node),
                    "kind": "note",
                    "other_names": self.names(node)[1:],
                    "definition": note["definition"],
                    "central_question": note["question"],
                }
            parents, source = self.parents(node)

            def named(nodes: Iterable[str]) -> list[dict[str, str]]:
                return [{"id": n, "name": self.display_name(n)} for n in nodes]

            def related(kind: str) -> list[dict[str, str]]:
                if kind in self.explicit(node):
                    return named(
                        t
                        for (t,) in self._rows(
                            "SELECT target FROM links WHERE owner=? AND kind=? "
                            "AND target IS NOT NULL",
                            node,
                            kind,
                        )
                    )
                return named(
                    t
                    for (t,) in self._rows(
                        "SELECT target FROM edges WHERE owner=? AND kind=? "
                        "ORDER BY target",
                        node,
                        kind,
                    )
                )

            info.update(
                {
                    "position": self.position(node),
                    "broader": named(parents),
                    "broader_set_by": source,
                    "narrower": named(self.children(node)),
                    "related": related("related"),
                    "requires": related("requires"),
                    "same_question": related("same"),
                }
            )
            return info

    def roots(self) -> list[dict[str, Any]]:
        """Top concepts of each tree with their child counts."""
        with self._lock:
            found = []
            for tree in [t for (t,) in self._rows("SELECT DISTINCT tree FROM notes")]:
                top = [
                    n
                    for (n,) in self._rows(
                        "SELECT path FROM notes WHERE tree=? AND enabled=1", tree
                    )
                ] + [
                    n for (n,) in self._rows("SELECT id FROM nodes WHERE tree=?", tree)
                ]
                tops = [n for n in top if not self.parents(n)[0]]
                ranked = sorted(
                    ((len(self.children(n)), n) for n in tops),
                    key=lambda x: (-x[0], x[1]),
                )
                found.append(
                    {
                        "tree": tree,
                        "top": [
                            {"id": n, "name": self.display_name(n), "narrower": count}
                            for count, n in ranked
                            if count
                        ],
                        "unplaced": sum(1 for count, _ in ranked if not count),
                    }
                )
            return found

    def find(self, text: str) -> str | None:
        """A concept by path, generated id or name."""
        with self._lock:
            if self._tree_of(text) is not None:
                return text
            key = norm(text)
            matches = {
                path
                for (path,) in self._rows(
                    "SELECT path FROM notes WHERE enabled=1 AND concept=1"
                )
                if key in {norm(name) for name in self.names(path)}
            }
            matches.update(
                node
                for (node,) in self._rows("SELECT id FROM nodes")
                if norm(self.display_name(node)) == key
            )
            return next(iter(matches)) if len(matches) == 1 else None

    def summary(self, path: str) -> dict[str, Any] | None:
        """Short tree facts for a search result or a draft."""
        with self._lock:
            note = self.note(path)
            if note is None:
                return None
            return {
                "name": note["name"] or self.display_name(path),
                "position": " > ".join(self.position(path)),
                "definition": note["definition"],
            }


class _Transaction:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock) -> None:
        self._db = db
        self._lock = lock
        self._outer = False

    def __enter__(self) -> None:
        self._lock.acquire()
        self._outer = not self._db.in_transaction
        if self._outer:
            self._db.execute("BEGIN IMMEDIATE")

    def __exit__(self, kind: object, *_: object) -> None:
        try:
            if self._outer:
                self._db.execute("ROLLBACK" if kind else "COMMIT")
        finally:
            self._lock.release()


def _normalize(path: str) -> str | None:
    parts: list[str] = []
    for part in PurePosixPath(path).parts:
        if part in {"", "."}:
            continue
        if part == "..":
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return "/".join(parts) or None


# ---------- running the model ----------


class KeywordBuilder:
    """Feeds queued notes and oversized concepts to the model, one at a time."""

    def __init__(
        self,
        graph: KeywordGraph,
        model: Asker,
        read: Callable[[str], tuple[str, str]],
        search: Callable[[str, int], list[str]],
        reindex: Callable[[set[str]], None],
    ) -> None:
        self.graph = graph
        self.model = model
        self.read = read
        self.search = search
        self.reindex = reindex
        self._resume_at = 0.0

    # cost: time O(t+n), heap O(n), stack O(1), io O(1), tokens O(n)
    # vars: t = model latency, n = note and candidate bytes
    # basis: estimate
    def step(self, is_cancelled: Callable[[], bool]) -> bool:
        """Do one piece of work. Returns False when nothing can be done now."""
        if time.monotonic() < self._resume_at:
            return False
        path = self.graph.next_note()
        if path is not None:
            self._extract(path, is_cancelled)
            return True
        node = self.graph.next_split()
        if node is not None:
            self._split(node, is_cancelled)
            return True
        return False

    def _extract(self, path: str, is_cancelled: Callable[[], bool]) -> None:
        try:
            text, digest = self.read(path)
        except FileNotFoundError:
            self.reindex(self.graph.forget(path))
            return
        except (OSError, HangerError) as error:
            self.graph.failed(path, f"cannot read the note: {error}")
            return
        note = self.graph.note(path)
        if note is None:
            return
        if note["hash"] != digest:
            self.reindex(self.graph.observe(path, text, digest) | {path})
            note = self.graph.note(path)
            if note is None:
                return
        body = plain_text(text)
        title = self.graph.display_name(path)
        query = f"{title} {body[:400]}"
        candidates = self.graph.candidates(
            path, self.search(query, CANDIDATES + 1), query
        )
        ids: dict[str, str] = {}
        for number, candidate in enumerate(c for c in candidates if "id" in c):
            short = f"c{number + 1}"
            ids[short] = candidate["id"]
            candidate["id"] = short
        try:
            prompt = extract_prompt(
                path=path,
                title=title,
                body=body[:6000],
                rules=self.graph.policy.rules([path]),
                candidates=candidates,
                explicit_values=self.graph.explicit(path),
            )
            answer = Extraction.model_validate(
                self.model.ask(prompt, Extraction, is_cancelled, "keywords")
            )
        except (ValidationError, HangerError) as error:
            if is_cancelled() or self._pause(error):
                return
            self.graph.failed(path, _reason(error))
            return
        problems = answer.problems()
        if problems:
            self.graph.failed(path, "; ".join(problems))
            return
        report = self.graph.apply(path, note["hash"], answer, ids)
        if not report.get("stale"):
            self.reindex({path})
            logger.info(
                "keywords: %s -> %s (new concepts: %s)",
                path,
                answer.name,
                ", ".join(report["created"]) or "none",
            )

    def _pause(self, error: Exception) -> bool:
        if not _transient(error):
            return False
        logger.warning("keywords: model unavailable, pausing: %s", error)
        self._resume_at = time.monotonic() + PAUSE_SECONDS
        return True

    def _split(self, node: str, is_cancelled: Callable[[], bool]) -> None:
        children = self.graph.split_input(node)
        ids = {f"c{n + 1}": child["id"] for n, child in enumerate(children)}
        for short, child in zip(ids, children, strict=True):
            child["id"] = short
        prompt = split_prompt(
            name=self.graph.display_name(node),
            definition=(self.graph.describe(node) or {}).get("definition", ""),
            children=children,
        )
        try:
            answer = Split.model_validate(
                self.model.ask(prompt, Split, is_cancelled, "keywords")
            )
        except (ValidationError, HangerError) as error:
            if is_cancelled() or self._pause(error):
                return
            logger.info("keywords: could not split %s: %s", node, _reason(error))
            self.graph.apply_split(node, Split(), {})
            return
        self.graph.apply_split(node, answer, ids)


def _transient(error: Exception) -> bool:
    """A failure that says nothing about the note itself."""
    if isinstance(error, ModelOutputError):
        return False
    if isinstance(error, ModelError):
        return True
    return isinstance(error, RequestError) and error.code == "model_missing"


def _reason(error: Exception) -> str:
    if isinstance(error, ValidationError):
        return "the model answer did not match the format"
    if isinstance(error, RequestError | HangerError):
        return str(error)
    return type(error).__name__
