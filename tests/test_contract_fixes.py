"""Gaps found by checking the tool contract against the code."""

import hashlib
import os
import threading
import time
from pathlib import Path

import pytest
from conftest import service_for
from test_review_fixes import Model, _edit, _finish, _runtime, closet  # noqa: F401

from hanger.closets import ClosetPolicy, register_closet
from hanger.config import Settings
from hanger.documents import Documents
from hanger.errors import RequestError
from hanger.hub import ClosetHub
from hanger.ignore import DEFAULT_IGNORE
from hanger.model import Intent
from hanger.requests import Change, Proposal
from hanger.rules import RULES_FILE
from hanger.runtime import Runtime
from hanger.search import SearchIndex
from hanger.service import Service
from hanger.store import StateStore
from hanger.sync import Synchronizer
from hanger.watcher import ClosetWatcher

_OFFLINE = "http://127.0.0.1:9"


def _service(
    root: Path, state: Path, policy: ClosetPolicy | None = None
) -> tuple[Service, Runtime]:
    policy = policy or ClosetPolicy(root)
    sync = Synchronizer(Documents(root), SearchIndex(_OFFLINE, str(root)), None, policy)
    runtime = Runtime(root, state)
    runtime.policy = policy
    return service_for(runtime, sync), runtime


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, ClosetPolicy]:
    """Ignores other/ and any private/ folder; inbox/ is read-only."""
    root = tmp_path / "closet"
    for folder in ("notes", "notes/private", "other", "inbox"):
        (root / folder).mkdir(parents=True)
    (root / "notes/a.md").write_text("# a")
    (root / "notes/private/s.md").write_text("# secret")
    (root / "other/c.md").write_text("# c")
    (root / "inbox" / RULES_FILE).write_text("---\nwrite: false\n---\n")
    policy = ClosetPolicy(root, DEFAULT_IGNORE + "other/\nprivate/\n")
    return root, tmp_path / "state", policy


# read_document honours the ignore rules.
def test_read_document_rejects_ignored(
    layout: tuple[Path, Path, ClosetPolicy],
) -> None:
    root, state, policy = layout
    service, runtime = _service(root, state, policy)
    with runtime:
        assert service.read_document("notes/a.md")["content"] == "# a"
        for path in ("other/c.md", "notes/private/s.md"):
            with pytest.raises(RequestError) as caught:
                service.read_document(path)
            assert caught.value.code == "excluded"


# A missing document is a clear not_found error without the absolute path.
def test_read_document_missing_is_not_found(
    layout: tuple[Path, Path, ClosetPolicy],
) -> None:
    root, state, policy = layout
    service, runtime = _service(root, state, policy)
    with runtime, pytest.raises(RequestError) as caught:
        service.read_document("notes/missing.md")
    assert caught.value.code == "not_found"
    assert str(root) not in str(caught.value)


# collect refuses to write where Hanger may not write, and bad Unicode.
def test_collect_respects_writable_and_rejects_bad_unicode(
    layout: tuple[Path, Path, ClosetPolicy],
) -> None:
    root, state, policy = layout
    service, runtime = _service(root, state, policy)
    with runtime:
        with pytest.raises(RequestError) as caught:
            service.collect(runtime.issue_key(), "memo")
        assert caught.value.code == "write_denied"
        with pytest.raises(RequestError, match="invalid Unicode"):
            service.collect(runtime.issue_key(), "a\ud800b")
    assert [p.name for p in (root / "inbox").iterdir()] == [RULES_FILE]


# Proposals submitted directly obey the same write policy as model edits.
def test_direct_proposal_outside_writable_is_rejected(
    layout: tuple[Path, Path, ClosetPolicy],
) -> None:
    root, state, policy = layout
    with (
        Runtime(root, state) as runtime,
        pytest.raises(RequestError) as caught,
    ):
        runtime.policy = policy
        runtime.submit(
            runtime.issue_key(),
            Proposal(changes=(Change(path="other/x.md", after="x"),)),
        )
    assert caught.value.code == "write_denied"
    assert not (root / "other/x.md").exists()


# A cancelled job that has not started reports cancelled, not queued.
def test_cancelled_queued_job_reports_cancelled(tmp_path: Path) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    with Runtime(root, tmp_path / "state") as runtime:
        key = runtime.issue_key()
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="x"),)))
        assert runtime.cancel(key)
        assert runtime.status(key)["status"] == "cancelled"


