from __future__ import annotations

__all__ = [
    "DEFAULT_STATE",
    "INSTALL_ROOT",
    "vault_command",
    "config_command",
    "decide_command",
    "decisions_command",
    "ignore_command",
    "provider_command",
    "setup_command",
]

import json
import shlex
import shutil
import sys
from pathlib import Path
from typing import Any, Literal

from markdown_notes_agent.config import Settings
from markdown_notes_agent.documents import Documents
from markdown_notes_agent.elastic import ES_VERSION, INSTALL_STEPS, ElasticManager
from markdown_notes_agent.errors import RequestError
from markdown_notes_agent.ignore import IGNORE_FILE
from markdown_notes_agent.progress import Progress
from markdown_notes_agent.providers import ProviderMissingError, resolve_choice
from markdown_notes_agent.proxy import Launcher
from markdown_notes_agent.store import StateStore, Vault
from markdown_notes_agent.vaults import (
    check_ignore,
    describe_decision,
    ignore_file,
    register_vault,
    resolve_decision,
)

INSTALL_ROOT = Path.home() / ".local" / "share" / "markdown-notes-agent"
DEFAULT_STATE = INSTALL_ROOT / "state"
_RESTART = (
    "Takes effect when the background service restarts: markdown-notes-agent stop"
)


def _ui() -> Progress:
    return Progress(sys.stdout)


def _can_confirm(*, yes: bool) -> None:
    """Fail before changing anything when no one can answer the question."""
    if not yes and not sys.stdin.isatty():
        raise RequestError(
            "This needs confirmation. Run it in a terminal, or add --yes.",
            code="confirm",
        )


def _confirm(ui: Progress, question: str, *, yes: bool) -> bool:
    _can_confirm(yes=yes)
    return yes or ui.ask(question + " (y/N)").lower() in {"y", "yes"}


def _pick(store: StateStore, reference: str | None) -> Vault:
    """The vault named by --vault, or the only one."""
    vaults = store.vaults()
    if not vaults:
        raise RequestError(
            "No vault yet. Add one: markdown-notes-agent vault add <folder>",
            code="vault_required",
        )
    if reference is None:
        if len(vaults) > 1:
            raise RequestError(
                "More than one vault; choose one with --vault <id|path>. "
                "See: markdown-notes-agent vault list",
                code="vault_ambiguous",
            )
        return vaults[0]
    found = store.vault(reference)
    if found is None:
        raise RequestError(
            f"No vault matches {reference}. See: markdown-notes-agent vault list",
            code="vault_unknown",
        )
    return found


# ---------- setup and provider ----------


def provider_lines(ui: Progress, settings: Settings) -> None:
    """Show which CLI and model each role runs with; fail when none is found."""
    roles: tuple[Literal["draft", "review", "keywords"], ...] = (
        "draft",
        "review",
        "keywords",
    )
    for role in roles:
        model = settings.role(role)
        try:
            choice = resolve_choice(model.provider, model.name, model.effort)
        except ProviderMissingError:
            ui.line("fail", "Model provider", "not found")
            raise ProviderMissingError(
                "Provider detection failed, so Markdown Notes Agent cannot run. "
                "Install Claude "
                "Code (claude) or Codex (codex), sign in, and run "
                "markdown-notes-agent setup "
                "again."
            ) from None
        ui.line("done", f"Model for {role}", choice.describe())


def provider_command(store: StateStore) -> None:
    ui = _ui()
    provider_lines(ui, store.settings())
    ui.note("change it: markdown-notes-agent config set model.provider <claude|codex>")
    ui.note("           markdown-notes-agent config set model.name <model>")


# cost: time O(a), heap O(1), stack O(1), io O(a)
# vars: a = search engine archive bytes, downloaded once per version
# basis: estimate
def setup_command(
    store: StateStore, launcher: Launcher, install_root: Path, *, reinstall: bool
) -> None:
    """Check the model provider and install the search engine. No questions."""
    ui = _ui()
    settings = store.settings()
    ui.title("Markdown Notes Agent setup")
    provider_lines(ui, settings)
    engine = ElasticManager(install_root, store.state, settings)
    with launcher.hold():
        install = reinstall or not engine.installed()
        total = 1 + (INSTALL_STEPS if install else 1)
        step = ui.step(1, total, "Stop background service")
        stopped = launcher.stop()
        if reinstall:
            # A fresh engine starts from empty indexes rebuilt from the vaults.
            shutil.rmtree(store.state / "elasticsearch", ignore_errors=True)
        step.done("stopped" if stopped else "not running")
        if install:
            engine.install(ui, reinstall=reinstall, first=2, total=total)
        else:
            step = ui.step(2, total, "Check search engine")
            step.done(f"Elasticsearch {ES_VERSION} already installed")
    ui.stream.write("\n")
    ui.line("done", "Markdown Notes Agent is ready")
    command = serve_command(store.state)
    ui.note("connect your AI clients (once):")
    ui.note(f"  claude mcp add --scope user markdown-notes-agent -- {command}")
    ui.note(f"  codex mcp add markdown-notes-agent -- {command}")
    if not store.vaults():
        ui.note("then add a vault, or let the AI client ask for one:")
        ui.note("  markdown-notes-agent vault add <folder>")
    ui.stream.write("\n")


def serve_command(state: Path) -> str:
    """The MCP server command for a state folder, as setup prints it."""
    command = shutil.which("markdown-notes-agent") or "markdown-notes-agent"
    if state.resolve() != DEFAULT_STATE.resolve():
        command += f" --state {shlex.quote(str(state))}"
    return command + " serve"


# ---------- vaults ----------


