"""Exercise issue #9 through an installed MCP process with synthetic files."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from hanger.search import SearchIndex

SOURCE = "# 재시도\n기본 재시도는 2회다.\n예외: 취소된 요청은 재시도하지 않는다.\n"
OPERATIONS = "# 운영\n재시도 횟수: 2회\n출처: 합성 서비스의 운영 규칙\n"


class Scenario:
    def __init__(self, output: Path, python: str, url: str) -> None:
        self.output = output.resolve()
        self.root = self.output / "closet"
        self.state = self.output / "state"
        self.python = python
        self.url = url
        self.checks: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.session: ClientSession
        self.completed_key = ""
        self.collected_path = ""

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = synthetic source bytes
    # basis: estimate
    def setup(self) -> None:
        self.output.mkdir(parents=True, exist_ok=False)
        for folder in ("docs/backend", "worktree/demo/docs", "private"):
            (self.root / folder).mkdir(parents=True)
        (self.root / "docs/backend/retry.md").write_text(SOURCE)
        (self.root / "worktree/demo/docs/operations.md").write_text(OPERATIONS)
        (self.root / "private/retry.md").write_text("# 재시도\n범위 밖: 99회")

    # cost: time O(n+t), heap O(n), stack O(1), io 2
    # vars: n = protocol bytes, t = server processing latency
    # basis: estimate
    async def call(self, name: str, values: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        response = await self.session.call_tool(name, values)
        row = {
            "tool": name,
            "input": values,
            "error": response.isError,
            "data": response.structuredContent or {},
            "text": [c.text for c in response.content if c.type == "text"],
            "seconds": time.perf_counter() - started,
        }
        self.events.append(row)
        self.save()
        return row

    def check(self, name: str, *, passed: bool, detail: Any = None) -> None:
        self.checks.append({"name": name, "passed": passed, "detail": detail})
        self.save()

    # cost: time O(n), heap O(n), stack O(1), io 2
    # vars: n = bounded scenario records
    # basis: estimate
    def save(self) -> None:
        (self.output / "events.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.events)
        )
        (self.output / "checks.json").write_text(
            json.dumps(self.checks, ensure_ascii=False, indent=2) + "\n"
        )

    async def key(self) -> str:
        return str((await self.call("issue_key", {}))["data"]["result"])

    # cost: time O(t*n), heap O(t*n), stack O(1), io O(t)
    # vars: t = bounded poll count, n = status response bytes
    # basis: estimate
    async def wait(self, key: str) -> dict[str, Any]:
        for _ in range(240):
            result = await self.call("job_status", {"key": key})
            if result["data"].get("status") not in {"queued", "planning", "running"}:
                return dict(result["data"])
            await asyncio.sleep(0.5)
        raise AssertionError("scenario exceeded 120 seconds")

    # cost: time O(n+t), heap O(n), stack O(1), io O(t)
    # vars: n = source and protocol bytes, t = server latency and bounded polling
    # basis: estimate
    async def intake(self) -> None:
        tools = await self.session.list_tools()
        self.events.append(
            {"tools": [tool.model_dump(mode="json") for tool in tools.tools]}
        )
        unknown = await self.call("job_status", {"key": "unknown"})
        self.check(
            "unknown status is actionable",
            passed=bool(unknown["error"] or unknown["data"].get("status")),
            detail=unknown,
        )
        key = await self.key()
        unused = await self.call("job_status", {"key": key})
        self.check(
            "unused key is explained",
            passed=bool(unused["data"].get("status")),
            detail=unused,
        )
        text = "# 수집 메모\n재시도 테스트의 근거를 원문 그대로 보관한다."
        collected = await self.call("collect", {"key": key, "text": text})
        self.collected_path = collected["data"]["path"]
        completed = await self.wait(key)
        self.completed_key = key
        self.check(
            "collection persisted",
            passed=completed.get("status") == "completed"
            and (self.root / self.collected_path).read_text() == text,
        )
        duplicate = await self.call("collect", {"key": key, "text": text})
        self.check(
            "duplicate collection blocked",
            passed=duplicate["data"].get("status") == "duplicate",
        )
        collision = await self.call("collect", {"key": key, "text": text + "변경"})
        self.check("changed payload rejected", passed=bool(collision["error"]))
        invalid = await self.call(
            "search_documents", {"query": "재시도", "folders": ["../"]}
        )
        self.check("invalid folder rejected", passed=bool(invalid["error"]))
        found = await self.call(
            "search_documents",
            {
                "query": "재시도",
                "folders": ["docs/backend", "worktree/demo/docs", "missing"],
            },
        )
        self.check(
            "folder search returns both sources",
            passed={d["path"] for d in found["data"]["documents"]}
            == {"docs/backend/retry.md", "worktree/demo/docs/operations.md"},
        )
        empty = await self.call(
            "search_documents", {"query": "재시도", "folders": ["missing"]}
        )
        self.check(
            "empty search distinguishes failure",
            passed=not empty["error"]
            and empty["data"]["documents"] == []
            and empty["data"].get("source") == "elasticsearch",
        )

    def intent(self, instruction: str) -> dict[str, Any]:
        return {
            "kind": "edit",
            "profile": "wiki",
            "query": "재시도",
            "folders": ["docs/backend", "worktree/demo/docs", "missing"],
            "instruction": instruction,
        }

    # cost: time O(n+t), heap O(n), stack O(1), io O(t), tokens O(n)
    # vars: n = synthetic context and output, t = bounded server latency
    # basis: estimate
    async def edit(self) -> None:
        key = await self.key()
        intent = self.intent(
            "합성 자료다. docs/backend/retry.md의 '기본 재시도는 2회다.'를 "
            "'기본 재시도는 3회다.'로, worktree/demo/docs/operations.md의 "
            "'재시도 횟수: 2회'를 '재시도 횟수: 3회'로 고친다. "
            "나머지 예외·출처·줄바꿈은 보존한다."
        )
        await self.call("request_change", {"key": key, "intent": intent})
        result = await self.wait(key)
        expected = SOURCE.replace("2회", "3회")
        self.check(
            "two-file edit preserves exception and source",
            passed=result.get("status") == "completed"
            and (self.root / "docs/backend/retry.md").read_text() == expected
            and (self.root / "worktree/demo/docs/operations.md").read_text()
            == OPERATIONS.replace("2회", "3회"),
        )
        self.check(
            "completed status explains next step",
            passed=bool(result.get("next_actions") and result.get("message")),
            detail={k: result.get(k) for k in ("status", "message", "next_actions")},
        )
        duplicate = await self.call("request_change", {"key": key, "intent": intent})
        self.check(
            "completed edit is not regenerated",
            passed=duplicate["data"].get("status") == "duplicate",
        )

    # cost: time O(p*t), heap O(p), stack O(1), io O(t)
    # vars: p = process listing bytes, t = bounded polling attempts
    # basis: estimate
    async def wait_model(self) -> None:
        for _ in range(300):
            rows = subprocess.check_output(
                ["ps", "-axo", "pid=,ppid=,command="], text=True
            ).splitlines()
            server = [
                row.split(None, 2)[0]
                for row in rows
                if f"-m hanger.cli --closet {self.root}" in row
            ]
            if any(
                row.split(None, 2)[1] in server and "--output-format json" in row
                for row in rows
            ):
                return
            await asyncio.sleep(0.1)
        raise AssertionError("model subprocess did not start")

    # cost: time O(n+t), heap O(n), stack O(1), io O(t), tokens O(n)
    # vars: n = synthetic context and output, t = bounded server latency
    # basis: estimate
    async def conflict(self) -> None:
        key = await self.key()
        intent = self.intent(
            "docs/backend/retry.md에서 기본 재시도는 4회다.로 바꾸고 "
            "나머지와 사용자 메모를 보존한다. 합성 규칙의 명시적 변경이다."
        )
        await self.call("request_change", {"key": key, "intent": intent})
        await self.wait_model()
        path = self.root / "docs/backend/retry.md"
        changed = path.read_text() + "사용자 메모: 배포 전 검토 필요\n"
        path.write_text(changed)
        result = await self.wait(key)
        self.check(
            "external edit is protected",
            passed=result.get("status") == "conflict" and path.read_text() == changed,
        )
        self.check(
            "conflict offers valid recovery choices",
            passed=bool(result.get("next_actions") and result.get("message")),
            detail={k: result.get(k) for k in ("status", "error", "next_actions")},
        )
        next_key = await self.key()
        await self.call(
            "resolve_conflict",
            {"key": key, "new_key": next_key, "choice": "reconsider"},
        )
        resumed = await self.wait(next_key)
        self.check(
            "reconsider retains user edit",
            passed=resumed.get("status") == "completed"
            and path.read_text()
            == SOURCE.replace("2회", "4회") + "사용자 메모: 배포 전 검토 필요\n",
        )

    # cost: time O(n+t), heap O(n), stack O(1), io O(t)
    # vars: n = source and protocol bytes, t = bounded server latency
    # basis: estimate
    async def cancel(self) -> None:
        key = await self.key()
        path = self.root / "docs/backend/retry.md"
        original = path.read_bytes()
        await self.call(
            "request_change",
            {"key": key, "intent": self.intent("기본 재시도를 5회로 바꾼다.")},
        )
        stopped = await self.call("cancel_job", {"key": key})
        result = await self.wait(key)
        self.check(
            "cancel prevents remaining changes",
            passed=bool(stopped["data"].get("cancelled"))
            and result.get("status") == "cancelled"
            and path.read_bytes() == original,
        )

    # cost: time O(n*t), heap O(n*t), stack O(1), io O(t)
    # vars: n = search response bytes, t = bounded polling attempts
    # basis: estimate
    async def relocate(self) -> None:
        source = self.root / "worktree/demo/docs/operations.md"
        destination = source.with_name("procedures.md")
        source.rename(destination)
        for deleted in (False, True):
            if deleted:
                destination.unlink()
            paths: set[str] = set()
            expected = {"docs/backend/retry.md"}
            if not deleted:
                expected.add("worktree/demo/docs/procedures.md")
            for _ in range(40):
                result = await self.call(
                    "search_documents",
                    {
                        "query": "재시도",
                        "folders": ["docs/backend", "worktree/demo/docs"],
                    },
                )
                paths = {d["path"] for d in result["data"].get("documents", [])}
                if paths == expected:
                    break
                await asyncio.sleep(0.25)
            self.check(
                "deleted file disappears" if deleted else "renamed file is found",
                passed=paths == expected,
                detail=sorted(paths),
            )

    # cost: time O(n+t), heap O(n), stack O(1), io O(t)
    # vars: n = protocol/source bytes, t = process and service lifetime
    # basis: estimate
    async def run(self, *, live_model: bool) -> None:
        params = StdioServerParameters(
            command=self.python,
            args=[
                "-m",
                "hanger.cli",
                "--closet",
                str(self.root),
                "--state",
                str(self.state),
                "--search-url",
                self.url,
                "--trace",
                "serve",
            ],
        )
        for first in (True, False):
            with (self.output / ("server.log" if first else "restart.log")).open(
                "w"
            ) as errors:
                async with (
                    stdio_client(params, errlog=errors) as (reader, writer),
                    ClientSession(reader, writer) as session,
                ):
                    self.session = session
                    await session.initialize()
                    if first:
                        await self.intake()
                        if live_model:
                            await self.edit()
                            await self.conflict()
                        await self.cancel()
                        await self.relocate()
                    else:
                        result = await self.call(
                            "job_status", {"key": self.completed_key}
                        )
                        self.check(
                            "completed state survives reconnect",
                            passed=result["data"].get("status") == "completed",
                        )
                        read = await self.call(
                            "read_document", {"path": self.collected_path}
                        )
                        self.check(
                            "collected original survives reconnect",
                            passed="원문 그대로" in read["data"].get("content", ""),
                        )


# cost: time O(n+t), heap O(n), stack O(1), io O(t), tokens O(n)
# vars: n = synthetic context and recorded results, t = bounded scenario duration
# basis: estimate
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--live-model", action="store_true")
    args = parser.parse_args()
    url = os.environ["HANGER_TEST_SEARCH_URL"]
    scenario = Scenario(args.output, args.python, url)
    scenario.setup()
    try:
        asyncio.run(scenario.run(live_model=args.live_model))
    finally:
        index = SearchIndex(url, str(scenario.root))
        index.client.options(ignore_status=404).indices.delete(index=index.name)
        index.close()
        scenario.save()
    print(json.dumps(scenario.checks, ensure_ascii=False, indent=2))
    if not all(check["passed"] for check in scenario.checks):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
