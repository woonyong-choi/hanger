import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from markchain.search import SearchIndex


# #4: the installed protocol keeps watch, search, writes and status in one owner.
def test_mcp_collect_search_and_control_in_one_process(tmp_path: Path) -> None:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    root.mkdir()
    try:
        asyncio.run(_scenario(root, tmp_path / "state", url))
    finally:
        index = SearchIndex(url, str(root))
        index.client.options(ignore_status=404).indices.delete(index=index.name)
        index.close()


async def _scenario(root: Path, state: Path, url: str) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "markchain.cli",
            "--vault",
            str(root),
            "--state",
            str(state),
            "--search-url",
            url,
            "--trace",
            "serve",
        ],
    )
    async with (
        stdio_client(parameters) as (reader, writer),
        ClientSession(reader, writer) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        assert {"collect", "search_documents", "request_change", "cancel_job"} <= {
            tool.name for tool in tools.tools
        }
        key = await _key(session)
        collected = await session.call_tool(
            "collect", {"key": key, "text": "# 결제\n멱등 키로 중복 방지"}
        )
        assert not collected.isError
        await _completed(session, key)
        result = await session.call_tool("search_documents", {"query": "결제"})
        assert result.structuredContent is not None
        assert (
            result.structuredContent["documents"][0]["content"]
            == "# 결제\n멱등 키로 중복 방지"
        )
        if os.environ.get("MARKCHAIN_TEST_LIVE_MODEL"):
            key = await _key(session)
            requested = await session.call_tool(
                "request_change",
                {
                    "key": key,
                    "intent": {
                        "kind": "organize",
                        "query": "결제",
                        "mode": "bypass",
                        "instruction": (
                            "합성 테스트입니다. 원문은 보존하고 docs/payment.md에 "
                            "제목 결제와 본문 멱등 키로 중복 방지를 담은 문서 한 개를 "
                            "작성하세요. 사실을 추가하지 마세요."
                        ),
                    },
                },
            )
            assert not requested.isError
            completed = await _completed(session, key)
            assert completed["usage"] is not None
            assert "멱등" in (root / "docs/payment.md").read_text()
            trace = (state / "trace.jsonl").read_text()
            assert '"input_tokens"' in trace
            assert '"stage": "model"' in trace
            assert "멱등" not in trace


async def _key(session: ClientSession) -> str:
    issued = await session.call_tool("issue_key", {})
    assert issued.structuredContent is not None
    return str(issued.structuredContent["result"])


async def _completed(session: ClientSession, key: str) -> dict[str, Any]:
    for _ in range(1200):
        response = await session.call_tool("job_status", {"key": key})
        data: dict[str, Any] = response.structuredContent or {}
        if data.get("status") not in {"queued", "planning", "running"}:
            assert data.get("status") == "completed", data
            return data
        await asyncio.sleep(0.1)
    raise AssertionError("MCP job did not complete within the scenario deadline")
