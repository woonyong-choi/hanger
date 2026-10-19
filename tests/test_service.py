import asyncio
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import offline_service, service_for
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from markdown_notes_agent.documents import Documents
from markdown_notes_agent.errors import RequestError
from markdown_notes_agent.requests import Change, Proposal, issue_key
from markdown_notes_agent.runtime import Runtime
from markdown_notes_agent.search import SearchIndex
from markdown_notes_agent.sync import Synchronizer
from markdown_notes_agent.vaults import VaultPolicy


# the installed protocol keeps watch, search, writes and status in one owner.
def test_mcp_collect_search_and_control_in_one_process(tmp_path: Path) -> None:
    url = os.environ.get("MARKDOWN_NOTES_AGENT_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    root.mkdir()
    try:
        asyncio.run(_scenario(root, tmp_path / "state", url))
    finally:
        subprocess.run(
            [
                sys.executable,
                "-m",
                "markdown_notes_agent.cli",
                "--state",
                str(tmp_path / "state"),
                "--search-url",
                url,
                "stop",
            ],
            check=False,
            timeout=120,
        )
        index = SearchIndex(url, str(root))
        index.client.options(ignore_status=404).indices.delete(index=index.name)
        index.close()


async def _scenario(root: Path, state: Path, url: str) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "markdown_notes_agent.cli",
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
        empty = await session.call_tool("list_vaults", {})
        assert empty.structuredContent is not None
        assert "register_vault" in empty.structuredContent["next"]
        added = await session.call_tool("register_vault", {"path": str(root)})
        assert not added.isError
        key = await _key(session)
        unused = await session.call_tool("job_status", {"key": key})
        assert unused.structuredContent is not None
        assert unused.structuredContent["status"] == "not_found"
        assert {"tool": "collect"} in unused.structuredContent["next_actions"]
        invalid = await session.call_tool("job_status", {"key": "unknown"})
        assert invalid.isError
        collected = await session.call_tool(
            "collect", {"key": key, "text": "# 결제\n멱등 키로 중복 방지"}
        )
        assert not collected.isError
        status = await _completed(session, key)
        assert status["message"] and status["next_actions"] == [
            {"tool": "read_document"}
        ]
        result = await session.call_tool("search_documents", {"query": "결제"})
        assert result.structuredContent is not None
        assert (
            result.structuredContent["documents"][0]["content"]
            == "# 결제\n멱등 키로 중복 방지"
        )
        scoped = await session.call_tool(
            "search_documents",
            {
                "query": "결제",
                "folders": ["missing/a", "missing/b", "inbox", "missing/c"],
            },
        )
        assert scoped.structuredContent is not None
        assert (
            scoped.structuredContent["documents"]
            == result.structuredContent["documents"]
        )
        empty = await session.call_tool(
            "search_documents", {"query": "결제", "folders": ["docs"]}
        )
        assert empty.structuredContent is not None
        assert empty.structuredContent["documents"] == []
        if os.environ.get("MARKDOWN_NOTES_AGENT_TEST_LIVE_MODEL"):
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


# a failing job never kills a worker, and a dead worker is restarted.
def test_worker_survives_errors_and_is_restarted(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    sync = Synchronizer(Documents(root), SearchIndex("http://127.0.0.1:9", str(root)))
    with Runtime(root, tmp_path / "state") as runtime:
        service = service_for(runtime, sync)
        calls = 0

        def explode() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("unexpected")

        runtime.run_next = explode  # type: ignore[method-assign]
        service._targets = {
            "markdown-notes-agent-worker-1": lambda: service._work(
                "markdown-notes-agent-worker-1"
            )
        }
        service._spawn("markdown-notes-agent-worker-1")
        time.sleep(3)
        assert calls >= 2
        assert service._threads["markdown-notes-agent-worker-1"].is_alive()
        assert service.health() is None
        service._stop.set()
        service._threads["markdown-notes-agent-worker-1"].join(5)
        service._stop.clear()
        service._beats["markdown-notes-agent-worker-1"] = time.monotonic() - 3600
        assert service.health() == (
            "A job worker has not responded for more than 60 seconds."
        )
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        service._threads["markdown-notes-agent-worker-1"] = dead
        monitor = threading.Thread(target=service._maintain, daemon=True)
        monitor.start()
        time.sleep(6)
        assert service._threads["markdown-notes-agent-worker-1"] is not dead
        service.close()


class _Engine:
    def __init__(self, ready_after: int) -> None:
        self.calls = 0
        self.ready_after = ready_after

    def ready(self) -> bool:
        self.calls += 1
        return self.calls > self.ready_after

    def message(self) -> str:
        return f"검색 서버 시작 중 ({self.calls}초 경과)"

    def require(self) -> None:
        if not self.ready():
            from markdown_notes_agent.errors import SearchUnavailableError

            raise SearchUnavailableError(self.message())


class _Context:
    def __init__(self) -> None:
        self.progress: list[tuple[float, float | None, str | None]] = []
        self.logs: list[str] = []

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        self.progress.append((progress, total, message))

    async def info(self, message: str) -> None:
        self.logs.append(message)


# a search during server start waits, reports elapsed seconds, then fails at 30s.
def test_search_waits_for_starting_server_with_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from markdown_notes_agent import service as service_module
    from markdown_notes_agent.errors import SearchUnavailableError

    root = tmp_path / "vault"
    root.mkdir()
    sync = Synchronizer(Documents(root), SearchIndex("http://127.0.0.1:9", str(root)))
    with Runtime(root, tmp_path / "state") as runtime:
        service = service_for(runtime, sync, _Engine(ready_after=2))
        monkeypatch.setattr(service, "search", lambda *args: {"documents": []})
        context = _Context()
        result = asyncio.run(service.search_documents("결제", ctx=context))  # type: ignore[arg-type]
        assert result == {"documents": []}
        assert [p[0] for p in context.progress] == [0, 1]
        assert context.logs[0].startswith("검색 서버 시작 중")
        monkeypatch.setattr(service_module, "SEARCH_WAIT_SECONDS", 2)
        service.engine = _Engine(ready_after=100)  # type: ignore[assignment]
        with pytest.raises(SearchUnavailableError, match="not ready within 2s"):
            asyncio.run(service.search_documents("결제", ctx=_Context()))  # type: ignore[arg-type]


# read_document honours the ignore rules.
def test_read_document_rejects_ignored(
    layout: tuple[Path, Path, VaultPolicy],
) -> None:
    root, state, policy = layout
    service, runtime = offline_service(root, state, policy)
    with runtime:
        assert service.read_document("notes/a.md")["content"] == "# a"
        for path in ("other/c.md", "notes/private/s.md"):
            with pytest.raises(RequestError) as caught:
                service.read_document(path)
            assert caught.value.code == "excluded"


# A missing document is a clear not_found error without the absolute path.
def test_read_document_missing_is_not_found(
    layout: tuple[Path, Path, VaultPolicy],
) -> None:
    root, state, policy = layout
    service, runtime = offline_service(root, state, policy)
    with runtime, pytest.raises(RequestError) as caught:
        service.read_document("notes/missing.md")
    assert caught.value.code == "not_found"
    assert str(root) not in str(caught.value)


# Proposals submitted directly obey the same write policy as model edits.
def test_direct_proposal_outside_writable_is_rejected(
    layout: tuple[Path, Path, VaultPolicy],
) -> None:
    root, state, policy = layout
    with (
        Runtime(root, state) as runtime,
        pytest.raises(RequestError) as caught,
    ):
        runtime.policy = policy
        runtime.submit(
            issue_key(runtime._secret),
            Proposal(changes=(Change(path="other/x.md", after="x"),)),
        )
    assert caught.value.code == "write_denied"
    assert not (root / "other/x.md").exists()


def test_mcp_exposes_vault_contract_without_registered_folders(tmp_path: Path) -> None:
    state = tmp_path / "state"
    arguments = [
        "-m",
        "markdown_notes_agent.cli",
        "--state",
        str(state),
        "--search-url",
        "http://127.0.0.1:9",
    ]

    async def scenario() -> None:
        parameters = StdioServerParameters(
            command=sys.executable, args=[*arguments, "serve"]
        )
        async with (
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "markdown-notes-agent"
            response = await session.list_tools()
            tools = {tool.name: tool for tool in response.tools}
            assert {"list_vaults", "register_vault", "remove_vault"} <= tools.keys()
            assert "vault" in tools["search_documents"].inputSchema["properties"]
            assert "vault" in tools["remove_vault"].inputSchema["required"]
            listed = await session.call_tool("list_vaults", {})
            assert not listed.isError
            assert listed.structuredContent is not None
            assert listed.structuredContent["vaults"] == []

    try:
        asyncio.run(scenario())
    finally:
        subprocess.run([sys.executable, *arguments, "stop"], check=True, timeout=30)
