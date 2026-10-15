"""Gaps found by checking the tool contract against the code."""

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


# Overwrite skips files already in the proposed state, and refuses a no-op.
def test_overwrite_skips_applied_changes(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("old a")
    (root / "b.md").write_text("old b")
    service, runtime = _service(root, tmp_path / "state", Settings())
    with runtime:
        key = runtime.issue_key()
        proposal = Proposal(
            changes=(
                Change(
                    path="a.md",
                    before=runtime.documents.read_hash("a.md"),
                    after="new a",
                ),
                Change(
                    path="b.md",
                    before=runtime.documents.read_hash("b.md"),
                    after=None,
                ),
            )
        )
        runtime.submit(key, proposal)
        (root / "a.md").write_text("user a")
        result = runtime.run_next()
        assert result is not None and result["status"] == "conflict"
        (root / "b.md").unlink()  # the delete already happened elsewhere
        new_key = runtime.issue_key()
        assert service.resolve_conflict(key, new_key, "overwrite")["status"] == (
            "accepted"
        )
        _finish(runtime)
        assert (root / "a.md").read_text() == "new a"


# Overwrite of a proposal that is already fully in place is refused.
def test_overwrite_of_applied_proposal_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("old a")
    service, runtime = _service(root, tmp_path / "state", Settings())
    with runtime:
        key = runtime.issue_key()
        before = runtime.documents.read_hash("a.md")
        runtime.submit(
            key, Proposal(changes=(Change(path="a.md", before=before, after="new"),))
        )
        (root / "a.md").write_text("user")
        result = runtime.run_next()
        assert result is not None and result["status"] == "conflict"
        (root / "a.md").write_text("new")
        with pytest.raises(RequestError) as caught:
            service.resolve_conflict(key, runtime.issue_key(), "overwrite")
    assert caught.value.code == "already_applied"


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
    assert any("exactly once" in prompt for prompt in model.prompts)


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
