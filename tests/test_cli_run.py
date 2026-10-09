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
    assert "requires trusted-local mode" in " ".join(denied.output.split())
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
    app_config_path = h.root / h.cli_app()
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["environment_digest"] = "test-runtime-pin"
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "slow 0.2" for i in range(4)}),
        application=str(app_config_path),
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
    """§13, shared by `run`, `resume`, `benchmark --auto` and `chat --send`."""
    from aibench.services.reports import outcome_summary
    from aibench.services.runs import run_exit_code

    def code(state: RunState, status: str, counts: dict, gates: tuple[str, ...] = ()) -> int:
        report = {
            "run": {"status": status},
            "work": {"counts": counts},
            "gates": [{"gate_id": f"g{i}", "status": g} for i, g in enumerate(gates)],
        }
        report["outcome"] = outcome_summary(report)
        return run_exit_code(state, report)

    ok = {"execution": {"succeeded": 2}}
    assert code(RunState.COMPLETED, "completed", ok) == 0
    assert code(RunState.COMPLETED, "completed", ok, ("pass", "pass")) == 0
    assert code(RunState.COMPLETED, "completed", ok, ("pass", "fail")) == 1
    assert code(RunState.INTERRUPTED, "interrupted", {"execution": {"pending": 2}}) == 130
    assert code(RunState.CANCELLED, "cancelled", {"execution": {"cancelled": 1}}) == 3
    for state in ("failed", "blocked", "unknown_effect"):
        # incompleteness wins over a failed gate; both stay in the JSON output
        assert code(RunState.COMPLETED, "completed", {"execution": {state: 1}}, ("fail",)) == 3


def test_read_commands_never_create_a_workspace(tmp_path: Path) -> None:
    for args in (["runs", "status", "r"], ["resume", "r"]):
        result = cli.invoke(app, [*args, "--workspace", str(tmp_path)])
        assert result.exit_code == 2 and "no aibench workspace" in result.output
    assert not (tmp_path / ".aibench").exists()
