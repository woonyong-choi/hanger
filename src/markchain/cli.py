from __future__ import annotations

__all__ = ["main"]

import argparse
import json
import logging
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

from elastic_transport import TransportError
from elasticsearch import ApiError
from pydantic import ValidationError

from markchain import __version__
from markchain.config import Settings, load_settings
from markchain.documents import Documents
from markchain.elastic import ES_VERSION, INSTALL_STEPS, ElasticManager
from markchain.errors import MarkchainError
from markchain.logs import configure
from markchain.model import ClaudeModel, Intent, Planner
from markchain.progress import Progress
from markchain.proxy import Launcher, Relay, call_tool
from markchain.requests import Proposal
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.service import Service
from markchain.sync import Synchronizer
from markchain.trace import Trace
from markchain.watcher import VaultWatcher
from markchain.wizard import SetupAbortedError, edit_command, run_setup

logger = logging.getLogger(__name__)

# Commands answered by the shared daemon when it runs (or must run).
_CLIENT = {"key", "search", "status", "cancel", "automate", "collect", "resolve"}
# A shutting-down daemon may hold the state lock briefly.
_DAEMON_LOCK_SECONDS = 90
_INSTALL_ROOT = Path.home() / ".local" / "share" / "markchain"
_DEFAULT_STATE = _INSTALL_ROOT / "vault"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="markchain")
    parser.add_argument(
        "--version", action="version", version=f"markchain {__version__}"
    )
    parser.add_argument(
        "--vault", type=Path, help="vault root (default: the one chosen in setup)"
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=_DEFAULT_STATE,
        help=f"runtime state folder outside the vault (default: {_DEFAULT_STATE})",
    )
    parser.add_argument(
        "--config", type=Path, help="settings TOML (default: <state>/config.toml)"
    )
    parser.add_argument(
        "--search-url",
        help="use an existing Elasticsearch instead of the managed one",
    )
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--trace", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve", help="MCP stdio entry; relays to the shared daemon"
    )
    serve.add_argument(
        "--foreground",
        action="store_true",
        help="run everything in this process instead of the shared daemon",
    )
    commands.add_parser("daemon", help="internal: shared background process")
    commands.add_parser("stop", help="stop the shared daemon")
    setup = commands.add_parser(
        "setup", help="choose the vault and folders, then install the search engine"
    )
    setup.add_argument(
        "--reinstall",
        action="store_true",
        help="download and verify the search engine again",
    )
    setup.add_argument(
        "--managed",
        action="append",
        help="top-level folder to manage (repeat); skips the prompts with --vault",
    )
    config = commands.add_parser("config", help="show or edit the settings file")
    config.add_argument("--edit", action="store_true", help="open it in an editor")
    commands.add_parser("key")
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int)
    search.add_argument(
        "--folder",
        action="append",
        default=[],
        dest="folders",
        help="vault-relative folder; repeat for OR scope; omit to search all",
    )
    search.add_argument(
        "--fallback-reason", choices=("insufficient_evidence", "user_reported_missing")
    )
    automate = commands.add_parser(
        "automate", help="queue an intent; the daemon executes it"
    )
    automate.add_argument("key")
    automate.add_argument(
        "intent", type=Path, help="JSON with kind, instruction and query"
    )
    collect = commands.add_parser(
        "collect", help="save a file or stdin text unchanged under inbox/"
    )
    collect.add_argument("key")
    collect.add_argument("file", help="any file, or - to read text from stdin")
    resolve = commands.add_parser(
        "resolve", help="re-plan a conflicted job from current sources"
    )
    resolve.add_argument("key")
    resolve.add_argument("new_key")
    resolve.add_argument(
        "choice", choices=("reconsider",), nargs="?", default="reconsider"
    )
    for name in ("status", "cancel"):
        commands.add_parser(name).add_argument("key")
    submit = commands.add_parser("submit")
    submit.add_argument("key")
    submit.add_argument("proposal", type=Path)
    commands.add_parser("run", help="execute one queued job locally, then exit")
    commands.add_parser("sync")
    commands.add_parser("watch")
    commands.add_parser("audit")
    commands.add_parser("cleanup")
    return parser


