"""The ``markdown-notes-agent`` command.

Three kinds of commands:

* Management (setup, vault, ignore, decisions, config, provider) works on
  the state database directly; see commands.py.
* Note requests (search, automate, collect, ...) go to the shared background
  service, which starts on demand.
* ``serve`` is the MCP entry point AI clients run; it relays to that service.
"""

from __future__ import annotations

__all__ = ["main"]

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
from contextlib import ExitStack, closing
from pathlib import Path
from typing import Any

from elastic_transport import TransportError
from elasticsearch import ApiError
from filelock import FileLock, Timeout
from pydantic import ValidationError

from markdown_notes_agent import __version__
from markdown_notes_agent.commands import (
    DEFAULT_STATE,
    INSTALL_ROOT,
    config_command,
    decide_command,
    decisions_command,
    ignore_command,
    provider_command,
    setup_command,
    vault_command,
)
from markdown_notes_agent.config import Settings
from markdown_notes_agent.elastic import ElasticManager
from markdown_notes_agent.errors import MarkdownNotesAgentError, for_terminal
from markdown_notes_agent.hub import VaultHub
from markdown_notes_agent.logs import configure
from markdown_notes_agent.model import Intent
from markdown_notes_agent.proxy import Launcher, Relay, call_tool
from markdown_notes_agent.requests import Proposal
from markdown_notes_agent.service import Service
from markdown_notes_agent.store import StateStore
from markdown_notes_agent.trace import Trace
from markdown_notes_agent.watcher import VaultWatcher

logger = logging.getLogger(__name__)

# A shutting-down daemon may hold the state lock briefly.
_DAEMON_LOCK_SECONDS = 90
# Note requests answered by the background service when it runs (or must run).
_CLIENT = {
    "key",
    "search",
    "keywords",
    "status",
    "cancel",
    "automate",
    "collect",
    "resolve",
}
_MANAGEMENT = {"setup", "provider", "vault", "ignore", "decisions", "decide", "config"}
# What to run next, for refusals whose message speaks to an AI client.
_TERMINAL_HINTS = {
    "vault_ambiguous": "choose one with --vault <id>",
    "vault_unknown": "see the vaults with: markdown-notes-agent vault list",
    "decision_required": "answer with: markdown-notes-agent decide <id> "
    "approve|decline",
}


