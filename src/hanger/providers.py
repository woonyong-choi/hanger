"""Model providers: the subscription CLIs Hanger runs models through.

Hanger never holds API keys. It calls a CLI the user has already signed in
to: Claude Code (``claude``) or Codex (``codex``). With no provider set, the
first one found in this order is used.
"""

from __future__ import annotations

__all__ = [
    "DEFAULT_MODELS",
    "PROVIDERS",
    "ProviderMissingError",
    "detect_provider",
    "resolve_choice",
]

import shutil
from dataclasses import dataclass
from typing import Literal

from hanger.errors import HangerError

Provider = Literal["claude", "codex"]
PROVIDERS: tuple[Provider, ...] = ("claude", "codex")
DEFAULT_MODELS: dict[Provider, str] = {"claude": "haiku", "codex": "gpt-6-luna"}
_NAMES: dict[Provider, str] = {"claude": "Claude Code", "codex": "Codex"}


class ProviderMissingError(HangerError):
    """No signed-in model CLI is available."""

    code = "provider_missing"


@dataclass(frozen=True)
class Choice:
    provider: Provider
    model: str
    effort: str

    def describe(self) -> str:
        effort = f", effort {self.effort}" if self.effort else ""
        return f"{_NAMES[self.provider]} ({self.provider}), model {self.model}{effort}"


def detect_provider() -> Provider | None:
    """Return the first installed provider CLI, or None."""
    for provider in PROVIDERS:
        if shutil.which(provider):
            return provider
    return None


def resolve_choice(provider: str, model: str, effort: str) -> Choice:
    """Fill unset values: provider by detection, model by the provider default."""
    chosen = provider or detect_provider()
    if chosen is None:
        raise ProviderMissingError(
            "No model provider found. Install Claude Code (claude) or Codex "
            "(codex), sign in, and try again."
        )
    if chosen not in PROVIDERS:
        raise ProviderMissingError(f"Unknown model provider: {chosen}")
    if not shutil.which(chosen):
        raise ProviderMissingError(
            f"The configured provider {_NAMES[chosen]} ({chosen}) is not installed "
            "or not on PATH. Install it, or change the provider with: "
            "hanger config set model.provider <claude|codex>"
        )
    return Choice(chosen, model or DEFAULT_MODELS[chosen], effort)
