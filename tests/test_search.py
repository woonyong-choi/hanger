import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import pytest
from conftest import OFFLINE
from elastic_transport import TransportError

from hanger.closets import ClosetPolicy
from hanger.documents import Documents
from hanger.errors import RequestError, SearchUnavailableError
from hanger.ignore import DEFAULT_IGNORE, IGNORE_FILE
from hanger.search import SearchIndex
from hanger.sync import Synchronizer
from hanger.watcher import ClosetWatcher


@pytest.fixture
def sync(tmp_path: Path) -> Iterator[Synchronizer]:
    url = os.environ.get("HANGER_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires an explicit local Elasticsearch test endpoint")
    closet = tmp_path / "closet"
    closet.mkdir()
    # A registered closet always holds its ignore file.
    (closet / IGNORE_FILE).write_text(DEFAULT_IGNORE)
    index = SearchIndex(url, str(closet))
    try:
        index.ensure()
        yield Synchronizer(Documents(closet), index)
    finally:
        try:
            index.delete()
        finally:
            index.close()


# rebuild after missing every event, including rename and deletion.
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


# unchanged trees must not reread all bodies during metadata reconciliation.
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


# a retrieved stale version must be repaired before its source is used.
def test_search_stale_hit_repairs_before_return(sync: Synchronizer) -> None:
    target = sync.documents.root / "a.md"
    target.write_text("# 결제\n이전 내용")
    sync.reconcile()
    target.write_text("# 결제\n최신 조건")
    result = sync.search("결제")
    assert result["source"] == "elasticsearch"
    assert result["unverified"] == []
    assert result["documents"][0]["bytes"] == len("# 결제\n최신 조건".encode())


# Korean grammatical particles must not turn unrelated notes into candidates.
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


# Nori split '재시도 횟수' differently and omitted a required edit target.
def test_search_same_korean_word_in_different_contexts(sync: Synchronizer) -> None:
    root = sync.documents.root
    (root / "retry.md").write_text("# 재시도\n기본 재시도는 2회다.")
    (root / "operations.md").write_text("# 운영\n재시도 횟수: 2회")
    (root / "garden.md").write_text("# 정원\n나무와 사진")
    sync.reconcile()

    assert {hit["path"] for hit in sync.index.find("재시도", 20)} == {
        "retry.md",
        "operations.md",
    }


# an unindexed file is found after a reasoned scope re-sync, not by a scan.
def test_search_unindexed_file_found_after_scope_resync(sync: Synchronizer) -> None:
    sync.reconcile()
    target = sync.documents.root / "a.md"
    target.write_text("# 결제\n새로운 결제 지식")
    assert sync.search("결제")["documents"] == []
    result = sync.search("결제", fallback_reason="user_reported_missing")
    assert result["source"] == "elasticsearch"
    assert result["reason"] == "user_reported_missing"
    assert result["documents"][0]["path"] == "a.md"
    assert sync.search("결제")["documents"][0]["path"] == "a.md"


# indexed hits do not prevent a reasoned search for missing evidence.
def test_search_missing_evidence_starts_at_first_file(sync: Synchronizer) -> None:
    (sync.documents.root / "z.md").write_text("# 결제\n기존 문서")
    sync.reconcile()
    (sync.documents.root / "a.md").write_text("# 결제\n누락된 근거")
    result = sync.search("결제", fallback_reason="insufficient_evidence")
    assert result["reason"] == "insufficient_evidence"
    assert {doc["path"] for doc in result["documents"]} == {"a.md", "z.md"}
    assert sync.index.metadata(["a.md"])


# a directory replacing a document must not retain its search entry.
def test_reconcile_file_replaced_by_directory_removes_entry(sync: Synchronizer) -> None:
    path = sync.documents.root / "a.md"
    path.write_text("# 결제")
    sync.reconcile()
    path.unlink()
    path.mkdir()
    sync.reconcile()
    assert not list(sync.index.entries())


# search can finish while reconciliation waits between filesystem batches.
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