def _vault_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vault", help="vault id or a path inside it (needed with several)"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="markdown-notes-agent",
        description="Keep Markdown notes searchable and in order.",
    )
    parser.add_argument(
        "--version", action="version", version=f"markdown-notes-agent {__version__}"
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
        help=f"Markdown Notes Agent's own data folder (default: {DEFAULT_STATE})",
    )
    parser.add_argument(
        "--search-url", help="use an existing Elasticsearch instead of the managed one"
    )
    parser.add_argument("--model", help="model name for every role, this run only")
    parser.add_argument("--trace", action="store_true", help="record job timings")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")

    setup = commands.add_parser(
        "setup", help="check the model provider and install the search engine"
    )
    setup.add_argument(
        "--reinstall", action="store_true", help="download the search engine again"
    )
    commands.add_parser("provider", help="show the model each role runs with")

    vault = commands.add_parser("vault", help="add, list or remove vaults")
    vault_actions = vault.add_subparsers(dest="action", required=True)
    vault_actions.add_parser("add", help="register a folder as a vault").add_argument(
        "path"
    )
    vault_actions.add_parser("list", help="show registered vaults")
    remove = vault_actions.add_parser("remove", help="forget a vault")
    remove.add_argument("vault", help="vault id or path")
    remove.add_argument("--yes", action="store_true", help="do not ask to confirm")

    ignore = commands.add_parser("ignore", help="show or edit a vault's ignore rules")
    ignore_actions = ignore.add_subparsers(dest="action", required=True)
    for name, text in (
        ("show", "show the applied rules"),
        ("add", "ignore a pattern (.gitignore syntax)"),
        ("remove", "stop ignoring a pattern"),
    ):
        action = ignore_actions.add_parser(name, help=text)
        if name != "show":
            action.add_argument("pattern")
            action.add_argument("--yes", action="store_true", help="approve at once")
        _vault_option(action)

    commands.add_parser("decisions", help="list questions waiting for your answer")
    decide = commands.add_parser("decide", help="answer a waiting question")
    decide.add_argument("id", type=int)
    decide.add_argument("answer", choices=("approve", "decline"))

    config = commands.add_parser("config", help="list or change settings")
    config_actions = config.add_subparsers(dest="action", required=True)
    config_actions.add_parser("list", help="every setting with its value")
    config_actions.add_parser("get", help="one setting").add_argument("key")
    setter = config_actions.add_parser("set", help="change a setting")
    setter.add_argument("key")
    setter.add_argument("value")
    config_actions.add_parser("unset", help="back to the default").add_argument("key")

    serve = commands.add_parser("serve", help="MCP server for AI clients (stdio)")
    serve.add_argument(
        "--foreground",
        action="store_true",
        help="run everything in this process instead of the shared service",
    )
    commands.add_parser("stop", help="stop the background service")
    # Internal: started by the relay. Without help it stays out of the list.
    commands.add_parser("daemon")

    commands.add_parser("key", help="issue a request key")
    search = commands.add_parser("search", help="search notes")
    search.add_argument("query")
    _vault_option(search)
    search.add_argument("--limit", type=int)
    search.add_argument(
        "--folder",
        action="append",
        default=[],
        dest="folders",
        help="folder inside the vault; repeat for several; omit to search all",
    )
    search.add_argument(
        "--fallback-reason", choices=("insufficient_evidence", "user_reported_missing")
    )
    keywords = commands.add_parser(
        "keywords", help="show the keyword tree, or one concept in it"
    )
    keywords.add_argument(
        "concept", nargs="?", help="note path, concept name or generated id"
    )
    _vault_option(keywords)
    automate = commands.add_parser("automate", help="queue a change request")
    automate.add_argument("key")
    automate.add_argument(
        "intent", type=Path, help="JSON with kind, instruction and query"
    )
    _vault_option(automate)
    collect = commands.add_parser("collect", help="save a file or stdin under inbox/")
    collect.add_argument("key")
    collect.add_argument("file", help="any file, or - to read text from stdin")
    _vault_option(collect)
    resolve = commands.add_parser("resolve", help="re-plan a job stopped by a change")
    resolve.add_argument("key")
    resolve.add_argument("new_key")
    resolve.add_argument(
        "choice", choices=("reconsider",), nargs="?", default="reconsider"
    )
    for name, text in (
        ("status", "show a job's progress and result"),
        ("cancel", "cancel a job that has not finished"),
    ):
        commands.add_parser(name, help=text).add_argument("key")

    # Maintenance, run in this process without the background service.
    for name, text in (
        ("sync", "re-check every note against the index"),
        ("audit", "re-read a batch of notes"),
        ("cleanup", "remove expired job records"),
        ("run", "run one queued job, then exit"),
        ("watch", "index changes until interrupted"),
    ):
        _vault_option(commands.add_parser(name, help=text))
    submit = commands.add_parser("submit", help="queue a ready-made proposal")
    submit.add_argument("key")
    submit.add_argument("proposal", type=Path)
    _vault_option(submit)
    return parser


def _daemon_command(arguments: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "markdown_notes_agent.cli",
        "--state",
        str(arguments.state),
    ]
    if arguments.search_url:
        command += ["--search-url", arguments.search_url]
    if arguments.model:
        command += ["--model", arguments.model]
    if arguments.trace:
        command.append("--trace")
    return [*command, "daemon"]


