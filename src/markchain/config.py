from __future__ import annotations

__all__ = ["Settings", "dump_settings", "load_settings"]

import tomllib
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from markchain.errors import RequestError


class _Vault(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Vault root chosen in setup; --vault on the command line overrides it.
    root: str | None = None
    # Top-level folders Markchain searches and edits; empty means all.
    managed: tuple[str, ...] = ()
    # Folders (within managed ones) the model may write; empty means managed.
    writable: tuple[str, ...] = ()
    # Folder or file names skipped at any depth.
    exclude: tuple[str, ...] = ()


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
    # Subscription CLI that runs the model: claude (Claude Code) or codex.
    provider: Literal["claude", "codex"] = "claude"
    # Model name passed to the CLI; empty means the CLI's own default.
    model: str = "sonnet"
    # Reasoning effort passed to the CLI; empty means the CLI's own default.
    effort: Literal["", "low", "medium", "high"] = ""


class _Models(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    # Drafting a document change.
    draft: _Model = _Model()
    # Reviewing a drafted change.
    review: _Model = _Model()


class Settings(BaseModel):
    """Optional operator settings; every value has a working default."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    vault: _Vault = _Vault()
    search: _Search = _Search()
    jobs: _Jobs = _Jobs()
    service: _Service = _Service()
    elasticsearch: _Elasticsearch = _Elasticsearch()
    models: _Models = _Models()

    @property
    def context_bytes(self) -> int:
        return self.search.context_kib * 1024

    def excluded(self, path: str) -> bool:
        """Return whether a vault-relative path is outside search and writes."""
        parts = PurePosixPath(path).parts
        if self.vault.managed and parts and parts[0] not in self.vault.managed:
            return True
        return any(p.startswith(".") or p in self.vault.exclude for p in parts)

    def writable(self, path: str) -> bool:
        if self.excluded(path):
            return False
        roots = self.vault.writable
        return not roots or any(
            path == root or path.startswith(root.rstrip("/") + "/") for root in roots
        )

    def check_folders(self, folders: tuple[str, ...]) -> None:
        for folder in folders:
            if self.excluded(folder):
                raise RequestError(
                    f"관리 범위 밖이거나 제외된 폴더입니다: {folder}", code="excluded"
                )


# Settings removed in later versions; old files still load.
_RETIRED = (("jobs", "max_change_ratio"),)


def load_settings(path: Path | None) -> Settings:
    """Load settings from TOML; a missing default file means defaults."""
    if path is None or not path.exists():
        return Settings()
    try:
        with path.open("rb") as source:
            values = tomllib.load(source)
        for section, key in _RETIRED:
            values.get(section, {}).pop(key, None)
        return Settings.model_validate(values)
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        raise RequestError(
            f"설정 파일을 읽을 수 없습니다: {path}: {error}", code="config"
        ) from error


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


_HEADER = """# Markchain settings. Edit freely; changes apply when the service restarts
# (markchain stop, or the next request after it exits).
# Every value has a default; delete a line to return to it.
"""


def _sections(name: str, section: dict[str, object]) -> list[str]:
    lines = [f"[{name}]"]
    lines += [
        f"{key} = {_value(value)}"
        for key, value in section.items()
        if value is not None and not isinstance(value, dict)
    ]
    nested = [
        text
        for key, value in section.items()
        if isinstance(value, dict)
        for text in _sections(f"{name}.{key}", value)
    ]
    return ([] if len(lines) == 1 and nested else ["\n".join(lines)]) + nested


def dump_settings(settings: Settings) -> str:
    """Render settings as TOML, omitting unset optional values."""
    sections = [
        text
        for name, section in settings.model_dump(mode="json").items()
        for text in _sections(name, section)
    ]
    return _HEADER + "\n" + "\n\n".join(sections) + "\n"