# a transient read failure is retried without another filesystem event.
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
    monkeypatch.setattr("hanger.watcher.time.monotonic", lambda: 0)
    watcher = ClosetWatcher(sync)
    watcher.flush()
    assert not sync.index.metadata(["a.md"])
    monkeypatch.setattr("hanger.watcher.time.monotonic", lambda: 11)
    watcher.flush()
    assert sync.index.metadata(["a.md"])


# out-of-order signals reread current files instead of indexing old payloads.
def test_repeated_signal_never_replays_old_content(sync: Synchronizer) -> None:
    target = sync.documents.root / "a.md"
    target.write_text("# 결제\n이전")
    sync.sync_paths(["a.md"])
    target.write_text("# 결제\n현재")
    sync.sync_paths(["a.md"])
    sync.sync_paths(["a.md"], force=True)
    sync.index.refresh()
    found = sync.search("결제")["documents"][0]
    assert sync.read(found["path"])["content"] == "# 결제\n현재"


# The native observer detects a file saved by another program.
def test_native_watch_receives_external_file(sync: Synchronizer) -> None:
    watcher = ClosetWatcher(sync)
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


# unreadable Markdown must not starve valid file synchronization.
@pytest.mark.parametrize("invalid", [b"\xff", b"a" * (2 * 1024 * 1024 + 1)])
def test_invalid_file_does_not_block_sync(sync: Synchronizer, invalid: bytes) -> None:
    (sync.documents.root / "a.md").write_bytes(invalid)
    (sync.documents.root / "b.md").write_text("# 결제\n정상 문서")
    report = sync.reconcile()
    assert report["diagnostics"][0]["path"] == "a.md"
    assert sync.search("결제")["documents"][0]["path"] == "b.md"
    assert sync.search("missing")["documents"] == []


# a search outage is an explicit error, never a silent local scan.
def test_search_outage_raises_without_local_scan(sync: Synchronizer) -> None:
    (sync.documents.root / "a.md").write_text("# 결제\n자료")
    sync.reconcile()
    url = os.environ["HANGER_TEST_SEARCH_URL"]
    sync.index.client.close()
    sync.index.client = SearchIndex(
        "http://127.0.0.1:9", str(sync.documents.root)
    ).client
    try:
        with pytest.raises(SearchUnavailableError):
            sync.search("결제")
    finally:
        sync.index.client = SearchIndex(url, str(sync.documents.root)).client


# folder filtering precedes top-k and is identical in fallback and continuation.
@pytest.mark.parametrize("source", ["indexed", "stale", "unindexed"])
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
    folders = ("docs/api/", "docs/api/child", "docs/api", "worktree/app/docs", "legacy")
    reason: Literal["user_reported_missing"] | None = (
        "user_reported_missing" if source == "unindexed" else None
    )

    result = sync.search("재시도", folders=folders, fallback_reason=reason)

    assert {d["path"] for d in result["documents"]} == set(selected)
    assert result["folders"] == ["docs/api", "legacy", "worktree/app/docs"]
    if source == "stale":
        assert all(
            "현재의" in sync.read(d["path"])["content"] for d in result["documents"]
        )
    rechecked = sync.search(
        "재시도", folders=folders, fallback_reason="insufficient_evidence"
    )
    assert {d["path"] for d in rechecked["documents"]} == set(selected)
    assert not sync.search("재시도", folders=("missing",))["documents"]


# many absent folders must not broaden the selected scope.
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


