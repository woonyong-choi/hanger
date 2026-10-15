"""Durable local document workflows."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("markchain")
except PackageNotFoundError:  # pragma: no cover - source tree without install
    __version__ = "0+unknown"
