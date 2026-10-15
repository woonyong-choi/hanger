"""Measure issue #9 with synthetic sources and the same model/review path."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from markchain.documents import Documents
from markchain.model import ClaudeModel, Intent, Planner
from markchain.requests import digest
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.sync import Synchronizer
from markchain.trace import Trace


class FullEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str
    content: str


class FullDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["keep", "link", "supplement", "merge", "new", "defer"]
    reason: str
    sources: list[str]
    uncertainties: list[str]
    changes: list[FullEdit]


class FullModel(ClaudeModel):
    def __init__(self, source: str) -> None:
        super().__init__()
        self.source = source

    # cost: time O(n+t), heap O(n), stack O(1), io 1, tokens O(n)
    # vars: n = source and result bytes, t = provider latency
    # basis: estimate
    def generate(self, prompt: str, is_cancelled: Any) -> dict[str, Any]:
        start = prompt.index("For existing files")
        end = prompt.index("Keep/defer", start)
        prompt = (
            prompt[:start]
            + "Return the COMPLETE replacement content for each changed file. "
            + prompt[end:]
        )
        response = self._invoke(prompt, is_cancelled, FullDecision)
        decision = response["value"]
        decision["changes"] = [
            {
                "path": edit["path"],
                "operation": "replace",
                "replacements": [{"old": self.source, "new": edit["content"]}],
            }
            for edit in decision["changes"]
        ]
        return {"decision": decision, "usage": response["usage"]}


def _cases() -> list[dict[str, str]]:
    paragraphs = "\n".join(
        f"## 사례 {number}\n가상 서비스의 원문 예시 {number}다. "
        "적용 범위와 출처를 보존한다.\n"
        for number in range(60)
    )
    return [
        {
            "id": "correction",
            "profile": "wiki",
            "source": "# 가상 서비스\n상태: 초안\n"
            + paragraphs
            + "\n예외: 오프라인 작업은 별도 검토한다.\n",
            "old": "상태: 초안",
            "new": "상태: 정리",
        },
        {
            "id": "supplement",
            "profile": "work_record",
            "source": "# 검증 기록\n"
            + paragraphs
            + "\n## 판정\n실행 여부: 예정\n출처: 합성 실험 조건\n",
            "old": "실행 여부: 예정",
            "new": "실행 여부: 예정\n측정 결과는 아직 없다.",
        },
    ]


def _profiles() -> list[dict[str, str]]:
    return [
        {
            "id": "wiki",
            "profile": "wiki",
            "source": "# 가상 시스템\n키는 24시간 안에만 유효하다.\n## 재시도\n"
            "무조건 같은 키를 재전송한다.\n출처: 가상 시스템의 주어진 규칙\n",
            "old": "무조건 같은 키를 재전송한다.",
            "new": "키가 유효한 24시간 안에서만 같은 키를 재전송한다.",
        },
        {
            "id": "work-record",
            "profile": "work_record",
            "source": "# 실험 기록\n실행 계획: 다음 주\n판정: 검증 완료\n"
            "가설: 실제 측정 전이다.\n",
            "old": "판정: 검증 완료",
            "new": "판정: 검증 예정",
        },
        {
            "id": "blog",
            "profile": "blog",
            "source": "# 내가 겪은 재시도\n테스트에서 두 번 저장되는 현상을 관찰했다.\n"
            "모든 시스템에서 이 오류가 생긴다.\n환경: 가상 실험 서버\n",
            "old": "모든 시스템에서 이 오류가 생긴다.",
            "new": "내 가상 실험 서버에서 이 오류를 관찰했다.",
        },
        {
            "id": "creative",
            "profile": "creative",
            "source": "# 푸른 숲\n설정: 민아의 눈은 푸른색이다.\n"
            "민아는 갈색 눈을 감았다.\n그녀가 왜 웃었는지는 아무도 몰랐다.\n",
            "old": "민아는 갈색 눈을 감았다.",
            "new": "민아는 푸른 눈을 감았다.",
        },
    ]


# cost: time O(n+t), heap O(n), stack O(d), io O(f), tokens O(n)
# vars: n = source/context bytes, t = model latency, d = path depth, f = checked files
# basis: estimate
def _run(case: dict[str, str], mode: str, output: Path) -> dict[str, Any]:
    folder = output / (case["id"] + "-" + mode)
    root, state = folder / "vault", folder / "state"
    (root / "docs").mkdir(parents=True)
    target = root / "docs/a.md"
    target.write_text(case["source"], encoding="utf-8")
    index = SearchIndex(os.environ["MARKCHAIN_TEST_SEARCH_URL"], str(root.resolve()))
    trace = Trace(state / "trace.jsonl")
    model = FullModel(case["source"]) if mode == "full" else ClaudeModel()
    sync = Synchronizer(Documents(root), index)
    sync.reconcile()
    started = time.perf_counter()
    try:
        with Runtime(root, state, Planner(sync, model), trace=trace) as runtime:
            intent = Intent.model_validate(
                {
                    "kind": "edit",
                    "profile": case["profile"],
                    "query": case["source"].splitlines()[0].lstrip("# "),
                    "folders": ["missing/one", "docs", "missing/two"],
                    "instruction": "합성 자료다. docs/a.md의 문구를 정확하게 교체한다. "
                    "다른 본문, 줄바꿈, 출처를 그대로 보존한다.\n기존: "
                    + case["old"]
                    + "\n변경: "
                    + case["new"],
                }
            )
            key = runtime.issue_key()
            runtime.submit_intent(key, intent)
            result = runtime.run_next() or {}
        observed = target.read_text(encoding="utf-8")
        expected = case["source"].replace(case["old"], case["new"])
        trace.close()
        events = [
            json.loads(line)
            for line in (state / "trace.jsonl").read_text().splitlines()
        ]
        return {
            "case": case["id"],
            "profile": case["profile"],
            "mode": mode,
            "elapsed_seconds": time.perf_counter() - started,
            "source_hash": digest(case["source"].encode()),
            "source_bytes": len(case["source"].encode()),
            "preserved": observed == expected,
            "status": result.get("status"),
            "decision": result.get("decision"),
            "review": result.get("review"),
            "error": result.get("error"),
            "usage": result.get("usage"),
            "events": events,
            "expected": expected,
            "observed": observed,
        }
    finally:
        trace.close()
        index.client.indices.delete(index=index.name)
        index.close()


# cost: time O(n), heap O(n), stack O(1), io O(1)
# vars: n = recorded run bytes
# basis: estimate
def _analyze(output: Path) -> None:
    rows = [
        json.loads(line) for line in (output / "raw.jsonl").read_text().splitlines()
    ]
    metrics = []
    for row in rows:
        stages = [
            event for event in row["events"] if event["stage"] in {"model", "review"}
        ]
        metrics.append(
            {
                "case": row["case"],
                "mode": row["mode"],
                "status": row["status"],
                "preserved": row["preserved"],
                "elapsed_seconds": row["elapsed_seconds"],
                "calls": len(stages),
                "input_tokens": sum(
                    event.get("input_tokens", 0)
                    + event.get("cache_read_input_tokens", 0)
                    + event.get("cache_creation_input_tokens", 0)
                    for event in stages
                ),
                "output_tokens": sum(event.get("output_tokens", 0) for event in stages),
                "draft_output_tokens": next(
                    (
                        event.get("output_tokens")
                        for event in stages
                        if event["stage"] == "model"
                    ),
                    None,
                ),
                "usage_available": all(
                    "input_tokens" in event and "output_tokens" in event
                    for event in stages
                ),
            }
        )
    (output / "summary.json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2) + "\n"
    )


# cost: time O(n+t), heap O(n), stack O(d), io O(f), tokens O(n)
# vars: n = all bounded case bytes, t = model latency, d = depth, f = checked files
# basis: estimate
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, default=Path("build/workflow-measurement")
    )
    output = parser.parse_args().output
    output.mkdir(parents=True, exist_ok=True)
    raw = output / "raw.jsonl"
    if raw.exists():
        raise RuntimeError("measurement already exists; preserve prior evidence")
    environment = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "model": "sonnet",
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip(),
        "source_hashes": {
            str(p): digest(p.read_bytes()) for p in Path("src/markchain").glob("*.py")
        },
    }
    (output / "env.json").write_text(json.dumps(environment, indent=2) + "\n")
    cases = _cases()
    runs = [
        (cases[0], "full"),
        (cases[0], "partial"),
        (cases[1], "partial"),
        (cases[1], "full"),
    ]
    runs += [(case, "partial") for case in _profiles()]
    for case, mode in runs:
        row = _run(case, mode, output)
        with raw.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        _analyze(output)
        print(
            json.dumps(
                {
                    k: row[k]
                    for k in ("case", "mode", "status", "preserved", "elapsed_seconds")
                }
            ),
            flush=True,
        )
        if row["status"] != "completed" or not row["preserved"]:
            raise RuntimeError("measurement failed; preserve evidence and investigate")


if __name__ == "__main__":
    main()
