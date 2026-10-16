"""Closets, ignore decisions, settings and the terminal commands around them."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest

from hanger.cli import _parser, main
from hanger.closets import ClosetPolicy, check_ignore, register_closet
from hanger.commands import serve_command
from hanger.config import Settings
from hanger.errors import RequestError
from hanger.hub import ClosetHub
from hanger.ignore import DEFAULT_IGNORE, IGNORE_FILE
from hanger.rules import DEFAULT_RULES, RULES_FILE
from hanger.service import Service
from hanger.store import StateStore

_OFFLINE = "http://127.0.0.1:9"


def _closet(root: Path) -> Path:
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
    root = _closet(tmp_path / "closet")
    store = StateStore(tmp_path / "state")
    result = register_closet(store, root)
    assert result["created"] == [IGNORE_FILE, RULES_FILE]
    assert (root / IGNORE_FILE).read_text() == DEFAULT_IGNORE
    assert (root / RULES_FILE).read_text() == DEFAULT_RULES
    other = tmp_path / "other"
    other.mkdir()
    (other / IGNORE_FILE).write_text("drafts/\n")
    assert register_closet(store, other)["created"] == [RULES_FILE]
    assert store.applied_ignore(2) == "drafts/\n"


# A folder inside, or containing, a registered closet is refused.
def test_nested_closets_are_refused(tmp_path: Path) -> None:
    root = _closet(tmp_path / "closet")
    store = StateStore(tmp_path / "state")
    register_closet(store, root)
    for path, words in (
        (root, "already a closet"),
        (root / "notes", "inside a registered closet"),
        (tmp_path, "contains"),
    ):
        with pytest.raises(RequestError, match=words):
            register_closet(store, path)
    assert len(store.closets()) == 1


# The default rules leave hidden folders and rule files out; write: false
# in a folder's rule file makes it read-only.
def test_policy_follows_ignore_and_rule_files(tmp_path: Path) -> None:
    root = _closet(tmp_path / "closet")
    (root / "private" / RULES_FILE).write_text("---\nwrite: false\n---\nKeep.\n")
    policy = ClosetPolicy(root)
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
    root = _closet(tmp_path / "closet")
    store = StateStore(tmp_path / "state")
    register_closet(store, root)
    (closet,) = store.closets()
    path = root / IGNORE_FILE
    path.write_text(DEFAULT_IGNORE + "# a comment\n")
    assert check_ignore(store, closet) is None
    assert store.applied_ignore(closet.id).endswith("# a comment\n")
    path.write_text(DEFAULT_IGNORE + "private/\n")
    first = check_ignore(store, closet)
    assert first is not None
    assert check_ignore(store, closet) == first
    decision = store.decision(first)
    assert decision is not None
    assert decision.detail["summary"]["files_leaving_index"] == 1
    path.write_text(DEFAULT_IGNORE + "notes/\n")
    second = check_ignore(store, closet)
    assert second != first
    assert store.decision(first).status == "withdrawn"  # type: ignore[union-attr]
    path.write_text(store.applied_ignore(closet.id))
    assert check_ignore(store, closet) is None
    assert not store.decisions()


# Terminal: answering in the terminal applies or restores the ignore file.
def test_decide_from_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _closet(tmp_path / "closet")
    assert _run(tmp_path, "closet", "add", str(root)) == 0
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
    root = _closet(tmp_path / "closet")
    _run(tmp_path, "closet", "add", str(root))
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


# Terminal: removing a closet asks first and leaves the notes alone.
def test_closet_remove(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _closet(tmp_path / "closet")
    _run(tmp_path, "closet", "add", str(root))
    assert _run(tmp_path, "closet", "remove", "1") == 1
    assert _run(tmp_path, "closet", "remove", str(root), "--yes") == 0
    assert not StateStore(tmp_path / "state").closets()
    assert (root / "notes/a.md").exists() and (root / IGNORE_FILE).exists()
    assert _run(tmp_path, "closet", "list") == 0
    assert "No closet yet" in capsys.readouterr().out


# setup stops when no model provider can be found.
def test_setup_needs_a_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    assert _run(tmp_path, "setup") == 1
    assert "Provider detection failed" in capsys.readouterr().err


# MCP: no closet means the client is told to ask for one; removal is refused.
def test_service_without_closets_and_removal(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    service = Service(ClosetHub(store, Settings(), _OFFLINE))
    assert "register_closet" in service.list_closets()["next"]
    with pytest.raises(RequestError) as caught:
        service.read_document("a.md")
    assert caught.value.code == "closet_required"
    root = _closet(tmp_path / "closet")
    service.register_closet(str(root))
    assert service.read_document("notes/a.md")["content"] == "# a"
    with pytest.raises(RequestError, match="hanger closet remove") as refused:
        service.remove_closet("1")
    assert refused.value.code == "terminal_only"
    service.hub.close()


# MCP: a pending decision blocks every request until the user answers.
def test_pending_decision_blocks_requests(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    root = _closet(tmp_path / "closet")
    service = Service(ClosetHub(store, Settings(), _OFFLINE))
    service.register_closet(str(root))
    (root / IGNORE_FILE).write_text(DEFAULT_IGNORE + "private/\n")
    for call in (service.issue_key, lambda: service.read_document("notes/a.md")):
        with pytest.raises(RequestError) as caught:
            call()
        assert caught.value.code == "decision_required"
        assert "translated into their language" in str(caught.value)
    (pending,) = service.list_decisions()["decisions"]
    service.decide(pending["id"], "approve")
    assert service.read_document("notes/a.md")["path"] == "notes/a.md"
    opened = service.hub.get(None)
    assert opened.policy.excluded("private/b.md")
    service.hub.close()


# Two closets: requests must say which one.
def test_several_closets_need_a_choice(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    first, second = _closet(tmp_path / "one"), _closet(tmp_path / "two")
    service = Service(ClosetHub(store, Settings(), _OFFLINE))
    service.register_closet(str(first))
    service.register_closet(str(second))
    with pytest.raises(RequestError) as caught:
        service.read_document("notes/a.md")
    assert caught.value.code == "closet_ambiguous"
    inside = str(second / "notes")
    assert service.read_document("notes/a.md", closet=inside)["path"] == "notes/a.md"
    assert service.hub.get("2").root == second.resolve()
    service.hub.close()


# The printed MCP command is one the CLI accepts, including a custom state.
def test_setup_prints_runnable_serve_command(tmp_path: Path) -> None:
    words = shlex.split(serve_command(tmp_path / "state"))
    arguments = _parser().parse_args(words[1:])
    assert arguments.command == "serve"
    assert arguments.state == tmp_path / "state"


# A key used in one closet cannot start a job in another.
def test_key_is_not_reused_across_closets(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state")
    service = Service(ClosetHub(store, Settings(), _OFFLINE))
    for name in ("one", "two"):
        service.register_closet(str(_closet(tmp_path / name)))
    key = service.issue_key()
    assert service.collect(key, "memo", closet="1")["status"] == "accepted"
    with pytest.raises(RequestError) as caught:
        service.collect(key, "memo", closet="2")
    assert caught.value.code == "key_reused"
    service.hub.close()


# A missing folder withdraws its question and is reported; it comes back later.
def test_missing_folder_withdraws_its_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state")
    root = _closet(tmp_path / "closet")
    hub = ClosetHub(store, Settings(), _OFFLINE)
    service = Service(hub)
    service.register_closet(str(root))
    (root / IGNORE_FILE).write_text(DEFAULT_IGNORE + "private/\n")
    assert service.list_decisions()["decisions"]
    moved = tmp_path / "moved"
    root.rename(moved)
    assert service.list_decisions()["decisions"] == []
    root.mkdir()  # an empty mount point counts as missing too
    monkeypatch.setattr("hanger.hub._RETRY_SECONDS", 0)
    hub._close(hub._open.pop(1))  # as if the folder vanished before opening
    hub._broken[1] = "gone"
    with pytest.raises(RequestError) as caught:
        service.read_document("notes/a.md")
    assert caught.value.code == "closet_unavailable"
    assert "hanger closet remove 1" in str(caught.value)
    root.rmdir()
    moved.rename(root)
    with pytest.raises(RequestError) as waiting:
        service.read_document("notes/a.md")
    assert waiting.value.code == "decision_required"
    hub.close()
