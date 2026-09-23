"""CLI runner integration tests against real subprocesses (03-T1, 03-T2; gates 03-G1..G4).

Every test spawns real child processes; nothing is mocked."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aibench.core.errors import ConfigError, PolicyError
from aibench.core.models import BenchmarkCase, EffectState, ErrorKind, ExecutionStatus
from aibench.runners import (
    AppInputEnvelope,
    CliRunner,
    InvocationContext,
    RunnerLifecycleError,
    create_runner,
    load_application,
)
from tests.runner_support import (
    EXAMPLE_APPS,
    SENTINEL,
    cli_spec,
    golden_case,
    misbehaving,
    read_pid,
    run,
    wait_until_dead,
)


def _runner(spec: Any, **kwargs: Any) -> CliRunner:
    return CliRunner(spec, base_dir=EXAMPLE_APPS, trusted_local=True, **kwargs)


async def _invoke(runner: CliRunner, case: BenchmarkCase | None = None, **ctx: Any) -> Any:
    case = case or golden_case()
    async with runner:
        return await runner.invoke(
            AppInputEnvelope.from_case(case),
            InvocationContext(run_id="r", case_id=case.case_id, **ctx),
        )


def _capture(outcome: Any, name: str) -> bytes:
    return next(c.data for c in outcome.captures if c.name == name)


# --------------------------------------------------------------------------- 03-G1


def test_example_chatbot_invocation_records_output_timing_and_captures() -> None:
    app = load_application(EXAMPLE_APPS / "cli_chatbot.app.json")
    runner = create_runner(app, trusted_local=True)
    outcome = run(_invoke(runner))  # type: ignore[arg-type]
    assert outcome.status is ExecutionStatus.OK
    assert outcome.output == "Refunds are available within 30 days of purchase."
    assert outcome.effect_state is EffectState.NONE_DECLARED
    assert outcome.timing["wall_ms"] > 0
    assert outcome.completeness["exit_status"] == {
        "state": "observed",
        "detail": "present",
        "value": 0,
    }
    assert [c.name for c in outcome.captures] == ["request", "stdout", "stderr"]
    assert json.loads(_capture(outcome, "stdout")) == {"output": outcome.output}


def test_legacy_text_mode_reads_plain_stdout() -> None:
    app = load_application(EXAMPLE_APPS / "blackbox_cli.app.json")
    outcome = run(_invoke(create_runner(app, trusted_local=True)))  # type: ignore[arg-type]
    assert outcome.status is ExecutionStatus.OK
    assert outcome.output == "You asked a 5-word question. Our team will follow up by email."
    assert outcome.completeness["output"]["method"] == "stdout_text"


@pytest.mark.parametrize(
    ("mode", "arg", "kind"),
    [
        ("exit", "3", ErrorKind.NONZERO_EXIT),
        ("badjson", "", ErrorKind.INVALID_OUTPUT),
        ("wrongfield", "", ErrorKind.INVALID_OUTPUT),
    ],
)
def test_application_failures_are_recorded_not_raised(mode: str, arg: str, kind: ErrorKind) -> None:
    spec = misbehaving(mode, *([arg] if arg else []), effects="reversible")
    outcome = run(_invoke(_runner(spec)))
    assert outcome.status is ExecutionStatus.ERROR
    assert outcome.error_kind is kind
    # The process finished on its own, so effects (if any) are as it left them.
    assert outcome.effect_state is EffectState.COMPLETED
    if mode == "exit":
        assert outcome.completeness["exit_status"]["value"] == 3
        assert b"configured failure" in _capture(outcome, "stderr")


def test_missing_executable_fails_in_prepare_with_a_clear_message() -> None:
    runner = _runner(cli_spec(["definitely-not-a-real-program-xyz"]))
    with pytest.raises(ConfigError, match="not found"):
        run(runner.prepare())


def test_lifecycle_order_is_enforced() -> None:
    runner = _runner(misbehaving("echo"))
    envelope = AppInputEnvelope.from_case(golden_case())
    with pytest.raises(RunnerLifecycleError):
        run(runner.invoke(envelope, InvocationContext(run_id="r", case_id="case-1")))

    async def closed_then_prepare() -> None:
        await runner.prepare()
        await runner.close()
        await runner.prepare()

    with pytest.raises(RunnerLifecycleError):
        run(closed_then_prepare())


def test_healthcheck_is_unknown_without_a_command_and_real_with_one() -> None:
    async def check(spec: Any) -> Any:
        async with _runner(spec) as runner:
            return await runner.healthcheck(), await runner.reset()

    health, reset = run(check(misbehaving("echo")))
    assert health.status == "unknown"
    assert reset.status == "not_needed"
    ok = misbehaving("echo", healthcheck_argv=[sys.executable, "-c", "raise SystemExit(0)"])
    bad = misbehaving("echo", healthcheck_argv=[sys.executable, "-c", "raise SystemExit(4)"])
    assert run(check(ok))[0].status == "healthy"
    assert run(check(bad))[0].status == "unhealthy"


# --------------------------------------------------------------------------- 03-G2


def test_trusted_local_mode_is_required() -> None:
    runner = CliRunner(misbehaving("echo"), base_dir=EXAMPLE_APPS, trusted_local=False)
    with pytest.raises(PolicyError, match="trusted-local"):
        run(runner.prepare())


def test_sentinel_reference_never_reaches_stdin_env_argv_or_captures() -> None:
    environ = {
        "PATH": __import__("os").environ["PATH"],
        "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
        "LEAKY_EVALUATOR_KEY": SENTINEL,
    }
    outcome = run(_invoke(_runner(misbehaving("echo"), environ=environ)))
    assert outcome.status is ExecutionStatus.OK
    seen = outcome.output
    assert seen["stdin"]["input"] == "What is your refund policy?"
    assert seen["stdin"]["fixtures"] == {"visible": {"note": "shown to the app"}}
    assert "LEAKY_EVALUATOR_KEY" not in seen["env"]  # not on the inherit allow-list
    assert SENTINEL not in json.dumps(seen)
    for capture in outcome.captures:
        assert SENTINEL.encode() not in capture.data, capture.name


def test_child_environment_is_an_allow_list_plus_explicit_values() -> None:
    import os

    environ = dict(os.environ, APP_TOKEN="tok-123456", UNRELATED="nope")
    spec = misbehaving("echo", env={"MODE": "test"}, secret_env={"TOKEN": "env:APP_TOKEN"})
    outcome = run(_invoke(_runner(spec, environ=environ), correlation_id="corr-1"))
    env = outcome.output["env"]
    assert env["MODE"] == "test"
    # The app received the real secret (only the real value is rewritten by redaction),
    # but neither the parsed output nor any persisted capture contains it.
    assert env["TOKEN"] == "<redacted:env:APP_TOKEN>"
    assert env["AIBENCH_CORRELATION_ID"] == "corr-1"
    assert "UNRELATED" not in env
    for capture in outcome.captures:
        assert b"tok-123456" not in capture.data, capture.name


def test_request_capture_redacts_a_secret_that_matches_app_input() -> None:
    token = "tok-request-capture"
    argv = [
        sys.executable,
        "-c",
        (
            "import json,os,sys; req=json.load(sys.stdin); "
            "print(json.dumps({'output': req['input'] == os.environ['TOKEN']}))"
        ),
    ]
    spec = cli_spec(argv, secret_env={"TOKEN": "env:APP_TOKEN"})
    environ = dict(__import__("os").environ, APP_TOKEN=token)
    outcome = run(
        _invoke(
            _runner(spec, environ=environ),
            BenchmarkCase(case_id="secret-input", input=token),
        )
    )
    assert outcome.output is True  # redaction changed the saved capture, not stdin
    request = _capture(outcome, "request")
    assert token.encode() not in request
    assert b"<redacted:env:APP_TOKEN>" in request
    assert all(token.encode() not in capture.data for capture in outcome.captures)


def test_missing_secret_fails_prepare() -> None:
    spec = misbehaving("echo", secret_env={"TOKEN": "env:NOT_SET_ANYWHERE_123"})
    with pytest.raises(ConfigError, match="not set"):
        run(_runner(spec, environ={"PATH": ""}).prepare())


def test_case_text_is_never_interpolated_into_a_shell(tmp_path: Path) -> None:
    marker = tmp_path / "pwned.txt"
    hostile = f'"; echo pwned > "{marker}" & echo pwned > "{marker}" | $(touch {marker}) `touch {marker}` {{input}}'
    case = BenchmarkCase(case_id="evil", input=hostile)
    outcome = run(_invoke(_runner(misbehaving("echo")), case))
    assert outcome.status is ExecutionStatus.OK
    assert outcome.output["stdin"]["input"] == hostile  # delivered verbatim, as data
    assert outcome.output["argv"][1:] == ["echo"]  # argv unchanged by case content
    assert not marker.exists()


# --------------------------------------------------------------------------- 03-G3


def test_timeout_kills_the_whole_process_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "grandchild.pid"
    spec = misbehaving("spawn", str(pidfile), timeout_seconds=3, effects="reversible")
    outcome = run(_invoke(_runner(spec)))
    assert outcome.status is ExecutionStatus.ERROR
    assert outcome.error_kind is ErrorKind.TIMEOUT
    assert outcome.effect_state is EffectState.UNKNOWN  # it ran; we cannot know what it did
    assert wait_until_dead(read_pid(pidfile)), "grandchild survived the timeout"


def test_descendants_are_cleaned_up_even_after_a_successful_exit(tmp_path: Path) -> None:
    """The child exits 0 but leaves a grandchild holding its stdout open. The invocation
    must still finish promptly with the child's answer and kill the grandchild."""
    pidfile = tmp_path / "orphan.pid"
    spec = misbehaving("orphan", str(pidfile), timeout_seconds=20)
    outcome = run(_invoke(_runner(spec)))
    assert outcome.status is ExecutionStatus.OK, outcome.error
    assert outcome.output == "done"
    assert outcome.timing["wall_ms"] < 15_000
    assert wait_until_dead(read_pid(pidfile)), "orphaned grandchild survived"


