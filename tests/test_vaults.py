"""Vaults, ignore decisions, settings and the terminal commands around them."""

from __future__ import annotations

import os
import shlex
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from markdown_notes_agent.cli import _parser, main
from markdown_notes_agent.commands import serve_command
from markdown_notes_agent.config import Settings
from markdown_notes_agent.documents import Documents
from markdown_notes_agent.editing import Decision, materialize
from markdown_notes_agent.errors import RequestError
from markdown_notes_agent.hub import VaultHub
from markdown_notes_agent.ignore import DEFAULT_IGNORE, IGNORE_FILE
from markdown_notes_agent.rules import DEFAULT_RULES, RULES_FILE
from markdown_notes_agent.search import SearchIndex
from markdown_notes_agent.service import Service
from markdown_notes_agent.store import StateStore
from markdown_notes_agent.vaults import (
    VaultPolicy,
    check_ignore,
    register_vault,
    resolve_decision,
)

_OFFLINE = "http://127.0.0.1:9"


def _vault(root: Path) -> Path:
    for folder in ("notes", "private", ".obsidian"):
        (root / folder).mkdir(parents=True)
    (root / "notes/a.md").write_text("# a")
    (root / "private/b.md").write_text("# b")
    (root / ".obsidian/c.md").write_text("# c")
    return root


def _run(tmp_path: Path, *words: str) -> int:
    return main(["--state", str(tmp_path / "state"), *words])


