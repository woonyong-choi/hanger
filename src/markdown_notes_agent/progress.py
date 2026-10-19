"""Terminal output for setup: one consistent line per step or answer.

Every line starts with a five-column status mark, then the label, then
details: a spinner while working, a bar when the size is known, and a green
``done`` or red ``fail`` when finished. When output is redirected the same
steps are written as plain lines, with progress at every 10 percent.
"""

from __future__ import annotations

__all__ = ["Progress", "Step"]

import shutil
import sys
import threading
import time
from typing import TextIO

_LABEL = 34
_BAR = 28
_REFRESH_SECONDS = 0.1
_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _size(value: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} GB"


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m{seconds:02d}s" if minutes else f"{seconds}s"


class Progress:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stderr
        self.tty = self.stream.isatty()
        self.lock = threading.Lock()

    def style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.tty else text

    def mark(self, kind: str) -> str:
        """Five visible columns: 'done ', 'fail ', '?    ' or a spinner frame."""
        if kind == "done":
            return self.style("done", "32") + " "
        if kind == "fail":
            return self.style("fail", "31") + " "
        if kind == "ask":
            return self.style("?", "36") + "    "
        return self.style(kind, "36") + "    "

    def label(self, text: str) -> str:
        return self.style(f"{text:<{_LABEL}}", "1")

    def line(self, kind: str, label: str, detail: str = "") -> None:
        """Write a finished line, replacing any live line on the terminal."""
        text = f"  {self.mark(kind)}{self.label(label)} {detail}".rstrip()
        with self.lock:
            if self.tty:
                self.stream.write("\r\033[2K")
            self.stream.write(text + "\n")
            self.stream.flush()

    def title(self, text: str, detail: str = "") -> None:
        line = self.style(text, "1")
        if detail:
            line += "  " + self.style(detail, "2")
        self.stream.write("\n" + line + "\n\n")
        self.stream.flush()

    def step(self, index: int, total: int, label: str, unit: str = "bytes") -> Step:
        return Step(self, f"[{index}/{total}] {label}", unit)

    def ask(self, label: str) -> str:
        """Ask one question on its own line and return the trimmed answer."""
        self.stream.write(f"  {self.mark('ask')}{self.style(label, '1')} ")
        self.stream.flush()
        return sys.stdin.readline().strip()

    def note(self, text: str) -> None:
        self.stream.write("       " + self.style(text, "2") + "\n")
        self.stream.flush()


class Step:
    """A live line: spinner and elapsed time, or a bar once sizes are known."""

    def __init__(self, progress: Progress, label: str, unit: str = "bytes") -> None:
        self.progress = progress
        self.label = label
        self.unit = unit
        self._total: int | None = None
        self.started = time.monotonic()
        self._drawn = 0.0
        self._decile = -1
        self._finished = threading.Event()
        # Latest bar text; the spinner thread redraws it at a steady rate so
        # the spinner keeps turning between slow progress reports.
        self._detail: str | None = None
        self._spinner: threading.Thread | None = None
        if progress.tty:
            self._spinner = threading.Thread(target=self._spin, daemon=True)
            self._spinner.start()

    def _spin(self) -> None:
        frame = 0
        while not self._finished.wait(_REFRESH_SECONDS):
            detail = self._detail
            if detail is None:
                elapsed = _duration(time.monotonic() - self.started)
                detail = self.progress.style(elapsed, "2")
            self._live(_FRAMES[frame % len(_FRAMES)], detail)
            frame += 1

    def _live(self, kind: str, detail: str) -> None:
        progress = self.progress
        text = f"  {progress.mark(kind)}{progress.label(self.label)} {detail}"
        with progress.lock:
            if self._finished.is_set():
                return
            progress.stream.write("\r\033[2K" + text)
            progress.stream.flush()

    def _amount(self, value: float) -> str:
        return _size(value) if self.unit == "bytes" else f"{int(value):,}"

    # cost: time O(1), heap O(1), stack O(1)
    # basis: estimate
    def update(self, done: int, total: int | None) -> None:
        now = time.monotonic()
        finished = total is not None and done >= total
        if self.progress.tty and now - self._drawn < _REFRESH_SECONDS and not finished:
            return
        self._drawn = now
        self._total = total
        if not total:
            return  # the spinner keeps showing elapsed time
        ratio = min(done / total, 1.0)
        if not self.progress.tty:
            decile = int(ratio * 10)
            if decile != self._decile:
                self._decile = decile
                self.progress.stream.write(
                    f"  {self.label}: {ratio * 100:.0f}% of {self._amount(total)}\n"
                )
                self.progress.stream.flush()
            return
        elapsed = max(now - self.started, 1e-6)
        rate = done / elapsed
        rate_text = (
            f"{_size(rate)}/s"
            if self.unit == "bytes"
            else f"{int(rate):,} {self.unit}/s"
        )
        remaining = (total - done) / rate if rate > 0 and elapsed > 1 else None
        fields = [
            f"{ratio * 100:5.1f}%",
            f"{self._amount(done)} / {self._amount(total)}"
            + ("" if self.unit == "bytes" else f" {self.unit}"),
            rate_text,
            f"ETA {_duration(remaining)}" if remaining is not None else "",
        ]
        width = shutil.get_terminal_size((100, 24)).columns
        fixed = 2 + 5 + _LABEL + 1
        # Drop the least important fields first, then shrink the bar to fit.
        while fields and fixed + 12 + len("  ".join(f for f in fields if f)) > width:
            fields.pop()
        tail = "  ".join(f for f in fields if f)
        bar_width = max(10, min(_BAR, width - fixed - len(tail) - 4))
        filled = int(ratio * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        self._detail = f"{self.progress.style(bar, '36')}  {tail}"

    def _stop(self) -> None:
        with self.progress.lock:
            self._finished.set()
        if self._spinner is not None:
            self._spinner.join(timeout=1)

    def done(self, note: str = "") -> None:
        self._stop()
        if not note and self._total:
            note = self._amount(self._total)
        took = _duration(time.monotonic() - self.started)
        detail = (f"{note}  " if note else "") + self.progress.style(took, "2")
        self.progress.line("done", self.label, detail)

    def fail(self, reason: str) -> None:
        self._stop()
        self.progress.line("fail", self.label, reason)
