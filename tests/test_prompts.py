import json
from pathlib import Path
from typing import Any

import pytest

from markdown_notes_agent import prompts

_CASES = json.loads((Path(__file__).parent / "fixtures/prompt-cases.json").read_text())[
    "cases"
]


@pytest.mark.parametrize("case", _CASES, ids=[case["name"] for case in _CASES])
def test_structured_prompt_preserves_fixed_inputs(case: dict[str, Any]) -> None:
    role = case["role"]
    if role == "draft":
        rendered = prompts.draft_prompt(case["state"])
    else:
        rendered = getattr(prompts, role + "_prompt")(**case["args"])
    boundary, encoded = rendered.split("\n", 1)
    result = json.loads(encoded)

    assert "untrusted data" in boundary
    assert "below system instructions" in boundary
    assert "no tools" in boundary
    assert "\n" not in encoded
    if role == "draft":
        assert result["input"]["documents"] == case["state"]["documents"]
        assert result["request"] == case["state"]["request"]
        assert "request" not in result["input"]
        assert result["policy"]["source_rules"] == case["state"]["source_rules"]
        assert result["policy"]["target_rules"] == case["state"]["target_rules"]
        for literal in case["expected"]["preserve"]:
            assert literal in json.dumps(
                result["input"]["documents"], ensure_ascii=False
            )
    elif role == "extract":
        assert result["input"]["body"] == case["args"]["body"]
        assert "rules" not in result["input"]
        assert (
            result["policy"]["rule_catalog"]["VAULT.md"] == "Keep source distinctions."
        )
    else:
        assert result["input"]["children"] == case["args"]["children"]