@pytest.mark.parametrize("ranking", ["ranked", "tied"])
def test_search_many_folders_preserves_global_top_k(
    sync: Synchronizer, ranking: str
) -> None:
    root = sync.documents.root
    folders = tuple(f"selected/{number:04d}" for number in range(1101))
    selected = []
    for number in (0, 200, 400, 600, 800, 1000, 1100):
        path = f"{folders[number]}/a.md"
        selected.append(path)
        target = root / path
        target.parent.mkdir(parents=True)
        repetitions = number // 200 + 1 if ranking == "ranked" else 1
        target.write_text(f"# retry\n{'retry ' * repetitions}")
    (root / "outside.md").write_text("# retry\nretry")
    sync.reconcile()
    expected = [
        hit for hit in sync.index.find("retry", 100) if hit["path"] in selected
    ][:4]

    result = sync.index.find("retry", 4, folders=folders)

    assert [hit["path"] for hit in result] == [hit["path"] for hit in expected]
    assert [hit["score"] for hit in result] == [hit["score"] for hit in expected]
    if ranking == "tied":
        assert [hit["path"] for hit in result] == selected[:4]


def test_search_many_folders_preserves_literal_boundaries(sync: Synchronizer) -> None:
    root = sync.documents.root
    selected = "chosen[api]*?.+"
    for folder in (selected, selected + "-old", "chosenapiXXXX"):
        (root / folder).mkdir()
        (root / folder / "a.md").write_text("# retry\nretry key")
    (root / (selected + ".md")).write_text("# retry\nretry key")
    sync.reconcile()

    result = sync.search(
        "retry", 1, folders=tuple(f"absent/{n}" for n in range(1100)) + (selected,)
    )

    assert [hit["path"] for hit in result["documents"]] == [selected + "/a.md"]


