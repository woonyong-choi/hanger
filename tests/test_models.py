from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from hanger.config import Settings, dump_settings, load_settings
from hanger.errors import RequestError
from hanger.model import CliModel

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


def _settings(text: str, tmp_path: Path) -> Settings:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return load_settings(path)


def test_default_runs_claude_sonnet_without_coding_agent_prompt(fake_cli: Path) -> None:
    result = CliModel().generate("판단 요청", lambda: False)
    assert result["decision"]["action"] == "keep"
    assert result["usage"] == {"input_tokens": 7}
    (call,) = _calls(fake_cli)
    args = call["args"]
    assert call["cli"] == "claude"
    assert call["prompt"] == "판단 요청"
    assert args[args.index("--model") + 1] == "sonnet"
    assert "--system-prompt" in args
    assert args[args.index("--tools") + 1] == ""
    assert "--effort" not in args


def test_roles_choose_provider_model_and_effort(fake_cli: Path, tmp_path: Path) -> None:
    settings = _settings(
        '[models.draft]\nprovider = "codex"\nmodel = "gpt-test"\neffort = "low"\n'
        '[models.review]\nprovider = "claude"\nmodel = "opus"\neffort = "high"\n',
        tmp_path,
    )
    model = CliModel(settings.models)
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


def test_command_line_model_overrides_every_role(fake_cli: Path) -> None:
    CliModel(override="haiku").review("검토 요청", lambda: False)
    (call,) = _calls(fake_cli)
    assert call["args"][call["args"].index("--model") + 1] == "haiku"


def test_empty_model_and_effort_use_cli_defaults(
    fake_cli: Path, tmp_path: Path
) -> None:
    settings = _settings('[models.draft]\nprovider = "codex"\nmodel = ""\n', tmp_path)
    CliModel(settings.models).generate("초안 요청", lambda: False)
    (call,) = _calls(fake_cli)
    assert "--model" not in call["args"]
    assert not any("model_reasoning_effort" in a for a in call["args"])


def test_missing_cli_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    settings = _settings('[models.draft]\nprovider = "codex"\n', tmp_path)
    with pytest.raises(RequestError) as caught:
        CliModel(settings.models).generate("초안 요청", lambda: False)
    assert caught.value.code == "model_missing"
    assert "codex" in str(caught.value)


def test_models_section_round_trips(tmp_path: Path) -> None:
    settings = _settings(
        '[models.review]\nprovider = "codex"\neffort = "medium"\n', tmp_path
    )
    text = dump_settings(settings)
    assert "[models.review]" in text
    assert "[models]\n" not in text
    assert _settings(text, tmp_path) == settings
