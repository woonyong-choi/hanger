import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from markchain.documents import Documents
from markchain.errors import RequestError
from markchain.requests import Change, Proposal, digest
from markchain.runtime import Limits, Runtime


# #5: daily cleanup removes expired work, not a still-valid completed identity.
def test_cleanup_preserves_valid_keys_and_discards_expired_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    proposal = Proposal(changes=(Change(path="a.md", after="saved"),))
    with Runtime(root, tmp_path / "state") as runtime:
        complete = runtime.issue_key()
        runtime.submit(complete, proposal)
        runtime.run_next()
        pending = runtime.issue_key()
        runtime.submit(pending, Proposal(changes=(Change(path="b.md", after="never"),)))
        clock += 23 * 3600
        valid = runtime.issue_key()
        runtime.submit(valid, proposal)
        runtime.run_next()
        # The older pending request is consumed first; no active consumer remains.
        runtime.run_next()
        clock += 2 * 3600
        result = runtime.cleanup()
        assert result["status"] == "cleaned"
        assert runtime.status(complete) == {}
        assert runtime.submit(valid, proposal) == "duplicate"
        with pytest.raises(RequestError, match="유효 기간"):
            runtime.submit(complete, proposal)
        assert runtime.cleanup()["status"] == "already_cleaned"


# #5: missed midnight cleanup runs on restart without applying an expired request.
def test_restart_removes_expired_pending_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    with Runtime(root, tmp_path / "state") as runtime:
        key = runtime.issue_key()
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="never"),)))
    future = time.time() + 25 * 3600
    monkeypatch.setattr(time, "time", lambda: future)
    with Runtime(root, tmp_path / "state") as runtime:
        assert runtime.status(key) == {}
        assert runtime.run_next() is None
        assert not (root / "a.md").exists()


# #5: a replaced source symlink cannot block cleanup or expose its external target.
def test_expired_symlink_target_does_not_block_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    state = tmp_path / "state"
    with Runtime(root, state) as runtime:
        runtime.submit(
            runtime.issue_key(), Proposal(changes=(Change(path="a.md", after="never"),))
        )
    outside = tmp_path / "outside.md"
    outside.write_text("external original")
    (root / "a.md").symlink_to(outside)
    future = time.time() + 25 * 3600
    monkeypatch.setattr(time, "time", lambda: future)
    with Runtime(root, state) as runtime:
        runtime.submit(
            runtime.issue_key(), Proposal(changes=(Change(path="b.md", after="valid"),))
        )
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
    assert outside.read_text() == "external original"
    assert (root / "b.md").read_text() == "valid"


# #5: capacity rejection does not enqueue the refused operation.
@pytest.mark.parametrize("boundary", ["count", "request", "bytes", "state", "disk"])
def test_admission_capacity_rejects_without_queueing(
    tmp_path: Path, boundary: str
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    overrides = {
        "count": {"pending_jobs": 0},
        "request": {"request_bytes": 1},
        "bytes": {"pending_bytes": 1},
        "state": {"state_bytes": 1},
        "disk": {"free_bytes": 10**18},
    }
    limits = replace(Limits(), **overrides[boundary])
    with Runtime(root, tmp_path / "state", limits=limits) as runtime:
        with pytest.raises(RequestError):
            runtime.submit(
                runtime.issue_key(),
                Proposal(changes=(Change(path="a.md", after="unaccepted"),)),
            )
        assert runtime.run_next() is None
        assert not (root / "a.md").exists()


# #5: cleanup must not remove a proposal while its writer is inside replacement.
def test_cleanup_busy_writer_keeps_recovery_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    started = threading.Event()
    released = threading.Event()
    apply = Documents.apply

    def pause(documents: Documents, change: Change) -> str:
        started.set()
        assert released.wait(5)
        return apply(documents, change)

    monkeypatch.setattr(Documents, "apply", pause)
    with Runtime(root, tmp_path / "state") as runtime:
        runtime.submit(
            runtime.issue_key(), Proposal(changes=(Change(path="a.md", after="saved"),))
        )
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(runtime.run_next)
            try:
                assert started.wait(5)
                future_time = time.time() + 25 * 3600
                monkeypatch.setattr(time, "time", lambda: future_time)
                assert runtime.cleanup()["status"] == "busy"
            finally:
                released.set()
            assert future.result(timeout=5) is not None
        assert (root / "a.md").read_text() == "saved"


# #5: a killed pre-replacement writer leaves only an owned candidate to expire.
def test_expired_crash_candidate_deleted_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    state = tmp_path / "state"
    (root / "a.md").write_text("original")
    with Runtime(root, state) as runtime:
        runtime.submit(
            runtime.issue_key(),
            Proposal(
                changes=(
                    Change(path="a.md", before=digest(b"original"), after="proposed"),
                )
            ),
        )
    code = """
import os, sys
from pathlib import Path
from markchain.runtime import Runtime
def crash(*args):
    os._exit(74)
os.replace = crash
with Runtime(Path(sys.argv[1]), Path(sys.argv[2])) as runtime:
    runtime.run_next()
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(root), str(state)], check=False, timeout=30
    )
    assert result.returncode == 74
    assert list(root.glob(".markchain-*.pending"))
    future = time.time() + 25 * 3600
    monkeypatch.setattr(time, "time", lambda: future)
    with Runtime(root, state) as runtime:
        assert runtime.run_next() is None
    assert not list(root.glob(".markchain-*.pending"))
    assert (root / "a.md").read_text() == "original"


# #5: retry counters survive restarts; delayed work must not block another job.
def test_retry_limit_survives_restart_and_allows_other_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    state = tmp_path / "state"
    clock = time.time()
    monkeypatch.setattr(time, "time", lambda: clock)
    apply = Documents.apply

    def fail_one(documents: Documents, change: Change) -> str:
        if change.path == "a.md":
            raise OSError("synthetic persistent I/O failure")
        return apply(documents, change)

    monkeypatch.setattr(Documents, "apply", fail_one)
    with Runtime(root, state) as runtime:
        key = runtime.issue_key()
        runtime.submit(key, Proposal(changes=(Change(path="a.md", after="never"),)))
        runtime.submit(
            runtime.issue_key(),
            Proposal(changes=(Change(path="b.md", after="independent"),)),
        )
        with pytest.raises(OSError, match="synthetic"):
            runtime.run_next()
        result = runtime.run_next()
        assert result is not None and result["status"] == "completed"
        assert (root / "b.md").read_text() == "independent"
    # Three retries 10 seconds apart survive restarts, then the job fails.
    for _ in range(2):
        clock += 11
        with (
            Runtime(root, state) as runtime,
            pytest.raises(OSError, match="synthetic"),
        ):
            runtime.run_next()
    clock += 11
    with Runtime(root, state) as runtime:
        result = runtime.run_next()
        assert result is not None and result["status"] == "failed"
        assert result["error"].startswith("처리 중 오류가 발생해 3회 재시도했으나 실패")
        assert "proposal" not in runtime.status(key)
        assert result["error_code"] == "internal"
        assert runtime.run_next() is None
        assert not (root / "a.md").exists()
