"""The set of open closets in one Hanger process.

Each registered closet gets its own search index, synchronizer, job runtime
and file watcher. The hub keeps that set in step with the closet catalog in
the state database: it opens closets added by the terminal or an AI client,
closes and cleans up removed ones, and hands newly approved ignore rules to
the closets they belong to.
"""

from __future__ import annotations

__all__ = ["ClosetHub", "OpenCloset"]

import logging
import shutil
import threading
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from elastic_transport import TransportError
from elasticsearch import ApiError, Elasticsearch

from hanger.closets import ClosetPolicy, check_ignore
from hanger.config import Settings
from hanger.documents import Documents
from hanger.errors import HangerError, RequestError
from hanger.model import CliModel, Planner
from hanger.requests import load_secret
from hanger.runtime import Runtime
from hanger.search import INDEX_PREFIX, SearchIndex
from hanger.store import Closet, StateStore
from hanger.sync import Synchronizer
from hanger.trace import Trace
from hanger.watcher import ClosetWatcher

logger = logging.getLogger(__name__)

# How long closing a removed closet waits for its running jobs to stop.
_DRAIN_SECONDS = 60


@dataclass
class OpenCloset:
    """Everything one registered closet runs on."""

    closet: Closet
    policy: ClosetPolicy
    synchronizer: Synchronizer
    runtime: Runtime
    resources: ExitStack
    stop: threading.Event = field(default_factory=threading.Event)
    watcher: threading.Thread | None = None
    scan: ClosetWatcher | None = None

    @property
    def id(self) -> int:
        return self.closet.id

    @property
    def root(self) -> Path:
        return self.closet.path


