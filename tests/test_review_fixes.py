import copy
import os
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from markchain.config import Settings
from markchain.context import shape
from markchain.documents import Documents
from markchain.editing import Decision, EditPolicy, materialize
from markchain.errors import RequestError, ReviewRejectedError
from markchain.model import ClaudeModel, Intent, Planner
from markchain.requests import digest
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.service import Service
from markchain.sync import Synchronizer


def _edit(path: str, old: str, new: str) -> dict[str, Any]:
    return {
        "action": "supplement",
        "reason": "수정",
        "uncertainties": [],
        "sources": [path],
        "changes": [
            {
                "path": path,
                "operation": "replace",
                "replacements": [{"old": old, "new": new}],
            }
        ],
    }


class Model(ClaudeModel):
    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.prompts: list[str] = []

    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.prompts.append(prompt)
        decision = (
            self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]
        )
        return {"decision": copy.deepcopy(decision), "usage": None}

    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.prompts.append(prompt)
        check = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
        return {"review": check, "usage": None}


@pytest.fixture
def vault(tmp_path: Path) -> Iterator[tuple[Path, Path, str]]:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    try:
        yield root, tmp_path / "state", url
    finally:
        index = SearchIndex(url, str(root))
        index.client.options(ignore_status=404).indices.delete(index=index.name)
        index.close()


def _runtime(
    root: Path, state: Path, url: str, model: Model, settings: Settings | None = None
) -> tuple[Runtime, Synchronizer]:
    settings = settings or Settings()
    sync = Synchronizer(Documents(root), SearchIndex(url, str(root)), settings)
    sync.reconcile()
    return Runtime(root, state, Planner(sync, model), settings=settings), sync


def _finish(runtime: Runtime) -> None:
    while runtime.run_next():
        pass


# R3: many matching documents no longer fail; extra ones are listed, not read.
def test_large_candidate_set_is_listed_instead_of_failing(
    vault: tuple[Path, Path, str],
) -> None:
    root, state, url = vault
    body = "결제 재시도 정책은 서비스마다 다르다. " * 300
    for number in range(12):
        (root / f"docs/n{number:02d}.md").write_text(f"# 결제 {number}\n{body}")
    model = Model()
    model.decisions = [_edit("docs/n00.md", "# 결제 0", "# 결제 0 (수정)")]
    model.checks = [{"approved": True, "issues": []}]
    runtime, _ = _runtime(root, state, url, model)
    with runtime:
        key = runtime.issue_key()
        runtime.submit_intent(
            key, Intent(kind="edit", instruction="수정", query="결제")
        )
        _finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert 1 <= len(status["search"]["read"]) <= 5
    assert len(status["search"]["read"]) + len(status["search"]["listed"]) == 12
    assert "OTHER MATCHED DOCUMENTS" in model.prompts[0]


# R3: the model can ask for a listed document and decide after reading it.
def test_read_more_loads_listed_document(vault: tuple[Path, Path, str]) -> None:
    root, state, url = vault
    for number in range(8):
        (root / f"docs/n{number}.md").write_text(f"# 결제 {number}\n결제 본문 {number}")
    model = Model()
    model.decisions = [
        {
            "action": "defer",
            "reason": "더 읽기",
            "uncertainties": [],
            "sources": [],
            "changes": [],
            "read_more": ["docs/n7.md", "docs/n6.md"],
        },
        _edit("docs/n7.md", "결제 본문 7", "결제 본문 7 보완"),
    ]
    model.checks = [{"approved": True, "issues": []}]
    settings = Settings.model_validate({"search": {"full_text": 1}})
    runtime, _ = _runtime(root, state, url, model, settings)
    with runtime:
        key = runtime.issue_key()
        runtime.submit_intent(
            key, Intent(kind="edit", instruction="보완", query="결제")
        )
        _finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert (root / "docs/n7.md").read_text().endswith("보완")
    assert "docs/n7.md" in status["search"]["read"]


# 6: review rejections are retried with feedback, then reported briefly.
@pytest.mark.parametrize("approve_on", [2, None])
def test_review_rejection_retries_with_feedback(
    vault: tuple[Path, Path, str], approve_on: int | None
) -> None:
    root, state, url = vault
    (root / "docs/a.md").write_text("# 결제\n재시도 2회")
    model = Model()
    model.decisions = [_edit("docs/a.md", "재시도 2회", "재시도 3회")]
    rejected = {"approved": False, "issues": ["출처 없는 수치"]}
    approved = {"approved": True, "issues": []}
    model.checks = [rejected] * 3 if approve_on is None else [rejected, approved]
    runtime, _ = _runtime(root, state, url, model)
    with runtime:
        key = runtime.issue_key()
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        _finish(runtime)
        status = runtime.describe(key)
    feedback_prompts = [p for p in model.prompts if "출처 없는 수치" in p]
    assert feedback_prompts
    if approve_on is None:
        assert status["status"] == "failed"
        assert status["message"] == "검토 3회 거절: 출처 없는 수치"
        assert status["error_code"] == "review_rejected"
        assert (root / "docs/a.md").read_text() == "# 결제\n재시도 2회"
    else:
        assert status["status"] == "completed"
        assert (root / "docs/a.md").read_text() == "# 결제\n재시도 3회"


