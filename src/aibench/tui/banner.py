"""The welcome screen: a gradient BENCHCRAFT logo over a bordered panel with the session on
the left and the commands on the right, coloured by the active theme.

Block and box-drawing characters are used only when the console's encoding can print them;
otherwise the logo and separators fall back to ASCII, so a legacy Windows code page still
gets a readable banner. A terminal too narrow for the one-line logo gets BENCH stacked over
CRAFT; one too narrow for that gets no logo.
"""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from aibench import __version__
from aibench.tui import render
from aibench.tui.render import safe
from aibench.tui.themes import Theme

BLOCK_LOGO = (
    "██████╗ ███████╗███╗   ██╗ ██████╗██╗  ██╗ ██████╗██████╗  █████╗ ███████╗████████╗",
    "██╔══██╗██╔════╝████╗  ██║██╔════╝██║  ██║██╔════╝██╔══██╗██╔══██╗██╔════╝╚══██╔══╝",
    "██████╔╝█████╗  ██╔██╗ ██║██║     ███████║██║     ██████╔╝███████║█████╗     ██║   ",
    "██╔══██╗██╔══╝  ██║╚██╗██║██║     ██╔══██║██║     ██╔══██╗██╔══██║██╔══╝     ██║   ",
    "██████╔╝███████╗██║ ╚████║╚██████╗██║  ██║╚██████╗██║  ██║██║  ██║██║        ██║   ",
    "╚═════╝ ╚══════╝╚═╝  ╚═══╝ ╚═════╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝        ╚═╝   ",
)

_BENCH_WIDTH = 42  # B, E, N, C, H: the columns before CRAFT in BLOCK_LOGO

STACKED_LOGO = tuple(row[:_BENCH_WIDTH] for row in BLOCK_LOGO) + tuple(
    row[_BENCH_WIDTH:] for row in BLOCK_LOGO
)

ASCII_LOGO = (
    r" ____                  _      ____            __ _   ",
    r"| __ )  ___ _ __   ___| |__  / ___|_ __ __ _ / _| |_ ",
    r"|  _ \ / _ \ '_ \ / __| '_ \| |   | '__/ _` | |_| __|",
    r"| |_) |  __/ | | | (__| | | | |___| | | (_| |  _| |_ ",
    r"|____/ \___|_| |_|\___|_| |_|\____|_|  \__,_|_|  \__|",
)

# Every slash command, grouped for the banner (tests check this matches the registries).
COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("plan", ("/plan", "/run", "/world", "/app")),
    ("run", ("/status", "/pause", "/resume", "/stop", "/budget")),
    ("results", ("/failures", "/case", "/report", "/compare")),
    ("session", ("/sessions", "/new", "/integrations", "/themes", "/help", "/exit")),
)

_UNICODE_PROBE = BLOCK_LOGO[0] + BLOCK_LOGO[-1] + "·❯◆•│─"


def unicode_ok(console: Console) -> bool:
    """Whether the console can print the block logo and the terminal's other glyphs."""
    try:
        _UNICODE_PROBE.encode(console.encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def logo(console: Console, theme: Theme) -> Text | None:
    """The widest logo that fits, each word in the theme's top-to-bottom gradient; None when
    none fits."""
    layouts = (
        ((BLOCK_LOGO, len(BLOCK_LOGO)), (STACKED_LOGO, len(BLOCK_LOGO)))
        if unicode_ok(console)
        else ((ASCII_LOGO, len(ASCII_LOGO)),)
    )
    for rows, band in layouts:
        if console.width >= max(len(row.rstrip()) for row in rows) + 2:
            break
    else:
        return None
    text = Text(no_wrap=True, overflow="crop")
    for index, row in enumerate(rows):
        line = index % band  # the gradient restarts for each stacked word
        colour = theme.logo[line * len(theme.logo) // band]
        text.append(row.rstrip() + "\n", style=f"bold {colour}" if line < 2 else colour)
    text.rstrip()
    return text


def swatch(console: Console, theme: Theme) -> str:
    """A short run of the theme's logo colours, for the /themes list."""
    cell = "█" if unicode_ok(console) else "#"
    return "".join(f"[{colour}]{cell}[/]" for colour in theme.logo)


def welcome(
    console: Console,
    theme: Theme,
    *,
    project: str,
    session_id: str,
    revision: int,
    model: str | None,
) -> None:
    dot = "·" if unicode_ok(console) else "|"
    accent, dim, text = theme.accent, theme.dim, theme.text

    def heading(label: str) -> str:
        return f"[bold {accent}]{label}[/]"

    assistant = (
        f"[{text}]{safe(model)}[/]" if model else f"[{dim}]none[/] [{dim}]{dot} commands only[/]"
    )
    left = [
        heading("Project"),
        f"[{text}]{safe(project)}[/]",
        "",
        heading("Session"),
        f"[{text}]{safe(session_id)}[/] [{dim}]{dot} draft revision {revision}[/]",
        "",
        heading("Assistant model"),
        assistant,
    ]
    right = [heading("Commands")]
    for group, names in COMMAND_GROUPS:
        right.append(f"[{dim}]{group}:[/] [{text}]{' '.join(names)}[/]")
    right += ["", f"[{dim}]theme {theme.name} {dot} /themes to change[/]"]

    grid = Table.grid(padding=(0, 4))
    grid.add_column("session", overflow="fold", max_width=48)
    grid.add_column("commands", overflow="fold")
    grid.add_row("\n".join(left), "\n".join(right))
    panel = Panel(
        grid,
        title=f"[bold {theme.title}]BenchCraft v{safe(__version__)}[/]",
        title_align="left",
        border_style=theme.border,
        padding=(1, 2),
        expand=False,
    )
    art = logo(console, theme)
    render.out(console)
    if art is not None:
        render.out(console, art)
        render.out(console)
    render.out(console, panel)
    render.out(
        console,
        f"[{dim}]Type /help for commands {dot} Enter sends {dot} Esc Enter adds a line[/]",
    )
