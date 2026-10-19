import json
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from hanger.documents import Documents
from hanger.model import CliModel, Intent, Planner
from hanger.requests import Change, Proposal, digest, issue_key
from hanger.runtime import Runtime
from hanger.search import SearchIndex
from hanger.sync import Synchronizer


# cancellation invalidates an in-flight result, even if the model returns late.
@pytest.mark.parametrize("stop", ["cancel", "shutdown"])
def test_cancel_running_model_never_applies_late_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    started = threading.Event()
    released = threading.Event()

    def prepare(
        planner: Planner,
        intent: Intent,
        cancelled: Callable[[], bool],
        previous: dict[str, Any] | None = None,
        feedback: list[str] | None = None,
    ) -> dict[str, Any]:
        started.set()
        assert released.wait(5)
        return {
            "proposal": {"changes": [{"path": "docs/a.md", "after": "late"}]},
            "policy_dependencies": planner.synchronizer.policy.context(
                (), targets=["docs/a.md"]
            )["policy_dependencies"],
        }

    monkeypatch.setattr(Planner, "prepare", prepare)
    index = SearchIndex("http://127.0.0.1:19200", str(root))
    planner = Planner(Synchronizer(Documents(root), index), CliModel())
    try:
        with Runtime(root, tmp_path / "state", planner) as runtime:
            key = issue_key(runtime._secret)
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