def test_stdout_limit_stops_an_unbounded_writer() -> None:
    spec = misbehaving("flood", str(5_000_000), max_stdout_bytes=10_000, timeout_seconds=30)
    outcome = run(_invoke(_runner(spec)))
    assert outcome.error_kind is ErrorKind.OUTPUT_LIMIT
    assert outcome.timing["wall_ms"] < 20_000  # stopped at the limit, not at the timeout
    stdout = next(c for c in outcome.captures if c.name == "stdout")
    assert stdout.truncated and len(stdout.data) == 10_000
    assert outcome.completeness["output"]["detail"] == "truncated"


def test_stderr_is_bounded_but_does_not_fail_the_invocation() -> None:
    spec = misbehaving("stderr", str(200_000), max_stderr_bytes=1_000)
    outcome = run(_invoke(_runner(spec)))
    assert outcome.status is ExecutionStatus.OK
    stderr = next(c for c in outcome.captures if c.name == "stderr")
    assert stderr.truncated and len(stderr.data) == 1_000
    assert outcome.completeness["stderr"] == {
        "state": "observed",
        "detail": "truncated",
        "total_bytes": 200_000,
    }


def test_cooperative_cancel_records_a_cancelled_attempt_and_kills_the_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "cancel.pid"
    spec = misbehaving("spawn", str(pidfile), timeout_seconds=60, effects="irreversible")

    async def scenario() -> Any:
        cancel = asyncio.Event()
        runner = _runner(spec)
        async with runner:
            task = asyncio.ensure_future(
                runner.invoke(
                    AppInputEnvelope.from_case(golden_case()),
                    InvocationContext(run_id="r", case_id="case-1", cancel=cancel),
                )
            )
            await asyncio.to_thread(read_pid, pidfile)
            cancel.set()
            return await task

    outcome = run(scenario())
    assert outcome.status is ExecutionStatus.CANCELLED
    assert outcome.error_kind is ErrorKind.CANCELLED
    assert outcome.effect_state is EffectState.UNKNOWN
    assert wait_until_dead(read_pid(pidfile))


