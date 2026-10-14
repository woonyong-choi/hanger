import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from markchain.documents import Documents
from markchain.errors import RequestError
from markchain.requests import Change, Proposal, digest
from markchain.runtime import Runtime


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
        key = runtime.issue_key()
        monkeypatch.setattr(time, "time", lambda: issued + 23 * 3600)
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="result"),)))
        monkeypatch.setattr(time, "time", lambda: issued + 25 * 3600)
        result = runtime.run_next()
        assert result is not None and result["status"] == "expired"
        assert not (paths[0] / "a.md").exists()


# #2: simultaneous resubmission must not duplicate a document operation.
def test_submit_concurrent_request_returns_one_acceptance(
    paths: tuple[Path, Path],
) -> None:
    with Runtime(*paths) as runtime:
        key = runtime.issue_key()
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


# #2: same key with different content is an error, including after restart.
def test_submit_completed_key_survives_restart(paths: tuple[Path, Path]) -> None:
    proposal = Proposal(changes=(Change(path="a.md", after="first"),))
    with Runtime(*paths) as runtime:
        key = runtime.issue_key()
        runtime.submit(key, proposal)
        runtime.run_next()

    with Runtime(*paths) as runtime:
        assert runtime.submit(key, proposal) == "duplicate"
        changed = Proposal(changes=(Change(path="a.md", after="second"),))
        with pytest.raises(RequestError, match="different content"):
            runtime.submit(key, changed)


# #2: cancellation persists without changing the document.
def test_cancel_before_restart_does_not_write(paths: tuple[Path, Path]) -> None:
    with Runtime(*paths) as runtime:
        key = runtime.issue_key()
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="first"),)))
        assert runtime.cancel(key)

    with Runtime(*paths) as runtime:
        result = runtime.run_next()
        assert result is not None and result["status"] == "cancelled"
        assert not (paths[0] / "a.md").exists()


# #2: a changed source must remain intact rather than receive a stale proposal.
def test_apply_modified_source_stops_without_overwrite(
    paths: tuple[Path, Path],
) -> None:
    target = paths[0] / "a.md"
    target.write_text("original")
    with Runtime(*paths) as runtime:
        key = runtime.issue_key()
        change = Change(path="a.md", before=digest(b"original"), after="proposal")
        runtime.submit(key, Proposal(changes=(change,)))
        target.write_text("user change")
        result = runtime.run_next()
        assert result is not None and result["status"] == "conflict"
        assert target.read_text() == "user change"


# #2: a killed writer must reconcile its completed replacement before resuming.
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
        key = runtime.issue_key()
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
from markchain.documents import Documents
from markchain.runtime import Runtime
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
            monkeypatch.setattr("markchain.runtime.time.time", lambda: future)
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


# #2: single-owner locking must reject a second owner and release on close.
def test_runtime_second_owner_rejected(paths: tuple[Path, Path]) -> None:
    with Runtime(*paths), pytest.raises(RequestError, match="already in use"):
        Runtime(*paths)
    with Runtime(*paths) as runtime:
        assert runtime.run_next() is None


# #2: the public CLI must preserve the same proposal and result contract.
def test_cli_submit_run_status(paths: tuple[Path, Path], tmp_path: Path) -> None:
    base = [
        sys.executable,
        "-m",
        "markchain.cli",
        "--vault",
        str(paths[0]),
        "--state",
        str(paths[1]),
    ]
    key_result = subprocess.run(
        [*base, "key"], capture_output=True, check=True, timeout=30
    )
    key = json.loads(key_result.stdout)["key"]
    proposal = tmp_path / "proposal.json"
    proposal.write_text(json.dumps({"changes": [{"path": "a.md", "after": "hello"}]}))
    subprocess.run([*base, "submit", key, str(proposal)], check=True, timeout=30)
    result = subprocess.run([*base, "run"], check=True, capture_output=True, timeout=30)
    assert json.loads(result.stdout)["status"] == "completed"
    assert (paths[0] / "a.md").read_text() == "hello"


# #2: a forged identity or escaping path must fail before queue admission.
@pytest.mark.parametrize("path", ["../outside.md", "/outside.md", ".obsidian/a.md"])
def test_submit_invalid_path_rejected(paths: tuple[Path, Path], path: str) -> None:
    with Runtime(*paths) as runtime:
        proposal = Proposal(changes=(Change(path=path, after="invalid"),))
        with pytest.raises(RequestError):
            runtime.submit(runtime.issue_key(), proposal)
        assert runtime.run_next() is None