# cost: time O(n), heap O(n), stack O(1), io O(f)
# vars: n = request and result bytes, f = target count
# basis: estimate
def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    arguments.state = arguments.state.expanduser().resolve()
    # The relay and daemon talk over stdio; management commands print their own.
    quiet = arguments.command in {"serve", "daemon"} | _MANAGEMENT
    configure(arguments.state, console=None if quiet else logging.WARNING)
    try:
        store = StateStore(arguments.state)
        launcher = Launcher(arguments.state, _daemon_command(arguments))
        result = _dispatch(arguments, store, launcher)
        if result is not _SILENT:
            print(json.dumps(result, ensure_ascii=False))
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'input'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        logger.error("invalid input: %s", details)
        print(f"markdown-notes-agent: invalid input: {details}", file=sys.stderr)
        return 1
    except MarkdownNotesAgentError as error:
        # Expected and explained to the user; the log keeps it at info level.
        logger.info("request refused [%s]: %s", error.code, error)
        if error.code == "vault_required":
            message = (
                "No vault is set up yet. Add one with: "
                "markdown-notes-agent vault add <folder>"
            )
        else:
            message = for_terminal(str(error))
        print(f"markdown-notes-agent: {message}", file=sys.stderr)
        if hint := _TERMINAL_HINTS.get(error.code):
            print(f"markdown-notes-agent: {hint}", file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error, ApiError, TransportError) as error:
        logger.error("operation failed: %s", error)
        print(f"markdown-notes-agent: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nmarkdown-notes-agent: cancelled", file=sys.stderr)
        return 130
    return 0


_SILENT = object()


def _dispatch(
    arguments: argparse.Namespace, store: StateStore, launcher: Launcher
) -> object:
    command = arguments.command
    if command in _MANAGEMENT:
        _manage(arguments, store, launcher)
        return _SILENT
    if command == "serve" and not arguments.foreground:
        Relay(launcher, sys.stdin.buffer, sys.stdout.buffer).run()
        return _SILENT
    if command == "stop":
        with launcher.hold():
            return {"stopped": launcher.stop()}
    settings = _settings(store, arguments)
    if command == "daemon":
        return _daemon(arguments, store, settings, launcher)
    if command in _CLIENT:
        connection = launcher.connect()
        if connection is not None or settings.elasticsearch.managed:
            if connection is not None:
                connection.close()
            return _remote(arguments, launcher)
    return _local(arguments, store, settings)


def _manage(
    arguments: argparse.Namespace, store: StateStore, launcher: Launcher
) -> None:
    match arguments.command:
        case "setup":
            setup_command(store, launcher, INSTALL_ROOT, reinstall=arguments.reinstall)
        case "provider":
            provider_command(store)
        case "vault":
            target = getattr(arguments, "path", None) or getattr(
                arguments, "vault", None
            )
            vault_command(
                store,
                launcher,
                arguments.action,
                target,
                yes=getattr(arguments, "yes", False),
            )
        case "ignore":
            ignore_command(
                store,
                arguments.action,
                getattr(arguments, "pattern", ""),
                arguments.vault,
                yes=getattr(arguments, "yes", False),
            )
        case "decisions":
            decisions_command(store)
        case "decide":
            decide_command(store, arguments.id, arguments.answer)
        case "config":
            config_command(
                store,
                arguments.action,
                getattr(arguments, "key", ""),
                getattr(arguments, "value", ""),
            )


def _settings(store: StateStore, arguments: argparse.Namespace) -> Settings:
    settings = store.settings()
    if arguments.search_url:
        unmanaged = settings.elasticsearch.model_copy(update={"managed": False})
        settings = settings.model_copy(update={"elasticsearch": unmanaged})
    return settings


def _daemon(
    arguments: argparse.Namespace,
    store: StateStore,
    settings: Settings,
    launcher: Launcher,
) -> object:
    """Become the daemon, or step aside when another one already serves.

    The daemon lock is held for the daemon's whole life, so a new daemon
    waits while an old one is still shutting down.
    """
    lock = FileLock(store.state / "daemon.lock", timeout=0)
    deadline = time.monotonic() + _DAEMON_LOCK_SECONDS
    while True:
        try:
            with lock:
                return _local(arguments, store, settings)
        except Timeout:
            pass
        connection = launcher.connect()
        if connection is not None:
            connection.close()
            logger.info("another service instance is already running")
            return _SILENT
        if time.monotonic() >= deadline:
            raise MarkdownNotesAgentError(
                "The state folder is locked by another process.", code="daemon_lock"
            )
        time.sleep(1)