# Writing many files keeps the job history small: one step, not one per file.
def test_many_file_job_keeps_checkpoint_small(tmp_path: Path) -> None:
    root = tmp_path / "closet"
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
def test_rule_violation_is_redrafted(closet: tuple[Path, Path, str]) -> None:  # noqa: F811
    root, state, url = closet
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
    layout: tuple[Path, Path, ClosetPolicy], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, policy = layout
    sync = Synchronizer(Documents(root), SearchIndex(_OFFLINE, str(root)), None, policy)
    watcher = ClosetWatcher(sync)
    calls = 0

    def flaky(*_: object) -> None:
        nonlocal calls
        calls += 1
        raise ValueError("unexpected")

    monkeypatch.setattr(watcher, "flush", flaky)
    monkeypatch.setattr("hanger.watcher.ClosetWatcher.close", lambda self: None)
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
    root = tmp_path / "closet"
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
            assert f"retrying in 10s ({attempt}/3)" in message
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
        with pytest.raises(RequestError, match="get a new key"):
            runtime.reconsider(key, runtime.issue_key())


def _hidden(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith("."))


# A delete removes only the planned content and leaves nothing hidden behind.
def test_delete_is_verified_and_leaves_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hanger import documents as documents_module
    from hanger.errors import ConflictError
    from hanger.requests import digest

    root = tmp_path / "closet"
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
    rename(target, root / ".a.md.hanger-deleting")
    assert documents.apply(change) == "already_applied"
    assert not target.exists() and _hidden(root) == []

    # A crash after the rename of changed content: it is put back.
    (root / ".a.md.hanger-deleting").write_text("changed")
    with pytest.raises(ConflictError):
        documents.apply(change)
    assert target.read_text() == "changed" and _hidden(root) == []

    # Expired or failed jobs settle the same way.
    target.unlink()
    (root / ".a.md.hanger-deleting").write_text("planned")
    documents.discard_pending(Proposal(changes=(change,)))
    assert not target.exists() and _hidden(root) == []


# Listed search results are matched without reading their whole body.
def test_listed_results_are_not_loaded() -> None:
    from hanger.context import shape

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


