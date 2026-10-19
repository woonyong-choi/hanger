import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import finish

from markdown_notes_agent.config import Settings
from markdown_notes_agent.documents import Documents
from markdown_notes_agent.errors import RequestError
from markdown_notes_agent.hub import VaultHub
from markdown_notes_agent.requests import Change, Proposal, digest, issue_key
from markdown_notes_agent.runtime import Runtime
from markdown_notes_agent.store import StateStore
from markdown_notes_agent.vaults import register_vault


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault, tmp_path / "state"


def test_delayed_submission_expires_from_key_issuance(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    issued = time.time()
    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        monkeypatch.setattr(time, "time", lambda: issued + 23 * 3600)
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="result"),)))
        monkeypatch.setattr(time, "time", lambda: issued + 25 * 3600)
        result = runtime.run_next()
        assert result is not None and result["status"] == "expired"
        assert not (paths[0] / "a.md").exists()


# simultaneous resubmission must not duplicate a document operation.
def test_submit_concurrent_request_returns_one_acceptance(
    paths: tuple[Path, Path],
) -> None:
    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        proposal = Proposal(changes=(Change(path="docs/a.md", after="first"),))
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(
                workers.map(lambda _: runtime.submit(key, proposal), range(8))
            )
        assert results.count("accepted") == 1
        assert results.count("duplicate") == 7
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
        assert runtime.run_next() is None


# same key with different content is an error, including after restart.
def test_submit_completed_key_survives_restart(paths: tuple[Path, Path]) -> None:
    proposal = Proposal(changes=(Change(path="a.md", after="first"),))
    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        runtime.submit(key, proposal)
        runtime.run_next()

    with Runtime(*paths) as runtime:
        assert runtime.submit(key, proposal) == "duplicate"
        changed = Proposal(changes=(Change(path="a.md", after="second"),))
        with pytest.raises(RequestError, match="already used for a different request"):
            runtime.submit(key, changed)


# cancellation persists without changing the document.
def test_cancel_before_restart_does_not_write(paths: tuple[Path, Path]) -> None:
    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="first"),)))
        assert runtime.cancel(key)

    with Runtime(*paths) as runtime:
        result = runtime.run_next()
        assert result is not None and result["status"] == "cancelled"
        assert not (paths[0] / "a.md").exists()


# a changed source must remain intact rather than receive a stale proposal.
def test_apply_modified_source_stops_without_overwrite(
    paths: tuple[Path, Path],
) -> None:
    target = paths[0] / "a.md"
    target.write_text("original")
    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        change = Change(path="a.md", before=digest(b"original"), after="proposal")
        runtime.submit(key, Proposal(changes=(change,)))
        target.write_text("user change")
        result = runtime.run_next()
        assert result is not None and result["status"] == "conflict"
        assert target.read_text() == "user change"
        status = runtime.describe(key)
        tools = [action["tool"] for action in status["next_actions"]]
        assert "request_change" in tools
        assert "resolve_conflict" not in tools
        with pytest.raises(RequestError, match="without a model instruction"):
            runtime.reconsider(key, issue_key(runtime._secret))


