from __future__ import annotations

__all__ = ["VaultWatcher"]

import logging
import threading
import time
from collections.abc import Callable
from itertools import batched
from pathlib import Path

from elastic_transport import TransportError
from elasticsearch import ApiError
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from markchain.errors import MarkchainError
from markchain.sync import Synchronizer

logger = logging.getLogger(__name__)


class VaultWatcher(FileSystemEventHandler):
    def __init__(self, synchronizer: Synchronizer) -> None:
        self.synchronizer = synchronizer
        self._observer = Observer()
        self._lock = threading.Lock()
        self._paths: set[str] = set()
        self._rescan = True
        self._retry_at = 0.0

    def start(self) -> None:
        self._observer.schedule(
            self, str(self.synchronizer.documents.root), recursive=True
        )
        self._observer.start()

    def close(self) -> None:
        self._observer.stop()
        self._observer.join()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.event_type not in {"created", "modified", "moved", "deleted"}:
            return
        if event.is_directory and event.event_type == "modified":
            return
        with self._lock:
            if event.is_directory or len(self._paths) >= 4096:
                self._rescan = True
                return
            for value in (event.src_path, event.dest_path):
                path = Path(str(value))
                if path.is_relative_to(self.synchronizer.documents.root):
                    self._paths.add(
                        path.relative_to(self.synchronizer.documents.root).as_posix()
                    )

    # cost: time O(n), heap O(b), stack O(d), io O(f)
    # vars: n = changed bytes, b = batch bytes, d = directory depth, f = file count
    # basis: estimate
    def flush(self, is_cancelled: Callable[[], bool] = lambda: False) -> None:
        with self._lock:
            paths, rescan = self._paths, self._rescan
            self._paths, self._rescan = set(), False
        if time.monotonic() >= self._retry_at:
            paths.update(self.synchronizer.drain_retries())
            self._retry_at = time.monotonic() + 10
        try:
            if rescan:
                self.synchronizer.reconcile(is_cancelled)
            elif paths:
                for batch in batched(paths, 256):
                    if is_cancelled():
                        raise InterruptedError("event synchronization interrupted")
                    self.synchronizer.sync_paths(list(batch))
                self.synchronizer.index.refresh()
        except (OSError, MarkchainError, TransportError, ApiError):
            with self._lock:
                self._rescan = True
            raise

    # cost: time O(n), heap O(b), stack O(d), io O(f)
    # vars: n = bytes processed during service lifetime, b = batch bytes
    # vars: d = directory depth, f = file checks over service lifetime
    # basis: estimate
    def run(self, stop: threading.Event) -> None:
        self.start()
        next_scan, next_audit = time.monotonic(), time.monotonic()
        try:
            while not stop.wait(0.5):
                try:
                    if time.monotonic() >= next_scan:
                        with self._lock:
                            self._rescan = True
                        next_scan = time.monotonic() + 3600
                    self.flush(stop.is_set)
                    if time.monotonic() >= next_audit:
                        self.synchronizer.audit()
                        next_audit = time.monotonic() + 60
                except (OSError, MarkchainError, TransportError, ApiError) as error:
                    logger.warning("search sync will retry: %s", error)
                    stop.wait(10)
                except Exception:
                    # Keep the index updating; the next pass rescans everything.
                    logger.exception("search sync failed unexpectedly, will retry")
                    with self._lock:
                        self._rescan = True
                    stop.wait(10)
        finally:
            self.close()
