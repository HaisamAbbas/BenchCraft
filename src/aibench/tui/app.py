"""The interactive terminal (§13, §15, 09-T1..T4): one asyncio loop runs the input prompt,
assistant turns, run progress and the engine, each independently cancellable.

- Input stays editable at all times: the prompt is asynchronous (`prompt_toolkit`), and
  output is printed above it (`patch_stdout`). It is drawn as a tinted box across the bottom
  (`tui.composer`), so it is always clear where typing goes. Enter sends; Esc then Enter adds
  a line. History is kept per workspace; slash commands complete with Tab.
- The welcome screen and chrome follow a colour theme (`/themes`), saved per workspace.
- Slash commands run at once, even while the assistant is replying, and never wait for a
  model (09-G2, 09-G3). Messages go to a turn worker, one at a time, in order.
- Replies stream: text fragments are shown as they arrive, grouped into whole lines so
  they do not tear the prompt, with a compact card per tool call. Their Markdown is shown
  styled and wrapped to the terminal's width (`tui.reply`), under a label per reply.
- Progress comes from committed run events, polled and coalesced: a line on each state
  change and at most one per interval otherwise, never one message per case. The bottom
  toolbar shows project, session, revision and the run's live state.
- A sent message is redrawn as a tinted band; while the assistant replies, a spinning
  `Working (Ns)` line sits above the input.
- Esc or Ctrl+C interrupts the assistant's reply, or else stops a long command (an install
  excepted: it is never left half built), never a benchmark; with a run active it points to
  /pause and /stop. /stop is the explicit cancel. Leaving (/exit, Ctrl+D) stops new
  dispatch, lets in-flight work finish and keeps the run resumable; reopening a session
  never restarts work (09-T4).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.output import Output
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import DynamicStyle
from rich.console import Console

from aibench.conversation.agent import ConversationAgent, TurnEvent, TurnLimits, TurnOutcome
from aibench.planning.planner import PlannerProvider
from aibench.services.plugins import judge_from_provider
from aibench.services.runs import RunError
from aibench.sessions.controller import SessionController
from aibench.tui import banner, composer, render
from aibench.tui.commands import COMMANDS, CommandResult, Commands, JudgeSource, NewSession
from aibench.tui.render import safe
from aibench.tui.reply import ReplyFormatter, user_band
from aibench.tui.themes import THEMES, Theme, load_theme, save_theme

MAX_REPLAYED = 10  # notable missed events shown on reopening; the rest are counted

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_ASCII_SPINNER = "|/-\\"
_FRAME_SECONDS = 0.1  # how often the Working line redraws while the assistant replies

# Commands that can take minutes (scoring, installs, a model writing cases, big reports).
# They run in the background like an assistant turn, so the input box stays and a Working
# line counts seconds; the quick ones (/status, /pause, /stop ...) still answer at once.
LONG_COMMANDS = frozenset({"/rescore", "/report", "/compare"})
# Commands that are long only for one subcommand: `/plugins` alone lists, `/plugins install`
# installs; `/cases` alone shows the pool, `/cases generate` calls a model; `/traces` shows a
# run's traces, `/traces import` reads a file.
LONG_SUBCOMMANDS = {"/plugins": "install", "/cases": "generate", "/traces": "import"}
_PROGRESS_SECONDS = 10.0  # how often a long command says how far it is
_HEARTBEAT_SECONDS = 30.0  # how often a command with nothing to count says it is still waiting

# Commands of the interactive terminal itself: they change only how it looks, so they are
# handled here rather than recorded in the session like the benchmark controls.
TERMINAL_COMMANDS: dict[str, str] = {
    "/themes": "/themes [NAME] - list colour themes, or switch (saved for this project)",
}


def is_install(text: str) -> bool:
    """`/plugins install ...`: builds an environment, so it is never stopped half way."""
    words = text.lower().split()
    return words[:2] == ["/plugins", "install"]


def stopped_note(text: str) -> str:
    """What stopping a long command part way leaves behind, in the user's terms."""
    name = text.split()[0].lower() if text.split() else text
    if name == "/rescore":
        return (
            f"{text} stopped. The evaluations it finished are stored and kept; /rescore "
            "continues from them."
        )
    if name == "/cases":
        return (
            f"{text} stopped. Nothing was stored; the request already sent to the model may "
            "still finish, and its reply is discarded."
        )
    return f"{text} stopped."


