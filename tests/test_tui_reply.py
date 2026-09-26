"""Assistant Markdown in the terminal: styled, never raw asterisks, wrapped to the console
width while streaming, and still escaped and redacted like any untrusted text."""

from __future__ import annotations

import asyncio
import io
import random
import re
import threading
from pathlib import Path
from typing import Any

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.text import Text

from aibench.planning.openai_provider import OpenAICompatibleConfig, OpenAICompatibleProvider
from aibench.planning.planner import ModelReply
from aibench.tui.app import ChatApp
from aibench.tui.reply import ReplyFormatter, user_band
from aibench.tui.themes import get_theme
from tests.chat_server_support import chat_server, text_stream
from tests.session_support import SessionHarness

REPLY = (
    "Here's where things stand:\n\n"
    "**Current state:** The draft plan has no objectives yet, so it can't run. The first "
    "step is to tell me what this benchmark should check.\n\n"
    "## Open question\n"
    "- **correctness** - is the output right?\n"
    "- **reliability** - does it work consistently, even when the same question is asked "
    "many times over a long period?\n"
    "2. Use `native.exact_match` for *exact* answers\n"
    "> quoted note\n"
    "---\n"
    "```\n"
    "aibench run\n"
    "```\n"
    "Brackets like [red]this[/red] stay literal."
)


def _render(text: str, width: int, *, chunk: int | None = None) -> list[Text]:
    """Feed `text` whole lines at a time, or in random pieces of up to `chunk` characters
    the way a stream arrives, and collect the printed lines."""
    formatter = ReplyFormatter(get_theme(None), unicode=True)
    printed: list[Text] = []
    buffer = ""
    rng = random.Random(7)
    position = 0
    while position < len(text):
        step = rng.randint(1, chunk) if chunk else len(text)
        buffer += text[position : position + step]
        position += step
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            printed += formatter.feed(line, width, complete=True)[0]
        if buffer:
            lines, buffer = formatter.feed(buffer, width, complete=False)
            printed += lines
    printed += formatter.feed(buffer, width, complete=True)[0]
    return printed


def _plain(lines: list[Text]) -> list[str]:
    return [line.plain for line in lines]


def test_markdown_is_styled_not_printed_raw() -> None:
    lines = _render(REPLY, 100)
    shown = "\n".join(_plain(lines))
    assert "**" not in shown and "`" not in shown and "## " not in shown
    assert "Current state: The draft plan" in shown
    assert "• correctness - is the output right?" in shown
    assert "2. Use native.exact_match for exact answers" in shown
    assert "│ quoted note" in shown
    assert "─" * 10 in shown
    assert "  aibench run" in shown and "```" not in shown
    assert "[red]this[/red]" in shown  # not interpreted as markup

    bold = next(line for line in lines if line.plain.startswith("Current state"))
    assert any("bold" in str(span.style) for span in bold.spans)
    heading = next(line for line in lines if line.plain == "Open question")
    assert any("bold" in str(span.style) for span in heading.spans)


def test_a_line_longer_than_four_widths_streams_before_it_ends() -> None:
    formatter = ReplyFormatter(get_theme(None), unicode=True)
    long_line = "word " * 50  # 250 characters, still streaming
    assert formatter.feed(long_line[:150], 60, complete=False) == ([], long_line[:150])
    lines, rest = formatter.feed(long_line, 60, complete=False)
    assert lines and all(len(line.plain) <= 60 for line in lines)
    assert rest and len(rest) < len(long_line)


def test_lines_fit_the_width_with_hanging_indents_and_whole_spans() -> None:
    for chunk in (None, 1, 5, 13):
        lines = _plain(_render(REPLY, 50, chunk=chunk))
        assert all(len(line) <= 50 for line in lines), (chunk, lines)
        start = next(i for i, line in enumerate(lines) if line.startswith("• reliability"))
        assert lines[start + 1].startswith("  ") and not lines[start + 1].startswith("  •")
        # Streaming in pieces prints exactly what whole lines would.
        assert lines == _plain(_render(REPLY, 50)), chunk


def test_bold_span_is_never_split_across_lines() -> None:
    text = "word " * 6 + "**a long bold phrase that crosses the edge** tail"
    lines = _render(text, 40)
    shown = _plain(lines)
    assert all(len(line) <= 40 for line in shown)
    joined = " ".join(shown)
    assert "a long bold phrase that crosses the edge" in joined
    assert "*" not in joined


