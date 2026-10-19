import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hanger import prompts
from hanger.editing import Decision, Review


@pytest.mark.parametrize("outcome", ["repair", "wrong_approval", "legitimate", "limit"])
def test_evaluation_runs_guarded_workflow_without_applying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "benchmarks"))
    evaluation = importlib.import_module("evaluate_prompts")
    case = evaluation._cases()[1 if outcome == "legitimate" else 0]
    noop = Decision(
        action="keep", reason="keep", sources=[], changes=[], uncertainties=[]
    )
    path = next(iter(case["state"]["documents"]))
    source = case["state"]["documents"][path]["content"]
    instruction = case["state"]["request"]["instruction"]
    repaired = Decision.model_validate(
        {
            "action": "supplement",
            "reason": "record uncertainty",
            "uncertainties": [],
            "sources": [path, "request"],
            "evidence": [
                {"path": path, "quote": source},
                {"path": "request", "quote": instruction},
            ],
            "changes": [
                {
                    "path": path,
                    "operation": "replace",
                    "replacements": [
                        {
                            "old": source,
                            "new": source
                            + "\n추가 관측은 미실험이며 성능 개선은 추측이다.",
                        }
                    ],
                }
            ],
        }
    )
    rejected = Review(approved=False, issues=["requested uncertainty omitted"])
    approved = Review(approved=True, issues=[])
    responses = (
        [noop, rejected, repaired, approved]
        if outcome in {"repair", "limit"}
        else [noop, approved]
    )
    roles: list[str] = []

    def invoke(
        prompt: str, role: str, folder: Path, args: Any
    ) -> tuple[dict[str, Any], Any]:
        folder.mkdir()
        roles.append(role)
        value = responses.pop(0)
        (folder / "prompt.txt").write_text(prompt)
        (folder / "answer.json").write_text(value.model_dump_json())
        return {
            "role": role,
            "status": "pass",
            "error": None,
            "checks": [],
            "usage": {"input_tokens": 10},
            "tool_calls": 0,
        }, value

    monkeypatch.setattr(evaluation, "_invoke", invoke)
    args = SimpleNamespace(output=tmp_path, max_calls=2 if outcome == "limit" else 6)
    result = evaluation._run(case, "compact", prompts, args)
    folder = tmp_path / (case["name"] + "-compact")
    assert (folder / "closet" / path).read_text() == source
    assert result["disk_apply"] is False
    assert result["final_pass"] == (outcome in {"repair", "legitimate"})
    assert result["first_pass"] == (outcome == "legitimate")
    assert result["model_calls"] == (4 if outcome == "repair" else 2)
    assert roles == (["draft", "review"] * (2 if outcome == "repair" else 1))
    assert result["usage"]["input_tokens"] == 10 * len(roles)
    if outcome == "repair":
        assert result["attempts"][0]["error_code"] == "review_rejected"
        assert result["attempts"][1]["feedback"] == rejected.issues
        materialized = json.loads((folder / "materialized.json").read_text())
        assert "미실험" in materialized["documents"][path]
    elif outcome == "wrong_approval":
        assert any("missing text" in check for check in result["checks"])
    elif outcome == "limit":
        assert result["attempts"][-1]["error_code"] == "evaluation_call_limit"