def is_long_command(text: str) -> bool:
    """Whether a slash command can take minutes, so it runs in the background."""
    words = text.split()
    if not words:
        return False
    name = words[0].lower()
    if name in LONG_COMMANDS:
        return True
    wanted = LONG_SUBCOMMANDS.get(name)
    return wanted is not None and wanted in [w.lower() for w in words[1:2]]


def notable_event(event: dict[str, Any]) -> str | None:
    """A replay line for an event worth seeing after being away (10-T1): failures,
    unknown effects, control requests, session ends and losses, recovery and budget stops.
    Routine progress is left to the status line."""
    kind, payload, seq = event["event_type"], event["payload"], event["sequence"]
    if kind == "item_state" and payload.get("state") in ("failed", "blocked", "unknown_effect"):
        reason = f": {payload['reason']}" if payload.get("reason") else ""
        return f"#{seq} {payload['task_key']} {payload['state']}{reason}"[:200]
    if kind == "control_requested":
        return f"#{seq} {payload.get('action', 'control')} requested ({payload.get('source')})"
    if kind in ("run_session_ended", "run_session_aborted", "run_session_lost", "recovered"):
        state = payload.get("state") or payload.get("stop_reason") or ""
        warnings = payload.get("warnings") or []
        why = f": {warnings[0]}" if warnings else ""
        return f"#{seq} {kind.replace('_', ' ')} {state}{why}".rstrip()[:200]
    if kind == "budget_exhausted":
        return f"#{seq} budget exhausted: {payload.get('reason', '')}".rstrip()
    return None