def _global_arguments(arguments: argparse.Namespace) -> list[str]:
    values = ["--state", str(arguments.state.resolve())]
    if arguments.vault is not None:
        values += ["--vault", str(arguments.vault.resolve())]
    if arguments.config:
        values += ["--config", str(arguments.config.resolve())]
    if arguments.search_url:
        values += ["--search-url", arguments.search_url]
    values += ["--model", arguments.model]
    if arguments.trace:
        values.append("--trace")
    return values


# cost: time O(n), heap O(n), stack O(1), io O(f)
# vars: n = request and result bytes, f = target count
# basis: estimate
def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    state: Path = arguments.state
    # The relay and daemon talk over stdio; setup draws its own status lines.
    quiet = arguments.command in {"serve", "daemon", "setup"}
    configure(state, console=None if quiet else logging.WARNING)
    try:
        settings = load_settings(arguments.config or state / "config.toml")
        if arguments.vault is None and arguments.command not in _NO_VAULT:
            if not settings.vault.root:
                raise MarkchainError(
                    "Vault is not configured. Run: markchain setup", code="setup"
                )
            arguments.vault = Path(settings.vault.root)
        if arguments.search_url:
            settings = settings.model_copy(
                update={
                    "elasticsearch": settings.elasticsearch.model_copy(
                        update={"managed": False}
                    )
                }
            )
        launcher = Launcher(
            state,
            [
                sys.executable,
                "-m",
                "markchain.cli",
                *_global_arguments(arguments),
                "daemon",
            ],
        )
        result = _dispatch(arguments, settings, launcher)
        if result is not _SILENT:
            print(json.dumps(result, ensure_ascii=False))
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'input'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        logger.error("invalid input: %s", details)
        print(f"markchain: invalid input: {details}", file=sys.stderr)
        return 1
    except (MarkchainError, OSError, sqlite3.Error, ApiError, TransportError) as error:
        logger.error("operation failed: %s", error)
        print(f"markchain: {error}", file=sys.stderr)
        return 1
    return 0


_SILENT = object()
# Commands that work before a vault is configured.
_NO_VAULT = {"setup", "config", "stop"}


def _dispatch(
    arguments: argparse.Namespace, settings: Settings, launcher: Launcher
) -> object:
    command = arguments.command
    if command == "serve" and not arguments.foreground:
        Relay(launcher, sys.stdin.buffer, sys.stdout.buffer).run()
        return _SILENT
    if command == "stop":
        with launcher.hold():
            return {"stopped": launcher.stop()}
    if command == "setup":
        return _setup(arguments, settings, launcher)
    if command == "config":
        return _config(arguments)
    if command == "daemon":
        return _daemon(arguments, settings, launcher)
    if command in _CLIENT:
        managed = settings.elasticsearch.managed
        connection = launcher.connect()
        if connection is not None or managed:
            if connection is not None:
                connection.close()
            return _remote(arguments, launcher)
    return _local(arguments, settings)


def _config_path(arguments: argparse.Namespace) -> Path:
    path: Path = arguments.config or arguments.state / "config.toml"
    return path


def _config(arguments: argparse.Namespace) -> object:
    path = _config_path(arguments)
    if not path.exists():
        print(f"No settings yet. Run: markchain setup  ({path})")
        return _SILENT
    if arguments.edit:
        subprocess.run(shlex.split(edit_command(path)), check=False)
        return _SILENT
    print(path.read_text(), end="")
    print(f"\n# {path}\n# edit: markchain config --edit  or  {edit_command(path)}")
    return _SILENT


