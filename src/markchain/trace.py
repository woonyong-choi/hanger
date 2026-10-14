from __future__ import annotations

__all__ = ["Trace"]

import json
import logging
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from logging.handlers import RotatingFileHandler
from pathlib import Path

from markchain.requests import digest

_job: ContextVar[str] = ContextVar("markchain_trace_job", default="")
logger = logging.getLogger(__name__)


class Trace:
    # cost: time O(1), heap O(1), stack O(1), io O(1)
    # basis: estimate
    def __init__(self, path: Path | None = None) -> None:
        self.enabled = path is not None
        self._path = path
        self._handler: RotatingFileHandler | None = None
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._handler is not None:
            self._handler.close()

    def begin(self, key: str) -> Token[str]:
        return _job.set(digest(key.encode())[:16] if self.enabled else "")

    def end(self, token: Token[str]) -> None:
        _job.reset(token)

    # cost: time O(1), heap O(1), stack O(1), io O(1)
    # basis: estimate
    @contextmanager
    def measure(self, stage: str) -> Iterator[dict[str, int | float | str]]:
        started = time.perf_counter() if self.enabled else 0
        fields: dict[str, int | float | str] = {}
        try:
            yield fields
        finally:
            if self.enabled:
                error = sys.exc_info()[0]
                fields["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
                fields["outcome"] = "ok" if error is None else error.__name__
                self.record(stage, fields)

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = metric bytes; rotations are bounded to three 1 MiB files
    # basis: estimate
    def record(self, stage: str, fields: dict[str, int | float | str]) -> None:
        if self._path is None:
            return
        payload = {"time": time.time(), "job": _job.get(), "stage": stage, **fields}
        with self._lock:
            try:
                self._write(payload)
            except OSError:
                self.enabled = False
                self._path = None
                logger.warning("trace disabled after a storage error")

    # cost: time O(n), heap O(n), stack O(1), io O(1)
    # vars: n = metric bytes
    # basis: estimate
    def _write(self, payload: dict[str, int | float | str]) -> None:
        if self._path is not None:
            if self._handler is None:
                self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                self._handler = RotatingFileHandler(
                    self._path, maxBytes=1024 * 1024, backupCount=2, encoding="utf-8"
                )
                self._path.chmod(0o600)
            self._handler.handle(
                logging.LogRecord(
                    "markchain.trace",
                    logging.INFO,
                    "",
                    0,
                    json.dumps(payload, ensure_ascii=False),
                    (),
                    None,
                )
            )
