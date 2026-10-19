"""Run a child command and stop it when the launching process disappears.

Usage: python -m markdown_notes_agent.guard <parent-pid> <command> [args...]

Elasticsearch is a separate Java process. If the Markdown Notes Agent daemon is killed
without cleanup, this guard notices that it was re-parented and stops the
child so no search server is left running unattended.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from types import FrameType

_POLL_SECONDS = 1.0
_STOP_SECONDS = 30.0


def _stop(child: subprocess.Popen[bytes]) -> int:
    if child.poll() is None:
        child.terminate()
        try:
            return child.wait(_STOP_SECONDS)
        except subprocess.TimeoutExpired:
            child.kill()
    return child.wait()


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: guard <parent-pid> <command> [args...]", file=sys.stderr)
        return 2
    parent = int(argv[1])
    child = subprocess.Popen(argv[2:])
    stopping = False

    def forward(signum: int, _frame: FrameType | None) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)
    while True:
        code = child.poll()
        if code is not None:
            return code
        if stopping or os.getppid() != parent:
            return _stop(child)
        time.sleep(_POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
