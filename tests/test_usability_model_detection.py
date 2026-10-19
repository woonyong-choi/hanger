import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.parametrize(
    "command",
    [
        "claude --safe-mode -p --output-format json --model sonnet",
        "/opt/bin/claude --safe-mode -p --output-format json --model sonnet",
        "codex exec --json --ephemeral --model gpt-5 -",
        "/opt/bin/codex exec --json --ephemeral --model gpt-5 -",
    ],
)
@pytest.mark.parametrize("parent", [101, 202, 303])
def test_wait_model_only_detects_current_server_direct_model_child(
    monkeypatch: pytest.MonkeyPatch, command: str, parent: int
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "benchmarks"))
    usability = importlib.import_module("measure_usability")
    scenario = usability.Scenario(Path(".local/model-detection"), "python", "unused")
    rows = (
        f"101 1 python -m markdown_notes_agent.cli --state {scenario.state}\n"
        "202 1 python -m markdown_notes_agent.cli "
        f"--state {scenario.output / 'other-state'}\n"
        "303 101 helper\n"
        f"404 {parent} {command}\n"
    )

    with (
        patch.object(usability.subprocess, "check_output", return_value=rows),
        patch.object(usability.asyncio, "sleep", new_callable=AsyncMock),
    ):
        if parent == 101:
            asyncio.run(scenario.wait_model())
        else:
            with pytest.raises(AssertionError, match="model subprocess did not start"):
                asyncio.run(scenario.wait_model())


@pytest.mark.parametrize(
    "command",
    [
        "helper --output-format json",
        "helper codex exec --json",
        "grep claude --safe-mode -p --output-format json",
        "/bin/sh -c claude --safe-mode -p --output-format json",
        "/bin/sh -c codex exec --json",
        "not-claude --safe-mode -p --output-format json",
        "claude --safe-mode -p --output-format jsonl",
        "codex exec --json-schema schema.json",
        "codex --json",
    ],
)
def test_wait_model_non_model_child_times_out(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "benchmarks"))
    usability = importlib.import_module("measure_usability")
    scenario = usability.Scenario(Path(".local/model-detection"), "python", "unused")
    rows = (
        f"101 1 python -m markdown_notes_agent.cli --state {scenario.state}\n"
        f"404 101 {command}\n"
    )

    with (
        patch.object(usability.subprocess, "check_output", return_value=rows),
        patch.object(usability.asyncio, "sleep", new_callable=AsyncMock),
        pytest.raises(AssertionError, match="model subprocess did not start"),
    ):
        asyncio.run(scenario.wait_model())


@pytest.mark.parametrize("suffix", ["-other", "/nested"])
def test_wait_model_rejects_another_state_with_the_same_prefix(
    monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "benchmarks"))
    usability = importlib.import_module("measure_usability")
    scenario = usability.Scenario(Path(".local/model-detection"), "python", "unused")
    rows = (
        "202 1 python -m markdown_notes_agent.cli "
        f"--state {scenario.state}{suffix} --search-url unused serve\n"
        "404 202 codex exec --json --ephemeral\n"
    )

    with (
        patch.object(usability.subprocess, "check_output", return_value=rows),
        patch.object(usability.asyncio, "sleep", new_callable=AsyncMock),
        pytest.raises(AssertionError, match="model subprocess did not start"),
    ):
        asyncio.run(scenario.wait_model())
