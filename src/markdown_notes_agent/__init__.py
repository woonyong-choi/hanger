"""Durable local document workflows."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("markdown-notes-agent")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0+unknown"
