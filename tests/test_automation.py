import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from markchain.documents import Documents
from markchain.model import ClaudeModel, Intent, Planner
from markchain.requests import Change
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.sync import Synchronizer


# #4: cancellation invalidates an in-flight result, even if the model returns late.
@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
def test_cancel_running_model_never_applies_late_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    started = threading.Event()
    released = threading.Event()

    def prepare(
        planner: Planner,
        intent: Intent,
        cancelled: Callable[[], bool],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        started.set()
        assert released.wait(5)
        return {"proposal": {"changes": [{"path": "docs/a.md", "after": "late"}]}}

    monkeypatch.setattr(Planner, "prepare", prepare)
    index = SearchIndex("http://127.0.0.1:19200", str(root))
    planner = Planner(Synchronizer(Documents(root), index), ClaudeModel())
    try:
        with Runtime(root, tmp_path / "state", planner) as runtime:
            key = runtime.issue_key()
            runtime.submit_intent(key, Intent(kind="write", query="a", instruction="a"))
            with ThreadPoolExecutor(max_workers=1) as workers:
                future = workers.submit(runtime.run_next)
                try:
                    assert started.wait(5)
                    if stop == "shutdown":
                        runtime.interrupt()
                    else:
                        assert runtime.cancel(key)
                finally:
                    released.set()
                if stop == "shutdown":
                    with pytest.raises(InterruptedError):
                        future.result(timeout=5)
                    result = None
                else:
                    result = future.result(timeout=5)
                    assert result is not None and result["status"] == "cancelled"
            assert not (root / "docs/a.md").exists()
            if stop == "cancel":
                assert runtime.run_next() is None
        if stop == "shutdown":
            with Runtime(root, tmp_path / "state", planner) as resumed:
                result = resumed.run_next()
                assert result is not None and result["status"] == "completed"
                assert (root / "docs/a.md").read_text() == "late"
    finally:
        index.close()


# #4: independent model jobs overlap, but conflicting replacements cannot both apply.
def test_parallel_proposals_recheck_shared_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "a.md").write_text("original")
    barrier = threading.Barrier(2)

    def prepare(
        planner: Planner,
        intent: Intent,
        cancelled: Callable[[], bool],
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        before = planner.synchronizer.documents.read_hash("a.md")
        barrier.wait(timeout=5)
        return {
            "proposal": {
                "changes": [
                    {"path": "a.md", "before": before, "after": intent.instruction}
                ]
            },
            "dependencies": {"a.md": before},
        }

    monkeypatch.setattr(Planner, "prepare", prepare)
    index = SearchIndex("http://127.0.0.1:19200", str(root))
    planner = Planner(Synchronizer(Documents(root), index), ClaudeModel())
    try:
        with Runtime(root, tmp_path / "state", planner) as runtime:
            for text in ("first", "second"):
                runtime.submit_intent(
                    runtime.issue_key(),
                    Intent(kind="edit", query="a", instruction=text),
                )
            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = [workers.submit(runtime.run_next) for _ in range(2)]
                results = [future.result(timeout=10) for future in futures]
            assert sorted(result["status"] for result in results if result) == [
                "completed",
                "conflict",
            ]
            assert (root / "a.md").read_text() in {"first", "second"}
    finally:
        index.close()


class EditingModel(ClaudeModel):
    def __init__(self, target: Path, *, conflict: bool = False) -> None:
        super().__init__()
        self.target = target
        self.calls = 0
        self.conflict = conflict

    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.calls += 1
        if self.conflict and self.calls == 1:
            self.target.write_text("# 결제\n사용자가 바꾼 원문")
        return {
            "decision": {
                "action": "supplement",
                "reason": "요청한 절 추가",
                "sources": ["docs/payment.md"],
                "uncertainties": [],
                "changes": [
                    {
                        "path": "docs/payment.md",
                        "operation": "replace",
                        "replacements": [
                            {
                                "old": self.target.read_text(),
                                "new": self.target.read_text()
                                + "\n## 재시도\n멱등 키 사용",
                            }
                        ],
                    }
                ],
            },
            "usage": None,
        }

    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        return {"review": {"approved": True, "issues": []}, "usage": None}


# #4: normal mode preserves external edits; bypass regenerates from the new source.
@pytest.mark.parametrize(
    "mode, status, calls", [("normal", "conflict", 1), ("bypass", "completed", 2)]
)
def test_model_conflict_uses_selected_mode(
    tmp_path: Path, mode: str, status: str, calls: int
) -> None:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires Elasticsearch test endpoint")
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    target = root / "docs/payment.md"
    target.write_text("# 결제\n최초 원문")
    index = SearchIndex(url, str(root))
    sync = Synchronizer(Documents(root), index)
    sync.reconcile()
    model = EditingModel(target, conflict=True)
    try:
        with Runtime(root, tmp_path / "state", Planner(sync, model)) as runtime:
            key = runtime.issue_key()
            intent = Intent.model_validate(
                {
                    "kind": "edit",
                    "query": "결제",
                    "instruction": "재시도 절 추가",
                    "mode": mode,
                }
            )
            runtime.submit_intent(key, intent)
            result = runtime.run_next()
            assert result is not None and result["status"] == status
            assert model.calls == calls
            assert "사용자가 바꾼 원문" in target.read_text()
            assert ("재시도" in target.read_text()) == (mode == "bypass")
            assert runtime.submit_intent(key, intent) == "duplicate"
    finally:
        index.client.indices.delete(index=index.name)
        index.close()


# #4: opt-in actual Claude contract, with no filesystem tools and synthetic input.
def test_live_claude_returns_structured_change() -> None:
    if not os.environ.get("MARKCHAIN_TEST_LIVE_MODEL"):
        pytest.skip("requires explicit model invocation")
    model = ClaudeModel()
    result = model.generate(
        'Return a Decision: action new, reason "test", sources [], uncertainties [], '
        'changes [{"path":"docs/example.md","operation":"create",'
        '"content":"# Example\\nSynthetic test"}]. '
        "Do not use tools or access files.",
        lambda: False,
    )
    assert result["decision"]["changes"][0]["path"] == "docs/example.md"
    assert result["usage"] is not None


# #4: inconsistent model changes terminate one job without killing the worker.
def test_invalid_model_proposal_does_not_block_next_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires Elasticsearch test endpoint")
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    target = root / "docs/payment.md"
    target.write_text("# 결제\n원문")
    index = SearchIndex(url, str(root))
    sync = Synchronizer(Documents(root), index)
    sync.reconcile()
    model = EditingModel(target)
    original = model.generate

    def invalid(prompt: str, cancelled: Callable[[], bool]) -> dict[str, Any]:
        result = original(prompt, cancelled)
        result["decision"]["changes"] *= 2
        return result

    try:
        with Runtime(root, tmp_path / "state", Planner(sync, model)) as runtime:
            intent = Intent(kind="edit", query="결제", instruction="절 추가")
            monkeypatch.setattr(model, "generate", invalid)
            runtime.submit_intent(runtime.issue_key(), intent)
            failed = runtime.run_next()
            assert failed is not None and failed["status"] == "failed"
            monkeypatch.setattr(model, "generate", original)
            runtime.submit_intent(runtime.issue_key(), intent)
            succeeded = runtime.run_next()
            assert succeeded is not None and succeeded["status"] == "completed"
    finally:
        index.client.indices.delete(index=index.name)
        index.close()


# #4: bypass replanning receives prior effects and retains their current evidence.
@pytest.mark.parametrize("mode", ["normal", "bypass"])
@pytest.mark.parametrize("delete_first", [False, True])
def test_partial_replan_preserves_applied_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, *, delete_first: bool
) -> None:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires Elasticsearch test endpoint")
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    for name in ("a", "b"):
        (root / f"docs/{name}.md").write_text("# 결제\n원문")
    index = SearchIndex(url, str(root))
    sync = Synchronizer(Documents(root), index)
    sync.reconcile()
    model = ClaudeModel()
    calls = 0

    def generate(prompt: str, cancelled: Callable[[], bool]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            assert "PRIOR ATTEMPT AND OBSERVED EFFECTS" in prompt
            assert '"applied": {"docs/a.md"' in prompt
        names = ("a", "b") if calls == 1 else ("b",)
        return {
            "decision": {
                "action": "supplement",
                "reason": "한 번씩 추가",
                "sources": [f"docs/{name}.md" for name in names],
                "uncertainties": [],
                "changes": [
                    {
                        "path": f"docs/{name}.md",
                        "operation": "delete"
                        if (delete_first and name == "a")
                        else "replace",
                        "replacements": []
                        if (delete_first and name == "a")
                        else [
                            {
                                "old": (root / f"docs/{name}.md").read_text(),
                                "new": (root / f"docs/{name}.md").read_text()
                                + "\n추가",
                            }
                        ],
                    }
                    for name in names
                ],
            },
            "usage": None,
        }

    apply = Documents.apply

    def external_after_first(documents: Documents, change: Change) -> str:
        result = apply(documents, change)
        if change.path == "docs/a.md":
            (root / "docs/b.md").write_text("# 결제\n사용자 변경")
        return result

    monkeypatch.setattr(model, "generate", generate)
    monkeypatch.setattr(
        model,
        "review",
        lambda prompt, cancelled: {
            "review": {"approved": True, "issues": []},
            "usage": None,
        },
    )
    monkeypatch.setattr(Documents, "apply", external_after_first)
    try:
        with Runtime(root, tmp_path / "state", Planner(sync, model)) as runtime:
            intent = Intent.model_validate(
                dict(
                    kind="edit",
                    query="결제",
                    instruction="각각 한 번 추가",
                    mode=mode,
                    paths=("docs/a.md", "docs/b.md"),
                    allow_delete=delete_first,
                )
            )
            key = runtime.issue_key()
            runtime.submit_intent(key, intent)
            result = runtime.run_next()
            if mode == "normal":
                assert result is not None and result["status"] == "conflict"
                new_key = runtime.issue_key()
                assert runtime.reconsider(key, new_key) == "accepted"
                assert runtime.reconsider(key, new_key) == "duplicate"
                result = runtime.run_next()
            assert result is not None and result["status"] == "completed"
            assert set(result["applied"]) == {"docs/a.md", "docs/b.md"}
            assert result["error"] == ""
            if delete_first:
                assert not (root / "docs/a.md").exists()
            else:
                assert (root / "docs/a.md").read_text().count("추가") == 1
            assert "사용자 변경" in (root / "docs/b.md").read_text()
            assert calls == 2
    finally:
        index.client.indices.delete(index=index.name)
        index.close()
