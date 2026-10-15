import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from markchain.documents import Documents
from markchain.errors import RequestError
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


# #3: Korean grammatical particles must not turn unrelated notes into candidates.
@pytest.mark.parametrize("query", ["멱등성이", "멱등셩"])
def test_search_korean_particle_and_typo_excludes_unrelated(
    sync: Synchronizer, query: str
) -> None:
    (sync.documents.root / "payment.md").write_text(
        "# 결제\n멱등성 키로 중복을 막는다."
    )
    (sync.documents.root / "garden.md").write_text("# 정원\n여행 사진을 정리한 자료다.")
    sync.reconcile()
    assert [hit["path"] for hit in sync.index.find(query, 20)] == ["payment.md"]


# #3: missing indexed results allow a reasoned local fallback and index repair.
def test_search_unindexed_file_uses_and_records_fallback(sync: Synchronizer) -> None:
    target = sync.documents.root / "a.md"
    target.write_text("# 결제\n새로운 결제 지식")
    result = sync.search("결제")
    assert result["source"] == "local_fallback"
    assert result["reason"] == "no_hits"
    assert result["documents"][0]["path"] == "a.md"
    assert sync.search("결제")["source"] == "elasticsearch"


# #3: indexed hits do not prevent a reasoned search for missing evidence.
def test_search_missing_evidence_starts_at_first_file(sync: Synchronizer) -> None:
    (sync.documents.root / "z.md").write_text("# 결제\n기존 문서")
    sync.reconcile()
    (sync.documents.root / "a.md").write_text("# 결제\n누락된 근거")
    result = sync.search("결제", fallback_reason="insufficient_evidence")
    assert result["reason"] == "insufficient_evidence"
    assert {doc["path"] for doc in result["documents"]} == {"a.md", "z.md"}
    assert sync.index.metadata(["a.md"])


# #3: a directory replacing a document must not retain its search entry.
def test_reconcile_file_replaced_by_directory_removes_entry(sync: Synchronizer) -> None:
    path = sync.documents.root / "a.md"
    path.write_text("# 결제")
    sync.reconcile()
    path.unlink()
    path.mkdir()
    sync.reconcile()
    assert not list(sync.index.entries())


