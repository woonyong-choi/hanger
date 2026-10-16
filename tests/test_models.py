from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from hanger.config import Settings, unflatten
from hanger.model import CliModel
from hanger.providers import ProviderMissingError

_DECISION = {
    "action": "keep",
    "reason": "이미 충분합니다.",
    "sources": [],
    "uncertainties": [],
    "evidence": [],
    "changes": [],
    "read_more": [],
}

# A stand-in for both CLIs: records how it was called and answers like the real one.
_FAKE = f"""#!{sys.executable}
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"cli": os.path.basename(sys.argv[0]), "args": args,
                          "prompt": prompt}}) + "\\n")
if os.path.basename(sys.argv[0]) == "codex":
    schema = json.load(open(args[args.index("--output-schema") + 1], encoding="utf-8"))
else:
    schema = json.loads(args[args.index("--json-schema") + 1])
decision = json.loads(os.environ["FAKE_ANSWER"])
if "approved" in schema["properties"]:
    decision = {{"approved": True, "issues": []}}
if os.path.basename(sys.argv[0]) == "codex":
    with open(os.environ["FAKE_LOG"] + ".schema", "w", encoding="utf-8") as out:
        json.dump(schema, out)
    answer = args[args.index("--output-last-message") + 1]
    with open(answer, "w", encoding="utf-8") as out:
        json.dump(decision, out)
    print(json.dumps({{"type": "thread.started"}}))
    print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 9,
                     "cached_input_tokens": 4, "output_tokens": 2}}}}))
else:
    print(json.dumps({{"structured_output": decision, "usage": {{"input_tokens": 7}}}}))
"""


@pytest.fixture
def fake_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "codex"):
        script = bin_dir / name
        script.write_text(_FAKE, encoding="utf-8")
        script.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("FAKE_ANSWER", json.dumps(_DECISION))
    return log


def _calls(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _settings(**values: object) -> Settings:
    return Settings.model_validate(unflatten(values))


def _only(fake_cli: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave only one provider CLI on PATH."""
    folder = fake_cli.parent / f"only-{name}"
    folder.mkdir()
    (folder / name).symlink_to(fake_cli.parent / "bin" / name)
    monkeypatch.setenv("PATH", str(folder))


def test_default_prefers_claude_haiku_without_coding_agent_prompt(
    fake_cli: Path,
) -> None:
    result = CliModel().generate("판단 요청", lambda: False)
    assert result["decision"]["action"] == "keep"
    assert result["usage"] == {"input_tokens": 7}
    (call,) = _calls(fake_cli)
    args = call["args"]
    assert call["cli"] == "claude"
    assert call["prompt"] == "판단 요청"
    assert args[args.index("--model") + 1] == "haiku"
    assert "--system-prompt" in args
    assert args[args.index("--tools") + 1] == ""
    assert "--effort" not in args


def test_default_falls_back_to_codex_luna(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _only(fake_cli, "codex", monkeypatch)
    CliModel().generate("초안 요청", lambda: False)
    (call,) = _calls(fake_cli)
    assert call["cli"] == "codex"
    assert call["args"][call["args"].index("--model") + 1] == "gpt-6-luna"
    assert not any("model_reasoning_effort" in a for a in call["args"])


def test_roles_choose_provider_model_and_effort(fake_cli: Path) -> None:
    settings = _settings(
        **{
            "models.draft.provider": "codex",
            "models.draft.name": "gpt-test",
            "models.draft.effort": "low",
            "models.review.provider": "claude",
            "models.review.name": "opus",
            "models.review.effort": "high",
        }
    )
    model = CliModel(settings)
    draft = model.generate("초안 요청", lambda: False)
    model.review("검토 요청", lambda: False)
    assert draft["usage"]["output_tokens"] == 2
    codex, claude = _calls(fake_cli)
    assert codex["cli"] == "codex"
    assert codex["args"][:2] == ["exec", "--json"]
    assert codex["args"][-1] == "-"
    assert "--ephemeral" in codex["args"]
    assert codex["args"][codex["args"].index("--model") + 1] == "gpt-test"
    assert 'model_reasoning_effort="low"' in codex["args"]
    assert claude["args"][claude["args"].index("--model") + 1] == "opus"
    assert claude["args"][claude["args"].index("--effort") + 1] == "high"
    schema = json.loads((fake_cli.parent / "calls.jsonl.schema").read_text("utf-8"))
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_role_settings_fall_back_to_the_shared_model(fake_cli: Path) -> None:
    settings = _settings(
        **{"model.provider": "codex", "model.name": "gpt-x", "models.review.name": "y"}
    )
    model = CliModel(settings)
    assert model.choice("draft").describe() == "Codex (codex), model gpt-x"
    assert model.choice("review").model == "y"
    assert model.choice("review").provider == "codex"


def test_command_line_model_overrides_every_role(fake_cli: Path) -> None:
    CliModel(override="sonnet").review("검토 요청", lambda: False)
    (call,) = _calls(fake_cli)
    assert call["args"][call["args"].index("--model") + 1] == "sonnet"


def test_configured_provider_must_be_installed(
    fake_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _only(fake_cli, "claude", monkeypatch)
    model = CliModel(_settings(**{"model.provider": "codex"}))
    with pytest.raises(ProviderMissingError) as caught:
        model.generate("초안 요청", lambda: False)
    assert caught.value.code == "provider_missing"
    assert "hanger config set model.provider" in str(caught.value)


def test_no_provider_at_all_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(ProviderMissingError) as caught:
        CliModel().generate("초안 요청", lambda: False)
    assert "No model provider found" in str(caught.value)


# A missing model command fails at once with a clear message, without retries.
def test_missing_model_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from hanger.model import CliModel
    from hanger.providers import ProviderMissingError

    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(ProviderMissingError) as caught:
        CliModel().generate("prompt", lambda: False)
    assert caught.value.code == "provider_missing"
    assert not caught.value.retryable