# a killed writer must reconcile its completed replacement before resuming.
@pytest.mark.parametrize("external", [None, "original", "user change", "second file"])
@pytest.mark.parametrize("stop", [None, "cancelled", "expired"])
def test_run_killed_after_replace_reconciles_files(
    paths: tuple[Path, Path],
    external: str | None,
    stop: str | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = paths[0] / "a.md"
    target.write_text("original")
    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        proposal = Proposal(
            changes=(
                Change(path="a.md", before=digest(b"original"), after="result"),
                Change(path="b.md", after="second"),
            )
        )
        runtime.submit(key, proposal)

    code = """
import os, sys
from pathlib import Path
from markdown_notes_agent.documents import Documents
from markdown_notes_agent.runtime import Runtime
from markdown_notes_agent.store import StateStore
original = Documents.apply
def crash(self, change):
    original(self, change)
    os._exit(73)
Documents.apply = crash
with Runtime(Path(sys.argv[1]), Path(sys.argv[2])) as runtime:
    runtime.run_next()
"""
    process = subprocess.run(
        [sys.executable, "-c", code, *map(str, paths)], timeout=30, check=False
    )
    assert process.returncode == 73
    assert target.read_text() == "result"
    if external == "second file":
        (paths[0] / "b.md").write_text("external second")
    elif external is not None:
        target.write_text(external)

    with Runtime(*paths) as runtime:
        if stop == "cancelled":
            runtime.cancel(key)
        elif stop == "expired":
            future = time.time() + 86401
            monkeypatch.setattr(
                "markdown_notes_agent.runtime.time.time", lambda: future
            )
        result = runtime.run_next()
        assert result is not None
        if stop:
            assert result["status"] == stop
            expected = {
                None: "result_present",
                "original": "source_present",
                "user change": "externally_changed",
                "second file": "result_present",
            }
            assert result["results"]["a.md"] == expected[external]
            if external != "second file":
                assert not (paths[0] / "b.md").exists()
        elif external == "second file":
            assert result["status"] == "conflict"
            assert result["results"]["a.md"] == "result_present"
            assert result["results"]["b.md"] == "externally_changed"
            assert (paths[0] / "b.md").read_text() == "external second"
        elif external == "user change":
            assert result["status"] == "conflict"
            assert target.read_text() == "user change"
            assert not (paths[0] / "b.md").exists()
        else:
            assert result["status"] == "completed"
            assert target.read_text() == "result"
            assert (paths[0] / "b.md").read_text() == "second"


# single-owner locking must reject a second owner and release on close.
def test_runtime_second_owner_rejected(paths: tuple[Path, Path]) -> None:
    with Runtime(*paths), pytest.raises(RequestError, match="already in use"):
        Runtime(*paths)
    with Runtime(*paths) as runtime:
        assert runtime.run_next() is None


# the public CLI must preserve the same proposal and result contract.
def test_cli_submit_run_status(paths: tuple[Path, Path], tmp_path: Path) -> None:
    url = os.environ.get("MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    base = [
        sys.executable,
        "-m",
        "markdown_notes_agent.cli",
        "--state",
        str(paths[1]),
        "--search-url",
        url,
    ]
    subprocess.run([*base, "vault", "add", str(paths[0])], check=True, timeout=30)
    key_result = subprocess.run(
        [*base, "key"], capture_output=True, check=True, timeout=30
    )
    key = json.loads(key_result.stdout)["key"]
    status = subprocess.run(
        [*base, "status", key], capture_output=True, check=True, timeout=30
    )
    assert json.loads(status.stdout)["status"] == "not_found"
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"changes": [{"path": "a.md", "after": "hello"}]}))
    subprocess.run([*base, "submit", key, str(proposal)], check=True, timeout=30)
    result = subprocess.run([*base, "run"], check=True, capture_output=True, timeout=30)
    assert json.loads(result.stdout)["status"] == "completed"
    assert (paths[0] / "a.md").read_text() == "hello"


# CLI input failures identify the missing field without leaking source text.
def test_cli_invalid_intent_names_missing_query(paths: tuple[Path, Path]) -> None:
    intent = paths[1].parent / "intent.json"
    intent.write_text(json.dumps({"kind": "edit", "instruction": "private-source"}))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "markdown_notes_agent.cli",
            "--state",
            str(paths[1]),
            "automate",
            "unused",
            str(intent),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "query: Field required" in result.stderr
    assert "private-source" not in result.stderr


# a forged identity or escaping path must fail before queue admission.
@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", "VAULT.md"])
def test_submit_invalid_path_rejected(paths: tuple[Path, Path], path: str) -> None:
    with Runtime(*paths) as runtime:
        proposal = Proposal(changes=(Change(path=path, after="invalid"),))
        with pytest.raises(RequestError):
            runtime.submit(issue_key(runtime._secret), proposal)
        assert runtime.run_next() is None


