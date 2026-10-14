import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from markchain.documents import Documents
from markchain.search import SearchIndex
from markchain.sync import Synchronizer
from markchain.watcher import VaultWatcher


@pytest.fixture
def sync(tmp_path: Path) -> Iterator[Synchronizer]:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires an explicit local Elasticsearch test endpoint")
    vault = tmp_path / "vault"
    vault.mkdir()
    index = SearchIndex(url, str(vault))
    index.ensure()
    yield Synchronizer(Documents(vault), index)
    index.client.indices.delete(index=index.name)
    index.close()


# #3: rebuild after missing every event, including rename and deletion.
def test_reconcile_recovers_offline_changes(sync: Synchronizer) -> None:
    root = sync.documents.root
    source = root / "a.md"
    source.write_text("# 결제\n결제가 중복됩니다.")
    assert sync.reconcile()["read"] == 1
    result = sync.search("결제")
    assert result["source"] == "elasticsearch"
    assert result["documents"][0]["path"] == "a.md"
    source.rename(root / "b.md")
    (root / "b.md").write_text("# 환불\n취소하면 환불합니다.")
    restarted = Synchronizer(sync.documents, sync.index)
    restarted.reconcile()
    assert [d["path"] for d in sync.search("환불")["documents"]] == ["b.md"]
    assert not sync.search("결제")["documents"]
    (root / "b.md").unlink()
    restarted.reconcile()
    assert not list(sync.index.entries())


# #3: unchanged trees must not reread all bodies during metadata reconciliation.
def test_reconcile_large_tree_reads_only_changed_body(sync: Synchronizer) -> None:
    root = sync.documents.root
    for number in range(1000):
        (root / f"{number}.md").write_text(f"# Payment {number}\nIdempotency key")
    assert sync.reconcile() == {"checked": 1000, "read": 1000, "diagnostics": []}
    assert sync.reconcile() == {"checked": 1000, "read": 0, "diagnostics": []}
    (root / "17.md").write_text("# Changed\nretry")
    assert sync.reconcile() == {"checked": 1000, "read": 1, "diagnostics": []}
    assert sync.audit(7) == {"checked": 7, "read": 7}
    assert sync.audit(7) == {"checked": 7, "read": 7}


# #3: a retrieved stale version must be repaired before its source is used.
def test_search_stale_hit_repairs_before_return(sync: Synchronizer) -> None:
    target = sync.documents.root / "a.md"
    target.write_text("# 결제\n이전 내용")
    sync.reconcile()
    target.write_text("# 결제\n최신 조건")
    result = sync.search("결제")
    assert result["source"] == "elasticsearch"
    assert result["documents"][0]["content"] == "# 결제\n최신 조건"


# #3: missing indexed results allow a reasoned local fallback and index repair.
def test_search_unindexed_file_uses_and_records_fallback(sync: Synchronizer) -> None:
    target = sync.documents.root / "a.md"
    target.write_text("# 결제\n새로운 결제 지식")
    result = sync.search("결제")
    assert result["source"] == "local_fallback"
    assert result["reason"] == "no_hits"
    assert result["documents"][0]["path"] == "a.md"
    assert sync.search("결제")["source"] == "elasticsearch"


# #3: out-of-order signals reread current files instead of indexing old payloads.
def test_repeated_signal_never_replays_old_content(sync: Synchronizer) -> None:
    target = sync.documents.root / "a.md"
    target.write_text("# 결제\n이전")
    sync.sync_paths(["a.md"])
    target.write_text("# 결제\n현재")
    sync.sync_paths(["a.md"])
    sync.sync_paths(["a.md"], force=True)
    sync.index.refresh()
    assert sync.search("결제")["documents"][0]["content"] == "# 결제\n현재"


# #3: native observer detects a file saved outside Obsidian.
def test_native_watch_receives_external_file(sync: Synchronizer) -> None:
    watcher = VaultWatcher(sync)
    watcher.start()
    try:
        watcher.flush()
        (sync.documents.root / "external.md").write_text("# 관찰\n외부 저장")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(0.2)
            watcher.flush()
            if sync.index.metadata(["external.md"]):
                break
        assert sync.index.metadata(["external.md"])
    finally:
        watcher.close()


# #3: unreadable Markdown must not starve valid file synchronization.
@pytest.mark.parametrize("invalid", [b"\xff", b"a" * (2 * 1024 * 1024 + 1)])
def test_invalid_file_does_not_block_sync(sync: Synchronizer, invalid: bytes) -> None:
    (sync.documents.root / "a.md").write_bytes(invalid)
    (sync.documents.root / "b.md").write_text("# 결제\n정상 문서")
    report = sync.reconcile()
    assert report["diagnostics"][0]["path"] == "a.md"
    assert sync.search("결제")["documents"][0]["path"] == "b.md"
    assert not sync.search("missing")["complete"]


# #3: bounded fallback exposes unfinished traversal rather than claiming absence.
def test_fallback_can_continue_after_result_limit(sync: Synchronizer) -> None:
    for number in range(3):
        (sync.documents.root / f"{number}.md").write_text("# 결제\n자료")
    first = sync.search("결제", 1)
    assert not first["complete"]
    second = sync.search("결제", 1, fallback_offset=first["next_offset"])
    assert first["documents"][0]["path"] != second["documents"][0]["path"]