def _setup(
    arguments: argparse.Namespace, settings: Settings, launcher: Launcher
) -> object:
    state: Path = arguments.state.expanduser().resolve()

    recorded = state / "vault.path"
    previous = recorded.read_text() if recorded.exists() else None
    try:
        settings = run_setup(
            _config_path(arguments),
            settings,
            vault=arguments.vault,
            managed=arguments.managed,
        )
    except SetupAbortedError as error:
        print(f"\n  {error}. Nothing was changed.\n", file=sys.stderr)
        raise SystemExit(1) from None
    ui = Progress(sys.stdout)
    engine = ElasticManager(_INSTALL_ROOT, state, settings)
    # Hold the start lock so connected clients cannot restart the service
    # (which would install or start the search engine) until setup ends.
    root = str(Path(str(settings.vault.root)).resolve())
    changed = previous is not None and previous != root
    with launcher.hold():
        install = arguments.reinstall or not engine.installed()
        first = 2 + changed
        total = first - 1 + (INSTALL_STEPS if install else 1)
        step = ui.step(1, total, "Stop background service")
        stopped = launcher.stop()
        if arguments.reinstall:
            # A fresh engine starts from an empty index rebuilt from the vault.
            shutil.rmtree(state / "elasticsearch", ignore_errors=True)
        step.done("stopped" if stopped else "not running")
        if changed:
            step = ui.step(2, total, "Reset for the new vault")
            # Opening the job store for the new root clears the old one; the
            # old search index is removed when the engine next starts.
            Runtime(Path(root), state, settings=settings).close()
            step.done("previous vault jobs removed; its index goes on next start")
        # A stopped service may have finished installing in the meantime.
        install = arguments.reinstall or not engine.installed()
        if install:
            engine.install(
                ui, reinstall=arguments.reinstall, title=None, first=first, total=total
            )
        else:
            step = ui.step(first, total, "Check search engine")
            step.done(f"Elasticsearch {ES_VERSION} already installed")
    ui.stream.write("\n")
    ui.line("done", "Markchain is ready", str(settings.vault.root))
    command = serve_command(state)
    ui.note("connect an MCP client (already registered clients need nothing):")
    ui.note(f"  claude mcp add --scope user markchain -- {command}")
    ui.note(f"  codex mcp add markchain -- {command}")
    ui.stream.write("\n")
    return _SILENT


def serve_command(state: Path) -> str:
    """Return the MCP server command for a state folder, as setup prints it."""
    command = shutil.which("markchain") or "markchain"
    if state != _DEFAULT_STATE.resolve():
        command += f" --state {shlex.quote(str(state))}"
    return command + " serve"


def _daemon(
    arguments: argparse.Namespace, settings: Settings, launcher: Launcher
) -> object:
    """Become the daemon, or step aside when another one already serves."""
    deadline = time.monotonic() + _DAEMON_LOCK_SECONDS
    while True:
        try:
            return _local(arguments, settings)
        except MarkchainError as error:
            if "already in use" not in str(error):
                raise
        connection = launcher.connect()
        if connection is not None:
            connection.close()
            logger.info("another service instance is already running")
            return _SILENT
        if time.monotonic() >= deadline:
            raise MarkchainError(
                "state folder is locked by another process", code="daemon_lock"
            )
        time.sleep(1)


def _remote(arguments: argparse.Namespace, launcher: Launcher) -> Any:
    match arguments.command:
        case "key":
            return {"key": call_tool(launcher, "issue_key", {})}
        case "search":
            return call_tool(launcher, "search_documents", _search_arguments(arguments))
        case "status":
            return call_tool(launcher, "job_status", {"key": arguments.key})
        case "cancel":
            return call_tool(launcher, "cancel_job", {"key": arguments.key})
        case "automate":
            intent = Intent.model_validate_json(arguments.intent.read_bytes())
            return call_tool(
                launcher,
                "request_change",
                {"key": arguments.key, "intent": intent.model_dump(mode="json")},
            )
        case "collect":
            return call_tool(launcher, "collect", _collect_arguments(arguments))
        case "resolve":
            return call_tool(
                launcher,
                "resolve_conflict",
                {
                    "key": arguments.key,
                    "new_key": arguments.new_key,
                    "choice": arguments.choice,
                },
            )
    raise AssertionError("client command must be supported")