class ClosetHub:
    def __init__(
        self,
        store: StateStore,
        settings: Settings,
        search_url: str,
        *,
        model: str | None = None,
        trace: Trace | None = None,
        owns_search: bool = False,
    ) -> None:
        self.store = store
        self.settings = settings
        self.search_url = search_url
        self.model = model
        self.trace = trace or Trace()
        # Only a search server Hanger runs itself may have its indexes pruned.
        self.owns_search = owns_search
        self.secret = load_secret(store.state)
        self._lock = threading.RLock()
        self._open: dict[int, OpenCloset] = {}
        # Closets that could not be opened, with the reason.
        self._broken: dict[int, str] = {}
        self._revision = -1
        self._watching = False
        # Set by the service: why queued jobs are not moving, if they are not.
        self.health: Callable[[], str | None] = lambda: None

    @property
    def folder(self) -> Path:
        """Per-closet job state lives under here, one folder per closet id."""
        return self.store.state / "closets"

    # ---------- catalog ----------

    # cost: time O(c*f), heap O(c), stack O(d), io O(c*f)
    # vars: c = closets, f = files walked when an ignore file changed
    # vars: d = folder depth
    # basis: estimate
    def refresh(self) -> None:
        """Bring the open closets in line with the catalog and ignore files."""
        with self._lock:
            for closet in self.store.closets():
                if closet.path.is_dir():
                    check_ignore(self.store, closet)
            revision = self.store.revision()
            if revision == self._revision:
                return
            self._revision = revision
            closets = {c.id: c for c in self.store.closets()}
            for closet_id in list(self._open):
                if closet_id not in closets:
                    self._close(self._open.pop(closet_id), removed=True)
            for closet_id in list(self._broken):
                if closet_id not in closets:
                    self._broken.pop(closet_id)
            for closet in closets.values():
                current = self._open.get(closet.id)
                if current is None:
                    self._try_open(closet)
                    continue
                applied = self.store.applied_ignore(closet.id)
                if applied != current.policy.ignore_text:
                    current.policy.update(applied)
                    if current.scan is not None:
                        current.scan.request_rescan()
            self._prune(set(closets))

    def _try_open(self, closet: Closet) -> None:
        try:
            self._open[closet.id] = self._build(closet)
            self._broken.pop(closet.id, None)
        except (OSError, HangerError) as error:
            logger.warning("cannot open closet %s: %s", closet.path, error)
            self._broken[closet.id] = str(error)
            return
        if self._watching:
            self._watch(self._open[closet.id])

    def _build(self, closet: Closet) -> OpenCloset:
        resources = ExitStack()
        try:
            policy = ClosetPolicy(closet.path, self.store.applied_ignore(closet.id))
            index = SearchIndex(self.search_url, str(closet.path))
            resources.callback(index.close)
            synchronizer = Synchronizer(
                Documents(closet.path), index, self.settings, policy
            )
            planner = Planner(synchronizer, CliModel(self.settings, self.model))
            runtime = resources.enter_context(
                Runtime(
                    closet.path,
                    self.folder / str(closet.id),
                    planner,
                    trace=self.trace,
                    settings=self.settings,
                    secret=self.secret,
                )
            )
        except BaseException:
            resources.close()
            raise
        runtime.health = lambda: self.health()
        return OpenCloset(closet, policy, synchronizer, runtime, resources)

    def _close(self, opened: OpenCloset, *, removed: bool = False) -> None:
        opened.stop.set()
        opened.runtime.interrupt()
        if opened.watcher is not None:
            opened.watcher.join(timeout=30)
        if not opened.runtime.drain(_DRAIN_SECONDS):
            logger.warning("closet %s: jobs did not stop in time", opened.root)
        if removed:
            try:
                opened.synchronizer.index.delete()
            except (TransportError, ApiError) as error:
                logger.warning(
                    "could not delete the index of %s: %s", opened.root, error
                )
        opened.resources.close()
        if removed:
            _release(opened.root)
            shutil.rmtree(self.folder / str(opened.id), ignore_errors=True)

    def _prune(self, registered: set[int]) -> None:
        """Remove state and indexes of closets removed while nothing ran."""
        if self.folder.is_dir():
            for entry in self.folder.iterdir():
                if entry.is_dir() and not (
                    entry.name.isdigit() and int(entry.name) in registered
                ):
                    shutil.rmtree(entry, ignore_errors=True)
        if not self.owns_search:
            return
        keep = {opened.synchronizer.index.name for opened in self._open.values()}
        client = Elasticsearch(self.search_url, request_timeout=20, max_retries=0)
        try:
            for name in client.indices.get(index=INDEX_PREFIX + "*"):
                if name not in keep:
                    client.options(ignore_status=404).indices.delete(index=name)
        except (TransportError, ApiError):
            return  # the search server is not up yet; the next refresh retries
        finally:
            client.close()

    # ---------- lookups ----------

    def opened(self) -> list[OpenCloset]:
        with self._lock:
            return list(self._open.values())

    # cost: time O(c), heap O(c), stack O(1), io O(1)
    # vars: c = registered closets
    # basis: estimate
    def get(self, reference: str | None) -> OpenCloset:
        """The closet a request names, or the only one when it names none."""
        self.refresh()
        closets = self.store.closets()
        if not closets:
            raise RequestError(
                "No closet is registered yet. Ask the user which folder holds "
                "their notes, then call register_closet with its absolute path "
                "(terminal: hanger closet add <folder>).",
                code="closet_required",
            )
        if reference is None:
            if len(closets) > 1:
                listed = ", ".join(f"#{c.id} {c.path}" for c in closets)
                raise RequestError(
                    "More than one closet is registered; pass closet as an id or "
                    f"a path inside one: {listed}",
                    code="closet_ambiguous",
                )
            closet = closets[0]
        else:
            found = self.store.closet(reference)
            if found is None:
                raise RequestError(
                    f"No registered closet matches {reference}. Registered: "
                    + ", ".join(f"#{c.id} {c.path}" for c in closets),
                    code="closet_unknown",
                )
            closet = found
        with self._lock:
            opened = self._open.get(closet.id)
            if opened is None:
                reason = self._broken.get(closet.id, "it is not open yet")
                raise RequestError(
                    f"Closet {closet.path} is not available: {reason}. If the "
                    "folder moved, remove it (hanger closet remove "
                    f"{closet.id}) and add the new location.",
                    code="closet_unavailable",
                )
            return opened

    def holding(self, key: str) -> OpenCloset | None:
        """The closet whose job store holds a request key."""
        for opened in self.opened():
            if opened.runtime.status(key):
                return opened
        return None

    def broken(self) -> dict[int, str]:
        with self._lock:
            return dict(self._broken)

    # ---------- background work ----------

    def watch(self) -> None:
        """Start a file watcher for every open closet, now and later."""
        with self._lock:
            self._watching = True
            for opened in self._open.values():
                self._watch(opened)

    def _watch(self, opened: OpenCloset) -> None:
        scan = ClosetWatcher(opened.synchronizer)
        opened.scan = scan
        opened.watcher = threading.Thread(
            target=scan.run,
            args=(opened.stop,),
            name=f"hanger-watch-{opened.id}",
            daemon=True,
        )
        opened.watcher.start()

    def restart_watchers(self) -> None:
        """Restart watchers that stopped unexpectedly."""
        with self._lock:
            for opened in self._open.values():
                stopped = opened.watcher is not None and not opened.watcher.is_alive()
                if stopped and not opened.stop.is_set():
                    logger.error("watcher of %s stopped; restarting", opened.root)
                    self._watch(opened)

    def each(self, action: Callable[[OpenCloset], object]) -> None:
        for opened in self.opened():
            try:
                action(opened)
            except Exception:
                logger.exception("closet %s: background step failed", opened.root)

    def close(self) -> None:
        with self._lock:
            for opened in self._open.values():
                self._close(opened)
            self._open.clear()


def _release(root: Path) -> None:
    """Remove Hanger's unfinished files from a closet it no longer keeps."""
    if not root.is_dir():
        return
    try:
        Documents(root).release()
    except OSError as error:
        logger.warning("could not clean up %s: %s", root, error)
