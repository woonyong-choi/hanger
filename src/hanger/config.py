"""Settings: every value has a working default and is stored in the state database.

Keys are dotted section names such as ``search.candidates`` or
``models.draft.provider``; ``hanger config`` lists and changes them.
"""

from __future__ import annotations

__all__ = ["Settings", "flatten", "unflatten"]

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Search(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    candidates: int = Field(default=20, ge=1, le=100)
    full_text: int = Field(default=5, ge=1, le=100)
    context_kib: int = Field(default=256, ge=16, le=4096)
    context_fill: float = Field(default=0.8, gt=0, le=1)
    extra_reads: int = Field(default=2, ge=0, le=10)
    extra_read_documents: int = Field(default=5, ge=1, le=50)


class _Jobs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    default_mode: Literal["normal", "bypass"] = "normal"
    source_changed_retries: int = Field(default=2, ge=0, le=10)
    review_attempts: int = Field(default=3, ge=1, le=10)
    # Retries 10 seconds apart after an unexpected error; also the number of
    # drafts when the model output breaks an edit rule.
    error_attempts: int = Field(default=3, ge=1, le=10)


class _Service(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    idle_minutes: float = Field(default=30, ge=0)


class _Elasticsearch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    managed: bool = True
    port: int = Field(default=19200, ge=1024, le=65535)
    heap_mb: int = Field(default=512, ge=256, le=8192)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # claude or codex; empty picks the first installed (claude, then codex).
    provider: Literal["", "claude", "codex"] = ""
    # Model name for the provider; empty uses haiku (claude) or gpt-6-luna (codex).
    name: str = ""
    # Reasoning effort; empty uses the provider's own default.
    effort: Literal["", "low", "medium", "high"] = ""


class _Models(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Empty fields here fall back to [model].
    draft: _Model = _Model()
    review: _Model = _Model()
    keywords: _Model = _Model()


class Settings(BaseModel):
    """Operator settings; every value has a working default."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    search: _Search = _Search()
    jobs: _Jobs = _Jobs()
    service: _Service = _Service()
    elasticsearch: _Elasticsearch = _Elasticsearch()
    model: _Model = _Model()
    models: _Models = _Models()

    @property
    def context_bytes(self) -> int:
        return self.search.context_kib * 1024

    def role(self, name: Literal["draft", "review", "keywords"]) -> _Model:
        """Model settings for one role, with empty fields taken from [model]."""
        own = getattr(self.models, name)
        return _Model(
            provider=own.provider or self.model.provider,
            name=own.name or self.model.name,
            effort=own.effort or self.model.effort,
        )


def flatten(values: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Turn nested settings into dotted keys."""
    flat: dict[str, Any] = {}
    for key, value in values.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, name + "."))
        else:
            flat[name] = value
    return flat


def unflatten(values: dict[str, Any]) -> dict[str, Any]:
    nested: dict[str, Any] = {}
    for key, value in values.items():
        node = nested
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return nested
