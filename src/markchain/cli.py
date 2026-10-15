from __future__ import annotations

__all__ = ["main"]

import argparse
import json
import logging
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from elastic_transport import TransportError
from elasticsearch import ApiError
from pydantic import ValidationError

from markchain.documents import Documents
from markchain.errors import MarkchainError
from markchain.model import ClaudeModel, Intent, Planner
from markchain.requests import Proposal
from markchain.runtime import Runtime
from markchain.search import SearchIndex
from markchain.service import Service
from markchain.sync import Synchronizer
from markchain.trace import Trace
from markchain.watcher import VaultWatcher

logger = logging.getLogger(__name__)


# cost: time O(n), heap O(n), stack O(1), io O(f)
# vars: n = request and result bytes, f = target count
# basis: estimate
def main() -> int:
    parser = argparse.ArgumentParser(prog="markchain")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--search-url", default="http://127.0.0.1:19200")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--trace", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key")
    submit = commands.add_parser("submit")
    submit.add_argument("key")
    submit.add_argument("proposal", type=Path)
    commands.add_parser("run", help="execute one queued job, then exit")
    commands.add_parser("sync")
    commands.add_parser("watch")
    commands.add_parser("audit")
    commands.add_parser("serve", help="run MCP, file watch and queue workers together")
    commands.add_parser("cleanup")
    automate = commands.add_parser(
        "automate", help="queue an intent; use run or serve to execute"
    )
    automate.add_argument("key")
    automate.add_argument(
        "intent", type=Path, help="JSON with kind, instruction and query"
    )
    search = commands.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=20)
    search.add_argument(
        "--folder",
        action="append",
        default=[],
        dest="folders",
        help="vault-relative folder; repeat for OR scope; omit to search all",
    )
    search.add_argument("--fallback-offset", type=int, default=0)
    search.add_argument(
        "--fallback-reason", choices=("insufficient_evidence", "user_reported_missing")
    )
    for name in ("status", "cancel"):
        commands.add_parser(name).add_argument("key")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        with closing(
            SearchIndex(arguments.search_url, str(arguments.vault.resolve()))
        ) as index:
            synchronizer = Synchronizer(Documents(arguments.vault), index)
            planner = Planner(synchronizer, ClaudeModel(arguments.model))
            with (
                closing(
                    Trace(arguments.state / "trace.jsonl" if arguments.trace else None)
                ) as trace,
                Runtime(
                    arguments.vault, arguments.state, planner, trace=trace
                ) as runtime,
            ):
                result = _execute(runtime, arguments, synchronizer)
        if arguments.command != "serve":
            print(json.dumps(result, ensure_ascii=False))
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, item['loc'])) or 'input'}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False)
        )
        logger.error("invalid input: %s", details)
        return 1
    except (MarkchainError, OSError, sqlite3.Error, ApiError, TransportError) as error:
        logger.error("operation failed: %s", error)
        return 1
    return 0


# cost: time O(n), heap O(n), stack O(1), io O(f)
# vars: n = request and result bytes, f = target count
# basis: estimate
def _execute(
    runtime: Runtime, arguments: argparse.Namespace, synchronizer: Synchronizer
) -> object:
    if arguments.command in {"sync", "watch", "audit", "search"}:
        return _search(synchronizer, arguments)
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
        case "serve":
            service = Service(runtime, synchronizer)
            service.start()
            try:
                service.mcp.run()
            finally:
                service.close()
            return None
        case "status":
            return runtime.describe(arguments.key)
        case "cancel":
            return {"cancelled": runtime.cancel(arguments.key)}
        case _:
            raise AssertionError("parser must select a supported command")


# cost: time O(n), heap O(b), stack O(d), io O(f)
# vars: n = document bytes processed, b = result or batch bytes
# vars: d = directory depth, f = files checked
# basis: estimate
def _search(synchronizer: Synchronizer, arguments: argparse.Namespace) -> object:
    match arguments.command:
        case "sync":
            return synchronizer.reconcile()
        case "audit":
            return synchronizer.audit()
        case "search":
            return synchronizer.search(
                arguments.query,
                arguments.limit,
                fallback_offset=arguments.fallback_offset,
                fallback_reason=arguments.fallback_reason,
                folders=tuple(arguments.folders),
            )
        case "watch":
            stop = threading.Event()
            try:
                VaultWatcher(synchronizer).run(stop)
            except KeyboardInterrupt:
                stop.set()
            return {"status": "stopped"}
    raise AssertionError("search command must be supported")


if __name__ == "__main__":
    raise SystemExit(main())
