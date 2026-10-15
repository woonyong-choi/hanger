import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from markchain.documents import Documents
from markchain.model import ClaudeModel, Intent, Planner
from markchain.profiles import Profile
from markchain.requests import digest
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.sync import Synchronizer


class Model(ClaudeModel):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self.reviews: list[str] = []
        self.check = {"approved": True, "issues": []}
        self.after_draft: Callable[[], object] = lambda: None
        self.decision: dict[str, Any] = {
            "action": "supplement",
            "reason": "요청한 문장 수정",
            "uncertainties": [],
            "sources": ["docs/a.md"],
            "evidence": [{"path": "docs/a.md", "quote": "원문값"}],
            "changes": [
                {
                    "path": "docs/a.md",
                    "operation": "replace",
                    "replacements": [{"old": "원문값", "new": "새값"}],
                }
            ],
        }

    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.prompts.append(prompt)
        self.after_draft()
        return {
            "decision": copy.deepcopy(self.decision),
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.reviews.append(prompt)
        return {
            "review": self.check,
            "usage": {"input_tokens": 130, "output_tokens": 10},
        }


@pytest.fixture
def flow(tmp_path: Path) -> Iterator[tuple[Runtime, Model]]:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    (root / "private").mkdir()
    (root / "docs/a.md").write_bytes(
        "# 제목\r\n원문값\r\n예외: 원문값이 아님\r\n[출처](https://example.com)\r\n".replace(
            "예외: 원문값이 아님", "예외: 별도 조건"
        ).encode()
    )
    (root / "private/b.md").write_text("# 제목\n범위 밖 비공개")
    model = Model()
    index = SearchIndex(url, str(root))
    sync = Synchronizer(Documents(root), index)
    sync.reconcile()
    try:
        with Runtime(root, tmp_path / "state", Planner(sync, model)) as runtime:
            yield runtime, model
    finally:
        index.client.indices.delete(index=index.name)
        index.close()


def run(
    runtime: Runtime, *, profile: Profile = "note", mode: str = "normal"
) -> dict[str, Any]:
    intent = Intent.model_validate(
        {
            "kind": "edit",
            "instruction": "원문값만 새값으로 바꾸고 나머지 보존",
            "query": "제목",
            "folders": ["docs"],
            "profile": profile,
            "mode": mode,
        }
    )
    key = runtime.issue_key()
    runtime.submit_intent(key, intent)
    result = runtime.run_next()
    assert result is not None
    assert runtime.submit_intent(key, intent) == "duplicate"
    return result


# #9: each purpose executes draft and review while preserving untouched source bytes.
@pytest.mark.parametrize(
    "profile, policy",
    [
        ("wiki", "prerequisites"),
        ("work_record", "chronologically"),
        ("blog", "reader journey"),
        ("creative", "world rules"),
    ],
)
def test_profile_graph_preserves_source_and_search_scope(
    flow: tuple[Runtime, Model], profile: Profile, policy: str
) -> None:
    runtime, model = flow
    path = runtime.documents.root / "docs/a.md"
    before = path.read_bytes()

    result = run(runtime, profile=profile)

    assert result["status"] == "completed"
    assert path.read_bytes() == before.replace("원문값".encode(), "새값".encode())
    assert len(model.prompts) == len(model.reviews) == 1
    assert policy in model.prompts[0] and policy in model.reviews[0]
    assert "범위 밖 비공개" not in model.prompts[0] + model.reviews[0]
    assert result["review"] == {"approved": True, "issues": []}
    assert result["usage"]["review"]["input_tokens"] == 130


# #9: rejected content and invalid patches must never reach the shared file writer.
@pytest.mark.parametrize(
    "defect",
    [
        "missing",
        "ambiguous",
        "overlap",
        "evidence",
        "unread",
        "delete",
        "full",
        "review",
        "contradictory_review",
        "keep_changes",
    ],
)
def test_invalid_workflow_preserves_source(
    flow: tuple[Runtime, Model], defect: str
) -> None:
    runtime, model = flow
    path = runtime.documents.root / "docs/a.md"
    edit = model.decision["changes"][0]
    if defect == "missing":
        edit["replacements"][0]["old"] = "없는 구간"
    elif defect == "ambiguous":
        edit["replacements"][0]["old"] = "\r\n"
    elif defect == "overlap":
        edit["replacements"].append({"old": "원문", "new": "중첩"})
    elif defect == "evidence":
        model.decision["evidence"][0]["quote"] = "조작한 근거"
    elif defect == "unread":
        edit["path"] = "private/b.md"
    elif defect == "delete":
        edit.update(operation="delete", replacements=[])
    elif defect == "full":
        edit.update(content="전체 덮어쓰기", replacements=[])
    elif defect == "review":
        model.check = {"approved": False, "issues": ["근거 없는 결론"]}
    elif defect == "contradictory_review":
        model.check = {"approved": True, "issues": ["남은 문제"]}
    else:
        model.decision["action"] = "keep"
    before = path.read_bytes()

    result = run(runtime)

    assert result["status"] == "failed"
    assert result["error"]
    assert path.read_bytes() == before
    assert (
        runtime.documents.root / "private/b.md"
    ).read_text() == "# 제목\n범위 밖 비공개"


# #9: observed user edits invalidate the draft before validation or replacement.
def test_external_edit_during_workflow_stops_apply(flow: tuple[Runtime, Model]) -> None:
    runtime, model = flow
    path = runtime.documents.root / "docs/a.md"
    model.after_draft = lambda: path.write_text("사용자 변경")

    result = run(runtime)

    assert result["status"] == "conflict"
    assert not model.reviews
    assert path.read_text() == "사용자 변경"


# #9: missing evidence must be distinguishable from completed integration.
def test_deferred_workflow_preserves_input_without_review(
    flow: tuple[Runtime, Model],
) -> None:
    runtime, model = flow
    model.decision.update(action="defer", changes=[], uncertainties=["출처 필요"])
    before = (runtime.documents.root / "docs/a.md").read_bytes()
    result = run(runtime)
    assert result["status"] == "deferred"
    assert result["decision"]["uncertainties"] == ["출처 필요"]
    assert not model.reviews and result["review"] == {}
    assert (runtime.documents.root / "docs/a.md").read_bytes() == before


# #9: late review approval cannot resurrect a cancelled or expired operation.
@pytest.mark.parametrize("stop", ["cancel", "expire"])
def test_late_review_cannot_apply_stopped_request(
    flow: tuple[Runtime, Model], monkeypatch: pytest.MonkeyPatch, stop: str
) -> None:
    runtime, model = flow
    path = runtime.documents.root / "docs/a.md"
    before = path.read_bytes()
    key = runtime.issue_key()
    runtime.submit_intent(
        key, Intent(kind="edit", query="제목", instruction="교체", folders=("docs",))
    )
    original = model.review

    def late(prompt: str, cancelled: Callable[[], bool]) -> dict[str, Any]:
        response = original(prompt, cancelled)
        if stop == "cancel":
            runtime.cancel(key)
        else:
            future = time.time() + 25 * 3600
            monkeypatch.setattr(time, "time", lambda: future)
        return response

    monkeypatch.setattr(model, "review", late)
    result = runtime.run_next()
    assert result is not None
    assert result["status"] == ("cancelled" if stop == "cancel" else "expired")
    assert path.read_bytes() == before
    assert runtime.run_next() is None


# #9: default new fields preserve the previously published request identity contract.
def test_old_request_identity_and_nested_cleanup(
    flow: tuple[Runtime, Model], monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime, _ = flow
    legacy = {
        "kind": "edit",
        "query": "제목",
        "instruction": "교체",
        "paths": [],
        "mode": "normal",
        "allow_delete": False,
        "fallback_reason": None,
    }
    key = runtime.issue_key()
    intent = Intent.model_validate(legacy)
    runtime.submit_intent(key, intent)
    result = runtime.run_next()
    assert result is not None and result["status"] == "completed"
    assert result["fingerprint"] == digest(json.dumps(legacy, sort_keys=True).encode())
    assert runtime.submit_intent(key, intent) == "duplicate"
    future = time.time() + 25 * 3600
    monkeypatch.setattr(time, "time", lambda: future)
    assert runtime.cleanup()["status"] == "cleaned"
    with closing(sqlite3.connect(runtime.state / "graph.sqlite")) as connection:
        assert (
            list(SqliteSaver(connection).list({"configurable": {"thread_id": key}}))
            == []
        )
    assert runtime.status(key) == {}


# #9: a real process crash after draft persistence must not regenerate the draft.
@pytest.mark.parametrize("external_edit", [False, True])
def test_killed_workflow_resumes_saved_draft(
    tmp_path: Path, *, external_edit: bool
) -> None:
    url = os.environ.get("MARKCHAIN_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "vault"
    (root / "docs").mkdir(parents=True)
    target = root / "docs/a.md"
    target.write_text("# 원문\n이전")
    state = tmp_path / "state"
    index = SearchIndex(url, str(root))
    Synchronizer(Documents(root), index).reconcile()
    with Runtime(
        root, state, Planner(Synchronizer(Documents(root), index), Model())
    ) as runtime:
        key = runtime.issue_key()
        runtime.submit_intent(
            key,
            Intent(
                kind="edit",
                query="원문",
                instruction="이전을 이후로",
                folders=("docs",),
                profile="wiki",
            ),
        )
    code = """
import os, sys
from pathlib import Path
from markchain.documents import Documents
from markchain.model import ClaudeModel, Planner
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.sync import Synchronizer
root, state = Path(sys.argv[1]), Path(sys.argv[2])
class Model(ClaudeModel):
    def generate(self, prompt, cancelled):
        with (state / "calls").open("a") as stream:
            stream.write("draft\\n")
        return {"decision": {
            "action":"supplement", "reason":"교체", "sources":["docs/a.md"],
            "uncertainties":[], "changes":[{
                "path":"docs/a.md", "operation":"replace",
                "replacements":[{"old":"이전","new":"이후"}]}]}, "usage":None}
    def review(self, prompt, cancelled):
        if sys.argv[4] == "crash":
            os._exit(74)
        return {"review":{"approved":True,"issues":[]},"usage":None}
index = SearchIndex(sys.argv[3], str(root))
try:
    planner = Planner(Synchronizer(Documents(root), index), Model())
    with Runtime(root, state, planner) as runtime:
        print(runtime.run_next()["status"])
finally:
    index.close()
"""
    try:
        args = [sys.executable, "-c", code, str(root), str(state), url]
        crashed = subprocess.run(
            [*args, "crash"], capture_output=True, text=True, timeout=30, check=False
        )
        assert crashed.returncode == 74, crashed.stderr
        assert target.read_text() == "# 원문\n이전"
        if external_edit:
            target.write_text("# 원문\n사용자 수정")
        resumed = subprocess.run(
            [*args, "resume"], capture_output=True, text=True, timeout=30, check=True
        )
        assert resumed.stdout.strip() == ("conflict" if external_edit else "completed")
        assert (state / "calls").read_text().splitlines() == ["draft"]
        assert target.read_text() == (
            "# 원문\n사용자 수정" if external_edit else "# 원문\n이후"
        )
        with Runtime(root, state) as runtime:
            assert runtime.run_next() is None
    finally:
        index.client.indices.delete(index=index.name)
        index.close()
