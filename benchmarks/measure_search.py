"""Time indexing and Korean search quality on a large synthetic closet."""

import argparse
import json
import platform
import resource
import time
from pathlib import Path
from typing import Any

from hanger.documents import Documents
from hanger.search import SearchIndex
from hanger.sync import Synchronizer

FIXTURES = {
    "payment": (
        "# 결제 재시도\n결제가 중복되면 멱등성 키로 막는다. idempotency를 설명한다."
    ),
    "exception": (
        "# 중복 방지 예외\n멱등성 키가 만료된 결제의 중복 처리 조건을 검토한다."
    ),
    "refund": (
        "# 결제 취소\n주문 취소 후 환불을 처리한다. 중복 요청 방지와는 다른 질문이다."
    ),
    "isolation": "# 트랜잭션 격리\n데이터베이스의 격리 수준과 동시 실행을 설명한다.",
    "index": "# 검색 색인\n역색인과 세그먼트 병합을 설명한다.",
}
QUERIES = {
    "결제 중복": ["payment", "exception"],
    "멱등성이": ["payment", "exception"],
    "멱등셩": ["payment", "exception"],
    "idempotency": ["payment", "exception"],
    "결제 취소": ["refund"],
    "트랜잭션 격리": ["isolation"],
    "같은 요청이 두 번 처리되지 않게": ["payment", "exception"],
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--count", type=int, default=100000)
    parser.add_argument("--reuse-corpus", action="store_true")
    args = parser.parse_args()
    if args.directory.exists() and not args.reuse_corpus:
        raise SystemExit("measurement directory must not already exist")
    root = args.directory / "closet"
    if not args.reuse_corpus:
        _corpus(root, args.count)
    index = SearchIndex("http://127.0.0.1:19200", str(root.resolve()))
    sync = Synchronizer(Documents(root), index)
    if sum(1 for _ in sync.paths()) != args.count:
        raise SystemExit("corpus count differs from requested count")
    results: dict[str, Any] = {
        "count": args.count,
        "platform": platform.platform(),
        "scope": "synthetic local observation",
    }
    try:
        results.update(_synchronize(sync))
        results["queries"] = _queries(index)
        (args.directory / "summary.json").write_text(
            json.dumps(results, ensure_ascii=False, indent=2)
        )
        print(json.dumps(results, ensure_ascii=False), flush=True)
    finally:
        index.client.options(ignore_status=404).indices.delete(index=index.name)
        index.close()


def _corpus(root: Path, count: int) -> None:
    root.mkdir(parents=True)
    for number in range(count - len(FIXTURES)):
        target = root / f"docs/{number // 1000:03d}/{number:06d}.md"
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_text(
            f"# 기록 {number}\n정원과 여행 사진을 날짜별로 정리한 합성 자료다.\n"
        )
    for name, content in FIXTURES.items():
        (root / f"docs/{name}.md").write_text(content)


def _synchronize(sync: Synchronizer) -> dict[str, Any]:
    results = {}
    for phase in ("initial", "unchanged", "single_change"):
        if phase == "single_change":
            (sync.documents.root / "docs/000/000000.md").write_text(
                "# 기록\n하나만 수정한 합성 자료"
            )
        started = time.perf_counter()
        result = sync.reconcile()
        results[phase] = {
            **result,
            "seconds": time.perf_counter() - started,
            "peak_rss_raw": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
        print(json.dumps({phase: results[phase]}), flush=True)
    started = time.perf_counter()
    results["audit"] = {**sync.audit(), "seconds": time.perf_counter() - started}
    return results


def _queries(index: SearchIndex) -> list[dict[str, Any]]:
    results = []
    for query, names in QUERIES.items():
        expected = {f"docs/{name}.md" for name in names}
        started = time.perf_counter()
        found = [hit["path"] for hit in index.find(query, 20)]
        results.append(
            {
                "query": query,
                "paths": found,
                "matched": len(expected & set(found)),
                "expected": len(expected),
                "extra": sorted(set(found) - expected),
                "milliseconds": (time.perf_counter() - started) * 1000,
            }
        )
    return results


if __name__ == "__main__":
    main()
