from __future__ import annotations

__all__ = ["configure"]

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-5s %(threadName)s %(name)s %(message)s"


def configure(state: Path, *, console: int | None = logging.WARNING) -> None:
    """Log to state/logs/markdown-notes-agent.log (rotated) and optionally to stderr."""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    try:
        folder = state / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            folder / "markdown-notes-agent.log",
            maxBytes=1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(_FORMAT, "%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
    except OSError as error:
        print(f"markdown-notes-agent: cannot open log file: {error}", file=sys.stderr)
    if console is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(console)
        stream.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        root.addHandler(stream)
    for noisy in ("elastic_transport", "httpx", "mcp", "watchdog"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
