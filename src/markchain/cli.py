from __future__ import annotations

__all__ = ["main"]

import argparse
import json
import logging
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from markchain.errors import MarkchainError
from markchain.requests import Proposal
from markchain.runtime import Runtime

logger = logging.getLogger(__name__)


# cost: time O(n), heap O(n), stack O(1), io O(f)
# vars: n = request and result bytes, f = target count
# basis: estimate
def main() -> int:
    parser = argparse.ArgumentParser(prog="markchain")
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("key")
    submit = commands.add_parser("submit")
    submit.add_argument("key")
    submit.add_argument("proposal", type=Path)
    commands.add_parser("run")
    for name in ("status", "cancel"):
        commands.add_parser(name).add_argument("key")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        with Runtime(arguments.vault, arguments.state) as runtime:
            result = _execute(runtime, arguments)
        print(json.dumps(result, ensure_ascii=False))
    except ValidationError:
        logger.error("invalid proposal format")
        return 1
    except (MarkchainError, OSError, sqlite3.Error) as error:
        logger.error("operation failed: %s", error)
        return 1
    return 0


# cost: time O(n), heap O(n), stack O(1), io O(f)
# vars: n = request and result bytes, f = target count
# basis: estimate
def _execute(runtime: Runtime, arguments: argparse.Namespace) -> object:
    match arguments.command:
        case "key":
            return {"key": runtime.issue_key()}
        case "submit":
            proposal = Proposal.model_validate_json(arguments.proposal.read_bytes())
            return {"status": runtime.submit(arguments.key, proposal)}
        case "run":
            return runtime.run_next()
        case "status":
            return runtime.status(arguments.key)
        case "cancel":
            return {"cancelled": runtime.cancel(arguments.key)}
        case _:
            raise AssertionError("parser must select a supported command")


if __name__ == "__main__":
    raise SystemExit(main())
