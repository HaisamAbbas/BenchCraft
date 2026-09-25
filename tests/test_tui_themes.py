"""The Hermes-style welcome screen and `/themes`: banner layout and fallbacks, theme
persistence, and switching through the real prompt loop."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from aibench.tui import banner
from aibench.tui.app import TERMINAL_COMMANDS, ChatApp, SlashCompleter
from aibench.tui.commands import COMMANDS
from aibench.tui.themes import DEFAULT_THEME, THEMES, get_theme, load_theme, save_theme
from tests.session_support import SessionHarness


class _Cp1252(io.StringIO):
    encoding = "cp1252"


def _welcome(console: Console, **overrides: object) -> str:
    fields: dict[str, object] = {
        "project": "D:/proj",
        "session_id": "ses-1",
        "revision": 3,
        "model": "test-model",
    }
    fields.update(overrides)
    banner.welcome(console, get_theme(None), **fields)  # type: ignore[arg-type]
    assert isinstance(console.file, io.StringIO)
    return console.file.getvalue()


def test_default_theme_is_red() -> None:
    assert DEFAULT_THEME == "crimson"
    theme = get_theme(None)
    assert theme.name == "crimson"
    red, green, blue = (int(theme.accent[i : i + 2], 16) for i in (1, 3, 5))
    assert red > 2 * max(green, blue)


def test_banner_groups_every_slash_command_exactly_once() -> None:
    grouped = [name for _, names in banner.COMMAND_GROUPS for name in names]
    assert len(grouped) == len(set(grouped))
    assert set(grouped) == set(COMMANDS) | set(TERMINAL_COMMANDS)


def test_wide_banner_shows_block_logo_panel_and_hint() -> None:
    shown = _welcome(Console(file=io.StringIO(), width=120))
    assert banner.BLOCK_LOGO[0].rstrip() in shown
    assert "BenchCraft v" in shown
    assert "ses-1" in shown and "draft revision 3" in shown and "test-model" in shown
    assert "theme crimson" in shown
    assert "Type /help for commands" in shown


def test_narrow_banner_skips_logo_and_legacy_code_page_gets_ascii() -> None:
    narrow = _welcome(Console(file=io.StringIO(), width=60))
    assert banner.BLOCK_LOGO[0].rstrip() not in narrow
    assert "Type /help for commands" in narrow

    legacy = _welcome(Console(file=_Cp1252(), width=120), model=None)
    assert banner.ASCII_LOGO[1].rstrip() in legacy
    legacy.encode("cp1252")  # every character printable on the code page
    assert "none | commands only" in legacy


def test_banner_escapes_untrusted_values() -> None:
    shown = _welcome(Console(file=io.StringIO(), width=120), project="D:/[bold]x[/bold]")
    assert "D:/[bold]x[/bold]" in shown


def test_saved_theme_round_trips_and_bad_files_fall_back(tmp_path: Path) -> None:
    path = tmp_path / "ui.json"
    assert load_theme(None).name == DEFAULT_THEME
    assert load_theme(path).name == DEFAULT_THEME  # missing
    save_theme(path, THEMES["ocean"])
    assert load_theme(path).name == "ocean"
    for content in ("{not json", json.dumps(["ocean"]), json.dumps({"theme": "nope"})):
        path.write_text(content, encoding="utf-8")
        assert load_theme(path).name == DEFAULT_THEME


def test_every_theme_builds_a_prompt_style() -> None:
    for theme in THEMES.values():
        assert len(theme.logo) == 6
        assert theme.prompt_style().style_rules


def test_themes_command_and_names_complete_with_tab() -> None:
    completer = SlashCompleter()
    commands = [c.text for c in completer.get_completions(Document("/the"), CompleteEvent())]
    assert commands == ["/themes"]
    names = [c.text for c in completer.get_completions(Document("/themes o"), CompleteEvent())]
    assert names == ["ocean"]


def test_themes_lists_switches_and_persists_through_the_prompt_loop(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    theme_path = tmp_path / "workspace" / "ui.json"
    output = io.StringIO()
    chat = ChatApp(
        ctl,
        provider=None,
        console=Console(file=output, width=120, highlight=False),
        theme_path=theme_path,
        output=DummyOutput(),
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            chat.input = pipe
            task = asyncio.create_task(chat.run())
            for line in ("/themes", "/themes nope", "/themes EMBER", "/help", "/exit"):
                pipe.send_text(line + "\r")
            await asyncio.wait_for(task, timeout=5)

    try:
        turns_before = len(ctl.store.turns(ctl.session_id))
        asyncio.run(scenario())
        shown = output.getvalue()
        assert "theme crimson" in shown  # the banner, before switching
        for name in THEMES:
            assert name in shown
        assert "unknown theme nope" in shown
        assert "theme ember" in shown
        assert "/themes [NAME]" in shown  # listed by /help
        assert chat.theme.name == "ember"
        assert json.loads(theme_path.read_text(encoding="utf-8")) == {"theme": "ember"}
        # A look-only command is not a benchmark decision: only /help and /exit are recorded.
        assert len(ctl.store.turns(ctl.session_id)) == turns_before + 2

        reopened = ChatApp(
            ctl, provider=None, console=Console(file=io.StringIO()), theme_path=theme_path
        )
        assert reopened.theme.name == "ember"
    finally:
        ctl.storage.db.close()
