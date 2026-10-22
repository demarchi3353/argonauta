"""Console helpers. Every state has a symbol as well as a colour so that the
meaning survives `--plain`, NO_COLOR and screen readers.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

NON_TTY_WIDTH = 160


def _make_console(stderr: bool = False, plain: bool = False) -> Console:
    kwargs: dict = {"stderr": stderr, "highlight": False}
    if plain:
        kwargs.update(no_color=True, emoji=False, force_terminal=False)
    probe = Console(**kwargs)
    if probe.is_terminal and not plain:
        return probe
    # Not a terminal (pipes, tests, logs): do not hard-wrap at 80 columns.
    return Console(**kwargs, width=NON_TTY_WIDTH, soft_wrap=True)


_console = _make_console()
_err_console = _make_console(stderr=True)


def configure(plain: bool) -> None:
    global _console, _err_console
    _console = _make_console(plain=plain)
    _err_console = _make_console(stderr=True, plain=plain)


def console() -> Console:
    return _console


def success(message: str) -> None:
    _console.print(Text("✓ ", style="bold green") + Text(message))


def failure(message: str) -> None:
    _err_console.print(Text("✗ ", style="bold red") + Text(message))


def warning(message: str) -> None:
    _console.print(Text("! ", style="bold yellow") + Text(message))


def info(message: str) -> None:
    _console.print(Text("· ", style="bold cyan") + Text(message))


def empty_state(title: str, hint: str) -> None:
    _console.print(Panel(Text(hint), title=f"Nothing here yet: {title}", border_style="cyan", title_align="left"))


def panel(body: str, title: str, style: str = "cyan") -> None:
    _console.print(Panel(Text(body), title=title, border_style=style, title_align="left"))


def diff_view(diff_text: str) -> None:
    if not diff_text.strip():
        warning("(no diff)")
        return
    _console.print(Syntax(diff_text, "diff", theme="ansi_dark", word_wrap=False, line_numbers=False))


def table(title: str, columns: list[str]) -> Table:
    t = Table(title=title, title_justify="left", show_lines=False, header_style="bold", expand=False)
    for col in columns:
        t.add_column(col, overflow="fold")
    return t


def print_table(t: Table) -> None:
    _console.print(t)


@contextmanager
def loading(message: str) -> Iterator[None]:
    """Spinner for operations that take a moment. Falls back to a plain line when not a TTY."""
    if _console.is_terminal:
        with _console.status(f"{message}…", spinner="dots"):
            yield
    else:
        _console.print(f"… {message}")
        yield


def short(text: str, width: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= width else text[: width - 1] + "…"
