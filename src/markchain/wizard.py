"""Interactive first-run setup: vault root, managed folders, then install."""

from __future__ import annotations

__all__ = ["SetupAbortedError", "choose_folders", "edit_command", "run_setup"]

import os
import shlex
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

from markchain.config import Settings, dump_settings

_ATTEMPTS = 3
_WINDOW = 15


class SetupAbortedError(Exception):
    """The user cancelled setup or gave no usable answer."""


class _Style:
    def __init__(self, stream: TextIO) -> None:
        self.tty = stream.isatty() and "NO_COLOR" not in os.environ

    def __call__(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.tty else text


def _notes(folder: Path) -> int:
    count = 0
    for _, directories, files in os.walk(folder):
        directories[:] = [d for d in directories if not d.startswith(".")]
        count += sum(1 for name in files if name.lower().endswith(".md"))
    return count


def edit_command(config: Path) -> str:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if editor:
        return f"{editor} {shlex.quote(str(config))}"
    if sys.platform == "darwin":
        return f"open -t {shlex.quote(str(config))}"
    return f"nano {shlex.quote(str(config))}"


# cost: time O(a), heap O(1), stack O(1), io O(a)
# vars: a = answers read, at most three
# basis: estimate
def _ask_vault(
    ask: Callable[[str], str],
    out: TextIO,
    style: _Style,
    default: str | None,
) -> Path:
    for attempt in range(1, _ATTEMPTS + 1):
        hint = f" [{default}]" if default else ""
        answer = ask(f"  Vault root folder{hint}: ").strip() or (default or "")
        if not answer:
            problem = "a path is required"
        else:
            path = Path(answer).expanduser()
            if path.is_dir() and os.access(path, os.R_OK | os.W_OK | os.X_OK):
                return path.resolve()
            problem = "not readable and writable" if path.is_dir() else "no such folder"
        left = _ATTEMPTS - attempt
        if left:
            out.write(
                "  "
                + style(f"{problem}: {answer or '(empty)'}", "31")
                + style(f"  ({left} attempt{'s' if left > 1 else ''} left)", "2")
                + "\n"
            )
    raise SetupAbortedError(f"No valid vault folder after {_ATTEMPTS} attempts")


def _read_key(descriptor: int) -> str:
    character = os.read(descriptor, 1).decode(errors="ignore")
    if character == "\x1b":
        sequence = os.read(descriptor, 2).decode(errors="ignore")
        return {"[A": "up", "[B": "down"}.get(sequence, "")
    return {
        " ": "toggle",
        "a": "all",
        "\r": "enter",
        "\n": "enter",
        "q": "quit",
        "\x03": "quit",
        "k": "up",
        "j": "down",
    }.get(character, "")


# cost: time O(k*f), heap O(f), stack O(1)
# vars: k = key presses, f = folders shown
# basis: estimate
def _toggle_tty(
    names: Sequence[str],
    counts: Sequence[int],
    selected: list[bool],
    source: TextIO,
    out: TextIO,
    style: _Style,
) -> list[bool]:
    import termios
    import tty

    cursor, top = 0, 0
    descriptor = source.fileno()
    saved = termios.tcgetattr(descriptor)
    drawn = 0
    try:
        tty.setraw(descriptor)
        out.write("\033[?25l")
        while True:
            if drawn:
                out.write(f"\033[{drawn}A")
            top = min(max(top, cursor - _WINDOW + 1), cursor)
            rows = range(top, min(len(names), top + _WINDOW))
            lines = []
            for index in rows:
                mark = style("[x]", "32") if selected[index] else style("[ ]", "2")
                pointer = style(">", "36") if index == cursor else " "
                label = f"{names[index]:<28}"
                label = style(label, "1") if index == cursor else label
                notes = style(f"{counts[index]:>6,} notes", "2")
                lines.append(f"  {pointer} {mark} {label} {notes}")
            chosen = sum(selected)
            lines.append(
                "  "
                + style(
                    f"{chosen} of {len(names)} selected"
                    + (
                        f"  (showing {rows.start + 1}-{rows.stop})"
                        if len(names) > _WINDOW
                        else ""
                    ),
                    "2",
                )
            )
            for line in lines:
                out.write("\r\033[2K" + line + "\r\n")
            drawn = len(lines)
            out.flush()
            key = _read_key(descriptor)
            if key == "up":
                cursor = (cursor - 1) % len(names)
            elif key == "down":
                cursor = (cursor + 1) % len(names)
            elif key == "toggle":
                selected[cursor] = not selected[cursor]
            elif key == "all":
                state = not all(selected)
                selected[:] = [state] * len(names)
            elif key == "quit":
                raise SetupAbortedError("Setup cancelled")
            elif key == "enter" and any(selected):
                return selected
    finally:
        termios.tcsetattr(descriptor, termios.TCSADRAIN, saved)
        out.write("\033[?25h")
        out.flush()


def _toggle_lines(
    names: Sequence[str],
    counts: Sequence[int],
    selected: list[bool],
    ask: Callable[[str], str],
    out: TextIO,
) -> list[bool]:
    while True:
        for index, name in enumerate(names, start=1):
            mark = "[x]" if selected[index - 1] else "[ ]"
            out.write(
                f"  {index:>3}. {mark} {name:<28} {counts[index - 1]:>6,} notes\n"
            )
        answer = ask("  Toggle numbers (e.g. 1 3), 'a' for all, Enter to accept: ")
        answer = answer.strip().lower()
        if not answer:
            if any(selected):
                return selected
            out.write("  Select at least one folder.\n")
            continue
        if answer == "a":
            state = not all(selected)
            selected[:] = [state] * len(names)
            continue
        for token in answer.replace(",", " ").split():
            if token.isdigit() and 1 <= int(token) <= len(names):
                selected[int(token) - 1] = not selected[int(token) - 1]


def choose_folders(
    root: Path,
    current: Sequence[str],
    *,
    source: TextIO,
    out: TextIO,
    ask: Callable[[str], str],
    interactive: bool,
) -> list[str]:
    names = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.is_dir() and not entry.name.startswith(".") and not entry.is_symlink()
    )
    if not names:
        return []
    counts = [_notes(root / name) for name in names]
    selected = [not current or name in current for name in names]
    style = _Style(out)
    if interactive:
        out.write(
            "  "
            + style("↑/↓ move   space toggle   a all   enter confirm   q quit", "2")
            + "\n"
        )
        result = _toggle_tty(names, counts, selected, source, out, style)
    else:
        result = _toggle_lines(names, counts, selected, ask, out)
    return [name for name, chosen in zip(names, result, strict=True) if chosen]


