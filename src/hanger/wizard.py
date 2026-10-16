"""Interactive setup questions: closet root, managed folders, confirmation."""

from __future__ import annotations

__all__ = ["SetupAbortedError", "edit_command", "run_setup"]

import os
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from hanger.config import Settings, dump_settings
from hanger.progress import Progress

_ATTEMPTS = 3
_WINDOW = 15
_ALL = "All folders"


class SetupAbortedError(Exception):
    """The user cancelled setup or gave no usable answer."""


def _notes(folder: Path) -> int:
    count = 0
    for _, directories, files in os.walk(folder):
        directories[:] = [d for d in directories if not d.startswith(".")]
        count += sum(1 for name in files if name.lower().endswith(".md"))
    return count


def _plural(count: int, word: str) -> str:
    return f"{count:,} {word}" + ("" if count == 1 else "s")


def edit_command(config: Path) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return f"{editor} {shlex.quote(str(config))}"
    if sys.platform == "darwin":
        return f"open -t {shlex.quote(str(config))}"
    return f"nano {shlex.quote(str(config))}"


class _Questions:
    def __init__(self, ui: Progress, source: TextIO) -> None:
        self.ui = ui
        self.source = source
        self.interactive = source.isatty() and ui.tty

    def ask(self, label: str, default: str | None = None) -> str:
        hint = " " + self.ui.style(f"({default})", "2") if default else ""
        self.ui.stream.write(
            f"  {self.ui.mark('ask')}{self.ui.style(label, '1')}{hint}: "
        )
        self.ui.stream.flush()
        line = self.source.readline()
        if not line:
            raise SetupAbortedError("Setup cancelled (no input)")
        if self.ui.tty:
            # Replace the prompt with its result line.
            self.ui.stream.write("\033[1A\r\033[2K")
        return line.rstrip("\n").strip() or (default or "")

    # cost: time O(a), heap O(1), stack O(1), io O(a)
    # vars: a = answers read, at most three
    # basis: estimate
    def closet(self, default: str | None) -> Path:
        label = "Closet root folder"
        for attempt in range(1, _ATTEMPTS + 1):
            answer = self.ask(label, default)
            path = Path(answer).expanduser() if answer else None
            if (
                path is not None
                and path.is_dir()
                and os.access(path, os.R_OK | os.W_OK)
            ):
                resolved = path.resolve()
                self.ui.line("done", label, str(resolved))
                return resolved
            if not answer:
                problem = "a path is required"
            elif path is not None and path.is_dir():
                problem = "not readable and writable"
            else:
                problem = "no such folder"
            left = _ATTEMPTS - attempt
            remain = (
                f"  ({left} attempt{'s' if left != 1 else ''} left)" if left else ""
            )
            self.ui.line(
                "fail",
                label,
                f"{answer or '(empty)'}  {problem}{self.ui.style(remain, '2')}",
            )
        raise SetupAbortedError(f"No valid closet folder after {_ATTEMPTS} attempts")

    def confirm(self, label: str) -> bool:
        answer = self.ask(label + " (Y/n)").lower()
        return answer in {"", "y", "yes"}

    def folders(self, root: Path, current: Sequence[str]) -> list[str]:
        names = sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and not entry.is_symlink()
        )
        if not names:
            self.ui.line("done", "Managed folders", "entire closet (no subfolders)")
            return []
        counts = [_notes(root / name) for name in names]
        selected = [not current or name in current for name in names]
        if self.interactive:
            selected = self._select_keys(names, counts, selected)
        else:
            selected = self._select_lines(names, counts, selected)
        chosen = [n for n, keep in zip(names, selected, strict=True) if keep]
        everything = len(chosen) == len(names)
        shown = (
            "all, including folders added later" if everything else ", ".join(chosen)
        )
        self.ui.line(
            "done",
            "Managed folders",
            f"{shown}  " + self.ui.style(f"({len(chosen)} of {len(names)})", "2"),
        )
        # An empty list means every top-level folder, now and later.
        return [] if everything else chosen

    def _row(self, *, pointer: bool, checked: bool, name: str, count: int) -> str:
        ui = self.ui
        mark = ui.style("[x]", "32") if checked else ui.style("[ ]", "2")
        arrow = ui.style(">", "36") if pointer else " "
        label = f"{name:<28}"
        label = ui.style(label, "1") if pointer else label
        return f"       {arrow} {mark} {label} {ui.style(_plural(count, 'note'), '2')}"

    # cost: time O(k*f), heap O(f), stack O(1)
    # vars: k = key presses, f = folders shown
    # basis: estimate
    def _select_keys(
        self, names: list[str], counts: list[int], selected: list[bool]
    ) -> list[bool]:
        import termios
        import tty

        ui, out = self.ui, self.ui.stream
        rows = [_ALL, *names]
        totals = [sum(counts), *counts]
        cursor, top, drawn = 0, 0, 0
        descriptor = self.source.fileno()
        saved = termios.tcgetattr(descriptor)
        header = f"  {ui.mark('ask')}{ui.style('Managed folders', '1')}  " + ui.style(
            "↑/↓ move   ←/→ select   enter confirm   q quit", "2"
        )
        try:
            tty.setraw(descriptor)
            out.write("\033[?25l" + header + "\r\n")
            drawn = 1
            while True:
                if drawn > 1:
                    out.write(f"\033[{drawn - 1}A")
                top = min(max(top, cursor - _WINDOW + 1), cursor)
                visible = range(top, min(len(rows), top + _WINDOW))
                lines = [
                    self._row(
                        pointer=index == cursor,
                        checked=all(selected) if index == 0 else selected[index - 1],
                        name=rows[index],
                        count=totals[index],
                    )
                    for index in visible
                ]
                more = (
                    f"  showing {visible.start + 1}-{visible.stop} of {len(rows)}"
                    if len(rows) > _WINDOW
                    else ""
                )
                summary = f"{sum(selected)} of {len(names)} selected{more}"
                lines.append("       " + ui.style(summary, "2"))
                for line in lines:
                    out.write("\r\033[2K" + line + "\r\n")
                drawn = 1 + len(lines)
                out.flush()
                key = _read_key(descriptor)
                if key == "up":
                    cursor = (cursor - 1) % len(rows)
                elif key == "down":
                    cursor = (cursor + 1) % len(rows)
                elif key in {"select", "clear"}:
                    state = key == "select"
                    if cursor == 0:
                        selected = [state] * len(names)
                    else:
                        selected[cursor - 1] = state
                elif key == "quit":
                    raise SetupAbortedError("Setup cancelled")
                elif key == "enter" and any(selected):
                    return selected
        finally:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
            # Collapse the list; the caller prints a one-line result.
            out.write(f"\033[{drawn}A\r\033[J\033[?25h")
            out.flush()

    def _select_lines(
        self, names: list[str], counts: list[int], selected: list[bool]
    ) -> list[bool]:
        out = self.ui.stream
        while True:
            out.write(f"  {'0':>3}. [{'x' if all(selected) else ' '}] {_ALL}\n")
            for index, name in enumerate(names, start=1):
                mark = "x" if selected[index - 1] else " "
                note = _plural(counts[index - 1], "note")
                out.write(f"  {index:>3}. [{mark}] {name:<28} {note}\n")
            out.write("  Numbers to select or clear (0 for all), Enter to confirm: ")
            out.flush()
            line = self.source.readline()
            if not line:
                raise SetupAbortedError("Setup cancelled (no input)")
            tokens = line.replace(",", " ").split()
            if not tokens:
                if any(selected):
                    return selected
                out.write("  Select at least one folder.\n")
            for token in tokens:
                if token == "0":
                    state = not all(selected)
                    selected = [state] * len(names)
                elif token.isdigit() and 1 <= int(token) <= len(names):
                    selected[int(token) - 1] = not selected[int(token) - 1]


