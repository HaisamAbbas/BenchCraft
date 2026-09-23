"""Evidence profiles and dataset summaries (07-T1): observed / declared / inferred /
unknown, evidence locations, and no label values in anything a planner sees."""

from __future__ import annotations

import json
from pathlib import Path

from aibench.core.models import ExecutionResult, ExecutionStatus, ObservationState
from aibench.inspection.dataset_summary import summarize_dataset
from aibench.inspection.profile import inspect_application
from tests.planning_support import write_app, write_dataset


def _states(profile: object) -> dict[str, str]:
    return {c.capability: c.state.value for c in profile.claims}  # type: ignore[attr-defined]


def _execution(n: int, retrieved: str) -> ExecutionResult:
    return ExecutionResult(
        execution_id=f"run-1:c{n}:r0:a1",
        run_id="run-1",
        case_id=f"c{n}",
        status=ExecutionStatus.OK,
        output="x",
        observation_completeness={
            "output": {"state": "observed", "detail": "present"},
            "retrieved_context": {
                "state": retrieved.split(":")[0],
                "detail": retrieved.split(":")[1],
            },
            "tool_events": {"state": "unknown", "detail": "not_bound"},
        },
    )


def test_declared_config_gives_declared_or_unknown_with_evidence_and_recipes(
    tmp_path: Path,
) -> None:
    app = write_app(tmp_path, output_binding={"output": "/answer", "retrieved_context": "/ctx"})
    profile = inspect_application(app)
    states = _states(profile)
    assert states["retrieved_context"] == "declared"
    assert states["tool_events"] == states["usage"] == states["cost"] == "unknown"
    claim = profile.claim("retrieved_context")
    assert claim is not None and claim.evidence_refs == (
        f"{app}#/output_binding/retrieved_context",
    )
    assert any(
        "tool_events is not observable" in g and "output_binding.tool_events" in g
        for g in profile.gaps
    )
    assert "no source code or architecture discovery" in profile.scope
    assert profile.endpoint == "http://127.0.0.1:9/"  # origin only


def test_recorded_executions_upgrade_to_observed_or_expose_a_contradiction(tmp_path: Path) -> None:
    app = write_app(tmp_path, output_binding={"output": "/answer", "retrieved_context": "/ctx"})
    observed = inspect_application(
        app, executions=[_execution(1, "observed:present"), _execution(2, "observed:empty")]
    )
    claim = observed.claim("retrieved_context")
    assert claim is not None and claim.state is ObservationState.OBSERVED
    assert claim.scope == "2 of 2 successful recorded executions (1 empty)"
    assert claim.limitations == "self-reported by the application"
    assert claim.evidence_refs[0].startswith("execution:run-1:c1")

    # Declared, but the application never actually returned it: partial/misleading evidence.
    silent = inspect_application(app, executions=[_execution(1, "unknown:missing")])
    claim = silent.claim("retrieved_context")
    assert claim is not None and claim.state is ObservationState.DECLARED
    assert claim.limitations == "declared, but absent from all 1 successful recorded executions"
    assert any("check the response mapping" in g for g in silent.gaps)


def test_dataset_summary_counts_fields_and_never_carries_values(tmp_path: Path) -> None:
    dataset = write_dataset(
        tmp_path,
        [
            {
                "case_id": "a",
                "input": "SECRET-INPUT-1",
                "expected_output": "SECRET-ANSWER-1",
                "context": ["SECRET-CONTEXT-1"],
                "metadata": {"category": "SECRET-CATEGORY"},
            },
            {"case_id": "b", "input": "SECRET-INPUT-2", "expected_tools": ["SECRET_TOOL"]},
        ],
    )
    summary = summarize_dataset(dataset)
    coverage = {f.path: (f.present, f.empty, f.missing) for f in summary.fields}
    assert coverage["case.reference.answer"] == (1, 0, 1)
    assert coverage["case.reference.context"] == (1, 1, 0)
    assert coverage["case.reference.tools"] == (1, 0, 1)
    assert coverage["case.metadata.category"] == (1, 0, 1)
    dumped = summary.model_dump_json()
    assert "SECRET" not in dumped  # shape only, never labels or inputs
    inferred = {c.capability: c for c in summary.inferred}
    assert inferred["retrieval_task"].state is ObservationState.INFERRED
    assert "not observed retrieval" in (inferred["retrieval_task"].limitations or "")
    assert "tool_use_task" in inferred


def test_inspect_cli_uses_only_runs_of_the_same_app_config(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from aibench.cli.main import app as cli_app
    from tests.engine_support import Harness

    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    h.execute(run_id)
    runner = CliRunner()
    ws = str(h.workspace.root.parent)
    result = runner.invoke(
        cli_app,
        ["inspect", str(tmp_path / "app.json"), "--run", run_id, "--workspace", ws, "--json"],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    claims = {c["capability"]: c for c in data["profile"]["claims"]}
    assert claims["output"]["state"] == "observed"
    assert data["profile"]["evidence_runs"] == [run_id]

    h.cli_app(timeout=5.0)  # a different config now
    changed = runner.invoke(
        cli_app,
        ["inspect", str(tmp_path / "app.json"), "--run", run_id, "--workspace", ws, "--json"],
    )
    data = json.loads(changed.output)
    assert data["profile"]["evidence_runs"] == []
    assert "used a different application config" in data["notes"][0]
