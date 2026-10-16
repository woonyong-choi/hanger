"""Planning: what the model reads, drafts and gets back for review."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from conftest import ScriptedModel, edit_decision, finish, open_runtime

from hanger.closets import ClosetPolicy
from hanger.config import Settings
from hanger.context import shape
from hanger.documents import Documents
from hanger.editing import Decision, EditPolicy, materialize
from hanger.errors import RequestError
from hanger.ignore import DEFAULT_IGNORE
from hanger.model import Intent
from hanger.requests import digest, issue_key
from hanger.rules import RULES_FILE


# many matching documents no longer fail; extra ones are listed, not read.
def test_large_candidate_set_is_listed_instead_of_failing(
    closet: tuple[Path, Path, str],
) -> None:
    root, state, url = closet
    body = "결제 재시도 정책은 서비스마다 다르다. " * 300
    for number in range(12):
        (root / f"docs/n{number:02d}.md").write_text(f"# 결제 {number}\n{body}")
    model = ScriptedModel()
    model.decisions = [edit_decision("docs/n00.md", "# 결제 0", "# 결제 0 (수정)")]
    model.checks = [{"approved": True, "issues": []}]
    runtime, _ = open_runtime(root, state, url, model)
    with runtime:
        key = issue_key(runtime._secret)
        runtime.submit_intent(
            key, Intent(kind="edit", instruction="수정", query="결제")
        )
        finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert 1 <= len(status["search"]["read"]) <= 5
    assert len(status["search"]["read"]) + len(status["search"]["listed"]) == 12
    assert "OTHER MATCHED DOCUMENTS" in model.prompts[0]


# the model can ask for a listed document and decide after reading it.
def test_read_more_loads_listed_document(closet: tuple[Path, Path, str]) -> None:
    root, state, url = closet
    for number in range(8):
        (root / f"docs/n{number}.md").write_text(f"# 결제 {number}\n결제 본문 {number}")
    model = ScriptedModel()
    model.decisions = [
        {
            "action": "defer",
            "reason": "더 읽기",
            "uncertainties": [],
            "sources": [],
            "changes": [],
            "read_more": ["docs/n7.md", "docs/n6.md"],
        },
        edit_decision("docs/n7.md", "결제 본문 7", "결제 본문 7 보완"),
    ]
    model.checks = [{"approved": True, "issues": []}]
    settings = Settings.model_validate({"search": {"full_text": 1}})
    runtime, _ = open_runtime(root, state, url, model, settings)
    with runtime:
        key = issue_key(runtime._secret)
        runtime.submit_intent(
            key, Intent(kind="edit", instruction="보완", query="결제")
        )
        finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert (root / "docs/n7.md").read_text().endswith("보완")
    assert "docs/n7.md" in status["search"]["read"]


# review rejections are retried with feedback, then reported briefly.
@pytest.mark.parametrize("approve_on", [2, None])
def test_review_rejection_retries_with_feedback(
    closet: tuple[Path, Path, str], approve_on: int | None
) -> None:
    root, state, url = closet
    (root / "docs/a.md").write_text("# 결제\n재시도 2회")
    model = ScriptedModel()
    model.decisions = [edit_decision("docs/a.md", "재시도 2회", "재시도 3회")]
    rejected = {"approved": False, "issues": ["출처 없는 수치"]}
    approved = {"approved": True, "issues": []}
    model.checks = [rejected] * 3 if approve_on is None else [rejected, approved]
    runtime, _ = open_runtime(root, state, url, model)
    with runtime:
        key = issue_key(runtime._secret)
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        finish(runtime)
        status = runtime.describe(key)
    feedback_prompts = [p for p in model.prompts if "출처 없는 수치" in p]
    assert feedback_prompts
    if approve_on is None:
        assert status["status"] == "failed"
        assert (
            status["message"]
            == "Failed after 3 drafts because the review rejected the draft: "
            "출처 없는 수치"
        )
        assert status["error_code"] == "review_rejected"
        assert (root / "docs/a.md").read_text() == "# 결제\n재시도 2회"
    else:
        assert status["status"] == "completed"
        assert (root / "docs/a.md").read_text() == "# 결제\n재시도 3회"


# the configured default mode applies when a request omits it.
def test_configured_bypass_reconsiders_after_source_change(
    closet: tuple[Path, Path, str],
) -> None:
    root, state, url = closet
    target = root / "docs/a.md"
    target.write_text("# 결제\n재시도 2회")
    model = ScriptedModel()
    calls = 0

    def generate(prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.write_text("# 결제\n재시도 2회\n사용자 메모")
        return {
            "decision": edit_decision("docs/a.md", "재시도 2회", "재시도 3회"),
            "usage": None,
        }

    model.generate = generate  # type: ignore[method-assign]
    model.checks = [{"approved": True, "issues": []}]
    settings = Settings.model_validate({"jobs": {"default_mode": "bypass"}})
    runtime, _ = open_runtime(root, state, url, model, settings)
    with runtime:
        key = issue_key(runtime._secret)
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        finish(runtime)
        status = runtime.describe(key)
    assert status["intent"]["mode"] == "bypass"
    assert status["status"] == "completed"
    assert target.read_text() == "# 결제\n재시도 3회\n사용자 메모"


# a reconsidered job reports only its own file effects.
def test_reconsider_does_not_claim_previous_results(
    closet: tuple[Path, Path, str],
) -> None:
    root, state, url = closet
    for name in "ab":
        (root / f"docs/{name}.md").write_text(f"# {name} 결제\n재시도 2회")
    model = ScriptedModel()
    both = {
        **edit_decision("docs/a.md", "재시도 2회", "재시도 3회"),
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

    runtime, _ = open_runtime(root, state, url, model)
    with runtime:
        pytest.MonkeyPatch().setattr(Documents, "apply", apply)
        key = issue_key(runtime._secret)
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        finish(runtime)
        assert runtime.describe(key)["status"] == "conflict"
        Documents.apply = original  # type: ignore[method-assign]
        second = issue_key(runtime._secret)
        runtime.reconsider(key, second)
        finish(runtime)
        status = runtime.describe(second)
    assert status["status"] == "completed"
    assert status["results"] == {}
    assert status["prior_results"]["docs/a.md"] == "result_present"
    assert status["message"] == "Done. No file needed a change."


# how much to change is the model's judgment; only exactness is enforced.
def test_large_rewrite_is_left_to_the_model(tmp_path: Path) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    content = "# 문서\n" + "".join(
        f"문단 {n}: 내용이 길게 이어진다.\n" for n in range(200)
    )
    sources = {
        "a.md": {"content": content, "hash": digest(content.encode()), "path": "a.md"}
    }
    rewritten = content.replace("내용이 길게 이어진다", "새 내용")
    decision = Decision.model_validate(edit_decision("a.md", content, rewritten))
    assert (
        materialize(decision, sources, Documents(root))["changes"][0]["after"]
        == rewritten
    )


# Profiles that require evidence reject changes without a quoted source.
def test_profile_evidence_requirement(tmp_path: Path) -> None:
    root = tmp_path / "closet"
    root.mkdir()
    content = "# 결제\n재시도 2회"
    sources = {"a.md": {"content": content, "hash": digest(content.encode())}}
    decision = Decision.model_validate(
        edit_decision("a.md", "재시도 2회", "재시도 3회")
    )
    with pytest.raises(RequestError) as error:
        materialize(
            decision, sources, Documents(root), EditPolicy(require_evidence=True)
        )
    assert error.value.code == "model_output"
    assert materialize(decision, sources, Documents(root))
    quoted = Decision.model_validate(
        {
            **edit_decision("a.md", "재시도 2회", "재시도 3회"),
            "evidence": [{"path": "a.md", "quote": "재시도 2회"}],
        }
    )
    assert materialize(
        quoted, sources, Documents(root), EditPolicy(require_evidence=True)
    )


# configured write roots and exclusions are enforced before any model call.
def test_closet_rules_limit_search_and_writes(closet: tuple[Path, Path, str]) -> None:
    root, state, url = closet
    (root / "notes").mkdir()
    (root / "notes/a.md").write_text("# 결제\n재시도 2회")
    (root / "notes" / RULES_FILE).write_text(
        "---\nwrite: false  # finished notes\n---\nNotes here are final.\n"
    )
    (root / "docs/agent").mkdir()
    (root / "docs/agent/b.md").write_text("# 결제\n에이전트")
    policy = ClosetPolicy(root, DEFAULT_IGNORE + "docs/agent/\n")
    model = ScriptedModel()
    model.decisions = [edit_decision("notes/a.md", "재시도 2회", "재시도 3회")]
    model.checks = [{"approved": True, "issues": []}]
    runtime, sync = open_runtime(root, state, url, model, policy=policy)
    with runtime:
        with pytest.raises(RequestError, match="ignored"):
            runtime.submit_intent(
                issue_key(runtime._secret),
                Intent(
                    kind="edit", instruction="x", query="결제", folders=("docs/agent",)
                ),
            )
        with pytest.raises(RequestError, match="ignored"):
            sync.search("결제", folders=("docs/agent",))
        found = {d["path"] for d in sync.search("결제")["documents"]}
        assert found == {"notes/a.md"}
        key = issue_key(runtime._secret)
        runtime.submit_intent(key, Intent(kind="edit", instruction="3회", query="결제"))
        finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "failed"
    assert status["message"].startswith(
        "Failed after 3 drafts because the model output broke an edit rule: "
        "This path may not be written"
    )
    assert all(a.get("args", {}).get("choice") is None for a in status["next_actions"])
    assert "Notes here are final." in model.prompts[0]
    assert (
        "READ-ONLY NOTES (cite them if useful; never change them): notes/a.md"
        in (model.prompts[0])
    )
    assert (root / "notes/a.md").read_text() == "# 결제\n재시도 2회"


# context shaping keeps required inputs and stays inside the fill budget.
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
    with pytest.raises(RequestError, match="model input limit"):
        shape([], huge, settings, [])


# An edit that breaks a rule (ambiguous anchor) is redrafted, not failed.
def test_rule_violation_is_redrafted(closet: tuple[Path, Path, str]) -> None:  # noqa: F811
    root, state, url = closet
    (root / "docs/a.md").write_text("# 결제\n중복\n중복\n")
    model = ScriptedModel()
    model.decisions = [
        edit_decision("docs/a.md", "중복", "고유"),
        edit_decision("docs/a.md", "# 결제", "# 결제 정책"),
    ]
    model.checks = [{"approved": True, "issues": []}]
    runtime, _ = open_runtime(root, state, url, model)
    with runtime:
        key = issue_key(runtime._secret)
        runtime.submit_intent(
            key,
            Intent(
                kind="edit", instruction="제목 수정", query="결제", paths=("docs/a.md",)
            ),
        )
        finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert (root / "docs/a.md").read_text().startswith("# 결제 정책")
    assert any("exactly once" in prompt for prompt in model.prompts)


# Before creating a document, a similar existing one is found and offered.
def test_similar_document_is_checked_before_create(
    closet: tuple[Path, Path, str],  # noqa: F811
) -> None:
    root, state, url = closet
    (root / "notes").mkdir()
    (root / "notes/cat.md").write_text("# 고양이\n고양이 메모")
    (root / "docs/payment.md").write_text("# 결제 재시도\n결제 재시도는 2회다.")
    model = ScriptedModel()
    model.decisions = [
        {
            "action": "new",
            "reason": "새 주제",
            "sources": [],
            "uncertainties": [],
            "changes": [
                {
                    "path": "docs/retry.md",
                    "operation": "create",
                    "content": "# 결제 재시도\n결제 재시도는 3회로 늘린다.",
                }
            ],
        },
        edit_decision(
            "docs/payment.md", "결제 재시도는 2회다.", "결제 재시도는 3회다."
        ),
    ]
    model.checks = [{"approved": True, "issues": []}]
    runtime, _ = open_runtime(root, state, url, model)
    with runtime:
        key = issue_key(runtime._secret)
        runtime.submit_intent(
            key,
            Intent(
                kind="write",
                instruction="재시도 3회 정리",
                query="고양이",
                folders=("notes",),
            ),
        )
        finish(runtime)
        status = runtime.describe(key)
    assert status["status"] == "completed", status.get("error")
    assert any("look like the one you are about to create" in p for p in model.prompts)
    assert not (root / "docs/retry.md").exists()
    assert "3회" in (root / "docs/payment.md").read_text()


# Listed search results are matched without reading their whole body.
def test_listed_results_are_not_loaded() -> None:
    from hanger.context import shape

    settings = Settings.model_validate({"search": {"full_text": 1}})
    ranked = [
        {"path": "a.md", "title": "a", "bytes": 10},
        {"path": "b.md", "title": "b", "bytes": 10},
    ]
    loaded: list[str] = []

    def load(path: str) -> dict[str, str]:
        loaded.append(path)
        return {"path": path, "title": path, "hash": "h", "content": "결제 정책"}

    full, listed = shape(
        ranked, {}, settings, ["결제"], load=load, peek=lambda p, t: ["결제 줄"]
    )
    assert loaded == ["a.md"] and list(full) == ["a.md"]
    assert listed == [
        {"path": "b.md", "title": "b", "bytes": 10, "snippets": ["결제 줄"]}
    ]


def test_blank_citations_are_dropped() -> None:
    from hanger.workflows import _tidy_citations

    decision = Decision.model_validate(
        {
            "action": "keep",
            "reason": "이유",
            "sources": ["", " ", "request.instruction", "a.md"],
            "uncertainties": [],
            "evidence": [{"path": " ", "quote": "x"}],
            "changes": [],
        }
    )
    tidy = _tidy_citations(decision)
    assert tidy.sources == ["request", "a.md"]
    assert tidy.evidence == []
