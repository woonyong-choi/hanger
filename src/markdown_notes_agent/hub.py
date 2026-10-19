from __future__ import annotations

__all__ = ["VaultHub", "OpenVault"]

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

from markdown_notes_agent.config import Settings
from markdown_notes_agent.documents import Documents
from markdown_notes_agent.errors import (
    MarkdownNotesAgentError,
    RequestError,
    SearchUnavailableError,
    guidance,
)
from markdown_notes_agent.keywords import KeywordBuilder, KeywordGraph
from markdown_notes_agent.model import CliModel, Intent, Planner
from markdown_notes_agent.requests import issue_key, load_secret
from markdown_notes_agent.runtime import Runtime
from markdown_notes_agent.search import INDEX_PREFIX, SearchIndex, index_name
from markdown_notes_agent.store import StateStore, Vault
from markdown_notes_agent.sync import Synchronizer
from markdown_notes_agent.trace import Trace
from markdown_notes_agent.vaults import VaultPolicy, check_ignore, vault_present
from markdown_notes_agent.watcher import VaultWatcher

logger = logging.getLogger(__name__)

# How long closing a removed vault waits for its running jobs to stop.
_DRAIN_SECONDS = 60
# How often a vault that could not be opened is tried again.
_RETRY_SECONDS = 10


@dataclass
class OpenVault:
    """Everything one registered vault runs on."""

    vault: Vault
    policy: VaultPolicy
    synchronizer: Synchronizer
    runtime: Runtime
    resources: ExitStack
    keywords: KeywordGraph | None = None
    builder: KeywordBuilder | None = None
    stop: threading.Event = field(default_factory=threading.Event)
    watcher: threading.Thread | None = None
    scan: VaultWatcher | None = None

    @property
    def id(self) -> int:
        return self.vault.id

    @property
    def root(self) -> Path:
        return self.vault.path


