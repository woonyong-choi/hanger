"""Gaps found by checking the tool contract against the code."""

import os
import threading
import time
from pathlib import Path

import pytest
from test_review_fixes import Model, _edit, _finish, _runtime, vault  # noqa: F401

from markchain.config import Settings
from markchain.documents import Documents
from markchain.errors import RequestError
from markchain.model import Intent
from markchain.requests import Change, Proposal
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.service import Service
from markchain.sync import Synchronizer
from markchain.watcher import VaultWatcher

_OFFLINE = "http://127.0.0.1:9"


def _service(root: Path, state: Path, settings: Settings) -> tuple[Service, Runtime]:
    sync = Synchronizer(Documents(root), SearchIndex(_OFFLINE, str(root)), settings)
    runtime = Runtime(root, state, settings=settings)
    return Service(runtime, sync), runtime


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, Settings]:
    root = tmp_path / "vault"
    for folder in ("notes", "notes/private", "other", "inbox"):
        (root / folder).mkdir(parents=True)
    (root / "notes/a.md").write_text("# a")
    (root / "notes/private/s.md").write_text("# secret")
    (root / "other/c.md").write_text("# c")
    settings = Settings.model_validate(
        {"vault": {"managed": ["notes"], "exclude": ["private"]}}
    )
    return root, tmp_path / "state", settings


# read_document honours managed and excluded folders.
def test_read_document_rejects_unmanaged_and_excluded(
    layout: tuple[Path, Path, Settings],
) -> None:
    root, state, settings = layout
    service, runtime = _service(root, state, settings)
    with runtime:
        assert service.read_document("notes/a.md")["content"] == "# a"
        for path in ("other/c.md", "notes/private/s.md"):
            with pytest.raises(RequestError) as caught:
                service.read_document(path)
            assert caught.value.code == "excluded"


# A missing document is a clear not_found error without the absolute path.
def test_read_document_missing_is_not_found(
    layout: tuple[Path, Path, Settings],
) -> None:
    root, state, settings = layout
    service, runtime = _service(root, state, settings)
    with runtime, pytest.raises(RequestError) as caught:
        service.read_document("notes/missing.md")
    assert caught.value.code == "not_found"
    assert str(root) not in str(caught.value)


# collect refuses to write where Markchain may not write, and bad Unicode.
def test_collect_respects_writable_and_rejects_bad_unicode(
    layout: tuple[Path, Path, Settings],
) -> None:
    root, state, settings = layout
    service, runtime = _service(root, state, settings)
    with runtime:
        with pytest.raises(RequestError) as caught:
            service.collect(runtime.issue_key(), "memo")
        assert caught.value.code == "write_denied"
        with pytest.raises(RequestError, match="유니코드"):
            service.collect(runtime.issue_key(), "a\ud800b")
    assert not any((root / "inbox").iterdir())


# Proposals submitted directly obey the same write policy as model edits.
def test_direct_proposal_outside_writable_is_rejected(
    layout: tuple[Path, Path, Settings],
) -> None:
    root, state, settings = layout
    with (
        Runtime(root, state, settings=settings) as runtime,
        pytest.raises(RequestError) as caught,
    ):
        runtime.submit(
            runtime.issue_key(),
            Proposal(changes=(Change(path="other/x.md", after="x"),)),
        )
    assert caught.value.code == "write_denied"
    assert not (root / "other/x.md").exists()


# A cancelled job that has not started reports cancelled, not queued.
def test_cancelled_queued_job_reports_cancelled(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    with Runtime(root, tmp_path / "state") as runtime:
        key = runtime.issue_key()
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="x"),)))
        assert runtime.cancel(key)
        assert runtime.status(key)["status"] == "cancelled"


