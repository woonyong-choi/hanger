"""Measure UTF-8 prompt bytes on fixed inputs, without invoking a model."""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from markdown_notes_agent import prompts


class _FixedTime(datetime):
    @classmethod
    def now(cls, tz: Any = None) -> _FixedTime:
        return cls(2026, 1, 1, tzinfo=UTC)


def render(module: Any, case: dict[str, Any]) -> str:
    role = case["role"]
    with patch.object(module, "datetime", _FixedTime):
        if role in {"draft", "review"}:
            state = dict(case["state"])
            state["rules"] = [
                {"file": path, "rules": text}
                for path, text in state["rule_catalog"].items()
            ]
            if role == "review":
                return str(module.review_prompt(state, case["changes"]))
            return str(module.draft_prompt(state))
        return str(getattr(module, role + "_prompt")(**case["args"]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="36906f6")
    args = parser.parse_args()
    project = tomllib.loads(
        subprocess.check_output(
            ["git", "show", f"{args.baseline}:pyproject.toml"], text=True
        )
    )
    package = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"][0]
    source = subprocess.check_output(
        ["git", "show", f"{args.baseline}:{package}/prompts.py"],
        text=True,
    )
    baseline = types.ModuleType("baseline_prompts")
    source = source.replace(f"from {Path(package).name}", "from markdown_notes_agent")
    exec(compile(source, "baseline_prompts", "exec"), baseline.__dict__)
    profile_source = subprocess.check_output(
        ["git", "show", f"{args.baseline}:{package}/profiles.py"],
        text=True,
    )
    baseline_profiles = {"__name__": "markdown_notes_agent.profiles"}
    exec(compile(profile_source, "baseline_profiles", "exec"), baseline_profiles)
    baseline.PROFILES = baseline_profiles["PROFILES"]
    cases = json.loads(
        (Path(__file__).parents[1] / "tests/fixtures/prompt-cases.json").read_text()
    )["cases"]
    review = {
        "name": "review-scoped-wiki",
        "role": "review",
        "state": cases[0]["state"],
        "changes": [
            {"path": "reference/cache.md", "diff": "-12 ms\n+12 ms (observed)"}
        ],
    }
    rows = []
    for case in [*cases, review]:
        before = render(baseline, case)
        after = render(prompts, case)
        decoded = json.loads(after.split("\n", 1)[1])
        assert decoded["task"] and isinstance(decoded["input"], dict)
        rows.append(
            {
                "case": case["name"],
                "before_utf8_bytes": len(before.encode()),
                "after_utf8_bytes": len(after.encode()),
            }
        )
    print(
        json.dumps(
            {
                "baseline": args.baseline,
                "unit": "UTF-8 bytes",
                "model_calls": 0,
                "cases": rows,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