# cost: time O(f), heap O(f), stack O(1), io O(f)
# vars: f = files under the vault for the note counts
# basis: estimate
def run_setup(
    config: Path,
    current: Settings,
    *,
    vault: Path | None = None,
    managed: Sequence[str] | None = None,
    source: TextIO | None = None,
    out: TextIO | None = None,
) -> Settings:
    """Ask for the vault and managed folders, then save the settings file."""
    source = source or sys.stdin
    out = out or sys.stdout
    style = _Style(out)
    interactive = source.isatty() and out.isatty()

    def ask(prompt: str) -> str:
        out.write(prompt)
        out.flush()
        line = source.readline()
        if not line:
            raise SetupAbortedError("Setup cancelled (no input)")
        return line.rstrip("\n")

    out.write("\n" + style("Markchain setup", "1") + "\n\n")
    out.write(style("  1/3  Vault", "1") + "\n")
    default = str(vault) if vault else current.vault.root
    if managed is not None and vault is not None:
        root = vault.expanduser().resolve()
        if not root.is_dir():
            raise SetupAbortedError(f"No such vault folder: {root}")
    else:
        root = _ask_vault(ask, out, style, default)
    out.write("  " + style("using", "2") + f" {root}\n\n")

    out.write(style("  2/3  Managed folders", "1") + "\n")
    out.write(
        "  "
        + style(
            "Markchain searches and edits only the folders you select.",
            "2",
        )
        + "\n  "
        + style("You can change this later in the settings file.", "2")
        + "\n"
    )
    if managed is not None:
        folders = list(managed)
        missing = [name for name in folders if not (root / name).is_dir()]
        if missing:
            raise SetupAbortedError("Unknown folders: " + ", ".join(missing))
    else:
        same_root = current.vault.root == str(root)
        folders = choose_folders(
            root,
            current.vault.managed if same_root else (),
            source=source,
            out=out,
            ask=ask,
            interactive=interactive,
        )
    shown = ", ".join(folders) if folders else "(entire vault)"
    out.write("  " + style("managing", "2") + f" {shown}\n\n")

    updated = current.model_copy(
        update={
            "vault": current.vault.model_copy(
                update={"root": str(root), "managed": tuple(folders)}
            )
        }
    )
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = config.with_name(config.name + ".tmp")
    temporary.write_text(dump_settings(updated))
    os.replace(temporary, config)
    out.write("  " + style("saved", "32") + f" {config}\n")
    out.write(
        "  "
        + style("edit later:", "2")
        + " markchain config --edit   "
        + style("or", "2")
        + f" {edit_command(config)}\n\n"
    )
    out.flush()
    return updated