# deadlines must not restart after a process restart.
def test_run_expired_request_does_not_write(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with Runtime(*paths) as runtime:
        runtime.submit(
            issue_key(runtime._secret),
            Proposal(changes=(Change(path="a.md", after="x"),)),
        )
    future = time.time() + 86401
    with Runtime(*paths) as runtime:
        monkeypatch.setattr("markdown_notes_agent.runtime.time.time", lambda: future)
        result = runtime.run_next()
        assert result is not None and result["status"] == "expired"
        assert not (paths[0] / "a.md").exists()


# an already-written dependency changed by an external writer stops later writes.
def test_run_partial_external_change_stops_remaining_files(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Documents.apply

    def modify_after_write(documents: Documents, change: Change) -> str:
        result = original(documents, change)
        (paths[0] / "a.md").write_text("external edit")
        return result

    monkeypatch.setattr(Documents, "apply", modify_after_write)
    with Runtime(*paths) as runtime:
        proposal = Proposal(
            changes=(
                Change(path="a.md", after="first"),
                Change(path="b.md", after="second"),
            )
        )
        runtime.submit(issue_key(runtime._secret), proposal)
        result = runtime.run_next()
        assert result is not None and result["status"] == "conflict"

        assert (paths[0] / "a.md").read_text() == "external edit"
        assert not (paths[0] / "b.md").exists()


# multi-document jobs must continue beyond the graph's default recursion limit.
def test_run_hundred_documents_completes(paths: tuple[Path, Path]) -> None:
    proposal = Proposal(
        changes=tuple(
            Change(path=f"docs/{index}.md", after=str(index)) for index in range(100)
        )
    )
    with Runtime(*paths) as runtime:
        runtime.submit(issue_key(runtime._secret), proposal)
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
        assert len(result["results"]) == 100
    assert (paths[0] / "docs/99.md").read_text() == "99"


def test_removed_vault_is_cleaned_up(paths: tuple[Path, Path]) -> None:
    store = StateStore(paths[1] / "hub")
    register_vault(store, paths[0])
    hub = VaultHub(store, Settings(), "http://127.0.0.1:9")
    try:
        opened = hub.get(None)
        runtime = opened.runtime
        runtime.submit(
            issue_key(runtime._secret),
            Proposal(changes=(Change(path="a.md", after="x"),)),
        )
        leftover = paths[0] / ".markdown-notes-agent-0123.pending"
        leftover.write_text("partial")
        (paths[0] / "b.md").write_text("planned delete")
        interrupted = paths[0] / ".b.md.note-deleting"
        (paths[0] / "b.md").rename(interrupted)
        state = hub.folder / str(opened.id)
        assert state.is_dir()
        store.remove_vault(opened.id)
        hub.refresh()
        assert not hub.opened()
        assert not state.exists()
        assert not leftover.exists() and not interrupted.exists()
        assert (paths[0] / "b.md").read_text() == "planned delete"
        assert not (paths[0] / "a.md").exists()
    finally:
        hub.close()


# invalid target replacement is terminal, rather than an invisible unacked task.
def test_run_symlink_replacement_reports_invalid_target(
    paths: tuple[Path, Path],
) -> None:
    with Runtime(*paths) as runtime:
        runtime.submit(
            issue_key(runtime._secret),
            Proposal(changes=(Change(path="a.md", after="x"),)),
        )
        outside = paths[0].parent / "outside.md"
        outside.write_text("external")
        (paths[0] / "a.md").symlink_to(outside)
        result = runtime.run_next()
        assert result is not None and result["status"] == "failed"
        assert result["error_code"] == "invalid_target"
        assert outside.read_text() == "external"
        assert runtime.run_next() is None


# portable path aliases must be rejected before any document changes.
@pytest.mark.parametrize("alias", ["docs//a.md", "docs/./a.md", "DOCS/A.md"])
def test_submit_aliased_targets_rejected(paths: tuple[Path, Path], alias: str) -> None:
    with Runtime(*paths) as runtime:
        proposal = Proposal(
            changes=(Change(path="docs/a.md", after="a"), Change(path=alias, after="b"))
        )
        with pytest.raises(RequestError):
            runtime.submit(issue_key(runtime._secret), proposal)
        assert runtime.run_next() is None


# invalid and expired credentials fail admission without writing a queue item.
def test_submit_expired_or_forged_key_rejected(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        proposal = Proposal(changes=(Change(path="a.md", after="a"),))
        with pytest.raises(
            RequestError, match="not issued by this Markdown Notes Agent"
        ) as forged:
            runtime.submit(key[:-1] + ("0" if key[-1] != "0" else "1"), proposal)
        assert forged.value.code == "invalid_key"
        future = time.time() + 86401
        monkeypatch.setattr("markdown_notes_agent.requests.time.time", lambda: future)
        with pytest.raises(RequestError, match="expired"):
            runtime.submit(key, proposal)
        assert runtime.run_next() is None


# a disk failure retries the persisted proposal without inventing new content.
def test_run_transient_write_failure_retries(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Documents.apply

    def fail_write(documents: Documents, change: Change) -> str:
        raise OSError("synthetic disk failure")

    with Runtime(*paths) as runtime:
        key = issue_key(runtime._secret)
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="a"),)))
        monkeypatch.setattr(Documents, "apply", fail_write)
        with pytest.raises(OSError, match="synthetic"):
            runtime.run_next()
        assert runtime.run_next() is None
        monkeypatch.setattr(Documents, "apply", original)
        future = time.time() + 11
        monkeypatch.setattr(time, "time", lambda: future)
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
        assert (paths[0] / "a.md").read_text() == "a"