def test_ascii_consoles_get_ascii_glyphs() -> None:
    formatter = ReplyFormatter(get_theme(None), unicode=False)
    assert formatter.feed("- item", 80, complete=True)[0][0].plain == "- item"
    assert formatter.feed("> note", 80, complete=True)[0][0].plain == "| note"


def test_secrets_in_replies_are_still_redacted() -> None:
    line = _plain(_render("key **sk-live-abcdefghijklmnopqrstuvwxyz0123**", 80))[0]
    assert "abcdefghijklmnopqrstuvwxyz0123" not in line


def test_streamed_chat_reply_is_labelled_rendered_and_wrapped(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
    output = io.StringIO()
    pieces = [REPLY[i : i + 7] for i in range(0, len(REPLY), 7)]

    async def scenario(provider: OpenAICompatibleProvider) -> None:
        chat = ChatApp(
            ctl, provider=provider, console=Console(file=output, width=75, highlight=False)
        )
        worker = asyncio.create_task(chat._turn_worker())
        await chat.handle_input("where do things stand?")
        await asyncio.sleep(0.05)
        deadline = asyncio.get_running_loop().time() + 5
        while chat._turn is not None or "(" not in output.getvalue().split("BenchCraft")[-1]:
            assert asyncio.get_running_loop().time() < deadline, output.getvalue()
            await asyncio.sleep(0.02)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    with chat_server([(200, text_stream(*pieces))]) as server:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(base_url=server.base_url, model="glm-test")
        )
        try:
            asyncio.run(scenario(provider))
        finally:
            provider.close()
            ctl.storage.db.close()

    shown = output.getvalue()
    assert "◆ BenchCraft glm-test" in shown
    assert "**" not in shown
    assert "• correctness - is the output right?" in shown
    assert all(len(line) <= 75 for line in shown.splitlines())


def test_user_band_is_tinted_full_width_with_a_marker() -> None:
    theme = get_theme(None)
    console = Console(file=io.StringIO(), width=60, force_terminal=True, color_system="truecolor")
    console.print(user_band("hi\nsecond [red]line[/red]", theme, unicode=True))
    shown = console.file.getvalue()  # type: ignore[attr-defined]
    plain = re.sub(r"\x1b\[[0-9;]*m", "", shown)
    assert "› hi" in plain and "  second [red]line[/red]" in plain
    red, green, blue = (int(theme.user_bg[i : i + 2], 16) for i in (1, 3, 5))
    assert f"48;2;{red};{green};{blue}" in shown  # the band's background
    assert all(len(line) == 60 for line in plain.splitlines())  # full width
    assert len(plain.splitlines()) == 4  # a padding row above and below


class _SlowProvider:
    name = "slow-test-provider"
    model = "slow-test-model"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.started.set()
        self.release.wait(10)
        return ModelReply(text="too late")


def test_working_line_shows_while_replying_and_esc_interrupts(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    provider = _SlowProvider()
    output = io.StringIO()
    chat = ChatApp(
        ctl,
        provider=provider,
        console=Console(file=output, width=80, highlight=False),
        output=DummyOutput(),
    )
    assert "".join(text for _, text in chat._prompt_message()) == "▌ ❯ "

    async def scenario() -> str:
        with create_pipe_input() as pipe:
            chat.input = pipe
            task = asyncio.create_task(chat.run())
            await asyncio.sleep(0.1)
            pipe.send_text("hello there\r")
            deadline = asyncio.get_running_loop().time() + 5
            while not provider.started.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.02)
            await asyncio.sleep(1.1)
            working = "".join(text for _, text in chat._prompt_message())
            pipe.send_text("\x1b")  # Esc alone, then the prompt's key timeout
            while chat.replying():
                assert asyncio.get_running_loop().time() < deadline + 5, output.getvalue()
                await asyncio.sleep(0.05)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, timeout=5)
            return working

    try:
        working = asyncio.run(scenario())
        assert "Working (1s · esc to interrupt)" in working and working.endswith("▌ ❯ ")
        shown = output.getvalue()
        assert "› hello there" in shown  # the sent message, redrawn as a band
        assert "Reply interrupted." in shown
        assert "too late" not in shown
    finally:
        provider.release.set()
        ctl.storage.db.close()