class VaultHub:
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
        self.owns_search = owns_search
        self.secret = load_secret(store.state)
        self._lock = threading.RLock()
        self._open: dict[int, OpenVault] = {}
        # Vaults that could not be opened, with the reason.
        self._broken: dict[int, str] = {}
        self._revision = -1
        self._retry_at = 0.0
        self._pruned = False
        self._watching = False
        # Set by the service: why queued jobs are not moving, if they are not.
        self.health: Callable[[], str | None] = lambda: None

    @property
    def folder(self) -> Path:
        """Per-vault job state lives under here, one folder per vault id."""
        return self.store.state / "vaults"

    # ---------- catalog ----------

    # cost: time O(c*f), heap O(c), stack O(d), io O(c*f)
    # vars: c = vaults, f = files walked when an ignore file changed
    # vars: d = folder depth
    # basis: estimate
    def refresh(self) -> None:
        """Bring the open vaults in line with the catalog and ignore files."""
        removed: list[OpenVault] = []
        with self._lock:
            # Revision first: a vault removed after this read bumps it again,
            # so the next refresh still closes it.
            revision = self.store.revision()
            vaults = {c.id: c for c in self.store.vaults()}
            for vault in vaults.values():
                self._check(vault)
            if revision != self._revision:
                self._revision = revision
                self._pruned = False
                for vault_id in list(self._open):
                    if vault_id not in vaults:
                        removed.append(self._open.pop(vault_id))
                for vault_id in list(self._broken):
                    if vault_id not in vaults:
                        self._broken.pop(vault_id)
                for vault in vaults.values():
                    current = self._open.get(vault.id)
                    if current is None:
                        self._try_open(vault)
                    else:
                        self._apply_rules(current)
                self._retry_at = time.monotonic() + _RETRY_SECONDS
            elif self._broken and time.monotonic() >= self._retry_at:
                # A missing folder may be back, or a lock released.
                for vault_id in list(self._broken):
                    if vault_id in vaults:
                        self._try_open(vaults[vault_id])
                self._retry_at = time.monotonic() + _RETRY_SECONDS
        # Closing waits for running jobs; other requests go on meanwhile.
        for opened in removed:
            self._close(opened, removed=True)
        if not self._pruned:
            self._pruned = self._prune(list(vaults.values()))

    def _check(self, vault: Vault) -> None:
        """Turn an edited ignore file into a decision; skip absent folders."""
        if not vault_present(vault.path):
            # Unmounted or moved: a question about it could not be answered.
            for decision in self.store.decisions():
                if decision.vault_id == vault.id:
                    self.store.close_decision(decision.id, "withdrawn")
            return
        try:
            check_ignore(self.store, vault)
        except OSError as error:
            logger.warning("cannot read the ignore file of %s: %s", vault.path, error)

    def _apply_rules(self, opened: OpenVault) -> None:
        applied = self.store.applied_ignore(opened.id)
        if applied != opened.policy.ignore_text:
            opened.policy.update(applied)
            if opened.scan is not None:
                opened.scan.request_rescan()

    def _try_open(self, vault: Vault) -> None:
        try:
            if not vault_present(vault.path):
                raise RequestError(f"The folder is missing or empty: {vault.path}")
            self._open[vault.id] = self._build(vault)
            self._broken.pop(vault.id, None)
        except (OSError, MarkdownNotesAgentError) as error:
            if self._broken.get(vault.id) != str(error):
                logger.warning("cannot open vault %s: %s", vault.path, error)
            self._broken[vault.id] = str(error)
            return
        if self._watching:
            self._watch(self._open[vault.id])

    def _build(self, vault: Vault) -> OpenVault:
        resources = ExitStack()
        try:
            policy = VaultPolicy(vault.path, self.store.applied_ignore(vault.id))
            index = SearchIndex(self.search_url, str(vault.path))
            resources.callback(index.close)
            synchronizer = Synchronizer(
                Documents(vault.path), index, self.settings, policy
            )
            model = CliModel(self.settings, self.model)
            planner = Planner(synchronizer, model)
            runtime = resources.enter_context(
                Runtime(
                    vault.path,
                    self.folder / str(vault.id),
                    planner,
                    trace=self.trace,
                    settings=self.settings,
                    secret=self.secret,
                )
            )
            graph = KeywordGraph(
                vault.path, self.folder / str(vault.id) / "keywords.sqlite", policy
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
        return OpenVault(
            vault, policy, synchronizer, runtime, resources, graph, builder
        )

    def _close(self, opened: OpenVault, *, removed: bool = False) -> None:
        opened.stop.set()
        opened.runtime.interrupt()
        if opened.watcher is not None:
            opened.watcher.join(timeout=30)
        if not opened.runtime.drain(_DRAIN_SECONDS):
            logger.warning("vault %s: jobs did not stop in time", opened.root)
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

    def _prune(self, registered: list[Vault]) -> bool:
        """Remove state and indexes of vaults that are no longer registered.

        Returns False when the search server could not be asked yet, so a
        later refresh tries again.
        """
        ids = {str(vault.id) for vault in registered}
        if self.folder.is_dir():
            for entry in self.folder.iterdir():
                if entry.is_dir() and entry.name not in ids:
                    shutil.rmtree(entry, ignore_errors=True)
        if not self.owns_search:
            return True
        keep = {index_name(str(vault.path)) for vault in registered}
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

    def opened(self) -> list[OpenVault]:
        with self._lock:
            return list(self._open.values())

    # cost: time O(c), heap O(c), stack O(1), io O(1)
    # vars: c = registered vaults
    # basis: estimate
    def get(self, reference: str | None) -> OpenVault:
        """The vault a request names, or the only one when it names none."""
        self.refresh()
        vaults = self.store.vaults()
        if not vaults:
            raise RequestError(
                guidance(
                    "No vault is set up yet. A vault is a folder of Markdown "
                    "notes that Markdown Notes Agent keeps. "
                    "Which folder should be the vault? "
                    "Send its absolute path.",
                    "Call register_vault with the path the user gives. Never "
                    "guess a folder.",
                ),
                code="vault_required",
            )
        listed = ", ".join(f"#{c.id} {c.path}" for c in vaults)
        if reference is None:
            if len(vaults) > 1:
                raise RequestError(
                    guidance(
                        f"There are {len(vaults)} vaults: {listed}. Which "
                        "vault should Markdown Notes Agent use?",
                        "If your working folder is inside one of them, pass it "
                        "as vault without asking. Otherwise ask, then pass the "
                        "vault's id.",
                    ),
                    code="vault_ambiguous",
                )
            vault = vaults[0]
        else:
            found = self.store.vault(reference)
            if found is None:
                raise RequestError(
                    guidance(
                        f"No vault matches {reference}. The vaults are: {listed}.",
                        "Ask which vault to use, or register the folder with "
                        "register_vault if the user wants it as a new vault.",
                    ),
                    code="vault_unknown",
                )
            vault = found
        with self._lock:
            opened = self._open.get(vault.id)
            if opened is None:
                reason = self._broken.get(vault.id, "it is still opening")
                tell = f"Vault #{vault.id} ({vault.path}) is not available: {reason}."
                if not vault_present(vault.path):
                    tell += (
                        " If the folder moved, remove the vault in a terminal "
                        f"with `markdown-notes-agent vault remove {vault.id}` "
                        "and add the "
                        "new location with `markdown-notes-agent vault add <folder>`."
                    )
                raise RequestError(guidance(tell), code="vault_unavailable")
            return opened

    def holding(self, key: str) -> OpenVault | None:
        """The vault whose job store holds a request key."""
        for opened in self.opened():
            if opened.runtime.status(key):
                return opened
        return None

    def broken(self) -> dict[int, str]:
        with self._lock:
            return dict(self._broken)

    # ---------- background work ----------

    def watch(self) -> None:
        """Start a file watcher for every open vault, now and later."""
        with self._lock:
            self._watching = True
            for opened in self._open.values():
                self._watch(opened)

    def _watch(self, opened: OpenVault) -> None:
        scan = VaultWatcher(opened.synchronizer)
        opened.scan = scan
        opened.watcher = threading.Thread(
            target=scan.run,
            args=(opened.stop,),
            name=f"markdown-notes-agent-watch-{opened.id}",
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

    def each(self, action: Callable[[OpenVault], object]) -> None:
        for opened in self.opened():
            try:
                action(opened)
            except Exception:
                logger.exception("vault %s: background step failed", opened.root)

    def close(self) -> None:
        with self._lock:
            for opened in self._open.values():
                self._close(opened)
            self._open.clear()


def _release(root: Path) -> None:
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
                    f"{first} and {second}. Merge them into one note as the vault "
                    "rules say. Keep every fact that only one of them has, keep the "
                    "note that other notes link to, and delete the other one."
                ),
                paths=(first, second),
                allow_delete=True,
            ),
        )
        logger.info("keywords: queued a merge of %s and %s", first, second)
    except MarkdownNotesAgentError as error:
        logger.warning(
            "keywords: could not queue a merge of %s and %s: %s", first, second, error
        )


def _find(index: SearchIndex, query: str, limit: int) -> list[str]:
    try:
        return [hit["path"] for hit in index.find(query, limit)]
    except (TransportError, ApiError) as error:
        raise SearchUnavailableError("The search server is not responding.") from error
