"""09-T1..T4: the actual prompt loop, direct controls and live run behavior."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from aibench.planning.openai_provider import OpenAICompatibleConfig, OpenAICompatibleProvider
from aibench.planning.planner import ModelReply
from aibench.sessions.controller import SessionController
from aibench.tui.app import ChatApp, SlashCompleter, key_bindings
from aibench.tui.commands import COMMANDS, Commands
from aibench.tui.render import status_line
from tests.chat_server_support import chat_server, text_stream, tool_stream
from tests.session_support import (
    ScriptedProvider,
    SessionHarness,
    call,
    patch_step,
    say,
    start_step,
)


class BlockingProvider:
    name = "blocking-test-provider"
    model = "blocking-test-model"

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.calls += 1
        self.started.set()
        if not self.release.wait(10):
            raise TimeoutError("test provider was not released")
        return ModelReply(text="late answer")


class FailingProvider:
    name = "failing-test-provider"
    model = "failing-test-model"

    def __init__(self) -> None:
        self.calls = 0
        self.started = threading.Event()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.calls += 1
        self.started.set()
        raise RuntimeError("provider unavailable")


async def _wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.01)


def test_slash_commands_use_real_session_services_and_cover_the_contract(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({case: "slow 8" for case in "abcd"}, objectives=("catch wrong answers",))
    created: list[SessionController] = []

    def new_session() -> SessionController:
        new = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
        created.append(new)
        return new

    commands = Commands(ctl, new_session)

    async def scenario() -> None:
        help_result = await commands.run("/help")
        assert help_result.kind == "help"
        assert set(help_result.data["commands"]) == set(COMMANDS)

        shown = await commands.run("/plan")
        assert shown.kind == "plan" and shown.data["revision"] == ctl.session.revision

        started = await commands.run("/run")
        assert started.kind == "action" and started.data["state"] == "done"
        run_id = started.data["run_id"]
        await h.wait_for_invocations(1)

        status = await commands.run("/status")
        assert status.kind == "status" and status.data["run_id"] == run_id
        assert status.data["provisional"] is True

        paused = await commands.run("/pause")
        assert paused.data["state"] == "done"
        resumed = await commands.run("/resume")
        assert resumed.data["state"] == "done"

        failures = await commands.run("/failures")
        budget = await commands.run("/budget")
        report = await commands.run("/report")
        sessions = await commands.run("/sessions")
        assert failures.kind == "failures" and failures.data["run_id"] == run_id
        assert budget.kind == "budget" and budget.data["run_id"] == run_id
        assert report.kind == "report" and report.data["run_id"] == run_id
        assert sessions.kind == "sessions" and sessions.data["sessions"]

        # Explicit cancellation returns before the slow application call is done.
        before = time.monotonic()
        stopped = await asyncio.wait_for(commands.run("/stop"), timeout=0.5)
        assert time.monotonic() - before < 0.5
        assert stopped.data["state"] == "done"
        outcome = await ctl.wait_for_run(run_id)
        assert outcome is not None and outcome.state.value == "cancelled"

        case = await commands.run("/case a")
        assert case.kind == "case" and case.data["case_id"] == "a", case.data
        partial = ctl.report(run_id)
        assert partial["run"]["partial"] is True and partial["run"]["provisional"] is False
        final_status = ctl.run_status(run_id)
        assert final_status["partial"] is True and final_status["provisional"] is False

        switched = await commands.run("/new")
        assert switched.switch_to is created[0]
        exited = await commands.run("/exit")
        assert exited.exit is True

    try:
        asyncio.run(scenario())
    finally:
        ctl.storage.db.close()
        for controller in created:
            controller.storage.db.close()


def test_compare_slash_command_uses_session_owned_stored_runs(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(
        {case: "hi" for case in ("a", "b", "c", "d")},
        objectives=("catch wrong answers",),
    )
    commands = Commands(ctl)

    async def scenario() -> tuple[str, str]:
        first = await ctl.start_run(action_id="compare-first", expected_revision=1)
        assert await ctl.wait_for_run(first.run_id) is not None
        second = await ctl.start_run(action_id="compare-second", expected_revision=1)
        assert await ctl.wait_for_run(second.run_id) is not None
        return str(first.run_id), str(second.run_id)

    try:
        baseline, current = asyncio.run(scenario())
        calls_before = h.count()
        result = asyncio.run(commands.run(f"/compare {baseline} {current}"))
        assert result.kind == "comparison" and result.ok
        assert result.exit_code == 0
        assert result.data["status"] == "qualified"
        assert result.data["qualified"] is True
        assert h.count() == calls_before
    finally:
        ctl.storage.db.close()


def test_status_line_labels_spend_completeness() -> None:
    snapshot = {
        "run_id": "run-1",
        "status": "running",
        "provisional": True,
        "partial": True,
        "counts": {"execution": {"succeeded": 1, "pending": 1}},
    }
    assert "spend provisional (in-flight calls excluded)" in status_line(snapshot)

    snapshot.update(
        status="cancelled",
        provisional=False,
        budget={
            "application": {"calls_with_unknown_cost": 1, "known_cost_usd": 0.0},
            "evaluator": {"calls_with_unknown_cost": 0, "known_cost_usd": 0.0123},
        },
    )
    # The judge's cost is known; only the app's one call is not priced, and the line says
    # which (it said "1 call(s) with unknown cost" and never the amount it did know).
    assert "spend partial: $0.0123 known; cost unknown for 1 app call(s)" in status_line(snapshot)
    snapshot["budget"]["application"]["calls_with_unknown_cost"] = 0
    assert "spend $0.0123" in status_line(snapshot)


def test_prompt_loop_accepts_multiline_input_history_completion_and_exit(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    provider = ScriptedProvider([say("ack")])
    output = io.StringIO()
    history_path = tmp_path / "history"
    chat = ChatApp(
        ctl,
        provider=provider,
        console=Console(file=output, force_terminal=False, highlight=False),
        history_path=history_path,
        output=DummyOutput(),
    )

    completions = list(SlashCompleter().get_completions(Document("/sta"), CompleteEvent()))
    assert any(item.text == "/status" for item in completions)
    assert key_bindings().get_bindings_for_keys(("escape", "c-m"))

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            chat.input = pipe
            task = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)
            pipe.send_text("what\x1b\rnext\r")
            await _wait_until(lambda: len(ctl.store.turns(ctl.session_id)) >= 2)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(task, timeout=3)

    try:
        asyncio.run(scenario())
        turns = ctl.store.turns(ctl.session_id)
        assert turns[0].role == "user" and turns[0].content == "what\nnext"
        assert "ack" in output.getvalue()
        assert history_path.is_file()
        assert "/exit" in history_path.read_text(encoding="utf-8")
    finally:
        ctl.storage.db.close()


def test_chat_renders_streamed_text_and_tool_cards(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
    output = io.StringIO()

    async def scenario(provider: OpenAICompatibleProvider, requests: list[dict[str, Any]]) -> None:
        chat = ChatApp(
            ctl,
            provider=provider,
            console=Console(file=output, force_terminal=False, highlight=False),
        )
        worker = asyncio.create_task(chat._turn_worker())
        await chat.handle_input("show the current plan")
        await _wait_until(lambda: len(requests) == 2)
        await _wait_until(lambda: chat._turn is None)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    with chat_server(
        [
            (200, tool_stream("show_plan", {})),
            (200, text_stream("Draft is ", "ready.")),
        ]
    ) as server:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(base_url=server.base_url, model="local")
        )
        try:
            asyncio.run(scenario(provider, server.requests))
        finally:
            provider.close()

    shown = output.getvalue()
    assert "show_plan" in shown and "Draft is ready." in shown
    assert "Plan, revision" in shown
    ctl.storage.db.close()


def test_explicit_run_starts_unpresented_validated_draft_with_a_preview(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, objectives=("catch wrong answers",))
    assert ctl.session.presented_revision is None

    async def scenario() -> None:
        result = await Commands(ctl).run("/run")
        assert result.kind == "action" and result.data["state"] == "done"
        assert result.data["plan_preview"]["revision"] == 1
        assert result.data["plan_preview"]["executable"] is True
        await ctl.wait_for_run(result.data["run_id"])

    asyncio.run(scenario())
    assert h.count() == 1 and len(h.runs()) == 1
    ctl.storage.db.close()


def test_prompted_conversation_drafts_and_runs_a_real_benchmark(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({case: "answer" for case in "abcd"})
    provider = ScriptedProvider(
        [
            call("get_session_state"),
            patch_step("catch wrong answers", add_objectives=["catch wrong answers"]),
            say("I drafted an exact-match check. Run it?"),
            start_step("Run it"),
            say("Started the reviewed run."),
        ]
    )
    output = io.StringIO()
    chat = ChatApp(
        ctl,
        provider=provider,
        console=Console(file=output, force_terminal=False, highlight=False),
    )

    async def scenario() -> None:
        with create_pipe_input() as pipe:
            chat.input = pipe
            chat.output = DummyOutput()
            prompt = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)

            pipe.send_text("I want to catch wrong answers.\r")
            await _wait_until(lambda: ctl.session.revision == 2 and chat._turn is None)
            assert ctl.current_decision().draft["executable"] is True
            assert ctl.session.presented_revision == 2

            pipe.send_text("Run it.\r")
            await _wait_until(lambda: ctl.session.active_run_id not in (None, "starting:"))
            run_id = ctl.session.active_run_id
            assert run_id is not None
            result = await ctl.wait_for_run(run_id)
            assert result is not None and result.state.value == "completed"
            assert h.count() == 4

            pipe.send_text("/status\r")
            await _wait_until(
                lambda: any(turn.content == "/status" for turn in ctl.store.turns(ctl.session_id))
            )
            pipe.send_text("/exit\r")
            await asyncio.wait_for(prompt, timeout=3)

    try:
        asyncio.run(scenario())
        assert "draft revision 2" in output.getvalue()
        assert "completed" in output.getvalue()
        assert len(provider.calls) == 5
    finally:
        ctl.storage.db.close()


def test_status_and_stop_work_during_slow_reply_and_ctrl_c_keeps_run_active(
    tmp_path: Path,
) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({case: "slow 8" for case in "abcd"}, objectives=("catch wrong answers",))
    provider = BlockingProvider()
    output = io.StringIO()
    chat = ChatApp(
        ctl,
        provider=provider,
        console=Console(file=output, highlight=False),
        progress_interval=0.01,
    )

    async def scenario() -> None:
        ctl.mark_presented(ctl.session.revision)
        action = await ctl.start_run(
            action_id="start-live-run", expected_revision=ctl.session.revision
        )
        run_id = action.run_id
        await h.wait_for_invocations(1)
        with create_pipe_input() as pipe:
            chat.input = pipe
            chat.output = DummyOutput()
            prompt = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)

            # Send through PromptSession while both an application call and assistant
            # response are slow. The second input remains available during the reply.
            pipe.send_text("explain the draft\r")
            assert await asyncio.to_thread(provider.started.wait, 3)
            pipe.send_text("/status\r")
            await _wait_until(
                lambda: any(turn.content == "/status" for turn in ctl.store.turns(ctl.session_id))
            )
            assert "run " + run_id in output.getvalue()

            # The terminal's Ctrl+C binding cancels only the reply, preserving the run.
            pipe.send_text("\x03")
            await _wait_until(lambda: chat._turn is None)
            assert ctl.active_run() == run_id

            before = time.monotonic()
            pipe.send_text("/stop\r")
            await _wait_until(
                lambda: any(turn.content == "/stop" for turn in ctl.store.turns(ctl.session_id))
            )
            assert time.monotonic() - before < 0.5
            result = await ctl.wait_for_run(run_id)
            assert result is not None and result.state.value == "cancelled"

            provider.release.set()
            pipe.send_text("/exit\r")
            await asyncio.wait_for(prompt, timeout=3)

    try:
        asyncio.run(scenario())
    finally:
        provider.release.set()
        ctl.storage.db.close()


def test_provider_failure_does_not_disable_terminal_controls(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({case: "slow 8" for case in "abcd"}, objectives=("catch wrong answers",))
    provider = FailingProvider()
    chat = ChatApp(ctl, provider=provider, console=Console(file=io.StringIO(), highlight=False))

    async def scenario() -> None:
        ctl.mark_presented(ctl.session.revision)
        action = await ctl.start_run(action_id="start-before-provider-failure", expected_revision=1)
        run_id = action.run_id
        await h.wait_for_invocations(1)

        worker = asyncio.create_task(chat._turn_worker())
        await chat.handle_input("this provider call will fail")
        assert await asyncio.to_thread(provider.started.wait, 3)
        await _wait_until(lambda: chat._turn is None)
        assert provider.calls == 1

        status = await asyncio.wait_for(chat.commands.run("/status"), timeout=0.5)
        assert status.data["run_id"] == run_id
        stopped = await asyncio.wait_for(chat.commands.run("/stop"), timeout=0.5)
        assert stopped.data["state"] == "done"
        result = await ctl.wait_for_run(run_id)
        assert result is not None and result.state.value == "cancelled"
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        ctl.storage.db.close()


def test_graceful_exit_drains_in_flight_work_and_leaves_run_resumable(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({case: "slow 0.3" for case in "abcd"}, objectives=("catch wrong answers",))
    chat = ChatApp(ctl, provider=None, console=Console(file=io.StringIO(), highlight=False))

    async def scenario() -> None:
        ctl.mark_presented(ctl.session.revision)
        started = await ctl.start_run(action_id="start-before-exit", expected_revision=1)
        await h.wait_for_invocations(1)

        await asyncio.wait_for(chat.shutdown(), timeout=3)
        status = ctl.run_status(started.run_id)
        assert status["status"] == "interrupted"
        assert status["counts"]["execution"].get("succeeded", 0) == 1
        assert h.count() == 1  # pending cases were not dispatched after graceful exit

    try:
        asyncio.run(scenario())
        reopened = h.reopen(ctl)
        assert reopened.active_run() is None
        assert reopened.live_runs() == []  # reopening does not restart dispatch
        assert reopened.run_status(reopened.session.active_run_id)["status"] == "interrupted"
        reopened.storage.db.close()
    finally:
        ctl.storage.db.close()


def test_a_long_slash_command_runs_in_the_background_and_the_input_box_stays(
    tmp_path: Path,
) -> None:
    """`/rescore all` ran inside the input loop: the box disappeared and nothing showed it was
    working for twenty minutes, unlike a message to the assistant. A long command now runs
    like a turn: the prompt stays, a Working line counts seconds, quick commands still answer
    meanwhile, a second long command queues behind it, and the result appears when it ends."""
    from aibench.tui.commands import CommandResult

    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    output = io.StringIO()
    chat = ChatApp(
        ctl, provider=None, console=Console(file=output, highlight=False), progress_interval=0.01
    )

    async def scenario() -> None:
        started, release = asyncio.Event(), asyncio.Event()
        quick = chat.commands.run

        async def run(text: str, **kwargs: Any) -> CommandResult:
            if text.startswith("/rescore"):
                started.set()
                await release.wait()
                return CommandResult("/rescore", "error", {"error": "rescore finished"}, ok=False)
            return await quick(text, **kwargs)

        chat.commands.run = run  # type: ignore[method-assign]
        with create_pipe_input() as pipe:
            chat.input = pipe
            chat.output = DummyOutput()
            prompt = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)

            pipe.send_text("/rescore all\r")
            await asyncio.wait_for(started.wait(), 3)
            assert chat.commanding()
            working = "".join(text for _, text in chat._working_line())
            assert "Working: /rescore all" in working and "to interrupt" not in working
            assert "working: /rescore" in chat.toolbar()

            # The prompt is open: a quick command is answered while /rescore is still running.
            pipe.send_text("/status\r")
            await _wait_until(
                lambda: any(turn.content == "/status" for turn in ctl.store.turns(ctl.session_id))
            )
            assert chat.commanding()

            # A second long command waits its turn, as a message does while the assistant
            # replies.
            pipe.send_text("/rescore\r")
            await _wait_until(lambda: "queued: /rescore all is still running" in output.getvalue())

            release.set()
            await _wait_until(lambda: output.getvalue().count("rescore finished") == 2)
            await _wait_until(lambda: not chat.commanding())
            assert "".join(text for _, text in chat._working_line()) == ""
            assert "idle" in chat.toolbar()

            pipe.send_text("/exit\r")
            await asyncio.wait_for(prompt, timeout=3)

    try:
        asyncio.run(scenario())
    finally:
        ctl.storage.db.close()


def test_a_long_command_that_fails_says_so_and_leaving_cancels_it(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    output = io.StringIO()
    chat = ChatApp(ctl, provider=None, console=Console(file=output, highlight=False))

    async def scenario() -> None:
        quick = chat.commands.run
        hang = asyncio.Event()

        async def run(text: str, **kwargs: Any) -> Any:
            if text.startswith("/plugins"):
                raise RuntimeError("the environment broke")
            if text.startswith("/cases"):
                await hang.wait()  # never ends on its own
            return await quick(text, **kwargs)

        chat.commands.run = run  # type: ignore[method-assign]
        with create_pipe_input() as pipe:
            chat.input = pipe
            chat.output = DummyOutput()
            prompt = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)
            pipe.send_text("/plugins install x\r")
            await _wait_until(lambda: "failed: the environment broke" in output.getvalue())
            await _wait_until(lambda: not chat.commanding())  # a failure frees the next one

            pipe.send_text("/cases generate x.md\r")
            await _wait_until(chat.commanding)
            pipe.send_text("/exit\r")
            await asyncio.wait_for(prompt, timeout=3)
            assert not chat.commanding()
            assert "was cancelled" in output.getvalue()

    try:
        asyncio.run(scenario())
    finally:
        ctl.storage.db.close()


def test_rescore_says_how_many_evaluations_are_stored_again_while_it_runs(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer", "b": "answer"}, objectives=("catch wrong answers",))
    output = io.StringIO()
    chat = ChatApp(
        ctl,
        provider=None,
        console=Console(file=output, highlight=False),
        command_progress_seconds=0.02,
    )

    async def scenario() -> None:
        started = await ctl.start_run(action_id="run-1", expected_revision=ctl.session.revision)
        done = await ctl.wait_for_run(started.run_id)
        assert done is not None and done.state.value == "completed"
        stored = ctl.storage.list_metric_results(started.run_id)
        planned = len(stored)  # one evaluation per case for the one metric
        again = [0]  # how many results the "rescore" has stored again so far

        original = ctl.storage.list_metric_results
        ctl.storage.list_metric_results = lambda *a, **k: stored + stored[: again[0]]  # type: ignore[method-assign]
        task = asyncio.create_task(chat._command_progress("/rescore all"))
        await asyncio.sleep(0.05)
        assert "rescoring" not in output.getvalue()  # nothing stored again yet
        again[0] = 1
        await _wait_until(lambda: f"rescoring: 1 of {planned} evaluations" in output.getvalue())
        again[0] = planned
        await _wait_until(
            lambda: f"rescoring: {planned} of {planned} evaluations" in output.getvalue()
        )
        task.cancel()
        # Another long command shows no progress line of its own.
        before = output.getvalue()
        other = asyncio.create_task(chat._command_progress("/plugins"))
        await asyncio.sleep(0.05)
        other.cancel()
        assert output.getvalue() == before
        ctl.storage.list_metric_results = original  # type: ignore[method-assign]

    try:
        asyncio.run(scenario())
    finally:
        ctl.storage.db.close()


def test_only_the_commands_that_can_take_minutes_run_in_the_background() -> None:
    from aibench.tui.app import is_long_command

    for text in (
        "/rescore",
        "/rescore all run-1",
        "/report html",
        "/compare a b",
        "/plugins install deepeval --yes",
        "/cases generate rules.txt --max 6",
        "/traces import file.json",
    ):
        assert is_long_command(text), text
    for text in (
        "/plugins",  # lists
        "/cases",  # shows the pool
        "/cases accept 1 2",
        "/cases save",
        "/traces",
        "/status",
        "/plan",
        "/stop",
        "/exit",
        "not a command",
        "",
    ):
        assert not is_long_command(text), text
