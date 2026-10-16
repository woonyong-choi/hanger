import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from markchain.documents import Documents
from markchain.model import CliModel, Intent, Planner
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.sync import Synchronizer
from markchain.trace import Trace


# #6: telemetry cannot alter the resulting document or introduce a model call.
def test_trace_records_metrics_without_source_or_extra_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def search(
        sync: Synchronizer,
        query: str,
        limit: int = 20,
        *,
        fallback_offset: int = 0,
        fallback_reason: str | None = None,
        folders: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return {"documents": [], "coverage": "synthetic"}

    def generate(
        model: CliModel, prompt: str, cancelled: Callable[[], bool]
    ) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "decision": {
                "action": "new",
                "reason": "synthetic",
                "sources": [],
                "uncertainties": [],
                "changes": [
                    {
                        "path": "docs/a.md",
                        "operation": "create",
                        "content": "private synthetic content",
                    }
                ],
            },
            "usage": {"input_tokens": 50, "output_tokens": 20},
        }

    monkeypatch.setattr(Synchronizer, "search", search)
    monkeypatch.setattr(CliModel, "generate", generate)
    monkeypatch.setattr(
        CliModel,
        "review",
        lambda self, prompt, cancelled: {
            "review": {"approved": True, "issues": []},
            "usage": {"input_tokens": 40, "output_tokens": 10},
        },
    )
    contents = []
    for enabled in (False, True):
        root = tmp_path / str(enabled) / "vault"
        root.mkdir(parents=True)
        log = root.parent / "state/trace.jsonl"
        index = SearchIndex("http://127.0.0.1:19200", str(root))
        planner = Planner(Synchronizer(Documents(root), index), CliModel())
        trace = Trace(log if enabled else None)
        try:
            with Runtime(root, log.parent, planner, trace=trace) as runtime:
                key = runtime.issue_key()
                runtime.submit_intent(
                    key,
                    Intent(
                        kind="write",
                        query="secret query",
                        instruction="private request",
                    ),
                )
                result = runtime.run_next()
                assert result is not None and result["status"] == "completed"
            contents.append((root / "docs/a.md").read_text())
            if not enabled:
                assert not log.exists()
                continue
            raw = log.read_text()
            records = [json.loads(line) for line in raw.splitlines()]
            assert {row["stage"] for row in records} == {
                "search",
                "model",
                "review",
                "apply",
            }
            assert len({row["job"] for row in records}) == 1
            assert records[0]["job"]
            model = next(row for row in records if row["stage"] == "model")
            assert model["input_tokens"] == 50 and model["output_tokens"] == 20
            assert "private" not in raw and "secret" not in raw and key not in raw
        finally:
            trace.close()
            index.close()
    assert contents[0] == contents[1]
    assert calls == 2


# #6: local diagnostic retention is bounded independently of the request lifecycle.
def test_trace_rotation_is_bounded(tmp_path: Path) -> None:
    trace = Trace(tmp_path / "trace.jsonl")
    try:
        for count in range(25000):
            trace.record(
                "model",
                {
                    "attempt": count,
                    "elapsed_ms": 123.456,
                    "input_tokens": 20000,
                    "output_tokens": 1000,
                },
            )
    finally:
        trace.close()
    files = list(tmp_path.glob("trace.jsonl*"))
    assert len(files) == 3
    assert sum(path.stat().st_size for path in files) <= 3 * 1024 * 1024


# #6: failure of the optional log must not fail or retry the document operation.
def test_trace_storage_failure_disables_recording(tmp_path: Path) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("not a directory")
    trace = Trace(blocked / "trace.jsonl")
    with trace.measure("model"):
        result = "success"
    assert result == "success"
    assert not trace.enabled
    trace.close()
