"""The interactive terminal (§13, §15, 09-T1..T4): one asyncio loop runs the input prompt,
assistant turns, run progress and the engine, each independently cancellable.

- Input stays editable at all times: the prompt is asynchronous (`prompt_toolkit`), and
  output is printed above it (`patch_stdout`). Enter sends; Esc then Enter adds a line.
  History is kept per workspace; slash commands complete with Tab.
- Slash commands run at once, even while the assistant is replying, and never wait for a
  model (09-G2, 09-G3). Messages go to a turn worker, one at a time, in order.
- Replies stream: text fragments are shown as they arrive, grouped into whole lines so
  they do not tear the prompt, with a compact card per tool call.
- Progress comes from committed run events, polled and coalesced: a line on each state
  change and at most one per interval otherwise, never one message per case. The bottom
  toolbar shows project, session, revision and the run's live state.
- Ctrl+C interrupts the assistant's reply, never a benchmark; with a run active it points
  to /pause and /stop. /stop is the explicit cancel. Leaving (/exit, Ctrl+D) stops new
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
from prompt_toolkit.history import FileHistory, History, InMemoryHistory
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.output import Output
from prompt_toolkit.patch_stdout import patch_stdout
from rich.console import Console

from aibench.conversation.agent import ConversationAgent, TurnEvent, TurnLimits, TurnOutcome
from aibench.planning.planner import PlannerProvider
from aibench.services.runs import RunError
from aibench.sessions.controller import SessionController
from aibench.tui import render
from aibench.tui.commands import COMMANDS, CommandResult, Commands, NewSession
from aibench.tui.render import safe

_WRAP = 88  # stream a partial line once it grows this long, at a word boundary


MAX_REPLAYED = 10  # notable missed events shown on reopening; the rest are counted


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
            for name, meaning in COMMANDS.items():
                if name.startswith(text.lower()):
                    yield Completion(name, start_position=-len(text), display_meta=meaning)


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
        limits: TurnLimits | None = None,
        input: Input | None = None,
        output: Output | None = None,
        progress_interval: float = 0.5,
        coalesce_seconds: float = 2.0,
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
        self.progress_interval = progress_interval
        self.coalesce_seconds = coalesce_seconds
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._turn: asyncio.Task[TurnOutcome] | None = None
        self._stream = ""
        self._streamed = False
        self._toolbar = ""
        self._exit = False
        self._use(controller)

    def _use(self, controller: SessionController) -> None:
        self.controller = controller
        self.agent = ConversationAgent(controller, self.provider, self.limits)
        self.commands = Commands(controller, self.new_session)
        self._seen_run: str | None = None
        self._last_sequence = 0
        self._last_state: str | None = None
        self._last_print = 0.0
        self._refresh_toolbar()

    # ------------------------------------------------------------------ display

    def say(self, text: str) -> None:
        render.out(self.console, text)

    def _banner(self) -> None:
        session = self.controller.session
        model = self.provider.model if self.provider else "none (commands only)"
        self.say(
            f"[bold]BenchCraft[/bold] project {safe(session.project_root)}\n"
            f"session {session.session_id}, draft revision {session.revision}; assistant "
            f"model: {safe(model)}. Type /help for commands; Enter sends, Esc Enter adds a line."
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
        parts.append("assistant replying" if self._turn and not self._turn.done() else "idle")
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
            self.say(safe(line))
        while len(self._stream) > _WRAP and " " in self._stream[:_WRAP]:
            cut = self._stream.rfind(" ", 0, _WRAP)
            self.say(safe(self._stream[:cut]))
            self._stream = self._stream[cut + 1 :]
        if final and self._stream:
            self.say(safe(self._stream))
            self._stream = ""

    def _show_outcome(self, outcome: TurnOutcome, streamed: bool) -> None:
        self._flush_stream(final=True)
        if not streamed and outcome.text:
            self.say(safe(outcome.text))
        if outcome.presented_draft is not None:
            render.draft(self.console, outcome.presented_draft)
        self.say(f"[dim]({safe(outcome.status_line)})[/dim]")

    # ------------------------------------------------------------------ turns

    async def _turn_worker(self) -> None:
        while True:
            text = await self._queue.get()
            self._streamed = False
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

    def on_ctrl_c(self) -> None:
        """Ctrl+C: interrupt the reply in progress; never a benchmark (§13)."""
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
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
            result = await self.commands.run(text)
            render_result(self.console, result)
            if result.switch_to is not None:
                self._use(result.switch_to)
                self._banner()
            if result.exit:
                self._exit = True
            return
        if self._turn is not None and not self._turn.done():
            self.say("[dim](queued: the assistant is still replying; Ctrl+C interrupts it)[/dim]")
        await self._queue.put(text)

    async def run(self) -> None:
        session: PromptSession[str] = PromptSession(
            history=self.history,
            completer=SlashCompleter(),
            key_bindings=key_bindings(),
            bottom_toolbar=self.toolbar,
            refresh_interval=self.progress_interval,
            input=self.input,
            output=self.output,
        )
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
            try:
                while not self._exit:
                    try:
                        text = await session.prompt_async("> ")
                    except KeyboardInterrupt:
                        self.on_ctrl_c()
                        continue
                    except EOFError:
                        break
                    await self.handle_input(text)
            finally:
                await self.shutdown(worker, watcher)

    async def shutdown(self, *tasks: asyncio.Task[Any]) -> None:
        if self._turn is not None and not self._turn.done():
            self._turn.cancel()
            await asyncio.wait([self._turn])
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