# A cancelled job that has not started reports cancelled, not queued.
def test_cancelled_queued_job_reports_cancelled(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    with Runtime(root, tmp_path / "state") as runtime:
        key = issue_key(runtime._secret)
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
        runtime.submit(issue_key(runtime._secret), proposal)
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
    size = sum(p.stat().st_size for p in state.glob("graph.sqlite*"))
    assert size < 40 * 1024 * 1024, size


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
        key = issue_key(runtime._secret)
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
            runtime.reconsider(key, issue_key(runtime._secret))


def _hidden(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir() if p.name.startswith("."))


# A delete removes only the planned content and leaves nothing hidden behind.
def test_delete_is_verified_and_leaves_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from markdown_notes_agent.errors import ConflictError
    from markdown_notes_agent.requests import digest

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

    def save_then_rename(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        if Path(source) == target:
            target.write_text("saved by the user")
        rename(source, destination)

    monkeypatch.setattr(os, "rename", save_then_rename)
    with pytest.raises(ConflictError):
        documents.apply(change)
    monkeypatch.undo()
    assert target.read_text() == "saved by the user" and _hidden(root) == []

    # A crash after the rename: the next attempt finishes the delete.
    target.write_text("planned")
    rename(target, root / ".a.md.note-deleting")
    assert documents.apply(change) == "already_applied"
    assert not target.exists() and _hidden(root) == []

    # A crash after the rename of changed content: it is put back.
    (root / ".a.md.note-deleting").write_text("changed")
    with pytest.raises(ConflictError):
        documents.apply(change)
    assert target.read_text() == "changed" and _hidden(root) == []

    # Expired or failed jobs settle the same way.
    target.unlink()
    (root / ".a.md.note-deleting").write_text("planned")
    documents.discard_pending(Proposal(changes=(change,)))
    assert not target.exists() and _hidden(root) == []


@pytest.mark.parametrize("stem_length", [230, 235])
def test_long_note_delete_completes(paths: tuple[Path, Path], stem_length: int) -> None:
    name = "a" * stem_length + ".md"
    target = paths[0] / name
    target.write_text("planned")
    with Runtime(*paths) as runtime:
        runtime.submit(
            issue_key(runtime._secret),
            Proposal(
                changes=(Change(path=name, before=digest(b"planned"), after=None),)
            ),
        )
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
    assert not target.exists()
    assert list(paths[0].iterdir()) == []


def test_release_restores_long_interrupted_delete(paths: tuple[Path, Path]) -> None:
    name = "a" * 235 + ".md"
    target = paths[0] / name
    target.write_text("planned")
    documents = Documents(paths[0])
    target.rename(paths[0] / f".{name}.note-deleting")
    assert documents.release() == 1
    assert target.read_text() == "planned"
    assert list(paths[0].iterdir()) == [target]


def test_delete_race_restores_both_long_notes(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    from markdown_notes_agent.errors import ConflictError

    name = "a" * 231 + ".md"
    target = paths[0] / name
    target.write_text("planned")
    rename = os.rename

    def save_then_rename(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        if Path(source) == target:
            target.write_text("first save")
        rename(source, destination)
        if Path(source) == target:
            target.write_text("second save")

    monkeypatch.setattr(os, "rename", save_then_rename)
    documents = Documents(paths[0])
    with pytest.raises(ConflictError):
        documents.apply(Change(path=name, before=digest(b"planned"), after=None))
    recovered = target.with_name(f"{target.stem}.note-recovered-1.md")
    assert target.read_text() == "second save"
    assert recovered.read_text() == "first save"
    assert sorted(paths[0].iterdir()) == sorted([target, recovered])


# A finished or stopped job keeps one record, not its step history.
def test_finished_jobs_keep_one_record(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "b.md").write_text("old")
    with Runtime(root, tmp_path / "state") as runtime:
        done = issue_key(runtime._secret)
        runtime.submit(done, Proposal(changes=(Change(path="a.md", after="x"),)))
        stopped = issue_key(runtime._secret)
        before = runtime.documents.read_hash("b.md")
        runtime.submit(
            stopped, Proposal(changes=(Change(path="b.md", before=before, after="y"),))
        )
        (root / "b.md").write_text("user")
        finish(runtime)
        for key, status in ((done, "completed"), (stopped, "conflict")):
            record = runtime.status(key)
            assert record["status"] == status
            assert (
                len(list(runtime._saver.list({"configurable": {"thread_id": key}})))
                == 1
            )
        assert "proposal" not in runtime.status(done)
        assert "proposal" in runtime.status(stopped)  # still needed to reconsider