def test_task_cancellation_propagates_and_still_kills_the_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "taskcancel.pid"
    spec = misbehaving("spawn", str(pidfile), timeout_seconds=60)

    async def scenario() -> None:
        runner = _runner(spec)
        async with runner:
            task = asyncio.ensure_future(
                runner.invoke(
                    AppInputEnvelope.from_case(golden_case()),
                    InvocationContext(run_id="r", case_id="case-1"),
                )
            )
            await asyncio.to_thread(read_pid, pidfile)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run(scenario())
    assert wait_until_dead(read_pid(pidfile))


# --------------------------------------------------------------------------- 03-G4


def test_self_reported_observations_are_read_only_when_declared() -> None:
    spec = misbehaving(
        "report",
        output_binding={
            "retrieved_context": "/docs",
            "retrieved_context_item": "/text",
            "tool_events": "/tools",
            "usage": "/usage",
        },
    )
    outcome = run(_invoke(_runner(spec)))
    obs, c = outcome.observations, outcome.completeness
    assert obs.retrieved_context == ("doc one", "doc two")
    assert c["retrieved_context"]["state"] == "observed"
    assert c["tool_events"]["detail"] == "empty"
    assert obs.usage is None and c["usage"]["detail"] == "invalid"
    assert obs.cost is None and c["cost"] == {"state": "unknown", "detail": "not_bound"}


