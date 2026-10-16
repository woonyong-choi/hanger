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
import time
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from elastic_transport import TransportError
from elasticsearch import ApiError, Elasticsearch

from hanger.closets import ClosetPolicy, check_ignore, closet_present
from hanger.config import Settings
from hanger.documents import Documents
from hanger.errors import HangerError, RequestError, SearchUnavailableError, guidance
from hanger.keywords import KeywordBuilder, KeywordGraph
from hanger.model import CliModel, Intent, Planner
from hanger.requests import issue_key, load_secret
from hanger.runtime import Runtime
from hanger.search import INDEX_PREFIX, SearchIndex, index_name
from hanger.store import Closet, StateStore
from hanger.sync import Synchronizer
from hanger.trace import Trace
from hanger.watcher import ClosetWatcher

logger = logging.getLogger(__name__)

# How long closing a removed closet waits for its running jobs to stop.
_DRAIN_SECONDS = 60
# How often a closet that could not be opened is tried again.
_RETRY_SECONDS = 10


@dataclass
class OpenCloset:
    """Everything one registered closet runs on."""

    closet: Closet
    policy: ClosetPolicy
    synchronizer: Synchronizer
    runtime: Runtime
    resources: ExitStack
    keywords: KeywordGraph | None = None
    builder: KeywordBuilder | None = None
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
        self._retry_at = 0.0
        self._pruned = False
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
        removed: list[OpenCloset] = []
        with self._lock:
            # Revision first: a closet removed after this read bumps it again,
            # so the next refresh still closes it.
            revision = self.store.revision()
            closets = {c.id: c for c in self.store.closets()}
            for closet in closets.values():
                self._check(closet)
            if revision != self._revision:
                self._revision = revision
                self._pruned = False
                for closet_id in list(self._open):
                    if closet_id not in closets:
                        removed.append(self._open.pop(closet_id))
                for closet_id in list(self._broken):
                    if closet_id not in closets:
                        self._broken.pop(closet_id)
                for closet in closets.values():
                    current = self._open.get(closet.id)
                    if current is None:
                        self._try_open(closet)
                    else:
                        self._apply_rules(current)
                self._retry_at = time.monotonic() + _RETRY_SECONDS
            elif self._broken and time.monotonic() >= self._retry_at:
                # A missing folder may be back, or a lock released.
                for closet_id in list(self._broken):
                    if closet_id in closets:
                        self._try_open(closets[closet_id])
                self._retry_at = time.monotonic() + _RETRY_SECONDS
        # Closing waits for running jobs; other requests go on meanwhile.
        for opened in removed:
            self._close(opened, removed=True)
        if not self._pruned:
            self._pruned = self._prune(list(closets.values()))

    def _check(self, closet: Closet) -> None:
        """Turn an edited ignore file into a decision; skip absent folders."""
        if not closet_present(closet.path):
            # Unmounted or moved: a question about it could not be answered.
            for decision in self.store.decisions():
                if decision.closet_id == closet.id:
                    self.store.close_decision(decision.id, "withdrawn")
            return
        try:
            check_ignore(self.store, closet)
        except OSError as error:
            logger.warning("cannot read the ignore file of %s: %s", closet.path, error)

    def _apply_rules(self, opened: OpenCloset) -> None:
        applied = self.store.applied_ignore(opened.id)
        if applied != opened.policy.ignore_text:
            opened.policy.update(applied)
            if opened.scan is not None:
                opened.scan.request_rescan()

    def _try_open(self, closet: Closet) -> None:
        try:
            if not closet_present(closet.path):
                raise RequestError(f"The folder is missing or empty: {closet.path}")
            self._open[closet.id] = self._build(closet)
            self._broken.pop(closet.id, None)
        except (OSError, HangerError) as error:
            if self._broken.get(closet.id) != str(error):
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
            model = CliModel(self.settings, self.model)
            planner = Planner(synchronizer, model)
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
            graph = KeywordGraph(
                closet.path, self.folder / str(closet.id) / "keywords.sqlite", policy
            )
            resources.callback(graph.close)
        except BaseException:
            resources.close()
            raise
        runtime.health = lambda: self.health()
        synchronizer.keywords = graph
        graph.on_same = lambda a, b, name: _queue_merge(
            runtime, self.secret, a, b, name
        )
        builder = KeywordBuilder(
            graph,
            model,
            read=synchronizer.text,
            search=lambda query, limit: _find(index, query, limit),
            reindex=synchronizer.reindex,
        )
        return OpenCloset(
            closet, policy, synchronizer, runtime, resources, graph, builder
        )

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
                # The prune on a later refresh removes it.
                self._pruned = False
                logger.warning(
                    "could not delete the index of %s: %s", opened.root, error
                )
        opened.resources.close()
        if removed:
            _release(opened.root)
            shutil.rmtree(self.folder / str(opened.id), ignore_errors=True)

    def _prune(self, registered: list[Closet]) -> bool:
        """Remove state and indexes of closets that are no longer registered.

        Returns False when the search server could not be asked yet, so a
        later refresh tries again.
        """
        ids = {str(closet.id) for closet in registered}
        if self.folder.is_dir():
            for entry in self.folder.iterdir():
                if entry.is_dir() and entry.name not in ids:
                    shutil.rmtree(entry, ignore_errors=True)
        if not self.owns_search:
            return True
        keep = {index_name(str(closet.path)) for closet in registered}
        client = Elasticsearch(self.search_url, request_timeout=20, max_retries=0)
        try:
            for name in client.indices.get(index=INDEX_PREFIX + "*"):
                if name not in keep:
                    client.options(ignore_status=404).indices.delete(index=name)
        except (TransportError, ApiError):
            return False
        finally:
            client.close()
        return True

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
                guidance(
                    "No closet is set up yet. A closet is a folder of Markdown "
                    "notes that Hanger keeps. Which folder should be the closet? "
                    "Send its absolute path.",
                    "Call register_closet with the path the user gives. Never "
                    "guess a folder.",
                ),
                code="closet_required",
            )
        listed = ", ".join(f"#{c.id} {c.path}" for c in closets)
        if reference is None:
            if len(closets) > 1:
                raise RequestError(
                    guidance(
                        f"There are {len(closets)} closets: {listed}. Which "
                        "closet should Hanger use?",
                        "If your working folder is inside one of them, pass it "
                        "as closet without asking. Otherwise ask, then pass the "
                        "closet's id.",
                    ),
                    code="closet_ambiguous",
                )
            closet = closets[0]
        else:
            found = self.store.closet(reference)
            if found is None:
                raise RequestError(
                    guidance(
                        f"No closet matches {reference}. The closets are: {listed}.",
                        "Ask which closet to use, or register the folder with "
                        "register_closet if the user wants it as a new closet.",
                    ),
                    code="closet_unknown",
                )
            closet = found
        with self._lock:
            opened = self._open.get(closet.id)
            if opened is None:
                reason = self._broken.get(closet.id, "it is still opening")
                tell = (
                    f"Closet #{closet.id} ({closet.path}) is not available: {reason}."
                )
                if not closet_present(closet.path):
                    tell += (
                        " If the folder moved, remove the closet in a terminal "
                        f"with `hanger closet remove {closet.id}` and add the "
                        "new location with `hanger closet add <folder>`."
                    )
                raise RequestError(guidance(tell), code="closet_unavailable")
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


def _queue_merge(
    runtime: Runtime, secret: bytes, first: str, second: str, name: str
) -> None:
    """Two notes answer the same question in folders that allow merging."""
    try:
        runtime.submit_intent(
            issue_key(secret),
            Intent(
                kind="organize",
                query=name,
                instruction=(
                    f"These notes answer the same central question ({name}): "
                    f"{first} and {second}. Merge them into one note as the closet "
                    "rules say. Keep every fact that only one of them has, keep the "
                    "note that other notes link to, and delete the other one."
                ),
                paths=(first, second),
                allow_delete=True,
            ),
        )
        logger.info("keywords: queued a merge of %s and %s", first, second)
    except HangerError as error:
        logger.warning(
            "keywords: could not queue a merge of %s and %s: %s", first, second, error
        )


def _find(index: SearchIndex, query: str, limit: int) -> list[str]:
    try:
        return [hit["path"] for hit in index.find(query, limit)]
    except (TransportError, ApiError) as error:
        raise SearchUnavailableError("The search server is not responding.") from error