# #3: search can finish while reconciliation waits between filesystem batches.
def test_reconcile_allows_search_between_batches(
    sync: Synchronizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = sync.documents.root / "a.md"
    path.write_text("# 결제")
    sync.reconcile()
    reached, release = threading.Event(), threading.Event()

    def paths() -> Iterator[str]:
        yield from ["a.md"] * 256
        reached.set()
        assert release.wait(10)

    monkeypatch.setattr(sync, "paths", paths)
    with ThreadPoolExecutor(max_workers=2) as pool:
        scan = pool.submit(sync.reconcile)
        try:
            assert reached.wait(10)
            result = pool.submit(sync.search, "결제").result(timeout=5)
            assert result["documents"][0]["path"] == "a.md"
        finally:
            release.set()
        scan.result(timeout=10)


# #3: a transient read failure is retried without another filesystem event.
def test_watch_retries_unreadable_new_file(
    sync: Synchronizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    (sync.documents.root / "a.md").write_text("# 결제")
    read = sync.read
    attempts = 0

    def transient_read(path: str) -> dict[str, str]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError("file temporarily unavailable")
        return read(path)

    monkeypatch.setattr(sync, "read", transient_read)
    monkeypatch.setattr("markchain.watcher.time.monotonic", lambda: 0)
    watcher = VaultWatcher(sync)
    watcher.flush()
    assert not sync.index.metadata(["a.md"])
    monkeypatch.setattr("markchain.watcher.time.monotonic", lambda: 11)
    watcher.flush()
    assert sync.index.metadata(["a.md"])


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


# #9: folder filtering precedes top-k and is identical in fallback and continuation.
@pytest.mark.parametrize("source", ["indexed", "stale", "unindexed", "unavailable"])
def test_search_folders_preserve_union_and_boundaries(
    sync: Synchronizer, source: str
) -> None:
    root = sync.documents.root
    selected = [
        "docs/api/a.md",
        "docs/api/child/b.md",
        "worktree/app/docs/c.md",
        "legacy/d.md",
    ]
    outside = ["docs/api-old/e.md", "docs/api.md", "private/f.md"]
    for path in selected + outside:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# 재시도\n멱등성 키")
    if source != "unindexed":
        sync.reconcile()
    if source == "stale":
        for path in selected:
            (root / path).write_text("# 재시도\n현재의 멱등성 키")
    if source == "unavailable":
        sync.index.client.close()
    folders = ("docs/api/", "docs/api/child", "docs/api", "worktree/app/docs", "legacy")

    result = sync.search("재시도", folders=folders)

    assert {d["path"] for d in result["documents"]} == set(selected)
    assert result["folders"] == ["docs/api", "legacy", "worktree/app/docs"]
    if source == "stale":
        assert all("현재의" in d["content"] for d in result["documents"])
    if source == "unavailable":
        sync.index.client = SearchIndex(
            os.environ["MARKCHAIN_TEST_SEARCH_URL"], str(root)
        ).client
    page = sync.search(
        "재시도", 1, folders=folders, fallback_reason="insufficient_evidence"
    )
    found: set[str] = set()
    while page["next_offset"] is not None:
        found.update(d["path"] for d in page["documents"])
        page = sync.search(
            "재시도",
            1,
            folders=folders,
            fallback_offset=page["next_offset"],
            fallback_reason="insufficient_evidence",
        )
    found.update(d["path"] for d in page["documents"])
    assert found == set(selected)
    assert not sync.search("재시도", folders=("missing",))["documents"]


# #9: many absent folders must not broaden the selected scope.
def test_search_many_folders_filters_before_top_k(sync: Synchronizer) -> None:
    root = sync.documents.root
    for folder in ("chosen", "unrelated"):
        (root / folder).mkdir()
        (root / folder / "a.md").write_text("# retry\nretry key")
    sync.reconcile()

    result = sync.search(
        "retry", 1, folders=tuple(f"absent/{n}" for n in range(1100)) + ("chosen",)
    )

    assert [d["path"] for d in result["documents"]] == ["chosen/a.md"]
    assert len(result["folders"]) == 1101


# #9: invalid scopes fail rather than silently reverting to the full vault.
@pytest.mark.parametrize(
    "folder",
    [
        "",
        "/docs",
        "../docs",
        "docs/../private",
        "docs//api",
        "docs/./api",
        ".hidden",
        "docs\\api",
    ],
)
def test_search_invalid_folder_is_rejected(sync: Synchronizer, folder: str) -> None:
    with pytest.raises(RequestError, match="vault-relative"):
        sync.search("retry", folders=(folder,))


# #9: CLI folder flags are repeatable and errors in other folders stay out of scope.
def test_cli_repeated_folders_and_local_diagnostics(sync: Synchronizer) -> None:
    root = sync.documents.root
    for folder in ("문서/개발", "docs/[api]", "private"):
        (root / folder).mkdir(parents=True)
    (root / "문서/개발/a.md").write_text("# retry\nkey")
    (root / "docs/[api]/b.md").write_text("# retry\nkey")
    (root / "private/c.md").write_bytes(b"\xff")
    sync.reconcile()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "markchain.cli",
            "--vault",
            str(root),
            "--state",
            str(root.parent / "cli-state"),
            "--search-url",
            os.environ["MARKCHAIN_TEST_SEARCH_URL"],
            "search",
            "retry",
            "--folder",
            "문서/개발",
            "--folder",
            "docs/[api]",
            "--folder",
            "missing",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    assert {d["path"] for d in json.loads(result.stdout)["documents"]} == {
        "문서/개발/a.md",
        "docs/[api]/b.md",
    }
    local = sync.search(
        "absent", folders=("문서/개발",), fallback_reason="insufficient_evidence"
    )
    assert local["complete"] and local["diagnostics"] == []