def _read_key(descriptor: int) -> str:
    character = os.read(descriptor, 1).decode(errors="ignore")
    if character == "\x1b":
        sequence = os.read(descriptor, 2).decode(errors="ignore")
        arrows = {"[A": "up", "[B": "down", "[C": "select", "[D": "clear"}
        return arrows.get(sequence, "")
    return {
        "\r": "enter",
        "\n": "enter",
        "q": "quit",
        "\x03": "quit",
        "k": "up",
        "j": "down",
    }.get(character, "")


# cost: time O(f), heap O(f), stack O(1), io O(f)
# vars: f = files under the closet for the note counts
# basis: estimate
def run_setup(
    config: Path,
    current: Settings,
    *,
    closet: Path | None = None,
    managed: Sequence[str] | None = None,
    source: TextIO | None = None,
    out: TextIO | None = None,
    confirm: Callable[[Settings], None] | None = None,
) -> Settings:
    """Ask for the closet and managed folders, confirm, then save settings.

    Nothing is written until the user confirms. ``confirm`` runs checks that
    must pass before saving.
    """
    ui = Progress(out or sys.stdout)
    questions = _Questions(ui, source or sys.stdin)
    ui.title("Hanger setup")
    if managed is not None and closet is not None:
        root = closet.expanduser().resolve()
        if not root.is_dir():
            raise SetupAbortedError(f"No such closet folder: {root}")
        missing = [name for name in managed if not (root / name).is_dir()]
        if missing:
            raise SetupAbortedError("Unknown folders: " + ", ".join(missing))
        ui.line("done", "Closet root folder", str(root))
        folders = list(managed)
        ui.line("done", "Managed folders", ", ".join(folders) or "all")
    else:
        default = str(closet) if closet else current.closet.root
        root = questions.closet(default)
        same_root = current.closet.root == str(root)
        folders = questions.folders(root, current.closet.managed if same_root else ())
        if not questions.confirm("Save settings and install the search engine?"):
            raise SetupAbortedError("Setup cancelled")
    updated = current.model_copy(
        update={
            "closet": current.closet.model_copy(
                update={"root": str(root), "managed": tuple(folders)}
            )
        }
    )
    if confirm is not None:
        confirm(updated)
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = config.with_name(config.name + ".tmp")
    temporary.write_text(dump_settings(updated))
    os.replace(temporary, config)
    ui.line("done", "Settings saved", str(config))
    ui.note(f"edit later: hanger config --edit   or   {edit_command(config)}")
    ui.stream.write("\n")
    ui.stream.flush()
    return updated