def test_black_box_output_leaves_every_other_capability_unknown() -> None:
    app = load_application(EXAMPLE_APPS / "blackbox_cli.app.json")
    outcome = run(_invoke(create_runner(app, trusted_local=True)))  # type: ignore[arg-type]
    for name in ("retrieved_context", "tool_events", "usage", "cost"):
        assert outcome.completeness[name]["state"] == "unknown"
    assert outcome.observations.retrieved_context is None
    assert outcome.observations.usage is None and outcome.observations.cost is None


# --------------------------------------------------------------------------- review regressions


@pytest.mark.parametrize("kind", ["deep", "bigint", "nan", "overflow_float", "surrogate"])
def test_hostile_json_is_an_invalid_output_not_a_crash(kind: str) -> None:
    """Deep nesting, huge numbers, non-finite floats, and lone surrogates must all become
    a recorded INVALID_OUTPUT attempt."""
    outcome = run(_invoke(_runner(misbehaving("hostile", kind))))
    assert outcome.status is ExecutionStatus.ERROR
    assert outcome.error_kind is ErrorKind.INVALID_OUTPUT
    assert outcome.output is None


def test_output_survives_an_uncontained_descendant_holding_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If tree containment is unavailable (Windows job assignment failed, or a POSIX
    descendant called setsid), a finished app's already-read stdout must not be lost."""
    import aibench.runners.cli_runner as cli_module
    from aibench.runners.process_tree import ProcessTree

    class Uncontained(ProcessTree):
        def __init__(self, pid: int) -> None:
            self.pid, self.contained, self._closed, self._job = pid, False, False, None

    monkeypatch.setattr(cli_module, "ProcessTree", Uncontained)
    pidfile = tmp_path / "escaped.pid"
    outcome = run(_invoke(_runner(misbehaving("orphan", str(pidfile), timeout_seconds=20))))
    grandchild = read_pid(pidfile)
    try:
        assert outcome.status is ExecutionStatus.OK, outcome.error
        assert outcome.output == "done"
        assert outcome.completeness["exit_status"]["value"] == 0
    finally:
        import os
        import signal

        try:
            os.kill(grandchild, signal.SIGTERM)  # test cleanup: containment was disabled
        except OSError:
            pass