class SlashCompleter(Completer):
    def get_completions(
        self, document: Document, complete_event: CompleteEvent
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        if text.startswith("/") and " " not in text:
            for name, meaning in {**COMMANDS, **TERMINAL_COMMANDS}.items():
                if name.startswith(text.lower()):
                    yield Completion(name, start_position=-len(text), display_meta=meaning)
        elif text.lower().startswith("/themes ") and text.count(" ") == 1:
            typed = text.split(" ", 1)[1].lower()
            for theme in THEMES.values():
                if theme.name.startswith(typed):
                    yield Completion(
                        theme.name, start_position=-len(typed), display_meta=theme.description
                    )


def key_bindings() -> KeyBindings:
    bindings = KeyBindings()

    @bindings.add("escape", "enter")
    def _newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    return bindings


def render_result(console: Console, result: CommandResult) -> None:
    data, kind = result.data, result.kind
    if kind == "error":
        render.out(console, f"[red]{safe(data['error'])}[/red]")
    elif kind == "help":
        for name, meaning in data["commands"].items():
            render.out(console, f"  {name:<10} {safe(meaning)}")
        render.out(console, safe(data["messages"]))
    elif kind in ("plan", "confirm"):
        render.draft(console, data)
        if kind == "confirm":
            render.out(console, f"[bold]{safe(data['note'])}[/bold]")
    elif kind == "action":
        if data.get("plan_preview"):
            render.draft(console, data["plan_preview"])
        verb = data["kind"].replace("_", " ")
        if data["state"] == "done":
            run = f" {data['run_id']}" if data.get("run_id") else ""
            render.out(console, f"{safe(verb)}: done{safe(run)}")
        else:
            render.out(
                console,
                f"[yellow]{safe(verb)}: {data['state']}[/yellow] {safe(str(data['reason']))}",
            )
            for finding in data.get("findings", [])[:10]:
                render.out(console, f"  {safe(finding['kind'])}: {safe(finding['message'])}")
    elif kind == "status":
        render.status(console, data)
    elif kind == "failures":
        render.failures(console, data)
    elif kind == "case":
        render.case(console, data)
    elif kind == "budget":
        render.budget(console, data)
    elif kind == "application":
        render.application(console, data)
    elif kind == "integrations":
        render.integrations(console, data)
    elif kind == "plugins":
        render.plugins(console, data)
    elif kind == "plugin_preview":
        render.plugin_preview(console, data)
    elif kind == "plugin_installed":
        render.plugin_installed(console, data)
    elif kind == "cases":
        render.cases(console, data)
    elif kind == "cases_decided":
        render.cases_decided(console, data)
    elif kind == "cases_saved":
        render.cases_saved(console, data)
    elif kind == "traces":
        render.traces(console, data)
    elif kind == "traces_imported":
        render.traces_imported(console, data)
    elif kind == "rescored":
        render.rescored(console, data)
    elif kind == "report":
        render.report(console, data)
    elif kind == "comparison":
        render.comparison(console, data)
    elif kind == "sessions":
        for row in data["sessions"]:
            mark = "*" if row["current"] else " "
            run = f" run {row['active_run_id']}" if row["active_run_id"] else ""
            render.out(
                console,
                f" {mark} {row['session_id']} revision {row['revision']}{run} {row['updated_at']}",
            )
    elif kind == "switched":
        render.out(console, f"switched to new session {data['session_id']}")
    elif kind == "exit":
        pass


class ChatApp:
    def __init__(
        self,
        controller: SessionController,
        *,
        provider: PlannerProvider | None,
        console: Console | None = None,
        new_session: NewSession | None = None,
        history_path: Path | None = None,
        theme_path: Path | None = None,
        limits: TurnLimits | None = None,
        input: Input | None = None,
        output: Output | None = None,
        progress_interval: float = 0.5,
        coalesce_seconds: float = 2.0,
        command_progress_seconds: float = _PROGRESS_SECONDS,
        command_heartbeat_seconds: float = _HEARTBEAT_SECONDS,
    ) -> None:
        self.provider = provider
        self.limits = limits
        self.console = console or Console(highlight=False)
        self.new_session = new_session
        self.history: History = (
            FileHistory(str(history_path)) if history_path else InMemoryHistory()
        )
        self.input = input
        self.output = output
        self.theme_path = theme_path
        self._session: PromptSession[str] | None = None
        theme = load_theme(theme_path)
        self.composer = composer.ComposerBand(theme, unicode=banner.unicode_ok(self.console))
        self._set_theme(theme)
        self.progress_interval = progress_interval
        self.coalesce_seconds = coalesce_seconds
        self.command_progress_seconds = command_progress_seconds
        self.command_heartbeat_seconds = command_heartbeat_seconds
        self._command: asyncio.Task[None] | None = None
        self._pending_commands: list[str] = []
        self._command_name = ""
        self._command_active = False
        self._command_stopped = False  # the user stopped it (Esc), not leaving the chat
        self._command_started = 0.0
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._turn: asyncio.Task[TurnOutcome] | None = None
        self._stream = ""
        self._streamed = False
        self._turn_started = 0.0
        self._new_reply()
        self._toolbar = ""
        self._exit = False
        self._use(controller)

    def _use(self, controller: SessionController) -> None:
        self.controller = controller
        self.agent = ConversationAgent(controller, self.provider, self.limits)
        self.commands = Commands(
            controller,
            self.new_session,
            judge=self._judge(),
            progress=lambda line: self.say(f"[dim]{safe(line)}[/dim]"),
            provider=self.provider,
        )
        self._seen_run: str | None = None
        self._last_sequence = 0
        self._last_state: str | None = None
        self._last_print = 0.0
        self._refresh_toolbar()

    # ------------------------------------------------------------------ display

    def say(self, text: str) -> None:
        render.out(self.console, text)

    def _judge(self) -> JudgeSource | None:
        """The assistant's own model, offered as the judge when a plugin is installed."""
        config = getattr(self.provider, "config", None)
        if config is None or not getattr(config, "base_url", None):
            return None
        api_key = str(config.api_key) if config.api_key else None
        return judge_from_provider(config.base_url, config.model, api_key)

    def _new_reply(self) -> None:
        self._reply = ReplyFormatter(self.theme, unicode=banner.unicode_ok(self.console))
        self._labelled = False

    def _set_theme(self, theme: Theme) -> None:
        """Put a theme on the running prompt: its styles, and the input box that is on screen."""
        self.theme = theme
        self._prompt_style = theme.prompt_style()
        # The prompt holds this one box, so a `/themes` switch repaints the box that is open
        # now rather than the one the next prompt would build.
        self.composer.set_theme(theme)
        if self._session is not None:
            self._session.app.invalidate()

    def replying(self) -> bool:
        return self._turn is not None and not self._turn.done()

    def commanding(self) -> bool:
        """A long slash command is running in the background."""
        return self._command_active

    def _prompt_message(self) -> StyleAndTextTuples:
        """The input box: its gutter, under a live `Working` line while the assistant replies."""
        return [*self._working_line(), *self.composer.gutter]

    def _working_line(self) -> StyleAndTextTuples:
        """The spinning `Working (Ns)` line, or nothing at all while the assistant is idle."""
        frames = _SPINNER if self.composer.unicode else _ASCII_SPINNER
        frame = frames[int(time.monotonic() * 10) % len(frames)]
        dot = "·" if self.composer.unicode else "|"
        if not self.replying():
            if not self.commanding():
                return []
            took = int(time.monotonic() - self._command_started)
            if not self.command_stoppable():
                return [
                    ("class:working.spinner", f"{frame} "),
                    ("class:working", f"Working: {self._command_name} ({took}s)\n"),
                ]
            return [
                ("class:working.spinner", f"{frame} "),
                ("class:working", f"Working: {self._command_name} ({took}s {dot} "),
                ("class:working.key", "esc"),
                ("class:working", " to stop)\n"),
            ]
        elapsed = int(time.monotonic() - self._turn_started)
        return [
            ("class:working.spinner", f"{frame} "),
            ("class:working", f"Working ({elapsed}s {dot} "),
            ("class:working.key", "esc"),
            ("class:working", " to interrupt)\n"),
        ]

    def _echo(self, text: str) -> None:
        """Redraw a sent message as a tinted band (the prompt erases its own line)."""
        band = user_band(text, self.theme, unicode=banner.unicode_ok(self.console))
        render.out(self.console, "")
        render.out(self.console, band)

    def _banner(self) -> None:
        session = self.controller.session
        banner.welcome(
            self.console,
            self.theme,
            project=str(session.project_root),
            session_id=session.session_id,
            revision=session.revision,
            model=self.provider.model if self.provider else None,
        )
        # Reopened: the authoritative picture from storage (10-T1). Missed run events are
        # summarized once and acknowledged; nothing is dispatched or repeated.
        report = self.controller.reconcile()
        for run in report["runs"]:
            if run["missed_events"]:
                self.say(
                    f"run {run['run_id']}: {run['missed_events']} event(s) since you last "
                    f"looked; now {run['condition'].replace('_', ' ')}"
                )
                missed = self.controller.missed_events(run["run_id"])
                notable = [line for line in map(notable_event, missed) if line]
                for line in notable[-MAX_REPLAYED:]:
                    self.say(f"  {safe(line)}")
                if len(notable) > MAX_REPLAYED:
                    self.say(
                        f"  ({len(notable) - MAX_REPLAYED} earlier notable event(s) not shown)"
                    )
                self.controller.acknowledge_events(run["run_id"], run["last_sequence"])
        for note in report["notes"]:
            self.say(f"[yellow]{safe(note)}[/yellow]")
        if report["open_questions"]:
            self.say(f"{len(report['open_questions'])} open question(s): /plan shows them")

    def _refresh_toolbar(self, status: dict[str, Any] | None = None) -> None:
        session = self.controller.session
        parts = [f" session {session.session_id} rev {session.revision}"]
        if status is not None:
            parts.append(render.status_line(status))
        elif session.active_run_id:
            parts.append(f"run {session.active_run_id}")
        if self._turn and not self._turn.done():
            parts.append("assistant replying")
        elif self.commanding():
            parts.append(f"working: {self._command_name.split()[0]}")
        else:
            parts.append("idle")
        self._toolbar = " | ".join(parts)

    def toolbar(self) -> str:
        return self._toolbar

    def _on_event(self, event: TurnEvent) -> None:
        if event.kind == "text":
            self._streamed = True
            self._stream += event.text
            self._flush_stream(final=False)
        elif event.kind == "tool":
            self._flush_stream(final=True)
            self.say(f"[dim]{render.tool_call(event.name, event.data)}[/dim]")
        elif event.kind == "tool_result":
            line = render.tool_result(event.name, event.data)
            if line:
                self.say(f"[dim]{line}[/dim]")

    def _flush_stream(self, *, final: bool) -> None:
        while "\n" in self._stream:
            line, self._stream = self._stream.split("\n", 1)
            self._reply_line(line, complete=True)
        if self._stream:
            self._stream = self._reply_line(self._stream, complete=final)
        elif final:
            self._reply.finish()

    def _reply_line(self, text: str, *, complete: bool) -> str:
        """Print what is ready of one reply line; return the part still streaming."""
        lines, rest = self._reply.feed(text, self.console.width, complete=complete)
        if lines and not self._labelled:
            self._labelled = True
            mark = "\u25c6" if banner.unicode_ok(self.console) else "*"
            model = f" [{self.theme.dim}]{safe(self.provider.model)}[/]" if self.provider else ""
            self.say(f"\n[bold {self.theme.accent}]{mark} BenchCraft[/]{model}")
        for line in lines:
            render.out(self.console, line)
        return rest

    def _show_outcome(self, outcome: TurnOutcome, streamed: bool) -> None:
        self._flush_stream(final=True)
        if not streamed and outcome.text:
            for line in outcome.text.split("\n"):
                self._reply_line(line, complete=True)
        if outcome.presented_draft is not None:
            render.draft(self.console, outcome.presented_draft)
        self.say(f"[dim]({safe(outcome.status_line)})[/dim]")

    # ------------------------------------------------------------------ turns

    async def _turn_worker(self) -> None:
        while True:
            text = await self._queue.get()
            self._streamed = False
            self._new_reply()
            self._turn_started = time.monotonic()
            self._turn = asyncio.ensure_future(
                self.agent.handle_message(text, on_event=self._on_event)
            )
            self._refresh_toolbar()
            await asyncio.wait([self._turn])
            turn, self._turn = self._turn, None
            if turn.cancelled():
                self._flush_stream(final=True)
                self._stream = ""
                self._interrupted_note()
            elif turn.exception() is not None:
                self.say(f"[red]the turn failed: {safe(str(turn.exception()))}[/red]")
            else:
                self._show_outcome(turn.result(), self._streamed)
            self._refresh_toolbar()

    def _interrupted_note(self) -> None:
        run = self.controller.active_run()
        suffix = f" Run {run} keeps going: /pause or /stop to control it." if run else ""
        self.say(f"[yellow]Reply interrupted.[/yellow]{suffix}")

    def interrupt_reply(self) -> None:
        """Esc or Ctrl+C: stop the assistant's reply in progress; never a benchmark (§13)."""
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()

    def command_stoppable(self) -> bool:
        """Whether the long command running now can be stopped part way: not an install,
        which stopped half way would leave a half-built environment behind."""
        return self.commanding() and not is_install(self._command_name)

    def interrupt_command(self) -> None:
        """Esc or Ctrl+C with no reply in progress: stop the long command running now. What
        it already stored is kept; a queued command still runs after it."""
        if self._command is None or self._command.done():
            return
        if not self.command_stoppable():
            self.say(
                f"[yellow]{safe(self._command_name)} cannot be stopped half way (it would leave a "
                "half-built environment); it finishes on its own.[/yellow]"
            )
            return
        self._command_stopped = True
        self._command.cancel()

    def interrupt(self) -> None:
        """Esc or Ctrl+C: the assistant's reply first, else a long command; never a run."""
        if self.replying():
            self.interrupt_reply()
        elif self.commanding():
            self.interrupt_command()

    def on_ctrl_c(self) -> None:
        if self.replying() or self.commanding():
            self.interrupt()
            return
        run = self.controller.active_run()
        if run:
            self.say(
                f"[dim](run {run} is active: /pause, /stop, or /exit to leave it resumable)[/dim]"
            )

    # ------------------------------------------------------------------ progress

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(self.progress_interval)
            with contextlib.suppress(RunError):
                self.poll_progress()

    def poll_progress(self) -> None:
        run_id = self.controller.session.active_run_id
        if run_id is None or run_id.startswith("starting:"):
            return
        if run_id != self._seen_run:
            # Start from what the user has already seen (the session's event cursor).
            cursor = self.controller.session.event_cursors.get(run_id, 0)
            self._seen_run, self._last_sequence, self._last_state = run_id, cursor, None
        events = self.controller.run_events(run_id, after=self._last_sequence)
        if not events:
            return
        self._last_sequence = events[-1]["sequence"]
        status = self.controller.run_status(run_id)
        self._refresh_toolbar(status)
        now = time.monotonic()
        changed = status["condition"] != self._last_state
        if changed or now - self._last_print >= self.coalesce_seconds:
            self._last_print, self._last_state = now, status["condition"]
            style = "dim" if status["provisional"] else "bold"
            self.say(f"[{style}]{safe(render.status_line(status))}[/{style}]")
            self.controller.acknowledge_events(run_id, self._last_sequence)

    # ------------------------------------------------------------------ input

    async def handle_input(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if text.startswith("/"):
            name, _, argument = text.partition(" ")
            if name.lower() == "/themes":
                self.themes(argument)
                return
            if is_long_command(text):
                if self.commanding():
                    # Like a message sent while the assistant is replying: it waits its turn.
                    self._pending_commands.append(text)
                    self.say(
                        f"[dim](queued: {safe(self._command_name)} is still running; "
                        f"{safe(text)} runs after it)[/dim]"
                    )
                    return
                self._start_command(text)
                return
            await self._run_and_show(text)
            return
        if self._turn is not None and not self._turn.done():
            self.say("[dim](queued: the assistant is still replying; Esc interrupts it)[/dim]")
        await self._queue.put(text)

    def _start_command(self, text: str) -> None:
        self._command_name = text
        self._command_active = True
        self._command_started = time.monotonic()
        self._command = asyncio.ensure_future(self._run_command(text))
        self._refresh_toolbar()

    async def _run_and_show(self, text: str) -> None:
        result = await self.commands.run(text)
        render_result(self.console, result)
        if result.kind == "help":
            for command, meaning in TERMINAL_COMMANDS.items():
                self.say(f"  {command:<10} {safe(meaning)}")
        if result.switch_to is not None:
            self._use(result.switch_to)
            self._banner()
        if result.exit:
            self._exit = True

    async def _run_command(self, text: str) -> None:
        """A long command, in the background: the prompt stays open and a Working line shows
        until it ends. A failure is reported, never left as silence."""
        self._command_stopped = False
        progress = asyncio.ensure_future(self._command_progress(text))
        try:
            await self._run_and_show(text)
        except asyncio.CancelledError:
            if not self._command_stopped:  # leaving the chat: let the cancellation through
                self.say(f"[yellow]{safe(text)} was cancelled.[/yellow]")
                raise
            self.say(f"[yellow]{safe(stopped_note(text))}[/yellow]")
        except Exception as exc:  # noqa: BLE001 - one command's failure must not end the chat
            self.say(f"[red]{safe(text)} failed: {safe(str(exc))}[/red]")
        finally:
            progress.cancel()
            await asyncio.gather(progress, return_exceptions=True)
            self._command_active = False
            self._refresh_toolbar()
            if self._pending_commands and not self._exit:
                self._start_command(self._pending_commands.pop(0))

    async def _command_progress(self, text: str) -> None:
        """While a long command runs, say how far it is: `/rescore` counts the evaluations
        stored again; `/cases generate`, one model call with nothing to count, says now and
        then that it is still waiting for the model."""
        words = text.split()
        if words[0].lower() == "/cases":
            waited = 0.0
            while True:
                await asyncio.sleep(self.command_heartbeat_seconds)
                waited += self.command_heartbeat_seconds
                self.say(
                    f"[dim]still waiting for the model to write the cases ({int(waited)}s; "
                    "often 1 to 3 minutes; Esc stops it)[/dim]"
                )
        if words[0].lower() != "/rescore":
            return
        try:
            run_id = next((w for w in words[1:] if w.startswith("run-")), None)
            status = self.controller.run_status(run_id)
            run_id = status["run_id"]
            planned = sum(status["counts"].get("evaluation", {}).values())
            baseline = len(self.controller.storage.list_metric_results(run_id))
        except Exception:  # noqa: BLE001 - progress is a courtesy; never break the command
            return
        last = 0
        while True:
            await asyncio.sleep(self.command_progress_seconds)
            try:
                done = len(self.controller.storage.list_metric_results(run_id)) - baseline
            except Exception:  # noqa: BLE001
                return
            if done != last and planned:
                last = done
                self.say(f"[dim]rescoring: {min(done, planned)} of {planned} evaluations[/dim]")

    def themes(self, argument: str) -> None:
        """`/themes` lists the themes; `/themes NAME` switches now and saves the choice."""
        name = argument.strip().lower()
        if not name:
            for theme in THEMES.values():
                mark = "*" if theme.name == self.theme.name else " "
                self.say(
                    f" {mark} {banner.swatch(self.console, theme)} "
                    f"[bold {theme.accent}]{theme.name:<8}[/] {safe(theme.description)}"
                )
            self.say("[dim]/themes NAME switches; the choice is saved for this project.[/dim]")
            return
        chosen = THEMES.get(name)
        if chosen is None:
            self.say(f"[red]unknown theme {safe(name)}; one of: {', '.join(THEMES)}[/red]")
            return
        self._set_theme(chosen)
        saved = ""
        if self.theme_path is not None:
            try:
                save_theme(self.theme_path, chosen)
            except OSError as exc:
                saved = f" [yellow](not saved: {safe(str(exc))})[/yellow]"
        self.say(
            f"{banner.swatch(self.console, chosen)} theme "
            f"[bold {chosen.accent}]{chosen.name}[/]{saved}"
        )

    async def _animate(self, session: PromptSession[str]) -> None:
        """Redraw the prompt while a reply is in progress, and once after, so the Working
        line spins, counts seconds, and disappears when the reply ends."""
        was_replying = False
        while True:
            await asyncio.sleep(_FRAME_SECONDS)
            replying = self.replying() or self.commanding()
            if replying or was_replying:
                session.app.invalidate()
            was_replying = replying

    async def run(self) -> None:
        bindings = key_bindings()

        # Esc then Enter still adds a line: prompt_toolkit waits briefly for the second key.
        @bindings.add("escape", filter=Condition(lambda: self.replying() or self.commanding()))
        def _interrupt(event: KeyPressEvent) -> None:
            self.interrupt()

        session: PromptSession[str] = PromptSession(
            history=self.history,
            completer=SlashCompleter(),
            key_bindings=bindings,
            style=DynamicStyle(lambda: self._prompt_style),
            bottom_toolbar=self.toolbar,
            refresh_interval=self.progress_interval,
            erase_when_done=True,
            # No rows held empty under the box for the completion menu, so the box sits on
            # the status bar; the menu opens above the box when there is no room below.
            reserve_space_for_menu=0,
            input=self.input,
            output=self.output,
            # The input box: the band's gutter before every line of the input, and the tint
            # and fill behind them. It keeps Enter as "send", so a newline is still Esc Enter.
            prompt_continuation=self.composer.continuation,
            input_processors=[self.composer],
        )
        composer.pin_to_bottom(session, self.composer)
        self._session = session
        # A real terminal needs patch_stdout so asynchronous Rich/progress output is
        # rendered above the editable prompt. Injected input/output pairs (used by PTY
        # adapters and the pipe-driven integration test) own their output implementation;
        # patch_stdout would otherwise ask prompt_toolkit to open the host console again.
        stdout_context = (
            patch_stdout(raw=True)
            if self.input is None and self.output is None
            else contextlib.nullcontext()
        )
        with stdout_context:
            self._banner()
            worker = asyncio.ensure_future(self._turn_worker())
            watcher = asyncio.ensure_future(self._watch())
            animator = asyncio.ensure_future(self._animate(session))
            try:
                while not self._exit:
                    try:
                        text = await session.prompt_async(self._prompt_message)
                    except KeyboardInterrupt:
                        self.on_ctrl_c()
                        continue
                    except EOFError:
                        break
                    if text.strip():
                        self._echo(text.strip())
                    await self.handle_input(text)
            finally:
                await self.shutdown(worker, watcher, animator)

    async def shutdown(self, *tasks: asyncio.Task[Any]) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            await asyncio.wait([self._turn])
        self._pending_commands.clear()  # queued commands do not run after leaving
        if self._command is not None and not self._command.done():
            # Leaving ends a long command; what it already stored is kept.
            self._command.cancel()
            await asyncio.gather(self._command, return_exceptions=True)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        run = self.controller.active_run()
        if run:
            self.say(
                f"Leaving: run {run} stops dispatching new work; in-flight work finishes and "
                "is recorded. It stays resumable (/resume after reopening this session)."
            )
        await self.controller.close()
        if run:
            with contextlib.suppress(RunError):
                self.say(safe(render.status_line(self.controller.run_status(run))))


def outcome_json(outcome: TurnOutcome) -> str:
    return json.dumps(outcome.as_dict(), default=str)