def test_search_many_folders_preserves_snapshot_during_refresh(
    sync: Synchronizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = sync.documents.root
    paths = ("selected/0000/a.md", "selected/1100/a.md")
    for path in paths:
        target = root / path
        target.parent.mkdir(parents=True)
        target.write_text("# retry\nretry key")
    sync.reconcile()
    expected = sync.index.find("retry", 20)
    search = sync.index.client.search
    changed = False

    def search_and_refresh(**kwargs: Any) -> Any:
        nonlocal changed
        response = search(**kwargs)
        if "pit" in kwargs and not changed:
            changed = True
            (root / paths[-1]).write_text("# garden\nflowers")
            sync.sync_paths([paths[-1]], force=True)
            sync.index.refresh()
        return response

    monkeypatch.setattr(sync.index.client, "search", search_and_refresh)

    result = sync.index.find(
        "retry", 20, folders=tuple(f"selected/{n:04d}" for n in range(1101))
    )

    assert changed
    assert result == expected
    assert [hit["path"] for hit in sync.index.find("retry", 20)] == [paths[0]]


@pytest.mark.parametrize("failure", ["timeout", "shard", "transport"])
def test_find_many_folders_rejects_partial_results(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    index = SearchIndex(OFFLINE, "partial-search")
    calls = 0
    closed = []

    def search(**request: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        assert "index" not in request
        assert request["pit"]["id"] == ("snapshot" if calls == 1 else "latest-snapshot")
        if calls == 1:
            return {
                "pit_id": "latest-snapshot",
                "hits": {
                    "hits": [{"_source": {"path": "selected/0000/a.md"}, "_score": 1.0}]
                },
            }
        if failure == "transport":
            raise TransportError("test search unavailable")
        return {
            "hits": {"hits": []},
            "timed_out": failure == "timeout",
            "_shards": {"failed": int(failure == "shard")},
        }

    monkeypatch.setattr(
        index.client, "open_point_in_time", lambda **_: {"id": "snapshot"}
    )
    monkeypatch.setattr(index.client, "search", search)
    monkeypatch.setattr(
        index.client, "close_point_in_time", lambda **kw: closed.append(kw)
    )
    error = TransportError if failure == "transport" else RequestError
    try:
        with pytest.raises(error):
            index.find(
                "retry", 1, folders=tuple(f"selected/{n:04d}" for n in range(1101))
            )
        assert closed == [{"id": "latest-snapshot"}]
    finally:
        index.close()


# invalid scopes fail rather than silently reverting to the full closet.
@pytest.mark.parametrize(
    "folder",
    [
        "",
        "/docs",
        "../docs",
        "docs/../private",
        "docs//api",
        "docs/./api",
        "docs\\api",
    ],
)
def test_search_invalid_folder_is_rejected(sync: Synchronizer, folder: str) -> None:
    with pytest.raises(RequestError, match="relative to the closet root"):
        sync.search("retry", folders=(folder,))


# Ignored folders cannot be searched.
def test_search_ignored_folder_is_rejected(sync: Synchronizer) -> None:
    sync.policy.update(".hidden/\n")
    with pytest.raises(RequestError, match="ignored in .hangerignore"):
        sync.search("retry", folders=(".hidden",))


# CLI folder flags are repeatable and errors in other folders stay out of scope.
def test_cli_repeated_folders_and_local_diagnostics(sync: Synchronizer) -> None:
    root = sync.documents.root
    for folder in ("문서/개발", "docs/[api]", "private"):
        (root / folder).mkdir(parents=True)
    (root / "문서/개발/a.md").write_text("# retry\nkey")
    (root / "docs/[api]/b.md").write_text("# retry\nkey")
    (root / "private/c.md").write_bytes(b"\xff")
    sync.reconcile()
    base = [
        sys.executable,
        "-m",
        "hanger.cli",
        "--state",
        str(root.parent / "cli-state"),
        "--search-url",
        os.environ["HANGER_TEST_SEARCH_URL"],
    ]
    subprocess.run([*base, "closet", "add", str(root)], check=True)
    result = subprocess.run(
        [
            *base,
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
    absent = sync.search(
        "absent", folders=("문서/개발",), fallback_reason="insufficient_evidence"
    )
    assert absent["documents"] == []


# A closet folder that vanished or became an empty mount point keeps its index.
def test_reconcile_refuses_a_missing_or_empty_folder(sync: Synchronizer) -> None:
    root = sync.documents.root
    (root / "a.md").write_text("# kept")
    sync.reconcile()
    for name in (IGNORE_FILE, "a.md"):
        (root / name).unlink()
    with pytest.raises(RequestError, match="missing or empty"):
        sync.reconcile()
    assert [entry["path"] for entry in sync.index.entries()] == ["a.md"]


# The watcher keeps running after an unexpected error.
def test_watcher_survives_unexpected_error(
    layout: tuple[Path, Path, ClosetPolicy], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, policy = layout
    sync = Synchronizer(Documents(root), SearchIndex(OFFLINE, str(root)), None, policy)
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

    def wait(seconds: float) -> bool:
        time.sleep(0.05)
        return False

    monkeypatch.setattr(stop, "wait", wait)
    thread.start()
    time.sleep(0.5)
    assert thread.is_alive()
    assert calls >= 2
    stop.set()
    monkeypatch.undo()
    thread.join(timeout=5)


def test_watcher_rebuilds_a_lost_index(
    sync: Synchronizer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from elasticsearch import NotFoundError

    root = sync.documents.root
    (root / "a.md").write_text("# 결제\n재시도")
    watcher = ClosetWatcher(sync)
    real_audit = sync.audit
    failures: list[bool] = []

    def audit(limit: int = 64) -> dict[str, int]:
        if not failures:
            # The server lost the index while the service ran.
            sync.index.delete()
            failures.append(True)
        return real_audit(limit)

    monkeypatch.setattr(sync, "audit", audit)
    monkeypatch.setattr("hanger.watcher.ClosetWatcher.start", lambda self: None)
    monkeypatch.setattr("hanger.watcher.ClosetWatcher.close", lambda self: None)
    stop = threading.Event()
    real_wait = stop.wait
    monkeypatch.setattr(stop, "wait", lambda seconds: real_wait(0.05))
    thread = threading.Thread(target=watcher.run, args=(stop,), daemon=True)
    thread.start()
    found: list[str] = []
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not found:
        time.sleep(0.2)
        try:
            sync.index.refresh()
            found = [h["path"] for h in sync.index.find("결제", 5)]
        except NotFoundError:
            continue
    stop.set()
    thread.join(timeout=5)
    assert failures and found == ["a.md"]