# independent model jobs overlap, but conflicting replacements cannot both apply.
def test_parallel_proposals_recheck_shared_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    (root / "a.md").write_text("original")
    barrier = threading.Barrier(2)

    def prepare(
        planner: Planner,
        intent: Intent,
        cancelled: Callable[[], bool],
        previous: dict[str, Any] | None = None,
        feedback: list[str] | None = None,
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
            "policy_dependencies": planner.synchronizer.policy.context(["a.md"])[
                "policy_dependencies"
            ],
        }

    monkeypatch.setattr(Planner, "prepare", prepare)
    index = SearchIndex("http://127.0.0.1:19200", str(root))
    planner = Planner(Synchronizer(Documents(root), index), CliModel())
    try:
        with Runtime(root, tmp_path / "state", planner) as runtime:
            for text in ("first", "second"):
                runtime.submit_intent(
                    issue_key(runtime._secret),
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


def test_parallel_proposals_preserve_first_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    target = root / "a.md"
    target.write_text("original")
    replacing = threading.Event()
    released = threading.Event()
    replace = os.replace

    def pause_first_replace(source: Path, destination: Path) -> None:
        if destination == target and not replacing.is_set():
            replacing.set()
            assert released.wait(5)
        replace(source, destination)

    with Runtime(root, tmp_path / "state") as runtime:
        admission = runtime._admission

        class ObservedAdmission:
            def __enter__(self) -> None:
                if not admission.acquire(blocking=False):
                    released.set()
                    admission.acquire()

            def __exit__(self, *_: object) -> None:
                admission.release()

        monkeypatch.setattr(runtime, "_admission", ObservedAdmission())
        monkeypatch.setattr(os, "replace", pause_first_replace)
        before = runtime.documents.read_hash("a.md")
        for text in ("first", "second"):
            runtime.submit(
                issue_key(runtime._secret),
                Proposal(changes=(Change(path="a.md", before=before, after=text),)),
            )

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(runtime.run_next)
            try:
                assert replacing.wait(5)
                second = workers.submit(runtime.run_next)
                second.add_done_callback(lambda _: released.set())
                results = [first.result(timeout=10), second.result(timeout=10)]
            finally:
                released.set()

        assert [result["status"] for result in results if result] == [
            "completed",
            "conflict",
        ]
        assert target.read_text() == "first"
        assert sorted(path.name for path in root.iterdir()) == ["a.md"]


def test_completed_job_cleanup_preserves_next_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    target = root / "a.md"
    cleaning = threading.Event()
    cleanup_released = threading.Event()
    replacing = threading.Event()
    replacement_released = threading.Event()
    replace = os.replace

    def pause_next_replace(source: Path, destination: Path) -> None:
        if destination == target and cleaning.is_set() and not replacing.is_set():
            replacing.set()
            assert replacement_released.wait(5)
        replace(source, destination)

    with Runtime(root, tmp_path / "state") as runtime:
        admission = runtime._admission
        discard = runtime._discard
        first_key = issue_key(runtime._secret)

        class ObservedAdmission:
            def __enter__(self) -> None:
                if not admission.acquire(blocking=False):
                    if replacing.is_set():
                        replacement_released.set()
                    admission.acquire()

            def __exit__(self, *_: object) -> None:
                admission.release()

        def pause_first_cleanup(key: str, *, resolved: str = "") -> None:
            if key == first_key:
                cleaning.set()
                assert cleanup_released.wait(5)
            discard(key, resolved=resolved)

        monkeypatch.setattr(runtime, "_admission", ObservedAdmission())
        monkeypatch.setattr(runtime, "_discard", pause_first_cleanup)
        monkeypatch.setattr(os, "replace", pause_next_replace)
        runtime.submit(
            first_key, Proposal(changes=(Change(path="a.md", after="first"),))
        )
        before = digest(b"first")
        runtime.submit(
            issue_key(runtime._secret),
            Proposal(changes=(Change(path="a.md", before=before, after="second"),)),
        )

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(runtime.run_next)
            try:
                assert cleaning.wait(5)
                second = workers.submit(runtime.run_next)
                assert replacing.wait(5)
                first.add_done_callback(lambda _: replacement_released.set())
                cleanup_released.set()
                results = [first.result(timeout=10), second.result(timeout=10)]
            finally:
                cleanup_released.set()
                replacement_released.set()

        assert [result["status"] for result in results if result] == [
            "completed",
            "completed",
        ]
        assert target.read_text() == "second"
        assert sorted(path.name for path in root.iterdir()) == ["a.md"]


class EditingModel(CliModel):
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


# normal mode preserves external edits; bypass regenerates from the new source.
@pytest.mark.parametrize(
    "mode, status, calls", [("normal", "conflict", 1), ("bypass", "completed", 2)]
)
def test_model_conflict_uses_selected_mode(
    tmp_path: Path, mode: str, status: str, calls: int
) -> None:
    url = os.environ.get("HANGER_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires Elasticsearch test endpoint")
    root = tmp_path / "closet"
    (root / "docs").mkdir(parents=True)
    target = root / "docs/payment.md"
    target.write_text("# 결제\n최초 원문")
    index = SearchIndex(url, str(root))
    sync = Synchronizer(Documents(root), index)
    sync.reconcile()
    model = EditingModel(target, conflict=True)
    try:
        with Runtime(root, tmp_path / "state", Planner(sync, model)) as runtime:
            key = issue_key(runtime._secret)
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


# opt-in actual Claude contract, with no filesystem tools and synthetic input.
def test_live_claude_returns_structured_change() -> None:
    if not os.environ.get("HANGER_TEST_LIVE_MODEL"):
        pytest.skip("requires explicit model invocation")
    model = CliModel()
    result = model.generate(
        'Return a Decision: action new, reason "test", sources [], uncertainties [], '
        'changes [{"path":"docs/example.md","operation":"create",'
        '"content":"# Example\\nSynthetic test"}]. '
        "Do not use tools or access files.",
        lambda: False,
    )
    assert result["decision"]["changes"][0]["path"] == "docs/example.md"
    assert result["usage"] is not None


# inconsistent model changes terminate one job without killing the worker.
def test_invalid_model_proposal_does_not_block_next_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = os.environ.get("HANGER_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires Elasticsearch test endpoint")
    root = tmp_path / "closet"
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
            runtime.submit_intent(issue_key(runtime._secret), intent)
            failed = runtime.run_next()
            assert failed is not None and failed["status"] == "failed"
            monkeypatch.setattr(model, "generate", original)
            runtime.submit_intent(issue_key(runtime._secret), intent)
            succeeded = runtime.run_next()
            assert succeeded is not None and succeeded["status"] == "completed"
    finally:
        index.client.indices.delete(index=index.name)
        index.close()


# bypass replanning receives prior effects and retains their current evidence.
@pytest.mark.parametrize("mode", ["normal", "bypass"])
@pytest.mark.parametrize("delete_first", [False, True])
def test_partial_replan_preserves_applied_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, *, delete_first: bool
) -> None:
    url = os.environ.get("HANGER_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires Elasticsearch test endpoint")
    root = tmp_path / "closet"
    (root / "docs").mkdir(parents=True)
    for name in ("a", "b"):
        (root / f"docs/{name}.md").write_text("# 결제\n원문")
    index = SearchIndex(url, str(root))
    sync = Synchronizer(Documents(root), index)
    sync.reconcile()
    model = CliModel()
    calls = 0

    def generate(prompt: str, cancelled: Callable[[], bool]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 2:
            context = json.loads(prompt.split("\n", 1)[1])["input"]
            assert "docs/a.md" in context["previous"]["applied"]
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
            key = issue_key(runtime._secret)
            runtime.submit_intent(key, intent)
            result = runtime.run_next()
            if mode == "normal":
                assert result is not None and result["status"] == "conflict"
                new_key = issue_key(runtime._secret)
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