# Writing many files keeps the job history small: one step, not one per file.
def test_many_file_job_keeps_checkpoint_small(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    body = "x" * 9000
    proposal = Proposal(
        changes=tuple(Change(path=f"d/{n}.md", after=body) for n in range(200))
    )
    state = tmp_path / "state"
    with Runtime(root, state) as runtime:
        runtime.submit(runtime.issue_key(), proposal)
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
    size = sum(p.stat().st_size for p in state.glob("graph.sqlite*"))
    assert size < 40 * 1024 * 1024, size


# An edit that breaks a rule (ambiguous anchor) is redrafted, not failed.
def test_rule_violation_is_redrafted(vault: tuple[Path, Path, str]) -> None:  # noqa: F811
    root, state, url = vault
    (root / "docs/a.md").write_text("# 결제\n중복\n중복\n")
    model = Model()
    model.decisions = [
        _edit("docs/a.md", "중복", "고유"),
        _edit("docs/a.md", "# 결제", "# 결제 정책"),
    ]
    model.checks = [{"approved": True, "issues": []}]
    runtime, _ = _runtime(root, state, url, model)
    with runtime:
        key = runtime.issue_key()
        runtime.submit_intent(
            key,
            Intent(
                kind="edit", instruction="제목 수정", query="결제", paths=("docs/a.md",)
            ),
        )
        _finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert (root / "docs/a.md").read_text().startswith("# 결제 정책")
    assert any("정확히 한 번" in prompt for prompt in model.prompts)


# The watcher keeps running after an unexpected error.
def test_watcher_survives_unexpected_error(
    layout: tuple[Path, Path, Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, settings = layout
    sync = Synchronizer(Documents(root), SearchIndex(_OFFLINE, str(root)), settings)
    watcher = VaultWatcher(sync)
    calls = 0

    def flaky(*_: object) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("unexpected")

    monkeypatch.setattr(watcher, "flush", flaky)
    monkeypatch.setattr("markchain.watcher.VaultWatcher.close", lambda self: None)
    stop = threading.Event()
    thread = threading.Thread(target=watcher.run, args=(stop,), daemon=True)
    monkeypatch.setattr(stop, "wait", lambda seconds: time.sleep(0.05) or False)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive()
    assert calls >= 2
    stop.set()
    monkeypatch.undo()
    thread.join(timeout=5)


# While a job waits to retry, job_status says so; a failed job keeps no work data.
def test_retry_is_visible_and_failure_leaves_no_work_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)

    def broken(documents: Documents, change: Change) -> str:
        raise OSError("disk unavailable")

    monkeypatch.setattr(Documents, "apply", broken)
    with Runtime(root, tmp_path / "state") as runtime:
        key = runtime.issue_key()
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="x"),)))
        for attempt in range(1, 4):
            with pytest.raises(OSError):
                runtime.run_next()
            message = runtime.describe(key)["message"]
            assert f"10초 후 다시 시도합니다 ({attempt}/3)" in message
            clock += 11
        result = runtime.run_next()
        assert result is not None and result["status"] == "failed"
        status = runtime.describe(key)
        assert {"proposal", "intent", "search"}.isdisjoint(status)
        assert [action["tool"] for action in status["next_actions"]] == [
            "issue_key",
            "request_change",
        ]
        assert (
            runtime.submit(key, Proposal(changes=(Change(path="a.md", after="x"),)))
            == "duplicate"
        )
        with pytest.raises(RequestError, match="새 키를 발급받아"):
            runtime.reconsider(key, runtime.issue_key())


def _hidden(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith("."))


# A delete removes only the planned content and leaves nothing hidden behind.
def test_delete_is_verified_and_leaves_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from markchain import documents as documents_module
    from markchain.errors import ConflictError
    from markchain.requests import digest

    root = tmp_path / "vault"
    root.mkdir()
    documents = Documents(root)
    target = root / "a.md"

    target.write_text("planned")
    change = Change(path="a.md", before=digest(b"planned"), after=None)
    assert documents.apply(change) == "applied"
    assert not target.exists() and _hidden(root) == []
    assert documents.apply(change) == "already_applied"

    # Someone saves the file between the check and the rename: it is restored.
    target.write_text("planned")
    rename = os.rename

    def save_then_rename(source: object, destination: object) -> None:
        if Path(str(source)) == target:
            target.write_text("saved by the user")
        rename(source, destination)  # type: ignore[arg-type]

    monkeypatch.setattr(documents_module.os, "rename", save_then_rename)
    with pytest.raises(ConflictError):
        documents.apply(change)
    monkeypatch.undo()
    assert target.read_text() == "saved by the user" and _hidden(root) == []

    # A crash after the rename: the next attempt finishes the delete.
    target.write_text("planned")
    rename(target, root / ".a.md.markchain-deleting")
    assert documents.apply(change) == "already_applied"
    assert not target.exists() and _hidden(root) == []

    # A crash after the rename of changed content: it is put back.
    (root / ".a.md.markchain-deleting").write_text("changed")
    with pytest.raises(ConflictError):
        documents.apply(change)
    assert target.read_text() == "changed" and _hidden(root) == []

    # Expired or failed jobs settle the same way.
    target.unlink()
    (root / ".a.md.markchain-deleting").write_text("planned")
    documents.discard_pending(Proposal(changes=(change,)))
    assert not target.exists() and _hidden(root) == []


# Listed search results are matched without reading their whole body.
def test_listed_results_are_not_loaded() -> None:
    from markchain.context import shape

    settings = Settings.model_validate({"search": {"full_text": 1}})
    ranked = [
        {"path": "a.md", "title": "a", "bytes": 10},
        {"path": "b.md", "title": "b", "bytes": 10},
    ]
    loaded: list[str] = []

    def load(path: str) -> dict[str, str]:
        loaded.append(path)
        return {"path": path, "title": path, "hash": "h", "content": "결제 정책"}

    full, listed = shape(
        ranked, {}, settings, ["결제"], load=load, peek=lambda p, t: ["결제 줄"]
    )
    assert loaded == ["a.md"] and list(full) == ["a.md"]
    assert listed == [
        {"path": "b.md", "title": "b", "bytes": 10, "snippets": ["결제 줄"]}
    ]


# The managed engine keeps only the current vault's index.
def test_owned_index_removes_other_vault_indexes(tmp_path: Path) -> None:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    old = SearchIndex(url, str(tmp_path / "old"))
    new = SearchIndex(url, str(tmp_path / "new"), owned=True)
    try:
        old.ensure()
        new.ensure()
        assert not old.client.indices.exists(index=old.name)
        assert new.client.indices.exists(index=new.name)
    finally:
        for index in (old, new):
            index.client.options(ignore_status=404).indices.delete(index=index.name)
            index.close()
