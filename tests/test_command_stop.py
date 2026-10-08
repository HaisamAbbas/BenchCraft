"""Esc stops a long slash command. `/rescore` and `/cases generate` ran to the end once
started: a judge stuck on one case held the chat's long-command slot for its whole time limit
and nothing but leaving the chat ended it. Esc (or Ctrl+C) now stops the command running in
the background, keeps what it already stored, and kills a worker stuck in a judge call; an
install is never stopped half way."""

from __future__ import annotations

import asyncio
import io
import time
from pathlib import Path
from typing import Any

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from aibench.core.plans import PluginEnvironmentRef
from aibench.core.sessions import PlanPatch
from aibench.tui.app import ChatApp, stopped_note
from aibench.tui.commands import CommandResult
from tests.deepeval_support import JUDGES, PLUGIN_ENV, requires_plugin_env
from tests.runner_support import wait_until_dead
from tests.session_support import SessionHarness

ESC = "\x1b"


async def _wait_until(predicate: Any, *, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition did not become true")
        await asyncio.sleep(0.02)


def test_esc_stops_a_long_command_and_the_queued_one_still_runs(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    output = io.StringIO()
    chat = ChatApp(
        ctl,
        provider=None,
        console=Console(file=output, highlight=False, width=200),
        command_heartbeat_seconds=0.05,
    )

    async def scenario() -> None:
        quick = chat.commands.run
        never = asyncio.Event()
        ran: list[str] = []

        async def run(text: str, **kwargs: Any) -> Any:
            ran.append(text)
            if text.startswith("/cases generate"):
                await never.wait()  # a model call that would take minutes
            if text.startswith("/rescore"):
                return CommandResult("/rescore", "error", {"error": "rescore ran"}, ok=False)
            return await quick(text, **kwargs)

        chat.commands.run = run  # type: ignore[method-assign]
        with create_pipe_input() as pipe:
            chat.input = pipe
            chat.output = DummyOutput()
            prompt = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)

            pipe.send_text("/cases generate rules.md\r")
            await _wait_until(chat.commanding)
            working = "".join(text for _, text in chat._working_line())
            assert "Working: /cases generate rules.md" in working and "to stop" in working
            # Nothing to count while one model call runs: it says it is still waiting.
            await _wait_until(lambda: "still waiting for the model" in output.getvalue())
            pipe.send_text("/rescore\r")  # queued behind it
            await _wait_until(lambda: "queued: /cases generate" in output.getvalue())

            pipe.send_text(ESC)
            await _wait_until(lambda: "rules.md stopped. Nothing was stored" in output.getvalue())
            await _wait_until(lambda: "rescore ran" in output.getvalue())  # the queued one ran
            await _wait_until(lambda: not chat.commanding())
            assert "was cancelled" not in output.getvalue()

            pipe.send_text("/exit\r")
            await asyncio.wait_for(prompt, timeout=5)
        assert ran[:2] == ["/cases generate rules.md", "/rescore"]

    try:
        asyncio.run(scenario())
    finally:
        ctl.storage.db.close()


def test_an_install_is_not_stopped_half_way(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer"})
    output = io.StringIO()
    chat = ChatApp(ctl, provider=None, console=Console(file=output, highlight=False, width=200))

    async def scenario() -> None:
        release = asyncio.Event()
        quick = chat.commands.run

        async def run(text: str, **kwargs: Any) -> Any:
            if not text.startswith("/plugins"):
                return await quick(text, **kwargs)
            await release.wait()
            return CommandResult("/plugins", "error", {"error": "install finished"}, ok=False)

        chat.commands.run = run  # type: ignore[method-assign]
        with create_pipe_input() as pipe:
            chat.input = pipe
            chat.output = DummyOutput()
            prompt = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)
            pipe.send_text("/plugins install deepeval --yes\r")
            await _wait_until(chat.commanding)
            assert "to stop" not in "".join(text for _, text in chat._working_line())
            pipe.send_text(ESC)
            await _wait_until(lambda: "cannot be stopped half way" in output.getvalue())
            assert chat.commanding()  # still running
            release.set()
            await _wait_until(lambda: "install finished" in output.getvalue())
            pipe.send_text("/exit\r")
            await asyncio.wait_for(prompt, timeout=5)

    try:
        asyncio.run(scenario())
    finally:
        ctl.storage.db.close()


def test_what_stopping_leaves_behind_is_said_in_the_users_terms() -> None:
    assert "stored and kept" in stopped_note("/rescore all")
    assert "Nothing was stored" in stopped_note("/cases generate a.md")
    assert stopped_note("/report html") == "/report html stopped."


AGREEING = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
BLOCKING = {"kind": "python_factory", "factory": "aibench_test_judges:blocking_judge"}


@requires_plugin_env
def test_esc_stops_a_rescore_stuck_in_a_judge_call_and_kills_its_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real DeepEval worker whose judge blocks for 180 s: Esc ends `/rescore all` within
    seconds, the worker process is gone, and the run's earlier results are still stored."""
    from aibench.evaluators import worker_client

    h = SessionHarness(tmp_path)
    ctl = h.open_session(
        {"a": "answer", "b": "answer"},
        objectives=("catch wrong answers",),
        policy={
            "data_roots": [str(tmp_path)],
            "allowed_evaluators": ["native.*", "deepeval.*"],
            "allowed_plugin_environments": [str(PLUGIN_ENV)],
            "allowed_plugin_paths": [str(JUDGES)],
            "allow_model_evaluators": True,
        },
    )

    def use_judge(judge: dict[str, Any]) -> None:
        settings = {"name": "polite", "criteria": "Is the answer polite?", "judge": judge}
        result = ctl.apply_patch(
            PlanPatch(
                add_objectives=("G-Eval check named polite",), params={"deepeval.g_eval": settings}
            ),
            expected_revision=ctl.session.revision,
            source="user",
        )
        assert result.status == "applied", result.problems

    spawned: list[int] = []
    real_spawn = worker_client.asyncio.create_subprocess_exec

    async def recording_spawn(*args: Any, **kwargs: Any) -> Any:
        proc = await real_spawn(*args, **kwargs)
        spawned.append(proc.pid)
        return proc

    output = io.StringIO()
    chat = ChatApp(ctl, provider=None, console=Console(file=output, highlight=False, width=200))

    async def scenario() -> None:
        environment = PluginEnvironmentRef(python=str(PLUGIN_ENV), paths=(str(JUDGES),))
        loaded = ctl.use_plugin_environments((environment,), {})
        assert loaded.status == "applied", loaded.problems
        use_judge(AGREEING)
        started = await ctl.start_run(action_id="run-1", expected_revision=ctl.session.revision)
        done = await ctl.wait_for_run(started.run_id)
        assert done is not None and done.state.value == "completed"
        before = {r.result_id for r in ctl.storage.list_metric_results(started.run_id)}
        assert before and all(
            r.status.value == "ok" for r in ctl.storage.list_metric_results(started.run_id)
        )

        use_judge(BLOCKING)  # the next scoring pass hangs in the judge
        monkeypatch.setattr(worker_client.asyncio, "create_subprocess_exec", recording_spawn)
        with create_pipe_input() as pipe:
            chat.input = pipe
            chat.output = DummyOutput()
            prompt = asyncio.create_task(chat.run())
            await asyncio.sleep(0.05)
            pipe.send_text("/rescore all\r")
            await _wait_until(lambda: bool(spawned), timeout=60)
            await asyncio.sleep(8)  # the worker is up and the judge call is blocking
            assert chat.commanding(), output.getvalue()[-3000:]

            pressed = time.monotonic()
            pipe.send_text(ESC)
            await _wait_until(lambda: "stopped. The evaluations" in output.getvalue(), timeout=30)
            assert time.monotonic() - pressed < 20  # not the judge's 180 s
            await _wait_until(lambda: not chat.commanding())

            pipe.send_text("/exit\r")
            await asyncio.wait_for(prompt, timeout=30)
        after = {r.result_id for r in ctl.storage.list_metric_results(started.run_id)}
        assert before <= after  # what was stored is kept

    try:
        asyncio.run(scenario())
        for pid in spawned:
            assert wait_until_dead(pid, timeout=10), f"worker {pid} outlived the stopped rescore"
    finally:
        ctl.storage.db.close()
