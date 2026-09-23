"""`aibench plan validate`, `run --plan`, `resume`, `evaluate`, `runs status` through the Typer
app (06-T4), with exit codes 0/2/3/4/130 and zero dispatch on refusal."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.engine.engine import RunController, RunState
from tests.engine_support import Harness
from tests.runner_support import REPO_ROOT

cli = CliRunner()
EXAMPLE_PLAN = str(REPO_ROOT / "examples" / "plans" / "support.plan.json")
DEV_POLICY = str(REPO_ROOT / "examples" / "policies" / "local-dev.policy.json")


def _json(output: str) -> dict:  # type: ignore[type-arg]
    return json.loads(output)


def test_plan_validate_reports_shape_and_dispatches_nothing(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(dataset=h.dataset({"a": "hi", "b": "hi"}), application=h.cli_app(), repetitions=2)
    ok = cli.invoke(app, ["plan", "validate", str(plan), "--trust-local-app", "--json"])
    assert ok.exit_code == 0, ok.output
    summary = _json(ok.output)
    assert (summary["cases"], summary["execution_items"], summary["evaluation_items"]) == (2, 4, 4)
    denied = cli.invoke(app, ["plan", "validate", str(plan)])
    assert denied.exit_code == 4
    assert "requires trusted-local mode" in denied.output
    invalid = h.plan(dataset="missing.jsonl", application=h.cli_app())
    assert cli.invoke(app, ["plan", "validate", str(invalid), "--trust-local-app"]).exit_code == 2
    assert h.count() == 0


def test_example_plan_runs_under_the_dev_policy_and_is_denied_by_default(tmp_path: Path) -> None:
    denied = cli.invoke(app, ["run", "--plan", EXAMPLE_PLAN, "--workspace", str(tmp_path)])
    assert denied.exit_code == 4, denied.output
    assert "nothing was dispatched" in denied.output
    assert not (tmp_path / ".aibench").exists()  # denial happens before any workspace write

    done = cli.invoke(
        app,
        [
            "run",
            "--plan",
            EXAMPLE_PLAN,
            "--policy",
            DEV_POLICY,
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    assert done.exit_code == 0, done.output
    outcome = _json(done.output)
    assert outcome["state"] == "completed"
    assert outcome["counts"]["execution"] == {"succeeded": 4}
    assert outcome["counts"]["evaluation"] == {"succeeded": 8}

    status = cli.invoke(
        app, ["runs", "status", outcome["run_id"], "--workspace", str(tmp_path), "--json"]
    )
    assert status.exit_code == 0, status.output
    data = _json(status.output)
    assert data["status"] == "completed" and data["needs_attention"] == []

    again = cli.invoke(app, ["resume", outcome["run_id"], "--workspace", str(tmp_path)])
    assert again.exit_code == 2 and "only interrupted or unfinished runs resume" in again.output


def test_run_exit_code_3_for_failures_and_status_lists_them(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"ok": "hi", "bad": "crash"}),
        application=h.cli_app(),
        retry={"max_attempts": 1},
    )
    ws = str(h.workspace.root.parent)
    result = cli.invoke(
        app, ["run", "--plan", str(plan), "--trust-local-app", "--workspace", ws, "--json"]
    )
    assert result.exit_code == 3, result.output
    run_id = _json(result.output)["run_id"]
    status = _json(cli.invoke(app, ["runs", "status", run_id, "--workspace", ws, "--json"]).output)
    assert [i["task_key"] for i in status["needs_attention"]] == ["exec:bad:r0"]
    missing = cli.invoke(app, ["runs", "status", "nope", "--workspace", ws])
    assert missing.exit_code == 2


def test_interrupted_run_resumes_through_the_cli_and_rescoring_never_invokes(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "slow 0.2" for i in range(4)}), application=h.cli_app()
    )

    async def during(ctl: RunController, harness: Harness) -> None:
        await harness.wait_for_invocations(1)
        ctl.interrupt()

    run_id = h.create(plan)
    assert h.execute(run_id, during=during).state is RunState.INTERRUPTED
    ws = str(h.workspace.root.parent)
    status = _json(cli.invoke(app, ["runs", "status", run_id, "--workspace", ws, "--json"]).output)
    assert status["status"] == "interrupted"

    resumed = cli.invoke(app, ["resume", run_id, "--workspace", ws, "--json"])
    assert resumed.exit_code == 0, resumed.output
    assert h.count() == 4  # each case exactly once across both sessions

    rescored = cli.invoke(
        app, ["evaluate", run_id, "--plan", str(plan), "--workspace", ws, "--json"]
    )
    assert rescored.exit_code == 0, rescored.output
    [summary] = _json(rescored.output)["summaries"]
    assert summary["completed"] == 4
    assert h.count() == 4  # rescoring used the saved executions


def test_exit_codes_map_run_outcomes() -> None:
    from aibench.cli.run import _exit_code
    from aibench.engine.engine import RunOutcome

    def outcome(state: RunState, counts: dict[str, dict[str, int]]) -> RunOutcome:
        return RunOutcome(state=state, counts=counts, budget={}, stop_reason=None)

    assert _exit_code(outcome(RunState.COMPLETED, {"execution": {"succeeded": 2}})) == 0
    assert _exit_code(outcome(RunState.INTERRUPTED, {"execution": {"pending": 2}})) == 130
    assert _exit_code(outcome(RunState.CANCELLED, {"execution": {"cancelled": 1}})) == 3
    for state in ("failed", "blocked", "unknown_effect"):
        assert _exit_code(outcome(RunState.COMPLETED, {"execution": {state: 1}})) == 3


def test_read_commands_never_create_a_workspace(tmp_path: Path) -> None:
    for args in (["runs", "status", "r"], ["resume", "r"]):
        result = cli.invoke(app, [*args, "--workspace", str(tmp_path)])
        assert result.exit_code == 2 and "no aibench workspace" in result.output
    assert not (tmp_path / ".aibench").exists()
