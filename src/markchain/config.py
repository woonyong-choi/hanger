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
    error_attempts: int = Field(default=3, ge=1, le=10)
    max_change_ratio: float = Field(default=0.3, gt=0, le=1)


class _Service(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    idle_minutes: float = Field(default=30, ge=0)


class _Elasticsearch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    managed: bool = True
    port: int = Field(default=19200, ge=1024, le=65535)
    heap_mb: int = Field(default=512, ge=256, le=8192)


class Settings(BaseModel):
    """Optional operator settings; every value has a working default."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    vault: _Vault = _Vault()
    search: _Search = _Search()
    jobs: _Jobs = _Jobs()
    service: _Service = _Service()
    elasticsearch: _Elasticsearch = _Elasticsearch()

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
                raise RequestError(f"검색 제외 폴더입니다: {folder}", code="excluded")


def load_settings(path: Path | None) -> Settings:
    """Load settings from TOML; a missing default file means defaults."""
    if path is None or not path.exists():
        return Settings()
    try:
        with path.open("rb") as source:
            return Settings.model_validate(tomllib.load(source))
    except (tomllib.TOMLDecodeError, ValidationError) as error:
        raise RequestError(f"설정 파일 오류: {path}: {error}", code="config") from error


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


def dump_settings(settings: Settings) -> str:
    """Render settings as TOML, omitting unset optional values."""
    sections = []
    for name, section in settings.model_dump(mode="json").items():
        lines = [f"[{name}]"]
        lines += [
            f"{key} = {_value(value)}"
            for key, value in section.items()
            if value is not None
        ]
        sections.append("\n".join(lines))
    return _HEADER + "\n" + "\n\n".join(sections) + "\n"