def vault_command(
    store: StateStore, launcher: Launcher, action: str, target: str | None, *, yes: bool
) -> None:
    ui = _ui()
    if action == "add":
        result = register_vault(store, Path(target or "."))
        vault = result["vault"]
        ui.line("done", "Vault added", f"#{vault['id']} {vault['path']}")
        for name in result["created"]:
            ui.note(f"created {name}")
        ui.note("Markdown Notes Agent indexes its notes in the background.")
        return
    if action == "list":
        _list_vaults(ui, store)
        return
    vault = _pick(store, target)
    question = (
        f"Remove vault #{vault.id} {vault.path}? Its search index and job "
        f"history are deleted; the notes, {IGNORE_FILE} and "
        "VAULT.md stay"
    )
    if not _confirm(ui, question, yes=yes):
        ui.line("fail", "Vault kept", str(vault.path))
        return
    store.remove_vault(vault.id)
    connection = launcher.connect()
    if connection is None:
        if vault.path.is_dir():
            Documents(vault.path).release()
        shutil.rmtree(store.state / "vaults" / str(vault.id), ignore_errors=True)
    else:
        connection.close()
    ui.line("done", "Vault removed", f"#{vault.id} {vault.path}")


def _list_vaults(ui: Progress, store: StateStore) -> None:
    vaults = store.vaults()
    if not vaults:
        ui.line(
            "ask", "No vault yet", "add one: markdown-notes-agent vault add <folder>"
        )
        return
    pending = {d.vault_id for d in store.decisions()}
    for vault in vaults:
        if not vault.path.is_dir():
            state = ui.style("folder missing", "31")
        elif vault.id in pending:
            state = ui.style(
                "waiting for a decision: markdown-notes-agent decisions", "33"
            )
        else:
            state = ui.style("ok", "32")
        ui.line("done", f"#{vault.id} {vault.path}", state)


# ---------- ignore rules ----------


# cost: time O(f*p), heap O(f), stack O(d), io O(f)
# vars: f = files in the vault, p = patterns, d = folder depth
# basis: estimate
def ignore_command(
    store: StateStore,
    action: str,
    pattern: str,
    vault_reference: str | None,
    *,
    yes: bool,
) -> None:
    ui = _ui()
    vault = _pick(store, vault_reference)
    path = ignore_file(vault.path)
    if action == "show":
        ui.line("done", "Ignore file", str(path))
        for line in store.applied_ignore(vault.id).splitlines():
            ui.stream.write(f"       {line}\n")
        if check_ignore(store, vault) is not None:
            ui.line(
                "ask",
                "Unapplied edit",
                "the file differs: markdown-notes-agent decisions",
            )
        return
    _can_confirm(yes=yes)
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = current.splitlines()
    if action == "add":
        if pattern in (line.strip() for line in lines):
            ui.line("done", "Already ignored", pattern)
            return
        lines.append(pattern)
    else:
        kept = [line for line in lines if line.strip() != pattern]
        if len(kept) == len(lines):
            raise RequestError(f"{IGNORE_FILE} has no line {pattern}", code="not_found")
        lines = kept
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    decision_id = check_ignore(store, vault)
    if decision_id is None:
        ui.line("done", "Ignore file updated", "nothing to re-index")
        return
    decision = store.decision(decision_id)
    if decision is None:
        return
    described = describe_decision(decision)
    ui.line("ask", f"Decision #{decision_id}", described["question"])
    approve = _confirm(ui, described["choices"]["approve"] + "?", yes=yes)
    result = resolve_decision(store, decision_id, "approve" if approve else "decline")
    ui.line("done" if approve else "fail", "Ignore rules", result["message"])


# ---------- decisions ----------


def decisions_command(store: StateStore) -> None:
    ui = _ui()
    for vault in store.vaults():
        if vault.path.is_dir():
            check_ignore(store, vault)
    pending = store.decisions()
    if not pending:
        ui.line("done", "No decision is waiting")
        return
    for decision in pending:
        described = describe_decision(decision)
        ui.line("ask", f"Decision #{decision.id}", described["question"])
        ui.note(f"approve: {described['choices']['approve']}")
        ui.note(f"decline: {described['choices']['decline']}")
        for example in described["examples_leaving"]:
            ui.note(f"leaves the index: {example}")
        for example in described["examples_entering"]:
            ui.note(f"enters the index: {example}")
        ui.note(f"answer: {described['answer_with']}")


def decide_command(store: StateStore, decision_id: int, answer: str) -> None:
    result = resolve_decision(
        store, decision_id, "approve" if answer == "approve" else "decline"
    )
    _ui().line("done", f"Decision #{decision_id} {answer}d", result["message"])


# ---------- settings ----------


def _parse(value: str) -> Any:
    try:
        return json.loads(value)
    except ValueError:
        return value


def config_command(store: StateStore, action: str, key: str, value: str) -> None:
    ui = _ui()
    if action == "list":
        changed = store.changed_settings()
        for name, current in store.setting_values().items():
            mark = ui.style("  (changed)", "33") if name in changed else ""
            ui.stream.write(f"  {name} = {json.dumps(current)}{mark}\n")
        ui.note(
            "change: markdown-notes-agent config set <key> <value>   "
            "reset: markdown-notes-agent config unset <key>"
        )
        return
    values = store.setting_values()
    if key not in values:
        raise RequestError(
            f"Unknown setting: {key}. See: markdown-notes-agent config list",
            code="config",
        )
    if action == "get":
        ui.stream.write(json.dumps(values[key]) + "\n")
        return
    if action == "set":
        stored = store.set_setting(key, _parse(value))
        ui.line("done", key, json.dumps(stored))
    else:
        store.unset_setting(key)
        ui.line(
            "done", key, f"back to default {json.dumps(store.setting_values()[key])}"
        )
    ui.note(_RESTART)
