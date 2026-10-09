"""`aibench plan validate`, `run --plan`, `resume`, `evaluate`, `runs status` through the Typer
app (06-T4), with exit codes 0/2/3/4/130 and zero dispatch on refusal."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.engine.engine import RunController, RunState
from aibench.security.policy import ExecutionPolicy
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
    invalid = tmp_path / "invalid-plan.json"
    invalid.write_text("{", encoding="utf-8")
    rejected = cli.invoke(
        app, ["plan", "validate", str(invalid), "--trust-local-app", "--json"]
    )
    assert rejected.exit_code == 2, rejected.output
    assert _json(rejected.stdout)["details"]
    assert h.count() == 0


def test_missing_plan_json_uses_shared_error_document(tmp_path: Path) -> None:
    missing = tmp_path / "missing.plan.json"
    result = cli.invoke(
        app,
        ["run", "--plan", str(missing), "--workspace", str(tmp_path), "--json"],
    )
    assert result.exit_code == 2, result.output
    document = _json(result.stdout)
    assert document["status"] == "error"
    assert document["message"] == "nothing was dispatched: the plan is invalid"
    assert document["exit_code"] == 2 and document["details"]


def test_run_dry_run_shows_exact_overridden_scope_and_dispatch_matches_it(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"case-a": "hi", "case-b": "hi"}),
        application=h.cli_app(),
    )
    project = str(h.workspace.root.parent)
    args = [
        "run",
        "--plan",
        str(plan),
        "--workspace",
        project,
        "--trust-local-app",
        "--limit",
        "1",
        "--repetitions",
        "2",
        "--application-concurrency",
        "2",
        "--evaluation-concurrency",
        "2",
        "--max-app-calls",
        "2",
        "--max-attempts",
        "1",
        "--no-cache-executions",
        "--cache-evaluations",
        "--run-seed",
        "1729",
        "--json",
    ]
    preview_result = cli.invoke(app, [*args, "--dry-run"])
    assert preview_result.exit_code == 0, preview_result.output
    preview = _json(preview_result.stdout)
    assert preview["schema"] == "aibench.run-preview/1"
    assert preview["will_dispatch"] is False
    assert preview["reproducibility"] == {
        "run_seed": 1729,
        "seed_will_be_random": False,
    }
    assert preview["scope"]["case_ids"] == ["case-a"]
    assert preview["scope"]["execution_items"] == 2
    assert preview["scope"]["evaluation_items"] == 2
    assert preview["frozen"]["effective_plan"]["repetitions"] == 2
    assert preview["frozen"]["effective_plan"]["budgets"]["max_application_calls"] == 2
    assert preview["frozen"]["effective_plan"]["concurrency"]["evaluation"] == 2
    assert preview["frozen"]["effective_plan"]["cache"] == {
        "executions": False,
        "evaluations": True,
    }
    assert h.count() == 0
    assert not h.workspace.db_path.exists()

    human_preview = cli.invoke(app, [*args[:-1], "--dry-run"])
    assert human_preview.exit_code == 0, human_preview.output
    assert json.loads(human_preview.stdout)["scope"]["case_ids"] == ["case-a"]
    assert not h.workspace.db_path.exists()

    run_result = cli.invoke(app, args)
    assert run_result.exit_code == 0, run_result.output
    outcome = _json(run_result.stdout)
    run_id = outcome["run_id"]
    assert outcome["run_seed"] == 1729
    assert h.count() == 2
    storage, _ = h.storage()
    try:
        record = storage.get_run(run_id)
        assert record is not None
        assert record.manifest.seed == 1729
        assert record.manifest.plan_hash == preview["frozen"]["plan_hash"]
    finally:
        storage.db.close()


def test_run_rejects_non_finite_wall_budget_before_preview_or_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"case-a": "hi"}),
        application=h.cli_app(),
    )
    result = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--workspace",
            str(h.workspace.root.parent),
            "--trust-local-app",
            "--max-wall-seconds",
            "inf",
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    document = _json(result.stdout)
    assert document["status"] == "error"
    assert "finite number" in " ".join(document["details"])
    assert h.count() == 0
    assert not h.workspace.db_path.exists()


def test_run_rejects_run_seed_outside_engine_range_before_preview(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"case-a": "hi"}),
        application=h.cli_app(),
    )
    result = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--workspace",
            str(h.workspace.root.parent),
            "--trust-local-app",
            "--run-seed",
            str(2**31),
            "--dry-run",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    assert h.count() == 0
    assert not h.workspace.db_path.exists()


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


def test_retry_creates_bounded_child_from_safe_application_failures(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"bad-a": "crash", "ok": "hi", "bad-b": "crash"}),
        application=h.cli_app(),
        retry={"max_attempts": 1},
    )
    ws = str(h.workspace.root.parent)
    parent_result = cli.invoke(
        app, ["run", "--plan", str(plan), "--trust-local-app", "--workspace", ws, "--json"]
    )
    assert parent_result.exit_code == 3, parent_result.output
    parent_id = _json(parent_result.output)["run_id"]
    assert h.count() == 3

    preview_result = cli.invoke(
        app,
        [
            "runs",
            "retry",
            parent_id,
            "--trust-local-app",
            "--workspace",
            ws,
            "--max-cases",
            "1",
            "--dry-run",
            "--json",
        ],
    )
    assert preview_result.exit_code == 0, preview_result.output
    preview = _json(preview_result.output)
    assert preview["parent_run_id"] == parent_id
    assert preview["selected_case_ids"] == ["bad-a"]
    assert preview["execution_items"] == 1
    assert preview["truncated_case_ids"] == ["bad-b"]
    assert preview["will_dispatch"] is False
    assert h.count() == 3

    human_preview = cli.invoke(
        app,
        [
            "runs", "retry", parent_id, "--trust-local-app", "--workspace", ws,
            "--max-cases", "1", "--dry-run",
        ],
    )
    assert human_preview.exit_code == 0, human_preview.output
    assert "bad-a" in human_preview.stdout and "nothing dispatched" in human_preview.stdout
    assert h.count() == 3

    # Repair the application in place; the retry recompiles current code while checking
    # that it still consumes the exact parent dataset and policy.
    app_path = tmp_path / "app.py"
    app_path.write_text(
        app_path.read_text(encoding="utf-8").replace(
            'if text.startswith("crash"):', 'if text.startswith("never-crash"):'
        ),
        encoding="utf-8",
    )
    child_result = cli.invoke(
        app,
        [
            "runs",
            "retry",
            parent_id,
            "--trust-local-app",
            "--workspace",
            ws,
            "--max-cases",
            "1",
            "--json",
        ],
    )
    assert child_result.exit_code == 0, child_result.output
    child = _json(child_result.output)
    assert child["parent_run_id"] == parent_id
    assert child["retry_scope"] == {
        "selected_case_ids": ["bad-a"],
        "repetitions": 1,
        "truncated_case_ids": ["bad-b"],
        "unsafe_failed_case_ids": [],
        "selected_effect_risk_case_ids": [],
    }
    assert h.count("bad-a") == 2
    assert h.count("bad-b") == 1
    assert h.count("ok") == 1

    shown = cli.invoke(app, ["runs", "show", child["run_id"], "--workspace", ws, "--json"])
    assert shown.exit_code == 0, shown.output
    assert _json(shown.output)["parent_run_id"] == parent_id
    report = cli.invoke(
        app,
        [
            "report",
            child["run_id"],
            "--format",
            "json",
            "--out",
            "-",
            "--workspace",
            ws,
        ],
    )
    assert report.exit_code == 0, report.output
    assert _json(report.output)["run"]["parent_run_id"] == parent_id


def test_retry_validates_parent_scope_and_dataset_identity(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    dataset = h.dataset({"selected": "hi", "other": "hello"})
    plan = h.plan(dataset=dataset, application=h.cli_app())
    ws = str(h.workspace.root.parent)
    parent_result = cli.invoke(
        app, ["run", "--plan", str(plan), "--trust-local-app", "--workspace", ws, "--json"]
    )
    assert parent_result.exit_code == 0, parent_result.output
    parent_id = _json(parent_result.output)["run_id"]
    before = h.count()

    revoked = cli.invoke(
        app,
        [
            "runs", "retry", parent_id, "--case", "selected", "--dry-run",
            "--workspace", ws, "--json",
        ],
    )
    assert revoked.exit_code == 4, revoked.output
    assert "policy denies this plan" in revoked.output
    assert h.count() == before

    selected = cli.invoke(
        app,
        [
            "runs",
            "retry",
            parent_id,
            "--trust-local-app",
            "--case",
            "selected",
            "--dry-run",
            "--workspace",
            ws,
            "--json",
        ],
    )
    assert selected.exit_code == 0, selected.output
    assert _json(selected.output)["selected_case_ids"] == ["selected"]

    outside_scope = cli.invoke(
        app,
        [
            "runs", "retry", parent_id, "--trust-local-app", "--case", "missing",
            "--dry-run", "--workspace", ws, "--json",
        ],
    )
    assert outside_scope.exit_code == 2
    assert "not selected by the parent" in outside_scope.output
    assert h.count() == before

    (tmp_path / dataset).write_text(
        json.dumps({"case_id": "selected", "input": "changed", "expected_output": "yes"})
        + "\n"
        + json.dumps({"case_id": "other", "input": "hello", "expected_output": "yes"})
        + "\n",
        encoding="utf-8",
    )
    changed = cli.invoke(
        app,
        [
            "runs",
            "retry",
            parent_id,
            "--trust-local-app",
            "--case",
            "selected",
            "--dry-run",
            "--workspace",
            ws,
            "--json",
        ],
    )
    assert changed.exit_code == 2
    assert "dataset changed" in changed.output
    assert h.count() == before


def test_retry_automatically_skips_failures_that_may_have_caused_effects(
    tmp_path: Path,
) -> None:
    from aibench.core.models import EffectLevel

    h = Harness(tmp_path)
    policy = ExecutionPolicy(max_effects=EffectLevel.REVERSIBLE)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(policy.model_dump_json(), encoding="utf-8")
    plan = h.plan(
        dataset=h.dataset({"bad": "crash"}),
        application=h.cli_app(effects="reversible"),
        retry={"max_attempts": 1},
    )
    ws = str(h.workspace.root.parent)
    parent_result = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--policy",
            str(policy_path),
            "--trust-local-app",
            "--workspace",
            ws,
            "--json",
        ],
    )
    assert parent_result.exit_code == 3, parent_result.output
    parent_id = _json(parent_result.output)["run_id"]
    before = h.count()

    automatic = cli.invoke(
        app,
        [
            "runs", "retry", parent_id, "--policy", str(policy_path), "--trust-local-app",
            "--workspace", ws, "--json",
        ],
    )
    assert automatic.exit_code == 2
    assert "no safely retryable failed cases" in automatic.output
    assert h.count() == before

    explicit = cli.invoke(
        app,
        [
            "runs",
            "retry",
            parent_id,
            "--policy",
            str(policy_path),
            "--trust-local-app",
            "--case",
            "bad",
            "--dry-run",
            "--workspace",
            ws,
            "--json",
        ],
    )
    assert explicit.exit_code == 0, explicit.output
    preview = _json(explicit.output)
    assert preview["selected_case_ids"] == ["bad"]
    assert preview["unsafe_failed_case_count"] == 1
    assert h.count() == before


def test_retry_rechecks_current_project_policy(tmp_path: Path) -> None:
    from aibench.core.models import EffectLevel

    h = Harness(tmp_path)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        ExecutionPolicy(max_effects=EffectLevel.REVERSIBLE).model_dump_json(), encoding="utf-8"
    )
    (tmp_path / "aibench.json").write_text(
        json.dumps({"policy_path": "policy.json"}), encoding="utf-8"
    )
    plan = h.plan(
        dataset=h.dataset({"bad": "crash"}),
        application=h.cli_app(effects="reversible"),
        retry={"max_attempts": 1},
    )
    ws = str(h.workspace.root.parent)
    parent_result = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--policy",
            str(policy_path),
            "--trust-local-app",
            "--workspace",
            ws,
            "--json",
        ],
    )
    assert parent_result.exit_code == 3, parent_result.output
    parent_id = _json(parent_result.output)["run_id"]

    policy_path.write_text(
        ExecutionPolicy(max_effects=EffectLevel.NONE).model_dump_json(), encoding="utf-8"
    )
    retry_result = cli.invoke(
        app,
        [
            "runs",
            "retry",
            parent_id,
            "--trust-local-app",
            "--case",
            "bad",
            "--dry-run",
            "--workspace",
            ws,
            "--json",
        ],
    )
    assert retry_result.exit_code == 4, retry_result.output
    assert "policy denies this plan" in retry_result.output
    assert h.count() == 1


def test_retry_case_level_safety_checks_every_final_repetition_and_work_item() -> None:
    from aibench.cli.run import _retry_failure_case_ids
    from aibench.core.models import (
        EffectState,
        ExecutionResult,
        ExecutionStatus,
        WorkItem,
        WorkItemState,
    )

    def execution(
        case_id: str,
        repetition: int,
        attempt: int,
        status: ExecutionStatus,
        effect: EffectState,
    ) -> ExecutionResult:
        return ExecutionResult(
            execution_id=f"run:{case_id}:r{repetition}:a{attempt}",
            run_id="run",
            case_id=case_id,
            repetition_id=repetition,
            attempt_id=attempt,
            status=status,
            effect_state=effect,
        )

    stranded = WorkItem(
        work_item_id="work-stranded",
        run_id="run",
        task_key="exec:stranded:r1",
        kind="execution",
        state=WorkItemState.UNKNOWN_EFFECT,
    )
    missing_failure = WorkItem(
        work_item_id="work-failed-without-result",
        run_id="run",
        task_key="exec:missing-result:r0",
        kind="execution",
        state=WorkItemState.FAILED,
        attempt=2,
    )
    failed, safe, effect_risk = _retry_failure_case_ids(
        [
            execution("mixed", 0, 0, ExecutionStatus.ERROR, EffectState.NOT_DISPATCHED),
            execution("mixed", 1, 0, ExecutionStatus.CANCELLED, EffectState.UNKNOWN),
            execution("effectful", 0, 0, ExecutionStatus.OK, EffectState.COMPLETED),
            execution("effectful", 1, 0, ExecutionStatus.ERROR, EffectState.NOT_DISPATCHED),
            execution("safe", 0, 0, ExecutionStatus.ERROR, EffectState.NOT_DISPATCHED),
            execution("recovered", 0, 0, ExecutionStatus.ERROR, EffectState.UNKNOWN),
            execution("recovered", 0, 1, ExecutionStatus.OK, EffectState.NONE_DECLARED),
            execution("stranded", 0, 0, ExecutionStatus.ERROR, EffectState.NONE_DECLARED),
            execution(
                "missing-result", 0, 1, ExecutionStatus.ERROR, EffectState.NOT_DISPATCHED
            ),
        ],
        [stranded, missing_failure],
    )

    assert failed == {"mixed", "effectful", "safe", "stranded", "missing-result"}
    assert safe == {"safe"}
    assert effect_risk == {"mixed", "effectful", "stranded", "missing-result"}


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

    retry = cli.invoke(
        app,
        [
            "runs",
            "retry",
            run_id,
            "--case",
            "c0",
            "--dry-run",
            "--workspace",
            ws,
            "--json",
        ],
    )
    assert retry.exit_code == 2, retry.output
    assert "finish or resume it before retrying" in retry.output
    assert h.count() == 1  # refusing the unfinished parent never dispatches a child

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