def _client_call(arguments: argparse.Namespace) -> tuple[str, dict[str, Any]]:
    """The MCP tool and arguments a client command stands for."""
    vault = {"vault": arguments.vault} if getattr(arguments, "vault", None) else {}
    match arguments.command:
        case "key":
            return "issue_key", {}
        case "search":
            return "search_documents", {**_search_arguments(arguments), **vault}
        case "keywords":
            values = {"concept": arguments.concept} if arguments.concept else {}
            return "keyword_tree", {**values, **vault}
        case "status":
            return "job_status", {"key": arguments.key}
        case "cancel":
            return "cancel_job", {"key": arguments.key}
        case "automate":
            intent = Intent.model_validate_json(arguments.intent.read_bytes())
            return "request_change", {
                "key": arguments.key,
                "intent": intent.model_dump(mode="json"),
                **vault,
            }
        case "collect":
            return "collect", {**_collect_arguments(arguments), **vault}
        case "resolve":
            return "resolve_conflict", {
                "key": arguments.key,
                "new_key": arguments.new_key,
                "choice": arguments.choice,
            }
    raise AssertionError("client command must be supported")


def _remote(arguments: argparse.Namespace, launcher: Launcher) -> Any:
    tool, values = _client_call(arguments)
    result = call_tool(launcher, tool, values)
    return {"key": result} if tool == "issue_key" else result


def _call_local(service: Service, tool: str, values: dict[str, Any]) -> Any:
    """Run a client command's tool in this process, as the daemon would."""
    if tool == "search_documents":
        return service.search(**{**values, "folders": tuple(values["folders"])})
    if tool == "request_change":
        values = {**values, "intent": Intent.model_validate(values["intent"])}
    result = getattr(service, tool)(**values)
    return {"key": result} if tool == "issue_key" else result


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


def _local(
    arguments: argparse.Namespace, store: StateStore, settings: Settings
) -> object:
    """Run the vaults in this process: as the daemon, or for one command."""
    serving = arguments.command in {"daemon", "serve"}
    with ExitStack() as stack:
        engine = None
        if settings.elasticsearch.managed and serving:
            engine = ElasticManager(INSTALL_ROOT, store.state, settings)
        url = arguments.search_url or (
            engine.url
            if engine is not None
            else f"http://127.0.0.1:{settings.elasticsearch.port}"
        )
        trace = stack.enter_context(
            closing(Trace(store.state / "trace.jsonl" if arguments.trace else None))
        )
        hub = VaultHub(
            store,
            settings,
            url,
            model=arguments.model,
            trace=trace,
            owns_search=engine is not None,
        )
        stack.callback(hub.close)
        if serving:
            return _serve(arguments, hub, engine, settings)
        return _execute(arguments, hub)


def _serve(
    arguments: argparse.Namespace,
    hub: VaultHub,
    engine: ElasticManager | None,
    settings: Settings,
) -> object:
    from markdown_notes_agent.daemon import Daemon

    hub.refresh()
    service = Service(hub, engine)
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
def _execute(arguments: argparse.Namespace, hub: VaultHub) -> object:
    hub.refresh()
    service = Service(hub)
    command = arguments.command
    if command in _CLIENT:
        return _call_local(service, *_client_call(arguments))
    vault = getattr(arguments, "vault", None)
    opened = hub.get(vault)
    runtime, synchronizer = opened.runtime, opened.synchronizer
    match command:
        case "submit":
            service.ready()
            proposal = Proposal.model_validate_json(arguments.proposal.read_bytes())
            return {"status": runtime.submit(arguments.key, proposal)}
        case "run":
            service.ready()
            return runtime.run_next()
        case "cleanup":
            return runtime.cleanup()
        case "sync":
            return synchronizer.reconcile()
        case "audit":
            return synchronizer.audit()
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
