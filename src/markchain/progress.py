"""Terminal progress output for long setup steps.

On a terminal each step is one live line with a bar, percentage, size, speed
and remaining time. When output is redirected (logs, CI) the same steps are
printed as plain lines at every 10 percent so nothing floods the file.
"""

from __future__ import annotations

__all__ = ["Progress", "Step"]

import shutil
import sys
import time
from typing import TextIO

_BAR = 28
_REFRESH_SECONDS = 0.1


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

    def _style(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.tty else text

    def title(self, text: str, detail: str = "") -> None:
        line = self._style(text, "1")
        if detail:
            line += "  " + self._style(detail, "2")
        self.stream.write(line + "\n\n")
        self.stream.flush()

    def step(self, index: int, total: int, label: str, unit: str = "bytes") -> Step:
        return Step(self, f"[{index}/{total}] {label}", unit)

    def note(self, text: str) -> None:
        self.stream.write("\n  " + text + "\n")
        self.stream.flush()


class Step:
    def __init__(self, progress: Progress, label: str, unit: str = "bytes") -> None:
        self.progress = progress
        self.label = label
        self.unit = unit
        self._total: int | None = None
        self.started = time.monotonic()
        self._drawn = 0.0
        self._decile = -1
        self._write(self._style_label() + "  " + progress._style("...", "2"), live=True)

    def _style_label(self) -> str:
        return self.progress._style(f"{self.label:<32}", "1")

    def _amount(self, value: float) -> str:
        return _size(value) if self.unit == "bytes" else f"{int(value):,}"

    def _write(self, text: str, *, live: bool) -> None:
        stream = self.progress.stream
        if self.progress.tty:
            stream.write("\r\033[2K  " + text + ("" if live else "\n"))
        elif not live:
            stream.write("  " + text + "\n")
        stream.flush()

    # cost: time O(1), heap O(1), stack O(1)
    # basis: estimate
    def update(self, done: int, total: int | None) -> None:
        now = time.monotonic()
        finished = total is not None and done >= total
        if self.progress.tty and now - self._drawn < _REFRESH_SECONDS and not finished:
            return
        self._drawn = now
        self._total = total
        elapsed = max(now - self.started, 1e-6)
        rate = done / elapsed
        rate_text = (
            f"{_size(rate)}/s"
            if self.unit == "bytes"
            else f"{int(rate):,} {self.unit}/s"
        )
        if not total:
            self._write(
                f"{self._style_label()}  {self._amount(done)}  {rate_text}", live=True
            )
            return
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
        remaining = (total - done) / rate if rate > 0 and elapsed > 1 else None
        fields = [
            f"{ratio * 100:5.1f}%",
            f"{self._amount(done)} / {self._amount(total)}"
            + ("" if self.unit == "bytes" else f" {self.unit}"),
            rate_text,
            f"ETA {_duration(remaining)}" if remaining is not None else "",
        ]
        width = shutil.get_terminal_size((100, 24)).columns
        fixed = 2 + 32 + 2
        # Drop the least important fields first, then shrink the bar to fit.
        while (
            fields and fixed + 2 + 10 + len("  ".join(f for f in fields if f)) > width
        ):
            fields.pop()
        tail = "  ".join(f for f in fields if f)
        bar_width = max(10, min(_BAR, width - fixed - 2 - len(tail) - 2))
        filled = int(ratio * bar_width)
        bar = "█" * filled + "░" * (bar_width - filled)
        self._write(
            f"{self._style_label()}  {self.progress._style(bar, '36')}  {tail}",
            live=True,
        )

    def done(self, note: str = "") -> None:
        if not note and self._total:
            note = self._amount(self._total)
        took = _duration(time.monotonic() - self.started)
        mark = self.progress._style("done", "32")
        extra = self.progress._style(f"{note}  {took}".strip(), "2")
        self._write(f"{self._style_label()}  {mark}  {extra}", live=False)

    def fail(self, reason: str) -> None:
        mark = self.progress._style("failed", "31")
        self._write(f"{self._style_label()}  {mark}  {reason}", live=False)