# A search server Hanger runs keeps only the indexes of registered closets.
def test_owned_search_keeps_only_registered_indexes(tmp_path: Path) -> None:
    url = os.environ.get("HANGER_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "closet"
    root.mkdir()
    old = SearchIndex(url, str(tmp_path / "removed"))
    store = StateStore(tmp_path / "state")
    register_closet(store, root)
    hub = ClosetHub(store, Settings(), url, owns_search=True)
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


# Collected input is named time-title-hash by one rule for every kind of input.
@pytest.mark.parametrize(
    ("text", "title"),
    [
        ("# 결제 재시도 정책\n본문", "결제-재시도-정책"),
        ("메모 첫 줄입니다\n둘째 줄", "메모-첫-줄입니다"),
        ("https://www.example.com/a/b?c=1", "example.com-a-b-c=1"),
        ("사용자: 안녕\n어시스턴트: 반가워요", "사용자-안녕"),
        ("   \n\n", "untitled"),
        ("." * 3 + "숨김" + "가" * 60, "숨김" + "가" * 38),
    ],
)
def test_inbox_name_rule(text: str, title: str) -> None:
    from hanger.workflows import inbox_path

    issued = time.mktime((2026, 10, 16, 7, 31, 5, 0, 0, -1))
    path = inbox_path(text, issued)
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    assert path == f"inbox/2026-10-16-073105-{title}-{digest}.md"
    assert not Path(path).name.startswith(".")


# A resend with the same key lands on the same file.
def test_collect_resend_is_duplicate(tmp_path: Path) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    service, runtime = _service(root, tmp_path / "state")
    with runtime:
        key = runtime.issue_key()
        first = service.collect(key, "# 링크 모음\nhttps://example.com")
        second = service.collect(key, "# 링크 모음\nhttps://example.com")
        assert first["status"] == "accepted" and second["status"] == "duplicate"
        assert first["path"] == second["path"]
        assert first["path"].startswith("inbox/") and "-링크-모음-" in first["path"]
        _finish(runtime)
    assert (root / first["path"]).read_text() == "# 링크 모음\nhttps://example.com"


# Before creating a document, a similar existing one is found and offered.
def test_similar_document_is_checked_before_create(
    closet: tuple[Path, Path, str],  # noqa: F811
) -> None:
    root, state, url = closet
    (root / "notes").mkdir()
    (root / "notes/cat.md").write_text("# 고양이\n고양이 메모")
    (root / "docs/payment.md").write_text("# 결제 재시도\n결제 재시도는 2회다.")
    model = Model()
    model.decisions = [
        {
            "action": "new",
            "reason": "새 주제",
            "sources": [],
            "uncertainties": [],
            "changes": [
                {
                    "path": "docs/retry.md",
                    "operation": "create",
                    "content": "# 결제 재시도\n결제 재시도는 3회로 늘린다.",
                }
            ],
        },
        _edit("docs/payment.md", "결제 재시도는 2회다.", "결제 재시도는 3회다."),
    ]
    model.checks = [{"approved": True, "issues": []}]
    runtime, _ = _runtime(root, state, url, model)
    with runtime:
        key = runtime.issue_key()
        runtime.submit_intent(
            key,
            Intent(
                kind="write",
                instruction="재시도 3회 정리",
                query="고양이",
                folders=("notes",),
            ),
        )
        _finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert any("look like the one you are about to create" in p for p in model.prompts)
    assert not (root / "docs/retry.md").exists()
    assert "3회" in (root / "docs/payment.md").read_text()


# Any file is collected byte for byte, named time-filename-hash.extension.
def test_collect_any_file(tmp_path: Path) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    picture = tmp_path / "회의 사진 (1).PNG"
    data = bytes(range(256)) * 4000
    picture.write_bytes(data)
    service, runtime = _service(root, tmp_path / "state")
    with runtime:
        key = runtime.issue_key()
        result = service.collect(key, file=str(picture))
        assert result["status"] == "completed"
        digest = hashlib.sha256(data).hexdigest()
        assert result["path"].endswith(f"-회의-사진-(1)-{digest[:8]}.png")
        assert (root / result["path"]).read_bytes() == data
        assert service.collect(key, file=str(picture))["status"] == "duplicate"
        assert runtime.describe(key)["status"] == "completed"
        other = tmp_path / "other.pdf"
        other.write_bytes(b"%PDF-1.7")
        with pytest.raises(RequestError, match="already used for a different request"):
            service.collect(key, file=str(other))
        with pytest.raises(RequestError, match="absolute file path"):
            service.collect(runtime.issue_key(), file="other.pdf")
        with pytest.raises(RequestError) as missing:
            service.collect(runtime.issue_key(), file=str(tmp_path / "none.bin"))
        assert missing.value.code == "not_found"
        with pytest.raises(RequestError, match="exactly one of text or file"):
            service.collect(runtime.issue_key(), text="a", file=str(other))
    leftovers = [p.name for p in (root / "inbox").iterdir() if p.name.startswith(".")]
    assert leftovers == []


# A file whose content changes while it is copied leaves nothing behind.
def test_collect_file_changed_while_copying(tmp_path: Path) -> None:
    from hanger.errors import ConflictError

    root = tmp_path / "closet"
    root.mkdir()
    source = tmp_path / "a.bin"
    source.write_bytes(b"first")
    documents = Documents(root)
    with pytest.raises(ConflictError):
        documents.store_file(
            "inbox/x-a-00000000.bin", source, hashlib.sha256(b"other").hexdigest()
        )
    assert not (root / "inbox" / "x-a-00000000.bin").exists()
    assert [p.name for p in (root / "inbox").iterdir()] == []


# Collecting a file into a folder that is not writable is refused.
def test_collect_file_respects_writable(
    layout: tuple[Path, Path, ClosetPolicy],
) -> None:
    root, state, policy = layout
    source = root.parent / "a.txt"
    source.write_text("x")
    service, runtime = _service(root, state, policy)
    with runtime, pytest.raises(RequestError) as caught:
        service.collect(runtime.issue_key(), file=str(source))
    assert caught.value.code == "write_denied"


# A missing model command fails at once with a clear message, without retries.
def test_missing_model_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from hanger.model import CliModel
    from hanger.providers import ProviderMissingError

    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ProviderMissingError) as caught:
        CliModel().generate("prompt", lambda: False)
    assert caught.value.code == "provider_missing"
    assert not caught.value.retryable


# A finished or stopped job keeps one record, not its step history.
def test_finished_jobs_keep_one_record(tmp_path: Path) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    (root / "b.md").write_text("old")
    with Runtime(root, tmp_path / "state") as runtime:
        done = runtime.issue_key()
        runtime.submit(done, Proposal(changes=(Change(path="a.md", after="x"),)))
        stopped = runtime.issue_key()
        before = runtime.documents.read_hash("b.md")
        runtime.submit(
            stopped, Proposal(changes=(Change(path="b.md", before=before, after="y"),))
        )
        (root / "b.md").write_text("user")
        _finish(runtime)
        for key, status in ((done, "completed"), (stopped, "conflict")):
            record = runtime.status(key)
            assert record["status"] == status
            assert (
                len(list(runtime._saver.list({"configurable": {"thread_id": key}})))
                == 1
            )
        assert "proposal" not in runtime.status(done)
        assert "proposal" in runtime.status(stopped)  # still needed to reconsider
