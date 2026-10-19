"""Compare prompts through the current guarded workflow with recorded checks."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import tomllib
import types
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from time import perf_counter
from typing import Any
from unittest.mock import patch

from measure_prompts import render

from markdown_notes_agent import prompts, workflows
from markdown_notes_agent.documents import Documents
from markdown_notes_agent.editing import Decision, EditPolicy, Review, materialize
from markdown_notes_agent.errors import (
    MarkdownNotesAgentError,
    ModelError,
    RequestError,
)
from markdown_notes_agent.jobs import JobState
from markdown_notes_agent.keywords import Extraction, Split
from markdown_notes_agent.model import CliModel, Intent, Planner
from markdown_notes_agent.profiles import PROFILES
from markdown_notes_agent.providers import Choice
from markdown_notes_agent.requests import issue_key
from markdown_notes_agent.runtime import Runtime
from markdown_notes_agent.search import SearchIndex
from markdown_notes_agent.sync import Synchronizer
from markdown_notes_agent.workflows import REQUEST_SOURCE, _tidy_citations

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {"draft": Decision, "review": Review, "extract": Extraction, "split": Split}
TOOL_TYPES = {
    "command_execution",
    "mcp_tool_call",
    "web_search",
    "file_change",
    "tool_use",
    "tool_call",
    "function_call",
}


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _baseline(ref: str) -> types.ModuleType:
    project = tomllib.loads(
        subprocess.check_output(
            ["git", "show", f"{ref}:pyproject.toml"], cwd=ROOT, text=True
        )
    )
    package = project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"][0]
    module = types.ModuleType("baseline_prompts")
    profiles: dict[str, Any] = {"__name__": "markdown_notes_agent.profiles"}
    for name, namespace in (("profiles", profiles), ("prompts", module.__dict__)):
        source = subprocess.check_output(
            ["git", "show", f"{ref}:{package}/{name}.py"],
            cwd=ROOT,
            text=True,
        )
        source = source.replace(
            f"from {Path(package).name}", "from markdown_notes_agent"
        )
        exec(compile(source, f"baseline_{name}", "exec"), namespace)
    module.PROFILES = profiles["PROFILES"]
    return module


def _cases() -> list[dict[str, Any]]:
    cases = json.loads((ROOT / "tests/fixtures/prompt-cases.json").read_text())["cases"]
    work = copy.deepcopy(cases[0])
    work["name"] = "work-record-revised-judgment"
    work["state"]["request"]["profile"] = "work_record"
    return [
        *cases,
        work,
        {
            "name": "review-reject-invented-result",
            "role": "review",
            "state": copy.deepcopy(cases[0]["state"]),
            "changes": [
                {
                    "path": "reference/cache.md",
                    "diff": (
                        "-판단: 검증 전이므로 운영 채택은 보류한다.\n"
                        "+판단: 운영 검증을 통과했으며 성능이 100배 향상되었다."
                    ),
                }
            ],
        },
    ]


def _tool_calls(output: str) -> int:
    calls: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            if value.get("type") in TOOL_TYPES:
                calls.add(str(value.get("id", json.dumps(value, sort_keys=True))))
            for item in value.values():
                if isinstance(item, (dict, list)):
                    visit(item)

    for line in output.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        visit(event)
    return len(calls)


def _draft_checks(case: dict[str, Any], value: Decision, folder: Path) -> list[str]:
    state = case["state"]
    sources = {
        **state["documents"],
        REQUEST_SOURCE: {
            "content": state["request"]["instruction"],
            "hash": "",
        },
    }
    proposal = materialize(
        _tidy_citations(value),
        sources,
        Documents(folder),
        EditPolicy(
            allow_delete=state["request"]["allow_delete"],
            require_evidence=PROFILES[state["request"]["profile"]].require_evidence,
        ),
    )
    return _proposal_checks(case, proposal, folder)


def _proposal_checks(
    case: dict[str, Any], proposal: dict[str, Any], folder: Path
) -> list[str]:
    state = case["state"]
    documents = {path: source["content"] for path, source in state["documents"].items()}
    failures = []
    for change in proposal.get("changes", []):
        if change["path"] not in documents or change["after"] is None:
            failures.append("unexpected document creation or deletion")
        documents[change["path"]] = change["after"]
    _write(folder / "materialized.json", {"proposal": proposal, "documents": documents})
    text = "\n".join(content for content in documents.values() if content is not None)
    expected = case["expected"]
    for required in [*expected["preserve"], *expected.get("required_after", [])]:
        alternatives = [required] if isinstance(required, str) else required
        if not any(candidate in text for candidate in alternatives):
            failures.append("missing text: " + " | ".join(alternatives))
    return failures


def _checks(case: dict[str, Any], value: Any, folder: Path) -> list[str]:
    if isinstance(value, Decision):
        return _draft_checks(case, value, folder)
    if isinstance(value, Review):
        return ["invented result approved"] if value.approved else []
    if isinstance(value, Extraction):
        allowed = {item["id"] for item in case["args"]["candidates"]}
        references = [value.same_as, *value.related, *value.requires]
        references += [ref for item in value.broader for ref in (item.id, item.under)]
        return [
            *value.problems(),
            *[
                "unknown candidate: " + ref
                for ref in references
                if ref and ref not in allowed
            ],
        ]
    allowed = {item["id"] for item in case["args"]["children"]}
    assigned = [child for group in value.groups for child in group.children]
    assigned += value.unassigned
    failures = []
    if len(assigned) != len(set(assigned)) or set(assigned) != allowed:
        failures.append("invalid child partition")
    if any(len(group.children) < 2 for group in value.groups):
        failures.append("singleton group")
    return failures


class _RecordingModel(CliModel):
    def __init__(self, case: dict[str, Any], folder: Path, args: Any) -> None:
        super().__init__()
        self.case = case
        self.folder = folder
        self.args = args
        self.calls: list[dict[str, Any]] = []

    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        return self._record("draft", prompt)

    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        return self._record("review", prompt)

    def _record(self, role: str, prompt: str) -> dict[str, Any]:
        if len(self.calls) >= self.args.max_calls:
            raise RequestError(
                "evaluation model call limit reached", code="evaluation_call_limit"
            )
        folder = self.folder / f"{len(self.calls) + 1:02d}-{role}"
        row, value = _invoke(prompt, role, folder, self.args)
        self.calls.append(row)
        if value is None:
            raise ModelError(row["error"])
        try:
            if role == "draft":
                row["checks"] = _draft_checks(self.case, value, folder)
            elif not value.approved or value.issues:
                row["checks"] = value.issues or ["review did not approve"]
        except MarkdownNotesAgentError as error:
            row["checks"] = [str(error)]
        if row["tool_calls"]:
            row["checks"].append("unexpected tool call")
        row["status"] = "fail" if row["checks"] else "pass"
        _write(folder / "results.json", row)
        key = "decision" if role == "draft" else "review"
        return {key: value.model_dump(mode="json"), "usage": row["usage"]}


class _FixturePlanner(Planner):
    def __init__(self, sync: Synchronizer, model: _RecordingModel) -> None:
        super().__init__(sync, model)
        self.case = model.case
        self.folder = model.folder
        self.attempts: list[dict[str, Any]] = []
        self.result: dict[str, Any] = {}
        self.calls = model.calls

    def prepare(
        self,
        intent: Intent,
        is_cancelled: Callable[[], bool],
        previous: dict[str, Any] | None = None,
        feedback: list[str] | None = None,
    ) -> dict[str, Any]:
        context = {
            **self.case["state"],
            "previous": previous,
            "feedback": feedback or [],
        }
        attempt: dict[str, Any] = {
            "feedback": feedback or [],
            "checks": [],
            "error": None,
        }
        self.attempts.append(attempt)
        try:
            self.result = workflows.run_workflow(
                context, self.model, self.synchronizer, self.trace, is_cancelled
            )
            attempt["result"] = self.result
            attempt["checks"] = _proposal_checks(
                self.case, self.result["proposal"], self.folder
            )
            return self.result
        except MarkdownNotesAgentError as error:
            attempt["error"] = str(error)
            attempt["error_code"] = error.code
            raise
        finally:
            attempt["call_end"] = len(self.calls)
            _write(self.folder / "attempts.json", self.attempts)


class _MaterializeOnlyRuntime(Runtime):
    def _apply(self, state: JobState) -> dict[str, Any]:
        return {"status": "completed"}


def _invoke(
    prompt: str, role: str, folder: Path, args: Any
) -> tuple[dict[str, Any], Any]:
    folder.mkdir(parents=True, exist_ok=False)
    (folder / "prompt.txt").write_text(prompt, encoding="utf-8")
    row: dict[str, Any] = {
        "role": role,
        "artifact": str(folder),
        "status": "error",
        "usage": None,
        "error": None,
        "tool_calls": 0,
        "checks": [],
        "input_utf8_bytes": len(prompt.encode()),
    }
    value = None
    started = perf_counter()
    try:
        schema = SCHEMAS[role]
        command = CliModel._command(
            Choice(args.provider, args.model, args.effort), schema, folder
        )
        proc = subprocess.run(
            command,
            input=prompt,
            text=True,
            capture_output=True,
            cwd=folder,
            timeout=args.timeout_seconds,
        )
        (folder / "events.jsonl").write_text(proc.stdout, encoding="utf-8")
        (folder / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
        row["tool_calls"] = _tool_calls(proc.stdout)
        row["returncode"] = proc.returncode
        if proc.returncode:
            raise RuntimeError(f"CLI exit {proc.returncode}: {proc.stderr[-400:]}")
        value, row["usage"] = CliModel._parse(
            args.provider, proc.stdout, folder, schema
        )
        _write(folder / "answer.json", value.model_dump(mode="json"))
        row["status"] = "pass"
    except subprocess.TimeoutExpired as error:
        for name, content in (
            ("events.jsonl", error.stdout),
            ("stderr.txt", error.stderr),
        ):
            data = content if isinstance(content, bytes) else (content or "").encode()
            (folder / name).write_bytes(data)
        row["tool_calls"] = _tool_calls((folder / "events.jsonl").read_text())
        row["error"] = f"timeout after {args.timeout_seconds} seconds"
    except (MarkdownNotesAgentError, OSError, ValueError, RuntimeError) as error:
        row["error"] = str(error)
    row["elapsed_seconds"] = perf_counter() - started
    _write(folder / "results.json", row)
    return row, value


def _writing_run(
    case: dict[str, Any], module: Any, folder: Path, args: Any
) -> dict[str, Any]:
    folder.mkdir(parents=True, exist_ok=False)
    root = folder / "vault"
    root.mkdir()
    state = case["state"]
    for path, content in {
        **{path: doc["content"] for path, doc in state["documents"].items()},
        **state["rule_catalog"],
    }.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    with closing(SearchIndex("http://127.0.0.1:9", str(root))) as index:
        sync = Synchronizer(Documents(root), index)
        case = copy.deepcopy(case)
        case["state"]["documents"] = {
            path: sync.read(path) for path in state["documents"]
        }
        case["state"]["signatures"] = {
            path: doc["signature"] for path, doc in case["state"]["documents"].items()
        }
        model = _RecordingModel(case, folder, args)
        planner = _FixturePlanner(sync, model)

        def draft(context: dict[str, Any]) -> str:
            return render(module, {"role": "draft", "state": context})

        def review(context: dict[str, Any], changes: list[dict[str, str]]) -> str:
            return render(
                module, {"role": "review", "state": context, "changes": changes}
            )

        with (
            patch.object(workflows, "draft_prompt", draft),
            patch.object(workflows, "review_prompt", review),
            patch.object(sync, "search", return_value={"documents": []}),
            _MaterializeOnlyRuntime(root, folder / "runtime", planner) as runtime,
        ):
            intent = Intent.model_validate(
                {**state["request"], "query": "fixed fixture"}
            )
            runtime.submit_intent(issue_key(runtime._secret), intent)
            result = runtime.run_next()
        _write(folder / "runtime-result.json", result)
        final = planner.attempts[-1]
        checks = list(final["checks"])
        status = result.get("status") if result else None
        if status not in {"completed", "deferred"}:
            checks.append("workflow did not finish: " + str(status))
        if any(call["tool_calls"] for call in model.calls):
            checks.append("unexpected tool call")
        for path, source in state["documents"].items():
            if (root / path).read_text() != source["content"]:
                checks.append("fixture source changed on disk: " + path)
        usage: dict[str, int | float] = {}
        for call in model.calls:
            for key, value in (call["usage"] or {}).items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
        first = planner.attempts[0]
        return {
            "status": "fail" if checks else "pass",
            "error": result.get("error") if result else "no runtime result",
            "checks": checks,
            "usage": usage or None,
            "tool_calls": sum(call["tool_calls"] for call in model.calls),
            "model_calls": len(model.calls),
            "calls": model.calls,
            "first_pass": not first["error"]
            and not first["checks"]
            and not any(
                call["tool_calls"] for call in model.calls[: first["call_end"]]
            ),
            "first_draft_pass": bool(model.calls)
            and model.calls[0]["status"] == "pass",
            "final_pass": not checks,
            "attempts": planner.attempts,
            "runtime_status": status,
            "disk_apply": False,
        }


def _run(case: dict[str, Any], variant: str, module: Any, args: Any) -> dict[str, Any]:
    folder = args.output / f"{case['name']}-{variant}"
    started = perf_counter()
    if case["role"] == "draft":
        row = _writing_run(case, module, folder, args)
    else:
        row, value = _invoke(render(module, case), case["role"], folder, args)
        if value is not None:
            row["checks"] = _checks(case, value, folder)
            if row["tool_calls"]:
                row["checks"].append("unexpected tool call")
            row["status"] = "fail" if row["checks"] else "pass"
        row.update(
            first_pass=row["status"] == "pass",
            final_pass=row["status"] == "pass",
            model_calls=1,
        )
    row.update(
        case=case["name"], variant=variant, elapsed_seconds=perf_counter() - started
    )
    _write(folder / "results.json", row)
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("codex", "claude"), default="codex")
    parser.add_argument("--model", default="gpt-6-luna")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--baseline", default="36906f6")
    parser.add_argument(
        "--output", type=Path, default=ROOT / ".local/runs/prompt-evaluation"
    )
    parser.add_argument(
        "--case", action="append", dest="cases", help="case name; repeatable"
    )
    parser.add_argument("--timeout-seconds", type=float, default=150)
    parser.add_argument(
        "--max-calls",
        type=int,
        default=6,
        help="total draft/review calls per writing case and variant",
    )
    args = parser.parse_args()
    if args.max_calls < 1 or args.timeout_seconds <= 0:
        parser.error("max-calls and timeout-seconds must be positive")
    args.output = args.output.resolve()
    if not args.output.is_relative_to(ROOT / ".local/runs/prompt-evaluation"):
        parser.error(
            "output must be under this repository's .local/runs/prompt-evaluation"
        )
    cases = _cases()
    names = {case["name"] for case in cases}
    if args.cases and set(args.cases) - names:
        parser.error("unknown case: " + ", ".join(sorted(set(args.cases) - names)))
    baseline = _baseline(args.baseline)
    report: dict[str, Any] = {
        "provider": args.provider,
        "model": args.model,
        "effort": args.effort,
        "baseline": args.baseline,
        "runs_per_variant": 1,
        "max_calls_per_writing_case": args.max_calls,
        "timeout_seconds_per_call": args.timeout_seconds,
        "review_attempts": 3,
        "scope": (
            "prompt comparison at current guarded workflow, not old runtime; "
            "fixed synthetic inputs; production assembly/review/runtime retry; "
            "fixture retrieval and materialized output, no disk apply; "
            "independent semantic review needed; "
            "no speed or general model quality conclusion; "
            "tool calls detected, not blocked"
        ),
        "rows": [],
    }
    for case in cases:
        if args.cases and case["name"] not in args.cases:
            continue
        for variant, module in (("baseline", baseline), ("compact", prompts)):
            row = _run(case, variant, module, args)
            report["rows"].append(row)
            _write(args.output / "results.json", report)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    return int(any(row["status"] != "pass" for row in report["rows"]))


if __name__ == "__main__":
    raise SystemExit(main())