# #2: deadlines must not restart after a process restart.
def test_run_expired_request_does_not_write(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with Runtime(*paths) as runtime:
        runtime.submit(
            runtime.issue_key(), Proposal(changes=(Change(path="a.md", after="x"),))
        )
    future = time.time() + 86401
    with Runtime(*paths) as runtime:
        monkeypatch.setattr("markchain.runtime.time.time", lambda: future)
        result = runtime.run_next()
        assert result is not None and result["status"] == "expired"
        assert not (paths[0] / "a.md").exists()


# #2: an already-written dependency changed by an external writer stops later writes.
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
        runtime.submit(runtime.issue_key(), proposal)
        result = runtime.run_next()
        assert result is not None and result["status"] == "conflict"
        assert result["cursor"] == 1
        assert (paths[0] / "a.md").read_text() == "external edit"
        assert not (paths[0] / "b.md").exists()


# #2: multi-document jobs must continue beyond the graph's default recursion limit.
def test_run_hundred_documents_completes(paths: tuple[Path, Path]) -> None:
    proposal = Proposal(
        changes=tuple(
            Change(path=f"docs/{index}.md", after=str(index)) for index in range(100)
        )
    )
    with Runtime(*paths) as runtime:
        runtime.submit(runtime.issue_key(), proposal)
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
        assert len(result["results"]) == 100
    assert (paths[0] / "docs/99.md").read_text() == "99"


# #2: persisted jobs belong to one Vault, including create-only proposals.
def test_runtime_other_vault_rejected(paths: tuple[Path, Path]) -> None:
    with Runtime(*paths) as runtime:
        runtime.submit(
            runtime.issue_key(), Proposal(changes=(Change(path="a.md", after="x"),))
        )
    other = paths[0].parent / "other-vault"
    other.mkdir()
    with pytest.raises(RequestError, match="different vault"):
        Runtime(other, paths[1])
    assert not (other / "a.md").exists()
    with Runtime(*paths) as runtime:
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"


# #2: invalid target replacement is terminal, rather than an invisible unacked task.
def test_run_symlink_replacement_reports_invalid_target(
    paths: tuple[Path, Path],
) -> None:
    with Runtime(*paths) as runtime:
        runtime.submit(
            runtime.issue_key(), Proposal(changes=(Change(path="a.md", after="x"),))
        )
        outside = paths[0].parent / "outside.md"
        outside.write_text("external")
        (paths[0] / "a.md").symlink_to(outside)
        result = runtime.run_next()
        assert result is not None and result["status"] == "invalid_target"
        assert outside.read_text() == "external"
        assert runtime.run_next() is None


# #2: portable path aliases must be rejected before any document changes.
@pytest.mark.parametrize("alias", ["docs//a.md", "docs/./a.md", "DOCS/A.md"])
def test_submit_aliased_targets_rejected(paths: tuple[Path, Path], alias: str) -> None:
    with Runtime(*paths) as runtime:
        proposal = Proposal(
            changes=(Change(path="docs/a.md", after="a"), Change(path=alias, after="b"))
        )
        with pytest.raises(RequestError):
            runtime.submit(runtime.issue_key(), proposal)
        assert runtime.run_next() is None


# #2: invalid and expired credentials fail admission without writing a queue item.
def test_submit_expired_or_forged_key_rejected(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    with Runtime(*paths) as runtime:
        key = runtime.issue_key()
        proposal = Proposal(changes=(Change(path="a.md", after="a"),))
        with pytest.raises(RequestError, match="signature"):
            runtime.submit(key[:-1] + ("0" if key[-1] != "0" else "1"), proposal)
        future = time.time() + 86401
        monkeypatch.setattr("markchain.requests.time.time", lambda: future)
        with pytest.raises(RequestError, match="expired"):
            runtime.submit(key, proposal)
        assert runtime.run_next() is None


# #2: a disk failure retries the persisted proposal without inventing new content.
def test_run_transient_write_failure_retries(
    paths: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    original = Documents.apply

    def fail_write(documents: Documents, change: Change) -> str:
        raise OSError("synthetic disk failure")

    with Runtime(*paths) as runtime:
        key = runtime.issue_key()
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="a"),)))
        monkeypatch.setattr(Documents, "apply", fail_write)
        with pytest.raises(OSError, match="synthetic"):
            runtime.run_next()
        assert runtime.run_next() is None
        monkeypatch.setattr(Documents, "apply", original)
        future = time.time() + 3
        monkeypatch.setattr(time, "time", lambda: future)
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
        assert (paths[0] / "a.md").read_text() == "a"
