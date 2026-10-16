"""Shared test helpers."""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from hanger.hub import OpenCloset
from hanger.runtime import Runtime
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