def _search_arguments(arguments: argparse.Namespace) -> dict[str, Any]:
    values: dict[str, Any] = {
        "query": arguments.query,
        "folders": list(arguments.folders),
    }
    if arguments.limit:
        values["limit"] = arguments.limit
    if arguments.fallback_reason:
        values["fallback_reason"] = arguments.fallback_reason
    return values


def _collect_arguments(arguments: argparse.Namespace) -> dict[str, str]:
    if arguments.file == "-":
        return {"key": arguments.key, "text": sys.stdin.read()}
    return {"key": arguments.key, "file": str(Path(arguments.file).resolve())}


def _local(arguments: argparse.Namespace, settings: Settings) -> object:
    state: Path = arguments.state
    with ExitStack() as stack:
        engine = None
        if settings.elasticsearch.managed and arguments.command in {"daemon", "serve"}:
            engine = ElasticManager(_INSTALL_ROOT, state, settings)
        url = arguments.search_url or (
            engine.url
            if engine is not None
            else f"http://127.0.0.1:{settings.elasticsearch.port}"
        )
        index = stack.enter_context(
            closing(
                SearchIndex(
                    url, str(arguments.vault.resolve()), owned=engine is not None
                )
            )
        )
        synchronizer = Synchronizer(Documents(arguments.vault), index, settings)
        planner = Planner(synchronizer, ClaudeModel(arguments.model))
        trace = stack.enter_context(
            closing(Trace(state / "trace.jsonl" if arguments.trace else None))
        )
        runtime = stack.enter_context(
            Runtime(arguments.vault, state, planner, trace=trace, settings=settings)
        )
        if arguments.command in {"daemon", "serve"}:
            return _serve(arguments, runtime, synchronizer, engine, settings)
        return _execute(runtime, arguments, synchronizer)


def _serve(
    arguments: argparse.Namespace,
    runtime: Runtime,
    synchronizer: Synchronizer,
    engine: ElasticManager | None,
    settings: Settings,
) -> object:
    from markchain.daemon import Daemon

    service = Service(runtime, synchronizer, engine)
    if engine is not None:
        engine.start()
    service.start()
    try:
        if arguments.command == "daemon":
            Daemon(service, arguments.state, settings.service.idle_minutes).run()
        else:
            service.mcp.run()
    finally:
        service.close()
        if engine is not None:
            engine.stop()
    return _SILENT


# cost: time O(n), heap O(n), stack O(1), io O(f)
# vars: n = request and result bytes, f = target count
# basis: estimate
def _execute(
    runtime: Runtime, arguments: argparse.Namespace, synchronizer: Synchronizer
) -> object:
    match arguments.command:
        case "key":
            return {"key": runtime.issue_key()}
        case "submit":
            proposal = Proposal.model_validate_json(arguments.proposal.read_bytes())
            return {"status": runtime.submit(arguments.key, proposal)}
        case "run":
            return runtime.run_next()
        case "cleanup":
            return runtime.cleanup()
        case "automate":
            intent = Intent.model_validate_json(arguments.intent.read_bytes())
            return {"status": runtime.submit_intent(arguments.key, intent)}
        case "collect":
            service = Service(runtime, synchronizer)
            return service.collect(**_collect_arguments(arguments))
        case "resolve":
            service = Service(runtime, synchronizer)
            return service.resolve_conflict(
                arguments.key, arguments.new_key, arguments.choice
            )
        case "status":
            return runtime.describe(arguments.key)
        case "cancel":
            return {"cancelled": runtime.cancel(arguments.key)}
        case "sync":
            return synchronizer.reconcile()
        case "audit":
            return synchronizer.audit()
        case "search":
            service = Service(runtime, synchronizer)
            return service.search(
                arguments.query,
                arguments.limit,
                arguments.fallback_reason,
                tuple(arguments.folders),
            )
        case "watch":
            stop = threading.Event()
            try:
                VaultWatcher(synchronizer).run(stop)
            except KeyboardInterrupt:
                stop.set()
            return {"status": "stopped"}
    raise AssertionError("parser must select a supported command")


if __name__ == "__main__":
    raise SystemExit(main())