# Registering creates the ignore and rule files, and keeps existing ones.
def test_register_creates_default_files(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    store = StateStore(tmp_path / "state")
    result = register_vault(store, root)
    assert result["created"] == [IGNORE_FILE, RULES_FILE]
    assert (root / IGNORE_FILE).read_text() == DEFAULT_IGNORE
    assert (root / RULES_FILE).read_text() == DEFAULT_RULES
    other = tmp_path / "other"
    other.mkdir()
    (other / IGNORE_FILE).write_text("drafts/\n")
    assert register_vault(store, other)["created"] == [RULES_FILE]
    assert store.applied_ignore(2) == "drafts/\n"


# A folder inside, or containing, a registered vault is refused.
def test_nested_vaults_are_refused(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    store = StateStore(tmp_path / "state")
    register_vault(store, root)
    for path, words in (
        (root, "already a vault"),
        (root / "notes", "inside a registered vault"),
        (tmp_path, "contains"),
    ):
        with pytest.raises(RequestError, match=words):
            register_vault(store, path)
    assert len(store.vaults()) == 1


def test_policy_follows_ignore_and_rule_files(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    (root / "private" / RULES_FILE).write_text("---\nwrite: false\n---\nKeep.\n")
    assert not VaultPolicy(root).excluded(".obsidian/c.md")
    policy = VaultPolicy(root, ".obsidian/\n")
    assert policy.excluded(".obsidian/c.md")
    assert policy.excluded(RULES_FILE)
    assert not policy.excluded("notes/a.md")
    assert policy.writable("notes/new.md")
    assert not policy.writable("private/new.md")
    rules = policy.rules(["private/b.md"])
    assert [rule["file"] for rule in rules] == [f"private/{RULES_FILE}"]
    policy.update("notes/\n")
    assert policy.excluded("notes/a.md")
    assert not policy.excluded(".obsidian/c.md")


# An edited ignore file is a question; comment-only edits apply at once.
def test_ignore_edits_wait_for_a_decision(tmp_path: Path) -> None:
    root = _vault(tmp_path / "vault")
    store = StateStore(tmp_path / "state")
    register_vault(store, root)
    (vault,) = store.vaults()
    path = root / IGNORE_FILE
    path.write_text(DEFAULT_IGNORE + "# a comment\n")
    assert check_ignore(store, vault) is None
    assert store.applied_ignore(vault.id).endswith("# a comment\n")
    path.write_text(DEFAULT_IGNORE + "private/\n")
    first = check_ignore(store, vault)
    assert first is not None
    assert check_ignore(store, vault) == first
    decision = store.decision(first)
    assert decision is not None
    assert decision.detail["summary"]["files_leaving_index"] == 1
    path.write_text(DEFAULT_IGNORE + "notes/\n")
    second = check_ignore(store, vault)
    assert second != first
    assert store.decision(first).status == "withdrawn"  # type: ignore[union-attr]
    path.write_text(store.applied_ignore(vault.id))
    assert check_ignore(store, vault) is None
    assert not store.decisions()


# Terminal: answering in the terminal applies or restores the ignore file.
def test_decide_from_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _vault(tmp_path / "vault")
    assert _run(tmp_path, "vault", "add", str(root)) == 0
    path = root / IGNORE_FILE
    path.write_text(DEFAULT_IGNORE + "private/\n")
    assert _run(tmp_path, "decisions") == 0
    out = capsys.readouterr().out
    assert "Decision #1" in out and "leaves the index: private/b.md" in out
    assert _run(tmp_path, "decide", "1", "decline") == 0
    assert path.read_text() == DEFAULT_IGNORE
    path.unlink()
    assert _run(tmp_path, "decisions") == 0
    assert "was deleted" in capsys.readouterr().out
    assert _run(tmp_path, "decide", "2", "approve") == 0
    assert path.read_text() == DEFAULT_IGNORE
    assert _run(tmp_path, "decide", "2", "approve") == 1
    assert "already answered" in capsys.readouterr().err


# Terminal: ignore add asks first; --yes approves and applies at once.
def test_ignore_add_and_remove(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _vault(tmp_path / "vault")
    _run(tmp_path, "vault", "add", str(root))
    assert _run(tmp_path, "ignore", "add", "private/") == 1
    assert "--yes" in capsys.readouterr().err
    assert (root / IGNORE_FILE).read_text() == DEFAULT_IGNORE
    assert _run(tmp_path, "ignore", "add", "private/", "--yes") == 0
    store = StateStore(tmp_path / "state")
    assert store.applied_ignore(1).endswith("private/\n")
    assert _run(tmp_path, "ignore", "remove", "private/", "--yes") == 0
    assert store.applied_ignore(1) == DEFAULT_IGNORE
    assert _run(tmp_path, "ignore", "remove", "nothing/", "--yes") == 1


# Terminal: settings are listed, changed, validated and reset.
def test_config_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _run(tmp_path, "config", "set", "search.candidates", "30") == 0
    assert _run(tmp_path, "config", "get", "search.candidates") == 0
    assert capsys.readouterr().out.splitlines()[-1] == "30"
    assert _run(tmp_path, "config", "set", "model.provider", "codex") == 0
    assert _run(tmp_path, "config", "set", "search.candidates", "0") == 1
    assert "Invalid value for search.candidates" in capsys.readouterr().err
    assert _run(tmp_path, "config", "set", "no.such", "1") == 1
    assert _run(tmp_path, "config", "list") == 0
    out = capsys.readouterr().out
    assert 'model.provider = "codex"  (changed)' in out
    assert _run(tmp_path, "config", "unset", "search.candidates") == 0
    assert StateStore(tmp_path / "state").settings().search.candidates == 20


# Terminal: removing a vault asks first and leaves the notes alone.
def test_vault_remove(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _vault(tmp_path / "vault")
    _run(tmp_path, "vault", "add", str(root))
    assert _run(tmp_path, "vault", "remove", "1") == 1
    assert _run(tmp_path, "vault", "remove", str(root), "--yes") == 0
    assert not StateStore(tmp_path / "state").vaults()
    assert (root / "notes/a.md").exists() and (root / IGNORE_FILE).exists()
    assert _run(tmp_path, "vault", "list") == 0
    assert "No vault yet" in capsys.readouterr().out


# setup stops when no model provider can be found.
def test_setup_needs_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _run(tmp_path, "setup") == 1
    assert "Provider detection failed" in capsys.readouterr().err


# MCP: no vault means the client is told to ask for one; removal is refused.
def test_service_without_vaults_and_removal(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    service = Service(VaultHub(store, Settings(), _OFFLINE))
    assert "register_vault" in service.list_vaults()["next"]
    with pytest.raises(RequestError) as caught:
        service.read_document("a.md")
    assert caught.value.code == "vault_required"
    root = _vault(tmp_path / "vault")
    service.register_vault(str(root))
    assert service.read_document("notes/a.md")["content"] == "# a"
    with pytest.raises(
        RequestError, match="markdown-notes-agent vault remove"
    ) as refused:
        service.remove_vault("1")
    assert refused.value.code == "terminal_only"
    service.hub.close()


# MCP: a pending decision blocks every request until the user answers.
def test_pending_decision_blocks_requests(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    root = _vault(tmp_path / "vault")
    service = Service(VaultHub(store, Settings(), _OFFLINE))
    service.register_vault(str(root))
    (root / IGNORE_FILE).write_text(DEFAULT_IGNORE + "private/\n")
    for call in (service.issue_key, lambda: service.read_document("notes/a.md")):
        with pytest.raises(RequestError) as caught:
            call()
        assert caught.value.code == "decision_required"
        assert str(caught.value).startswith(
            "Tell the user: Markdown Notes Agent is waiting"
        )
    (pending,) = service.list_decisions()["decisions"]
    service.decide(pending["id"], "approve")
    assert service.read_document("notes/a.md")["path"] == "notes/a.md"
    opened = service.hub.get(None)
    assert opened.policy.excluded("private/b.md")
    service.hub.close()


# Two vaults: requests must say which one.
def test_several_vaults_need_a_choice(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first, second = _vault(tmp_path / "one"), _vault(tmp_path / "two")
    service = Service(VaultHub(store, Settings(), _OFFLINE))
    service.register_vault(str(first))
    service.register_vault(str(second))
    with pytest.raises(RequestError) as caught:
        service.read_document("notes/a.md")
    assert caught.value.code == "vault_ambiguous"
    inside = str(second / "notes")
    assert service.read_document("notes/a.md", vault=inside)["path"] == "notes/a.md"
    assert service.hub.get("2").root == second.resolve()
    service.hub.close()


# The printed MCP command is one the CLI accepts, including a custom state.
def test_setup_prints_runnable_serve_command(tmp_path: Path) -> None:
    words = shlex.split(serve_command(tmp_path / "state"))
    arguments = _parser().parse_args(words[1:])
    assert arguments.command == "serve"
    assert arguments.state == tmp_path / "state"


# A key used in one vault cannot start a job in another.
def test_key_is_not_reused_across_vaults(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    service = Service(VaultHub(store, Settings(), _OFFLINE))
    for name in ("one", "two"):
        service.register_vault(str(_vault(tmp_path / name)))
    key = service.issue_key()
    assert service.collect(key, "memo", vault="1")["status"] == "accepted"
    with pytest.raises(RequestError) as caught:
        service.collect(key, "memo", vault="2")
    assert caught.value.code == "key_reused"
    service.hub.close()


# A missing folder withdraws its question and is reported; it comes back later.
def test_missing_folder_withdraws_its_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state")
    root = _vault(tmp_path / "vault")
    hub = VaultHub(store, Settings(), _OFFLINE)
    service = Service(hub)
    service.register_vault(str(root))
    (root / IGNORE_FILE).write_text(DEFAULT_IGNORE + "private/\n")
    assert service.list_decisions()["decisions"]
    moved = tmp_path / "moved"
    root.rename(moved)
    assert service.list_decisions()["decisions"] == []
    root.mkdir()  # an empty mount point counts as missing too
    monkeypatch.setattr("markdown_notes_agent.hub._RETRY_SECONDS", 0)
    hub._close(hub._open.pop(1))  # as if the folder vanished before opening
    hub._broken[1] = "gone"
    with pytest.raises(RequestError) as caught:
        service.read_document("notes/a.md")
    assert caught.value.code == "vault_unavailable"
    assert "markdown-notes-agent vault remove 1" in str(caught.value)
    root.rmdir()
    moved.rename(root)
    with pytest.raises(RequestError) as waiting:
        service.read_document("notes/a.md")
    assert waiting.value.code == "decision_required"
    hub.close()


# Rule files are not citable; any spelling of the request becomes "request",
# whose text can back a change; prompts carry the time for file names.
def test_request_text_is_a_source_and_rules_are_not(tmp_path: Path) -> None:
    from markdown_notes_agent.prompts import draft_prompt
    from markdown_notes_agent.workflows import REQUEST_SOURCE, _tidy_citations

    decision = _tidy_citations(
        Decision.model_validate(
            {
                "action": "new",
                "reason": "memo",
                "sources": [
                    "request.instruction",
                    "VAULT.md",
                    "docs/VAULT.md",
                ],
                "uncertainties": [],
                "evidence": [{"path": "request.instruction", "quote": "keep-alive"}],
                "changes": [
                    {"path": "a.md", "operation": "create", "content": "# keep-alive"}
                ],
            }
        )
    )
    assert decision.sources == [REQUEST_SOURCE]
    given = {REQUEST_SOURCE: {"content": "memo about keep-alive", "hash": ""}}
    proposal = materialize(decision, given, Documents(tmp_path))
    assert proposal["changes"][0]["path"] == "a.md"
    prompt = draft_prompt(
        {"request": {"profile": "note", "instruction": "x"}, "documents": {}}
    )
    assert '"now_for_file_names":"' in prompt
    assert "never list a VAULT.md file" not in prompt


def test_owned_search_keeps_only_registered_indexes(tmp_path: Path) -> None:
    url = os.environ.get("MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    root.mkdir()
    old = SearchIndex(url, str(tmp_path / "removed"))
    store = StateStore(tmp_path / "state")
    register_vault(store, root)
    hub = VaultHub(store, Settings(), url, owns_search=True)
    current = SearchIndex(url, str(root.resolve()))
    try:
        old.ensure()
        current.ensure()
        hub.refresh()
        assert not old.client.indices.exists(index=old.name)
        assert current.client.indices.exists(index=current.name)
    finally:
        hub.close()
        for index in (old, current):
            index.delete()
            index.close()


def test_terminal_shows_only_the_user_part() -> None:
    from markdown_notes_agent.errors import for_terminal, guidance

    assert for_terminal(guidance("Pick a vault.", "Call decide.")) == "Pick a vault."
    assert for_terminal("Plain message.") == "Plain message."


def test_answer_for_removed_vault_is_not_found(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    root = tmp_path / "notes"
    root.mkdir()
    (root / "a.md").write_text("# a")
    vault = register_vault(store, root)["vault"]
    (root / ".markdown-notes-agentignore").write_text("drafts/\n")
    decision = check_ignore(store, store.vaults()[0])
    assert decision is not None
    store.remove_vault(vault["id"])
    with pytest.raises(RequestError) as caught:
        resolve_decision(store, decision, "approve")
    assert caught.value.code in {"not_found", "decision_withdrawn", "decision_closed"}


def test_vault_catalog_keeps_identity_and_decisions_after_reopen(
    tmp_path: Path,
) -> None:
    root = _vault(tmp_path / "notes")
    state = tmp_path / "state"
    store = StateStore(state)
    registered = register_vault(store, root)["vault"]
    store.set_applied_ignore(registered["id"], "private/\n")
    decision = store.open_decision(registered["id"], "ignore", {"reason": "preserve"})
    reopened = StateStore(state)
    assert reopened.vaults()[0].as_dict() == registered
    assert reopened.applied_ignore(registered["id"]) == "private/\n"
    pending = reopened.decision(decision)
    assert pending is not None
    assert pending.vault_id == registered["id"]
    assert pending.detail == {"reason": "preserve"}


def test_prior_state_schema_requires_explicit_migration(tmp_path: Path) -> None:
    path = tmp_path / "markdown-notes-agent.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE retained (value TEXT)")
        connection.execute("INSERT INTO retained VALUES ('unchanged')")
        connection.execute("PRAGMA user_version = 1")
    with pytest.raises(RequestError) as caught:
        StateStore(tmp_path)
    assert caught.value.code == "state_version"
    with closing(sqlite3.connect(path)) as connection, connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM retained").fetchone() == (
            "unchanged",
        )
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall() == [("retained",)]
