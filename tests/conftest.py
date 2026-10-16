"""Shared test helpers."""

from __future__ import annotations

import copy
import os
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest

from hanger.closets import ClosetPolicy
from hanger.config import Settings
from hanger.documents import Documents
from hanger.hub import OpenCloset
from hanger.ignore import DEFAULT_IGNORE
from hanger.model import CliModel, Planner
from hanger.rules import RULES_FILE
from hanger.runtime import Runtime
from hanger.search import SearchIndex
from hanger.service import Service
from hanger.store import Closet
from hanger.sync import Synchronizer


class _NoDecisions:
    def decisions(self) -> list[Any]:
        return []


class OneCloset:
    """Stands in for ClosetHub with one closet the test already opened."""

    def __init__(self, runtime: Runtime, synchronizer: Synchronizer) -> None:
        self.closet = OpenCloset(
            Closet(1, runtime.documents.root),
            synchronizer.policy,
            synchronizer,
            runtime,
            ExitStack(),
        )
        self.store = _NoDecisions()
        self.secret = runtime._secret
        self.health: Any = None

    def refresh(self) -> None:
        return None

    def get(self, reference: str | None) -> OpenCloset:
        return self.closet

    def holding(self, key: str) -> OpenCloset | None:
        return self.closet if self.closet.runtime.status(key) else None

    def opened(self) -> list[OpenCloset]:
        return [self.closet]

    def each(self, action: Any) -> None:
        action(self.closet)

    def watch(self) -> None:
        return None

    def restart_watchers(self) -> None:
        return None

    def close(self) -> None:
        return None


def service_for(
    runtime: Runtime, synchronizer: Synchronizer, engine: Any = None
) -> Service:
    """A service over one already-open closet, without the state database."""
    return Service(OneCloset(runtime, synchronizer), engine)  # type: ignore[arg-type]


def edit_decision(path: str, old: str, new: str) -> dict[str, Any]:
    return {
        "action": "supplement",
        "reason": "수정",
        "uncertainties": [],
        "sources": [path],
        "changes": [
            {
                "path": path,
                "operation": "replace",
                "replacements": [{"old": old, "new": new}],
            }
        ],
    }


class ScriptedModel(CliModel):
    def __init__(self) -> None:
        super().__init__()
        self.decisions: list[dict[str, Any]] = []
        self.checks: list[dict[str, Any]] = []
        self.prompts: list[str] = []

    def generate(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.prompts.append(prompt)
        decision = (
            self.decisions.pop(0) if len(self.decisions) > 1 else self.decisions[0]
        )
        return {"decision": copy.deepcopy(decision), "usage": None}

    def review(self, prompt: str, is_cancelled: Callable[[], bool]) -> dict[str, Any]:
        self.prompts.append(prompt)
        check = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
        return {"review": check, "usage": None}


@pytest.fixture
def closet(tmp_path: Path) -> Iterator[tuple[Path, Path, str]]:
    url = os.environ.get("HANGER_TEST_SEARCH_URL")
    if not url:
        pytest.skip("requires explicit Elasticsearch test endpoint")
    root = tmp_path / "closet"
    (root / "docs").mkdir(parents=True)
    try:
        yield root, tmp_path / "state", url
    finally:
        index = SearchIndex(url, str(root))
        index.client.options(ignore_status=404).indices.delete(index=index.name)
        index.close()


def open_runtime(
    root: Path,
    state: Path,
    url: str,
    model: ScriptedModel,
    settings: Settings | None = None,
    policy: ClosetPolicy | None = None,
) -> tuple[Runtime, Synchronizer]:
    settings = settings or Settings()
    sync = Synchronizer(Documents(root), SearchIndex(url, str(root)), settings, policy)
    sync.reconcile()
    return Runtime(root, state, Planner(sync, model), settings=settings), sync


def finish(runtime: Runtime) -> None:
    while runtime.run_next():
        pass


OFFLINE = "http://127.0.0.1:9"


def offline_service(
    root: Path, state: Path, policy: ClosetPolicy | None = None
) -> tuple[Service, Runtime]:
    policy = policy or ClosetPolicy(root)
    sync = Synchronizer(Documents(root), SearchIndex(OFFLINE, str(root)), None, policy)
    runtime = Runtime(root, state)
    runtime.policy = policy
    return service_for(runtime, sync), runtime


@pytest.fixture
def layout(tmp_path: Path) -> tuple[Path, Path, ClosetPolicy]:
    """Ignores other/ and any private/ folder; inbox/ is read-only."""
    root = tmp_path / "closet"
    for folder in ("notes", "notes/private", "other", "inbox"):
        (root / folder).mkdir(parents=True)
    (root / "notes/a.md").write_text("# a")
    (root / "notes/private/s.md").write_text("# secret")
    (root / "other/c.md").write_text("# c")
    (root / "inbox" / RULES_FILE).write_text("---\nwrite: false\n---\n")
    policy = ClosetPolicy(root, DEFAULT_IGNORE + "other/\nprivate/\n")
    return root, tmp_path / "state", policy
