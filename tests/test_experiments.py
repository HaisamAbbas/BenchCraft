"""Prompt 19: finite development search, protected final evaluation and adoption proposal."""

from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

import pytest

from aibench.core.models import ExperimentEventKind, ExperimentStatus
from aibench.experiments.service import (
    ExperimentError,
    create_experiment,
    evaluate_protected_holdout,
    execute_experiment,
    experiment_report,
    extend_trial_budget,
    prepare_experiment,
    propose_adoption,
)
from aibench.security.policy import ExecutionPolicy
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

EXAMPLE = Path(__file__).parents[1] / "examples" / "experiments" / "known_objective"


def _copy_example(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "known_objective"
    shutil.copytree(EXAMPLE, target)
    return target


def _open(root: Path) -> tuple[Database, Storage, ArtifactStore]:
    workspace = Workspace.at(root)
    database = Database.open_workspace(workspace)
    return database, Storage(database), ArtifactStore(workspace.artifacts_dir)


def _prepare(spec_path: Path):
    return prepare_experiment(
        spec_path,
        policy=ExecutionPolicy(),
        trusted_local=True,
    )


def test_known_objective_budget_resume_and_protected_holdout(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    database, storage, artifacts = _open(tmp_path)
    try:
        prepared = _prepare(project / "experiment.json")
        record = create_experiment(prepared, storage=storage, artifacts=artifacts)
        trials = storage.list_experiment_trials(record.experiment_id)
        assert len(trials) == 2
        assert storage.get_dataset(record.holdout_dataset_hash) is None
        assert storage.list_cases(record.holdout_dataset_hash) == []
        assert storage.is_protected_dataset_digest(record.holdout_dataset_hash)

        exhausted = asyncio.run(
            execute_experiment(record.experiment_id, storage=storage, artifacts=artifacts)
        )
        assert exhausted.status is ExperimentStatus.BUDGET_EXHAUSTED
        [baseline] = storage.list_experiment_trials(record.experiment_id)[:1]
        baseline_run = storage.get_run(baseline.run_id)
        assert baseline_run is not None and baseline_run.status == "completed"
        run_parameters = baseline_run.manifest.parameters
        context = run_parameters["experiment_context"]
        assert context["parameters"] == {"answer_mode": "baseline"}
        assert context["parameter_hash"] == baseline.parameter_hash
        assert context["run_seed"] == 41
        assert baseline_run.manifest.seed == 41
        assert baseline_run.manifest.dataset_hash == record.development_dataset_hash
        assert storage.get_dataset(record.holdout_dataset_hash) is None
        assert storage.list_cases(record.holdout_dataset_hash) == []

        extend_trial_budget(
            record.experiment_id,
            additional_trials=1,
            storage=storage,
        )
        selected = asyncio.run(
            execute_experiment(record.experiment_id, storage=storage, artifacts=artifacts)
        )
        assert selected.status is ExperimentStatus.SELECTED, [
            (item.trial_id, item.status.value, item.failure, item.comparison_to_baseline)
            for item in storage.list_experiment_trials(record.experiment_id)
        ]
        assert selected.selection_locked_at is not None
        assert selected.selected_trial_id is not None
        candidate = storage.get_experiment_trial(selected.selected_trial_id)
        assert candidate is not None
        assert candidate.parameters == {"answer_mode": "accurate"}
        assert candidate.comparison_to_baseline["claim_qualified"] is True
        development_comparison = candidate.comparison_to_baseline["comparison"]
        assert development_comparison["measurement"] == "paired_binary_rate_difference"
        assert development_comparison["unit"] == "proportion"
        assert development_comparison["uncertainty"]["lower"] > 0

        complete = asyncio.run(
            evaluate_protected_holdout(
                record.experiment_id,
                storage=storage,
                artifacts=artifacts,
            )
        )
        assert complete.status is ExperimentStatus.COMPLETED
        report = experiment_report(record.experiment_id, storage=storage, artifacts=artifacts)
        assert report["parameter_space"] == [
            {"name": "answer_mode", "values": ["baseline", "accurate"]}
        ]
        assert report["run_contract"]["per_run_budgets"]["max_application_calls"] == 4
        assert report["constraints"] == []
        protected = report["protected_holdout_evaluation"]
        assert protected["status"] == "completed"
        assert protected["comparison"]["claim_qualified"] is True
        assert protected["comparison"]["runs"]["baseline"]["dataset_hash"] == record.holdout_dataset_hash
        assert protected["comparison"]["runs"]["current"]["dataset_hash"] == record.holdout_dataset_hash
        assert protected["runs"]["selected"]["metrics"]["0"]["mean"] == 1.0
        assert report["development_selection"]["dataset_hash"] == record.development_dataset_hash
        assert "elm" not in json.dumps(report).casefold()

        existing_run_ids = {item.run_id for item in storage.list_experiment_trials(record.experiment_id)}
        existing_run_ids |= {complete.holdout_baseline_run_id, complete.holdout_run_id}
        again = asyncio.run(
            evaluate_protected_holdout(
                record.experiment_id,
                storage=storage,
                artifacts=artifacts,
            )
        )
        assert again.status is ExperimentStatus.COMPLETED
        assert {item.run_id for item in storage.list_experiment_trials(record.experiment_id)} | {
            again.holdout_baseline_run_id,
            again.holdout_run_id,
        } == existing_run_ids
        assert asyncio.run(
            execute_experiment(record.experiment_id, storage=storage, artifacts=artifacts)
        ).status is ExperimentStatus.COMPLETED

        proposal = propose_adoption(record.experiment_id, storage=storage, artifacts=artifacts)
        assert proposal["recommendation"] == "review_candidate_for_explicit_adoption"
        assert proposal["applied"] is False
        assert "separate explicit authorization" in proposal["authorization"]
        assert any(
            event.kind is ExperimentEventKind.ADOPTION_PROPOSED
            for event in storage.list_experiment_events(record.experiment_id)
        )

        reverse = _copy_example(tmp_path / "reverse")
        reverse_spec = json.loads((reverse / "experiment.json").read_text(encoding="utf-8"))
        reverse_spec["experiment_id"] = "attempt-holdout-reuse"
        reverse_spec["development_dataset"] = "holdout.jsonl"
        reverse_spec["holdout_dataset"] = "development.jsonl"
        (reverse / "experiment.json").write_text(json.dumps(reverse_spec), encoding="utf-8")
        reverse_plan = json.loads((reverse / "plan.json").read_text(encoding="utf-8"))
        reverse_plan["dataset"] = "holdout.jsonl"
        (reverse / "plan.json").write_text(json.dumps(reverse_plan), encoding="utf-8")
        with pytest.raises(ExperimentError, match="already protected as holdout"):
            create_experiment(_prepare(reverse / "experiment.json"), storage=storage, artifacts=artifacts)
    finally:
        database.close()


def test_frozen_rubric_and_exposed_parameter_space_reject_changes(tmp_path: Path) -> None:
    project = _copy_example(tmp_path)
    database, storage, artifacts = _open(tmp_path)
    try:
        record = create_experiment(
            _prepare(project / "experiment.json"),
            storage=storage,
            artifacts=artifacts,
        )
        plan_path = project / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["metrics"][0]["params"]["case_sensitive"] = False
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        with pytest.raises(ExperimentError, match="evaluation plan changed"):
            asyncio.run(execute_experiment(record.experiment_id, storage=storage, artifacts=artifacts))
        assert storage.get_experiment(record.experiment_id).status is ExperimentStatus.READY
        assert storage.list_experiment_trials(record.experiment_id)[0].status.value == "pending"

        bad = _copy_example(tmp_path / "bad")
        definition = json.loads((bad / "experiment.json").read_text(encoding="utf-8"))
        definition["experiment_id"] = "unexposed-parameter"
        definition["parameters"][0]["name"] = "rubric"
        (bad / "experiment.json").write_text(json.dumps(definition), encoding="utf-8")
        with pytest.raises(ExperimentError, match="not exposed by the application"):
            _prepare(bad / "experiment.json")
    finally:
        database.close()


def test_holdout_outside_policy_roots_is_rejected_before_plan_compilation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aibench.experiments.service as experiment_service

    project = _copy_example(tmp_path)
    outside = tmp_path / "outside" / "holdout.jsonl"
    outside.parent.mkdir()
    shutil.copyfile(project / "holdout.jsonl", outside)
    definition = json.loads((project / "experiment.json").read_text(encoding="utf-8"))
    definition["holdout_dataset"] = str(outside)
    spec_path = project / "outside-holdout.json"
    spec_path.write_text(json.dumps(definition), encoding="utf-8")

    def plan_must_not_compile(*_args, **_kwargs):
        pytest.fail("policy must approve both splits before compiling or loading plugins")

    monkeypatch.setattr(experiment_service, "compile_plan", plan_must_not_compile)
    with pytest.raises(ExperimentError, match="holdout dataset is outside.*data_roots"):
        prepare_experiment(
            spec_path,
            policy=ExecutionPolicy(data_roots=(str(project),)),
            trusted_local=True,
        )


def test_experiment_requires_a_verifiable_application_code_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aibench.experiments.service as experiment_service

    project = _copy_example(tmp_path)
    monkeypatch.setattr(
        experiment_service,
        "code_identity_problem",
        lambda *_args: "the application source cannot be enumerated",
    )

    with pytest.raises(ExperimentError, match="application code identity cannot be frozen"):
        _prepare(project / "experiment.json")


def test_interrupted_holdout_resumes_same_frozen_plan_and_run_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aibench.experiments.service as experiment_service

    project = _copy_example(tmp_path)
    definition = json.loads((project / "experiment.json").read_text(encoding="utf-8"))
    definition["budget"]["max_trials"] = 2
    (project / "experiment.json").write_text(json.dumps(definition), encoding="utf-8")
    database, storage, artifacts = _open(tmp_path)
    try:
        prepared = _prepare(project / "experiment.json")
        record = create_experiment(prepared, storage=storage, artifacts=artifacts)
        selected = asyncio.run(
            execute_experiment(record.experiment_id, storage=storage, artifacts=artifacts)
        )
        assert selected.status is ExperimentStatus.SELECTED

        original_execute = experiment_service.execute_run
        interrupted_run_id = experiment_service._holdout_run_id(record.experiment_id, "selected")

        async def interrupt_selected(run_id: str, **kwargs):
            if run_id == interrupted_run_id:
                return None
            return await original_execute(run_id, **kwargs)

        monkeypatch.setattr(experiment_service, "execute_run", interrupt_selected)
        paused = asyncio.run(
            evaluate_protected_holdout(record.experiment_id, storage=storage, artifacts=artifacts)
        )
        assert paused.status is ExperimentStatus.HOLDOUT_RUNNING
        assert paused.holdout_plan_hash is not None
        assert paused.holdout_plan_artifact_id is not None
        assert storage.get_run(paused.holdout_baseline_run_id).status == "completed"
        assert storage.get_run(paused.holdout_run_id).status == "created"

        monkeypatch.setattr(experiment_service, "execute_run", original_execute)
        resumed = asyncio.run(
            evaluate_protected_holdout(record.experiment_id, storage=storage, artifacts=artifacts)
        )
        assert resumed.status is ExperimentStatus.COMPLETED
        assert resumed.holdout_plan_hash == paused.holdout_plan_hash
        assert resumed.holdout_plan_artifact_id == paused.holdout_plan_artifact_id
        assert resumed.holdout_baseline_run_id == paused.holdout_baseline_run_id
        assert resumed.holdout_run_id == paused.holdout_run_id
    finally:
        database.close()


def test_experiment_conversation_tools_are_read_only_proposals() -> None:
    from aibench.conversation.agent import SYSTEM_PROMPT, TOOL_NAMES, tool_specs

    assert {"list_experiments", "get_experiment_report", "propose_experiment_adoption"} <= TOOL_NAMES
    assert "Never edit source files, deploy, or change production settings" in SYSTEM_PROMPT
    tools = {item["function"]["name"]: item for item in tool_specs()}
    assert "never applies changes" in tools["propose_experiment_adoption"]["function"]["description"]
    assert "apply_experiment_adoption" not in TOOL_NAMES


def test_experiment_cli_creates_and_reports_without_running_trials(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from aibench.cli.main import app

    project = _copy_example(tmp_path)
    runner = CliRunner()
    created = runner.invoke(
        app,
        [
            "experiments",
            "create",
            str(project / "experiment.json"),
            "--workspace",
            str(tmp_path),
            "--trust-local-app",
        ],
    )
    assert created.exit_code == 0, created.output
    assert '"trial_limit": 1' in created.output
    shown = runner.invoke(
        app,
        ["experiments", "report", "known-objective", "--workspace", str(tmp_path)],
    )
    assert shown.exit_code == 0, shown.output
    assert '"development_selection"' in shown.output
    assert '"protected_holdout_evaluation"' in shown.output
    assert '"selection_locked": false' in shown.output
