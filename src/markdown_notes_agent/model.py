from __future__ import annotations

__all__ = ["CliModel", "Intent", "Planner"]

import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from markdown_notes_agent.config import Settings
from markdown_notes_agent.context import shape
from markdown_notes_agent.editing import Decision, Review
from markdown_notes_agent.errors import (
    ConflictError,
    ModelError,
    ModelOutputError,
    RequestError,
)
from markdown_notes_agent.profiles import Profile
from markdown_notes_agent.providers import Choice, resolve_choice
from markdown_notes_agent.scope import SearchScope
from markdown_notes_agent.sync import Synchronizer
from markdown_notes_agent.trace import Trace
from markdown_notes_agent.workflows import run_workflow


class Intent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["write", "edit", "organize"]
    instruction: str = Field(min_length=1, max_length=16000)
    query: str = Field(
        min_length=1,
        max_length=1000,
        description="Question or source text for search; not a required document title",
    )
    paths: tuple[str, ...] = Field(
        default=(),
        max_length=100,
        description="Existing notes to read in addition to the search results. "
        "Do not list a note that is still to be created",
    )
    folders: tuple[str, ...] = Field(
        default=(),
        description="Vault-relative folder array with OR scope; empty means all",
    )
    profile: Profile = "note"
    mode: Literal["normal", "bypass"] | None = Field(
        default=None,
        description="normal stops on source change; bypass re-plans. "
        "Omitted uses the configured default",
    )
    allow_delete: bool = False
    fallback_reason: (
        Literal["insufficient_evidence", "user_reported_missing"] | None
    ) = None

    @field_validator("folders")
    @classmethod
    def validate_folders(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return SearchScope(value).folders


# Replaces the CLI's built-in coding-agent system prompt; the task rules travel
# in the prompt itself. Keeps each call free of unrelated tool instructions.
_SYSTEM = (
    "You are a judgment step inside a Markdown document tool. "
    "Follow the instructions in the user message and return only the required JSON."
)


_STOPPED = "The model call stopped: a source note changed or the job was cancelled."


def _strict_schema(node: Any) -> Any:
    """Make every object closed and fully required, as codex structured output needs."""
    if isinstance(node, list):
        return [_strict_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {k: _strict_schema(v) for k, v in node.items() if k != "default"}
    if isinstance(out.get("properties"), dict):
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


class CliModel:
    """Runs models only through the subscription CLIs chosen in settings."""

    def __init__(
        self,
        settings: Settings | None = None,
        override: str | None = None,
        timeout_seconds: float = 600,
    ) -> None:
        self.settings = settings or Settings()
        # --model on the command line replaces the model name for every role.
        self.override = override
        self.timeout_seconds = timeout_seconds

    def choice(self, role: Literal["draft", "review", "keywords"]) -> Choice:
        """The provider, model and effort one role runs with right now."""
        model = self.settings.role(role)
        return resolve_choice(model.provider, self.override or model.name, model.effort)

    # cost: time O(t+n), heap O(n), stack O(1), io 1, tokens O(n)
    # vars: t = provider latency, n = bounded request and response bytes
    # basis: estimate
    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        response = self._invoke(prompt, is_cancelled, Decision, "draft")
        return {"decision": response["value"], "usage": response["usage"]}

    # cost: time O(t+n), heap O(n), stack O(1), io 1, tokens O(n)
    # vars: t = provider latency, n = bounded request and response bytes
    # basis: estimate
    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        response = self._invoke(prompt, is_cancelled, Review, "review")
        return {"review": response["value"], "usage": response["usage"]}

    def ask(
        self,
        prompt: str,
        schema: type[BaseModel],
        is_cancelled: Callable[[], bool],
        role: str = "draft",
    ) -> dict[str, Any]:
        """One structured answer in the given schema."""
        if role not in {"draft", "review", "keywords"}:
            raise ValueError(f"unknown model role: {role}")
        response = self._invoke(prompt, is_cancelled, schema, role)  # type: ignore[arg-type]
        value: dict[str, Any] = response["value"]
        return value

    # cost: time O(1), heap O(s), stack O(d), io O(1), tokens 0
    # vars: s = schema bytes, d = schema depth
    # basis: estimate
    @staticmethod
    def _command(choice: Choice, schema: type[BaseModel], folder: Path) -> list[str]:
        if choice.provider == "codex":
            schema_path = folder / "schema.json"
            schema_path.write_text(
                json.dumps(_strict_schema(schema.model_json_schema())),
                encoding="utf-8",
            )
            command = [
                "codex",
                "exec",
                "--json",
                "--ephemeral",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(folder / "answer.json"),
                "--model",
                choice.model,
            ]
            if choice.effort:
                command += ["-c", f'model_reasoning_effort="{choice.effort}"']
            return [*command, "-"]
        command = [
            "claude",
            "--safe-mode",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema.model_json_schema()),
            "--system-prompt",
            _SYSTEM,
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--no-session-persistence",
            "--model",
            choice.model,
        ]
        if choice.effort:
            command += ["--effort", choice.effort]
        return command

    # cost: time O(n), heap O(n), stack O(1), io O(1), tokens 0
    # vars: n = CLI output bytes
    # basis: estimate
    @staticmethod
    def _parse(
        provider: str, output: str, folder: Path, schema: type[BaseModel]
    ) -> tuple[BaseModel, Any]:
        if provider == "codex":
            usage = None
            for line in output.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.completed":
                    usage = event.get("usage")
                if isinstance(event, dict) and event.get("type") in {
                    "turn.failed",
                    "error",
                }:
                    raise ModelError("The model returned an error.")
            answer = (folder / "answer.json").read_text(encoding="utf-8")
            return schema.model_validate_json(answer), usage
        envelope = json.loads(output)
        if envelope.get("is_error"):
            raise ModelError("The model returned an error.")
        return schema.model_validate(envelope.get("structured_output")), envelope.get(
            "usage"
        )

    # cost: time O(t+n), heap O(n), stack O(1), io O(1), tokens O(n)
    # vars: t = provider latency, n = bounded request and response bytes
    # basis: estimate
    def _invoke(
        self,
        prompt: str,
        is_cancelled: Callable[[], bool],
        schema: type[BaseModel],
        role: Literal["draft", "review", "keywords"] = "draft",
    ) -> dict[str, Any]:
        choice = self.choice(role)
        if is_cancelled():
            raise ConflictError(_STOPPED)
        with tempfile.TemporaryDirectory(prefix="markdown-notes-agent-model-") as temp:
            folder = Path(temp)
            return self._run(prompt, is_cancelled, schema, choice, folder)

    # cost: time O(t+n), heap O(n), stack O(1), io O(1), tokens O(n)
    # vars: t = provider latency, n = bounded request and response bytes
    # basis: estimate
    def _run(
        self,
        prompt: str,
        is_cancelled: Callable[[], bool],
        schema: type[BaseModel],
        choice: Choice,
        folder: Path,
    ) -> dict[str, Any]:
        command = self._command(choice, schema, folder)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=folder,
            )
        except FileNotFoundError as error:
            raise RequestError(
                f"Cannot run {choice.describe()}. Check that it is installed, "
                "signed in and on PATH.",
                code="model_missing",
            ) from error
        with process:
            pending: str | None = prompt
            try:
                while True:
                    if is_cancelled():
                        raise ConflictError(_STOPPED)
                    if time.monotonic() >= deadline:
                        raise ModelError("The model did not answer in time.")
                    try:
                        output, _ = process.communicate(pending, timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        pending = None
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
        if process.returncode != 0:
            raise ModelError(f"The model run failed (exit code {process.returncode}).")
        try:
            value, usage = self._parse(choice.provider, output, folder, schema)
        except (ValueError, ValidationError, AttributeError, OSError) as error:
            raise ModelOutputError(
                "The model output does not match its format."
            ) from error
        return {"value": value.model_dump(), "usage": usage}


class Planner:
    def __init__(self, synchronizer: Synchronizer, model: CliModel) -> None:
        self.synchronizer = synchronizer
        self.model = model
        self.trace = Trace()

    # cost: time O(n+t), heap O(n), stack O(d), io O(f), tokens O(n)
    # vars: n = bounded context and model output bytes, t = model latency
    # vars: d = filesystem depth, f = documents checked
    # basis: estimate
    def prepare(
        self,
        intent: Intent,
        is_cancelled: Callable[[], bool],
        previous: dict[str, Any] | None = None,
        feedback: list[str] | None = None,
    ) -> dict[str, Any]:
        settings = self.synchronizer.settings
        with self.trace.measure("search") as metrics:
            search = self.synchronizer.search(
                intent.query,
                settings.search.candidates,
                fallback_reason=intent.fallback_reason,
                folders=intent.folders,
            )
            if self.trace.enabled:
                metrics["candidates"] = len(search["documents"])
                metrics["source"] = str(search.get("source", "unknown"))
                metrics["reason"] = str(search.get("reason", "ranked"))
                metrics["source_bytes"] = sum(
                    doc["bytes"] for doc in search["documents"]
                )
        required: dict[str, dict[str, Any]] = {}
        applied = previous.get("applied", {}) if previous else {}
        for path in intent.paths:
            try:
                required[path] = self.synchronizer.read(path)
            except FileNotFoundError as error:
                if path not in applied or applied[path] is not None:
                    raise RequestError(
                        f"An input note is missing: {path}. paths lists existing "
                        "notes to read; to create a new note, leave it out of paths "
                        "and say where it goes in the instruction.",
                        code="input_missing",
                    ) from error
        if previous:
            for change in previous.get("proposal", {}).get("changes", []):
                try:
                    required[change["path"]] = self.synchronizer.read(change["path"])
                except FileNotFoundError:
                    required.pop(change["path"], None)
        terms = self.synchronizer.terms(intent.query)
        documents, listed = shape(
            search["documents"],
            required,
            settings,
            terms,
            load=self.synchronizer.read,
            peek=self.synchronizer.peek,
        )
        context = {
            "request": intent.model_dump(mode="json"),
            "documents": documents,
            "listed": listed,
            "reads_left": settings.search.extra_reads,
            "coverage": search.get("coverage"),
            "previous": previous,
            "feedback": feedback or [],
            "applied": sorted(applied),
            "signatures": {path: doc["signature"] for path, doc in documents.items()},
        }
        planned = run_workflow(
            context, self.model, self.synchronizer, self.trace, is_cancelled
        )
        return {
            **planned,
            "search": {
                **{k: v for k, v in search.items() if k != "documents"},
                "read": planned.pop("read"),
                "listed": planned.pop("listed"),
            },
        }