# 6: the configured default mode applies when a request omits it.
def test_configured_bypass_reconsiders_after_source_change(
    vault: tuple[Path, Path, str],
) -> None:
    root, state, url = vault
    target = root / "docs/a.md"
    target.write_text("# 결제\n재시도 2회")
    model = Model()
    calls = 0

    def generate(prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text("# 결제\n재시도 2회\n사용자 메모")
        return {
            "decision": _edit("docs/a.md", "재시도 2회", "재시도 3회"),
            "usage": None,
        }

    model.generate = generate  # type: ignore[method-assign]
    model.checks = [{"approved": True, "issues": []}]
    settings = Settings.model_validate({"jobs": {"default_mode": "bypass"}})
    runtime, _ = _runtime(root, state, url, model, settings)
    with runtime:
        key = runtime.issue_key()
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        _finish(runtime)
        status = runtime.describe(key)
    assert status["intent"]["mode"] == "bypass"
    assert status["status"] == "completed"
    assert target.read_text() == "# 결제\n재시도 3회\n사용자 메모"


# R5/7: a reconsidered job reports only its own file effects.
def test_reconsider_does_not_claim_previous_results(
    vault: tuple[Path, Path, str],
) -> None:
    root, state, url = vault
    for name in "ab":
        (root / f"docs/{name}.md").write_text(f"# {name} 결제\n재시도 2회")
    model = Model()
    both = {
        **_edit("docs/a.md", "재시도 2회", "재시도 3회"),
        "sources": ["docs/a.md", "docs/b.md"],
        "changes": [
            {
                "path": f"docs/{name}.md",
                "operation": "replace",
                "replacements": [{"old": "재시도 2회", "new": "재시도 3회"}],
            }
            for name in "ab"
        ],
    }
    keep = {
        "action": "keep",
        "reason": "이미 반영",
        "uncertainties": [],
        "sources": [],
        "changes": [],
    }
    model.decisions = [both, keep]
    model.checks = [{"approved": True, "issues": []}]
    original = Documents.apply

    def apply(documents: Documents, change: Any) -> str:
        outcome = original(documents, change)
        if change.path == "docs/a.md":
            (root / "docs/b.md").write_text("# b 결제\n사용자가 직접 수정")
        return outcome

    runtime, _ = _runtime(root, state, url, model)
    with runtime:
        pytest.MonkeyPatch().setattr(Documents, "apply", apply)
        key = runtime.issue_key()
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        _finish(runtime)
        assert runtime.describe(key)["status"] == "conflict"
        Documents.apply = original  # type: ignore[method-assign]
        second = runtime.issue_key()
        runtime.reconsider(key, second)
        _finish(runtime)
        status = runtime.describe(second)
    assert status["status"] == "completed"
    assert status["results"] == {}
    assert status["prior_results"]["docs/a.md"] == "result_present"
    assert status["message"] == "완료: 변경 없음"


# 8: an edit that retypes most of a document is held unless rewrite is allowed.
def test_large_rewrite_is_held_without_permission(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    content = "# 문서\n" + "".join(
        f"문단 {n}: 내용이 길게 이어진다.\n" for n in range(200)
    )
    sources = {
        "a.md": {"content": content, "hash": digest(content.encode()), "path": "a.md"}
    }
    rewritten = content.replace("내용이 길게 이어진다", "새 내용")
    decision = Decision.model_validate(_edit("a.md", content, rewritten))
    with pytest.raises(RequestError) as error:
        materialize(decision, sources, Documents(root))
    assert error.value.code == "change_too_large"
    allowed = materialize(
        decision, sources, Documents(root), EditPolicy(allow_rewrite=True)
    )
    assert allowed["changes"][0]["after"] == rewritten
    small = Decision.model_validate(_edit("a.md", content, content + "추가 한 줄\n"))
    assert materialize(small, sources, Documents(root))


# 8: a re-plan may not add text that the earlier attempt already wrote.
def test_repeated_addition_after_partial_apply_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    added = "\n만료된 키는 새 요청으로 처리한다는 예외를 둔다."
    content = "# 결제\n재시도 2회" + added
    sources = {"a.md": {"content": content, "hash": digest(content.encode())}}
    decision = Decision.model_validate(
        _edit("a.md", "재시도 2회", "재시도 2회" + added)
    )
    with pytest.raises(ReviewRejectedError):
        materialize(
            decision, sources, Documents(root), EditPolicy(applied=frozenset({"a.md"}))
        )
    assert materialize(decision, sources, Documents(root))


# 5: configured write roots and exclusions are enforced before any model call.
def test_configured_layout_rules(vault: tuple[Path, Path, str]) -> None:
    root, state, url = vault
    (root / "notes").mkdir()
    (root / "notes/a.md").write_text("# 결제\n재시도 2회")
    (root / "docs/agent").mkdir()
    (root / "docs/agent/b.md").write_text("# 결제\n에이전트")
    settings = Settings.model_validate(
        {"vault": {"writable": ["docs", "inbox"], "exclude": ["agent"]}}
    )
    model = Model()
    model.decisions = [_edit("notes/a.md", "재시도 2회", "재시도 3회")]
    model.checks = [{"approved": True, "issues": []}]
    runtime, sync = _runtime(root, state, url, model, settings)
    with runtime:
        with pytest.raises(RequestError, match="검색 제외 폴더"):
            runtime.submit_intent(
                runtime.issue_key(),
                Intent(
                    kind="edit", instruction="x", query="결제", folders=("docs/agent",)
                ),
            )
        with pytest.raises(RequestError, match="검색 제외 폴더"):
            sync.search("결제", folders=("docs/agent",))
        found = {d["path"] for d in sync.search("결제")["documents"]}
        assert found == {"notes/a.md"}
        key = runtime.issue_key()
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        _finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "failed"
    assert status["message"] == "쓰기 금지 경로: notes/a.md"
    assert all(a.get("args", {}).get("choice") is None for a in status["next_actions"])
    assert "Writable folders: docs, inbox." in model.prompts[0]


# 4/R3: context shaping keeps required inputs and stays inside the fill budget.
def test_shape_respects_budget_and_required_inputs() -> None:
    settings = Settings.model_validate(
        {"search": {"context_kib": 16, "full_text": 5, "context_fill": 0.5}}
    )
    ranked = [
        {"path": f"d{n}.md", "title": "t", "content": "결제 " * 1000, "hash": "h"}
        for n in range(5)
    ]
    required = {
        "in.md": {"path": "in.md", "title": "i", "content": "입력", "hash": "h"}
    }
    full, listed = shape(ranked, required, settings, ["결제"])
    assert "in.md" in full
    assert len(full) - 1 < 5 and listed
    assert listed[0]["snippets"]
    huge = {
        "x.md": {"path": "x.md", "title": "x", "content": "가" * 20000, "hash": "h"}
    }
    with pytest.raises(RequestError, match="입력 문서가 너무 큽니다"):
        shape([], huge, settings, [])


# 1/R1: a failing job never kills a worker, and a dead worker is restarted.
def test_worker_survives_errors_and_is_restarted(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    sync = Synchronizer(Documents(root), SearchIndex("http://127.0.0.1:9", str(root)))
    with Runtime(root, tmp_path / "state") as runtime:
        service = Service(runtime, sync)
        calls = 0

        def explode() -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("unexpected")

        runtime.run_next = explode  # type: ignore[method-assign]
        service._targets = {
            "markchain-worker-1": lambda: service._work("markchain-worker-1")
        }
        service._spawn("markchain-worker-1")
        time.sleep(3)
        assert calls >= 2
        assert service._threads["markchain-worker-1"].is_alive()
        assert service.health() is None
        service._stop.set()
        service._threads["markchain-worker-1"].join(5)
        service._stop.clear()
        service._beats["markchain-worker-1"] = time.monotonic() - 3600
        assert service.health() == "작업 처리 멈춤"
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        service._threads["markchain-worker-1"] = dead
        monitor = threading.Thread(target=service._maintain, daemon=True)
        monitor.start()
        time.sleep(6)
        assert service._threads["markchain-worker-1"] is not dead
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
            from markchain.errors import SearchUnavailableError

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


# 4: a search during server start waits, reports elapsed seconds, then fails at 30s.
def test_search_waits_for_starting_server_with_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from markchain import service as service_module
    from markchain.errors import SearchUnavailableError

    root = tmp_path / "vault"
    root.mkdir()
    sync = Synchronizer(Documents(root), SearchIndex("http://127.0.0.1:9", str(root)))
    with Runtime(root, tmp_path / "state") as runtime:
        service = Service(runtime, sync, _Engine(ready_after=2))  # type: ignore[arg-type]
        monkeypatch.setattr(service, "search", lambda *args: {"documents": []})
        context = _Context()
        result = asyncio.run(service.search_documents("결제", ctx=context))  # type: ignore[arg-type]
        assert result == {"documents": []}
        assert [p[0] for p in context.progress] == [0, 1]
        assert context.logs[0].startswith("검색 서버 시작 중")
        monkeypatch.setattr(service_module, "SEARCH_WAIT_SECONDS", 2)
        service.engine = _Engine(ready_after=100)  # type: ignore[assignment]
        with pytest.raises(
            SearchUnavailableError, match="2초 안에 준비되지 않았습니다"
        ):
            asyncio.run(service.search_documents("결제", ctx=_Context()))  # type: ignore[arg-type]
