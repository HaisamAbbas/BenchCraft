"""Real-storage tests for the Prompt 14 comparison service.

The fixtures below commit ordinary SQLite rows and use the production storage
facade.  They intentionally do not mock the database, evaluators, workers, or
application: comparison is a read-only operation over those committed records.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    BenchmarkCase,
    DatasetManifest,
    Decision,
    EffectState,
    EvaluationResult,
    ExecutionResult,
    ExecutionStatus,
    MetricDirection,
    MetricValue,
    RunManifest,
    WorkItem,
    WorkItemState,
    deep_unfreeze,
)
from aibench.core.plans import ExecutablePlan
from aibench.services.comparison import ComparisonError, compare_runs, comparison_exit_code
from aibench.services.regression_policy import (
    evaluate_regression_policy,
    parse_regression_policy,
)
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.db import Database
from aibench.storage.repositories import Storage

_TEST_ARTIFACTS: ArtifactStore | None = None


@pytest.fixture(autouse=True)
def _comparison_artifacts(tmp_path: Path) -> None:
    """Use the same test workspace artifact root as the comparison calls."""
    global _TEST_ARTIFACTS
    _TEST_ARTIFACTS = ArtifactStore(tmp_path / "artifacts")


def _profile(
    metric_id: str = "native.exact_match",
    *,
    binding_hash: str | None = None,
    direction: str = "higher",
    value_kind: str = "scalar",
    scope: str = "case",
    aggregation: str = "mean",
    plugin_id: str = "aibench-native",
    plugin_version: str = "1.0.0",
    judge: dict[str, Any] | None = None,
    rubric: dict[str, Any] | None = None,
    instrumentation: dict[str, Any] | None = None,
    parameters_hash: str = "sha256:parameters",
    dependency_lock_hash: str | None = None,
    uses_models: bool = False,
) -> tuple[str, dict[str, Any]]:
    binding = binding_hash or content_hash(
        {"evaluator_id": metric_id, "version": "1.0.0", "params": {}, "rule": None}
    )
    judge = judge or {"kind": "not_used", "digest": None, "verified": True}
    rubric = rubric or {
        "kind": "framework_internal",
        "digest": "sha256:native-rubric",
        "verified": True,
    }
    instrumentation = instrumentation or {
        "kind": "observation_contract",
        "digest": "sha256:instrumentation",
        "verified": True,
    }
    manifest = {
        "evaluator_id": metric_id,
        "version": "1.0.0",
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "value_kind": value_kind,
        "direction": direction,
        "scope": scope,
        "aggregation": aggregation,
        "uses_models": uses_models,
        "requires": [],
        "description": f"test {metric_id}",
    }
    compatibility = {
        "schema_version": "aibench.evaluation-identity/1",
        "metric_id": metric_id,
        "metric_version": "1.0.0",
        "value_kind": value_kind,
        "direction": direction,
        "scope": scope,
        "aggregation": aggregation,
        "binding_hash": binding,
        "parameters_hash": parameters_hash,
        "rule": None,
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "package_name": None,
        "package_version": None,
        "dependency_lock_hash": dependency_lock_hash,
        "judge": judge,
        "rubric": rubric,
        "instrumentation": instrumentation,
        "required_fields": [],
        "final_attempt_rule": "highest_attempt_id_per_case_and_repetition",
    }
    compatibility["compatibility_hash"] = content_hash(
        {key: value for key, value in compatibility.items() if key != "schema_version"}
    )
    return binding, {
        "metric": metric_id,
        "manifest": manifest,
        "params": {},
        "rule": None,
        "compatibility": compatibility,
        "source": "frozen_with_run",
    }


def _result(
    run_id: str,
    scoring_id: str,
    case_id: str,
    repetition: int,
    value: float | None,
    *,
    binding_hash: str,
    metric_id: str = "native.exact_match",
    status: ExecutionStatus = ExecutionStatus.OK,
    decision: Decision = Decision.PASS,
    execution_suffix: str | None = None,
    compatibility: dict[str, Any] | None = None,
) -> EvaluationResult:
    execution_id = execution_suffix or f"{run_id}:{case_id}:r{repetition}:a0"
    return EvaluationResult(
        result_id=f"{scoring_id}:{case_id}:r{repetition}",
        run_id=run_id,
        case_id=case_id,
        metric_id=metric_id,
        metric_version="1.0.0",
        value=None if value is None else MetricValue(kind="scalar", value=value),
        status=status,
        decision=decision,
        scoring_id=scoring_id,
        execution_id=execution_id,
        repetition_id=repetition,
        attempt_number=0,
        binding_hash=binding_hash,
        direction=MetricDirection.HIGHER,
        provenance={
            "plugin_id": "aibench-native",
            "plugin_version": "1.0.0",
            "binding": {"metric": metric_id, "params": {}},
            **({"compatibility": compatibility} if compatibility is not None else {}),
        },
    )


def _seed_run(
    storage: Storage,
    run_id: str,
    *,
    cases: list[BenchmarkCase],
    values: dict[tuple[str, int], float | None],
    application_hash: str = "sha256:app-a",
    scoring_id: str | None = None,
    repetitions: int = 1,
    profile: tuple[str, dict[str, Any]] | None = None,
    extra_passes: list[tuple[str, dict[tuple[str, int], float | None]]] | None = None,
    execution_ids: dict[tuple[str, int], str] | None = None,
    result_execution_ids: dict[tuple[str, int], str] | None = None,
    missing_execution_keys: set[tuple[str, int]] | None = None,
    cached_pass_ids: set[str] | None = None,
    timings: dict[tuple[str, int], dict[str, Any]] | None = None,
    costs: dict[tuple[str, int], float | None] | None = None,
    cached_execution_keys: set[tuple[str, int]] | None = None,
    warmup_repetitions: int = 0,
    warmup_costs: dict[tuple[str, int], float | None] | None = None,
    warmup_timings: dict[tuple[str, int], dict[str, Any]] | None = None,
) -> tuple[str, str]:
    scoring_id = scoring_id or f"engine-{run_id}"
    binding, metric_profile = profile or _profile()
    metric_id = str(metric_profile.get("metric", "native.exact_match")).partition("@")[0]
    result_execution_ids = result_execution_ids or {}
    dataset_hash = content_hash([case.model_dump(mode="json") for case in cases])
    storage.commit_dataset(
        DatasetManifest(
            dataset_id="dataset-test",
            content_hash=dataset_hash,
            case_count=len(cases),
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    storage.commit_cases(dataset_hash, cases)
    assert _TEST_ARTIFACTS is not None
    plan = ExecutablePlan(
        plan_id=f"plan-{run_id}",
        dataset="dataset.jsonl",
        application="application.json",
        repetitions=repetitions,
        warmup_repetitions=warmup_repetitions,
    )
    plan_bytes = plan.model_dump_json().encode("utf-8")
    plan_ref = _TEST_ARTIFACTS.write_bytes(plan_bytes, mime_type="application/json")
    commit_verified_artifact(_TEST_ARTIFACTS, storage, plan_ref)
    storage.commit_run(
        RunManifest(
            run_id=run_id,
            dataset_hash=dataset_hash,
            application_hash=application_hash,
            plan_hash=bytes_hash(plan_bytes),
            parameters={
                "scoring_id": scoring_id,
                "repetitions": repetitions,
                "warmup_repetitions": warmup_repetitions,
                "plan_artifact_id": plan_ref.artifact_id,
                "metric_profiles": {binding: metric_profile},
            },
        ),
        status="completed",
    )
    execution_ids = execution_ids or {}
    timings = timings or {}
    costs = costs or {}
    warmup_costs = warmup_costs or {}
    warmup_timings = warmup_timings or {}
    for case in cases:
        for repetition in range(repetitions):
            key = (case.case_id, repetition)
            execution_id = execution_ids.get(key, f"{run_id}:{case.case_id}:r{repetition}:a0")
            storage.commit_execution_attempt(
                ExecutionResult(
                    execution_id=execution_id,
                    run_id=run_id,
                    case_id=case.case_id,
                    repetition_id=repetition,
                    attempt_id=0,
                    status=ExecutionStatus.OK,
                    output=f"answer {case.case_id}",
                    timing=timings.get(key, {}),
                    cost=costs.get(key),
                    cache={"key": "test-cache"} if key in (cached_execution_keys or set()) else None,
                )
            )
            storage.commit_work_item(
                WorkItem(
                    work_item_id=f"{run_id}:exec:{case.case_id}:r{repetition}",
                    run_id=run_id,
                    task_key=f"exec:{case.case_id}:r{repetition}",
                    kind="execution",
                    state=WorkItemState.SUCCEEDED,
                )
            )
            storage.commit_work_item(
                WorkItem(
                    work_item_id=f"{run_id}:eval:{case.case_id}:r{repetition}:{binding[7:23]}",
                    run_id=run_id,
                    task_key=f"eval:{case.case_id}:r{repetition}:{binding[7:23]}",
                    kind="evaluation",
                    state=WorkItemState.SUCCEEDED,
                )
            )
        for warmup_index in range(warmup_repetitions):
            repetition_id = repetitions + warmup_index
            storage.commit_execution_attempt(
                ExecutionResult(
                    execution_id=f"{run_id}:{case.case_id}:warmup{warmup_index}:a0",
                    run_id=run_id,
                    case_id=case.case_id,
                    repetition_id=repetition_id,
                    attempt_id=0,
                    warmup=True,
                    status=ExecutionStatus.OK,
                    output=f"warmup {case.case_id}",
                    timing=warmup_timings.get((case.case_id, warmup_index), {}),
                    cost=warmup_costs.get((case.case_id, warmup_index)),
                    effect_state=EffectState.COMPLETED,
                )
            )
            storage.commit_work_item(
                WorkItem(
                    work_item_id=f"{run_id}:exec:{case.case_id}:r{repetition_id}",
                    run_id=run_id,
                    task_key=f"exec:{case.case_id}:r{repetition_id}",
                    kind="execution",
                    warmup=True,
                    state=WorkItemState.SUCCEEDED,
                )
            )
    storage.append_run_event(
        run_id,
        "scoring_pass",
        {
            "scoring_id": scoring_id,
            "kind": "engine",
            "metric_profiles": {binding: metric_profile},
        },
    )
    for (case_id, repetition), value in values.items():
        result = _result(
            run_id,
            scoring_id,
            case_id,
            repetition,
            value,
            binding_hash=binding,
            metric_id=metric_id,
            decision=(
                Decision.NOT_EVALUATED
                if value is None
                else (Decision.PASS if value >= 0.5 else Decision.FAIL)
            ),
            execution_suffix=result_execution_ids.get((case_id, repetition)),
            compatibility=metric_profile.get("compatibility"),
        )
        if (case_id, repetition) in (missing_execution_keys or set()):
            result = result.model_copy(
                update={
                    "execution_id": None,
                    "status": ExecutionStatus.SKIPPED,
                    "decision": Decision.NOT_EVALUATED,
                    "value": None,
                }
            )
        if scoring_id in (cached_pass_ids or set()):
            result = result.model_copy(
                update={
                    "provenance": {
                        **deep_unfreeze(result.provenance),
                        "cache": {"key": "test-cache"},
                    }
                }
            )
        storage.commit_metric_result(result)
    for pass_id, pass_values in extra_passes or []:
        storage.append_run_event(
            run_id,
            "scoring_pass",
            {"scoring_id": pass_id, "kind": "rescore", "metric_profiles": {binding: metric_profile}},
        )
        for (case_id, repetition), value in pass_values.items():
            result = _result(
                run_id,
                pass_id,
                case_id,
                repetition,
                value,
                binding_hash=binding,
                metric_id=metric_id,
                decision=(
                    Decision.NOT_EVALUATED
                    if value is None
                    else (Decision.PASS if value >= 0.5 else Decision.FAIL)
                ),
                execution_suffix=result_execution_ids.get((case_id, repetition)),
                compatibility=metric_profile.get("compatibility"),
            )
            if pass_id in (cached_pass_ids or set()):
                result = result.model_copy(
                    update={
                        "provenance": {
                            **deep_unfreeze(result.provenance),
                            "cache": {"key": "test-cache"},
                        }
                    }
                )
            storage.commit_metric_result(result)
        storage.append_run_event(
            run_id,
            "scoring_pass_completed",
            {"scoring_id": pass_id, "result_count": len(pass_values), "status": "completed"},
        )
    return dataset_hash, binding


def _cases(*case_ids: str, changed: set[str] | None = None) -> list[BenchmarkCase]:
    changed = changed or set()
    return [
        BenchmarkCase(
            case_id=case_id,
            input={"question": f"changed {case_id}" if case_id in changed else f"question {case_id}"},
            group_id="group-a" if case_id.startswith("a") else "group-b",
        )
        for case_id in case_ids
    ]


def _values(*items: tuple[str, int, float]) -> dict[tuple[str, int], float]:
    return {(case, rep): value for case, rep, value in items}


def test_compatible_runs_pair_case_repetition_and_report_macro_groups(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1", "b1")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        repetitions=2,
        values=_values(("a1", 0, 1), ("a1", 1, 2), ("b1", 0, 4), ("b1", 1, 5)),
    )
    _seed_run(
        storage,
        "current",
        cases=cases,
        application_hash="sha256:app-b-intentional-change",
        repetitions=2,
        values=_values(("a1", 0, 2), ("a1", 1, 3), ("b1", 0, 4), ("b1", 1, 8)),
    )
    storage.promote_baseline("production", "baseline", "release-manager")

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current", bootstrap_replicates=100)
    aliased = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "@production",
        "current",
        bootstrap_replicates=100,
    )
    named = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "Production",
        "current",
        bootstrap_replicates=100,
    )

    assert report["status"] == "qualified"
    assert aliased["baseline_run_id"] == "baseline"
    assert aliased["baseline_alias"] == "production"
    assert aliased["qualified"] is True
    assert named["baseline_run_id"] == "baseline"
    assert named["baseline_alias"] == "production"
    assert report["qualified"] is True
    assert report["execution_identity"]["classification"] == "fresh_or_unknown_execution_identity"
    assert report["identity_checks"]["application"]["compatible"] is False
    assert report["identity_checks"]["application"]["blocking"] is False
    [comparison] = report["qualified_metric_deltas"]
    assert comparison["denominators"]["paired_selected"] == 4
    assert comparison["case_macro"]["mean_current_minus_baseline"] == pytest.approx(1.25)
    assert {row["group_id"] for row in comparison["groups"]} == {"group-a", "group-b"}
    assert comparison["uncertainty"]["independent_unit"] == "explicit_group_id_or_case_id"
    assert comparison["direction_handling"]["generic_quality_score_calculated"] is False


def test_missing_and_status_losses_are_coverage_not_zero_and_gate_exit_is_one(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1", "a2")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        values=_values(("a1", 0, 1), ("a2", 0, 1)),
    )
    _seed_run(
        storage,
        "current",
        cases=cases,
        values=_values(("a1", 0, 2)),
    )
    # The selected a2 item has a committed evaluator error, not a zero score.
    binding = EvaluationResult.model_validate_json(
        storage.conn.execute(
            "SELECT data FROM metric_results WHERE run_id='current' LIMIT 1"
        ).fetchone()["data"]
    ).binding_hash
    assert binding is not None
    current_record = storage.get_run("current")
    assert current_record is not None
    current_profile = deep_unfreeze(current_record.manifest.parameters)["metric_profiles"][binding]
    storage.commit_metric_result(
        _result(
            "current",
            "engine-current",
            "a2",
            0,
            None,
            binding_hash=binding,
            status=ExecutionStatus.ERROR,
            decision=Decision.NOT_EVALUATED,
            compatibility=current_profile["compatibility"],
        )
    )

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "qualified"
    assert report["qualified"] is True
    assert report["overall_coverage_gate"]["status"] == "fail"
    assert comparison_exit_code(report) == 1
    comparison = report["qualified_metric_deltas"][0]
    assert comparison["sides"]["current"]["status_counts"]["error"] == 1
    assert comparison["denominators"]["complete_numeric_pairs"] == 1
    assert comparison["case_macro"]["mean_current_minus_baseline"] == 1.0


def test_changed_case_content_is_never_paired(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    _seed_run(storage, "baseline", cases=_cases("a1", "b1"), values=_values(("a1", 0, 1), ("b1", 0, 2)))
    _seed_run(
        storage,
        "current",
        cases=_cases("a1", "b1", changed={"b1"}),
        values=_values(("a1", 0, 2), ("b1", 0, 3)),
    )

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current", mode="exploratory")

    assert report["status"] == "exploratory"
    assert report["qualified"] is False
    assert report["identity_checks"]["dataset"]["compatible"] is False
    assert report["identity_checks"]["case_content"]["compatible"] is False
    # Only the unchanged case is available to the explicitly exploratory pair.
    diagnostic = report["exploratory_diagnostics"][0]
    assert diagnostic["denominators"]["paired_selected"] == 1
    assert diagnostic["cases"][0]["case_id"] == "a1"


def test_coverage_gate_includes_unpaired_selected_units() -> None:
    from aibench.services.comparison import _coverage_gate

    gate = _coverage_gate(
        {
            "denominators": {
                "baseline_selected": 2,
                "current_selected": 1,
                "paired_selected": 1,
                "complete_numeric_pairs": 1,
                "coverage": {
                    "complete_numeric_pairs_over_paired_selected": 1.0,
                    "complete_numeric_pairs_over_baseline_selected": 0.5,
                    "complete_numeric_pairs_over_current_selected": 1.0,
                },
            }
        },
        0.95,
    )

    assert gate["status"] == "fail"
    assert gate["complete_numeric_pairs_over_required_selected"] == 0.5
    assert gate["required_selected_denominator"] == 2


def test_undispatched_result_is_coverage_loss_not_lineage_corruption(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(
        storage,
        "current",
        cases=cases,
        values=_values(("a1", 0, 2)),
        missing_execution_keys={("a1", 0)},
    )

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "qualified"
    assert report["identity_checks"]["lineage"]["compatible"] is True
    assert report["coverage_gate"]["status"] == "fail"


def test_same_metric_label_with_different_binding_blocks_strict(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    left_binding, left_profile = _profile(binding_hash="sha256:left-binding")
    right_binding, right_profile = _profile(binding_hash="sha256:right-binding")
    _seed_run(
        storage,
        "baseline",
        cases=_cases("a1"),
        profile=(left_binding, left_profile),
        values=_values(("a1", 0, 1)),
    )
    _seed_run(
        storage,
        "current",
        cases=_cases("a1"),
        profile=(right_binding, right_profile),
        values=_values(("a1", 0, 2)),
    )

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "blocked"
    assert report["qualified"] is False
    assert "same_metric_label_different_binding" in report["warning_codes"]
    assert report["qualified_metric_deltas"] == []
    assert comparison_exit_code(report) == 2


@pytest.mark.parametrize(
    ("left_overrides", "right_overrides", "expected_code"),
    [
        (
            {"instrumentation": {"kind": "observation_contract", "digest": "sha256:left", "verified": True}},
            {"instrumentation": {"kind": "observation_contract", "digest": "sha256:right", "verified": True}},
            "instrumentation_contract_changed",
        ),
        (
            {"judge": {"kind": "configured", "digest": "sha256:left-judge", "verified": True}},
            {"judge": {"kind": "configured", "digest": "sha256:right-judge", "verified": True}},
            "judge_changed",
        ),
        (
            {"rubric": {"kind": "configured", "digest": "sha256:left-rubric", "verified": True}},
            {"rubric": {"kind": "configured", "digest": "sha256:right-rubric", "verified": True}},
            "rubric_changed",
        ),
    ],
)
def test_metric_identity_components_block_strict(
    tmp_path: Path,
    left_overrides: dict[str, Any],
    right_overrides: dict[str, Any],
    expected_code: str,
) -> None:
    storage = Storage(Database.open_in_memory())
    _seed_run(
        storage,
        "baseline",
        cases=_cases("a1"),
        profile=_profile(**left_overrides),
        values=_values(("a1", 0, 1)),
    )
    _seed_run(
        storage,
        "current",
        cases=_cases("a1"),
        profile=_profile(**right_overrides),
        values=_values(("a1", 0, 2)),
    )
    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    assert report["status"] == "blocked"
    assert expected_code in report["warning_codes"]


def test_unknown_model_backed_judge_cannot_qualify_but_exploratory_is_explicit(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    unknown = {"kind": "unknown", "digest": None, "verified": False}
    _seed_run(
        storage,
        "baseline",
        cases=_cases("a1"),
        profile=_profile(metric_id="ragas.faithfulness", uses_models=True, judge=unknown, dependency_lock_hash=None),
        values=_values(("a1", 0, 1)),
    )
    _seed_run(
        storage,
        "current",
        cases=_cases("a1"),
        profile=_profile(metric_id="ragas.faithfulness", uses_models=True, judge=unknown, dependency_lock_hash=None),
        values=_values(("a1", 0, 2)),
    )
    strict = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    exploratory = compare_runs(
        storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current", mode="exploratory"
    )
    assert strict["status"] == "blocked"
    assert "unknown_judge" in strict["warning_codes"]
    assert exploratory["status"] == "exploratory"
    assert exploratory["qualified"] is False
    assert exploratory["exploratory_diagnostics"]


def test_explicit_rescore_selection_and_same_stored_execution_ids(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    dataset_hash, binding = _seed_run(
        storage,
        "baseline",
        cases=cases,
        values=_values(("a1", 0, 1)),
        extra_passes=[("rescore-b", _values(("a1", 0, 2)))],
    )
    # The engine and rescore passes below refer to the same stored execution.
    report = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "baseline",
        "baseline",
        baseline_scoring_id="rescore-b",
        current_scoring_id="engine-baseline",
    )
    assert report["selected_passes"]["baseline_scoring_id"] == "rescore-b"
    assert report["passes"]["baseline"]["kind"] == "rescore"
    assert report["execution_identity"]["same_stored_executions"] is True
    assert report["status"] == "qualified"
    assert binding and dataset_hash


def test_multiple_legacy_rescore_passes_require_an_explicit_selection() -> None:
    from aibench.services.comparison import _Pass, _select_pass

    passes = (
        _Pass(run_id="legacy", scoring_id="rescore-a", kind="rescore", sequence=1),
        _Pass(run_id="legacy", scoring_id="rescore-b", kind="rescore", sequence=2),
    )

    implicit = _select_pass(passes, None)
    explicit = _select_pass(passes, "rescore-b")

    assert implicit is not None
    assert implicit.source == "pass_selection_required"
    assert explicit is not None and explicit.scoring_id == "rescore-b"


def test_judge_stability_groups_all_passes_and_reports_missing_repeats(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        values=_values(("a1", 0, 1)),
        extra_passes=[("repeat-b", _values(("a1", 0, 2)))],
    )
    _seed_run(
        storage,
        "current",
        cases=cases,
        values=_values(("a1", 0, 3)),
        extra_passes=[("repeat-c", _values(("a1", 0, 4)))],
    )
    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    # The service groups each run's passes, so both repeated units are visible.
    assert {row["run_id"] for row in report["judge_stability"]} == {"baseline", "current"}
    baseline = next(row for row in report["judge_stability"] if row["run_id"] == "baseline")
    assert set(baseline["scoring_ids"]) == {"engine-baseline", "repeat-b"}
    assert baseline["denominators"]["observed_pass_count"] == 2
    assert baseline["numeric_scores"]["max_spread"] == pytest.approx(1.0)
    assert "missing_repeats" in baseline


def test_cached_evaluation_is_not_counted_as_an_independent_judge_repeat(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        values=_values(("a1", 0, 1)),
        extra_passes=[("repeat-b", _values(("a1", 0, 2)))],
        cached_pass_ids={"repeat-b"},
    )
    _seed_run(storage, "current", cases=cases, values=_values(("a1", 0, 3)))

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    baseline = next(row for row in report["judge_stability"] if row["run_id"] == "baseline")

    assert set(baseline["scoring_ids"]) == {"engine-baseline", "repeat-b"}
    assert baseline["denominators"]["observed_pass_count"] == 1
    assert baseline["denominators"]["missing_repeat_count"] == 1


def test_cross_framework_matrix_has_no_cross_framework_score(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    left_binding, left = _profile(
        metric_id="deepeval.faithfulness",
        plugin_id="aibench-deepeval",
        judge={"kind": "not_used", "digest": "d", "verified": True},
    )
    right_binding, right = _profile(
        metric_id="ragas.faithfulness",
        plugin_id="aibench-ragas",
        uses_models=True,
        judge={"kind": "configured", "digest": "r", "verified": True},
        dependency_lock_hash="sha256:ragas-lock",
    )
    cases = _cases("a1", "a2")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        profile=(left_binding, left),
        values=_values(("a1", 0, 1), ("a2", 0, 0)),
    )
    storage.append_run_event(
        "baseline",
        "scoring_pass",
        {
            "scoring_id": "ragas-pass",
            "kind": "rescore",
            "metric_profiles": {right_binding: right},
        },
    )
    for case_id, value in (("a1", 0.0), ("a2", 1.0)):
        storage.commit_metric_result(
            _result(
                "baseline",
                "ragas-pass",
                case_id,
                0,
                value,
                binding_hash=right_binding,
                metric_id="ragas.faithfulness",
                decision=Decision.PASS if value >= 0.5 else Decision.FAIL,
                execution_suffix=f"baseline:{case_id}:r0:a0",
            )
        )
    report = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "baseline",
        "baseline",
        baseline_scoring_id="engine-baseline",
        current_scoring_id="ragas-pass",
    )
    [diagnostic] = report["cross_framework"]
    assert diagnostic["matrix"] == {
        "pass_pass": 0,
        "pass_fail": 1,
        "fail_pass": 1,
        "fail_fail": 0,
        "unmeasured": 0,
    }
    assert diagnostic["execution_identity_mismatch_count"] == 0
    assert diagnostic["cross_framework_difference_calculated"] is False
    assert diagnostic["combined_score_calculated"] is False
    assert "delta" not in json.dumps(diagnostic).lower()
    assert diagnostic["baseline_summary"]["score_summary"]["denominator"].endswith("side_only")


def test_comparison_does_not_mutate_file_storage_or_database(tmp_path: Path) -> None:
    db = Database.open(tmp_path / "workspace.db")
    storage = Storage(db)
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(storage, "current", cases=cases, values=_values(("a1", 0, 2)))
    before = {
        table: storage.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("datasets", "cases", "runs", "work_items", "run_events", "metric_results")
    }
    files_before = sorted(path.name for path in (tmp_path).rglob("*") if path.is_file())
    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    after = {
        table: storage.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("datasets", "cases", "runs", "work_items", "run_events", "metric_results")
    }
    assert report["invocation_basis"]["application_invocations"] == 0
    assert after == before
    assert sorted(path.name for path in (tmp_path).rglob("*") if path.is_file()) == files_before
    db.close()


def test_repetition_policy_change_blocks_strict(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, repetitions=1, values=_values(("a1", 0, 1)))
    _seed_run(
        storage,
        "current",
        cases=cases,
        repetitions=2,
        values=_values(("a1", 0, 2), ("a1", 1, 2)),
    )
    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    assert report["status"] == "blocked"
    assert report["identity_checks"]["repetition_policy"]["compatible"] is False
    assert "repetition_policy_changed_or_unknown" in report["warning_codes"]



def _json_default(value: Any) -> Any:
    if hasattr(value, "items"):
        return dict(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    raise TypeError(f"unsupported manifest value: {type(value).__name__}")


def _set_run_parameter(storage: Storage, run_id: str, key: str, value: Any) -> None:
    record = storage.get_run(run_id)
    assert record is not None
    parameters = dict(deep_unfreeze(record.manifest.parameters) or {})
    parameters[key] = value
    manifest = record.manifest.model_copy(update={"parameters": parameters})
    storage.conn.execute(
        "UPDATE runs SET data = ? WHERE run_id = ?",
        (
            json.dumps(
                deep_unfreeze(manifest.model_dump(mode="python")),
                separators=(",", ":"),
                default=_json_default,
            ),
            run_id,
        ),
    )
    storage.conn.commit()


def test_missing_frozen_plan_artifact_blocks_strict_comparison(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(storage, "current", cases=cases, values=_values(("a1", 0, 2)))
    _set_run_parameter(storage, "current", "plan_artifact_id", "missing-plan")

    strict = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    exploratory = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "baseline",
        "current",
        mode="exploratory",
    )

    assert strict["status"] == "blocked"
    assert "frozen_plan_unavailable" in strict["warning_codes"]
    assert exploratory["status"] == "exploratory"
    assert exploratory["qualified"] is False


def test_missing_frozen_application_artifact_blocks_strict(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(storage, "current", cases=cases, values=_values(("a1", 0, 2)))
    _set_run_parameter(storage, "current", "application_artifact_id", "missing-application")

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "blocked"
    assert report["identity_checks"]["application_artifact"]["compatible"] is False


def test_legacy_run_without_frozen_plan_cannot_qualify_strict(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(storage, "current", cases=cases, values=_values(("a1", 0, 2)))
    _set_run_parameter(storage, "baseline", "plan_artifact_id", None)
    _set_run_parameter(storage, "current", "plan_artifact_id", None)

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "blocked"
    assert report["identity_checks"]["repetition_policy"]["compatible"] is False
    assert "repetition_policy_changed_or_unknown" in report["warning_codes"]


def test_case_ids_with_colons_are_parsed_from_the_right_edge(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("tenant:alpha:r1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("tenant:alpha:r1", 0, 1)))
    _seed_run(storage, "current", cases=cases, values=_values(("tenant:alpha:r1", 0, 2)))

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "qualified"
    comparison = report["qualified_metric_deltas"][0]
    assert comparison["denominators"]["paired_selected"] == 1
    assert comparison["cases"][0]["case_id"] == "tenant:alpha:r1"


def test_unfinished_runs_block_strict_comparison(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(storage, "current", cases=cases, values=_values(("a1", 0, 2)))
    storage.update_run_status("current", "running")

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "blocked"
    assert report["identity_checks"]["run_state"]["compatible"] is False
    assert "run_not_finished" in report["warning_codes"]


def test_compact_profile_hash_is_exploratory_only(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    binding, profile = _profile()
    compact = dict(profile)
    compact["compatibility"] = dict(profile["compatibility"])
    compact["compatibility"].pop("schema_version")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        profile=(binding, compact),
        values=_values(("a1", 0, 1)),
    )
    _seed_run(
        storage,
        "current",
        cases=cases,
        profile=(binding, compact),
        values=_values(("a1", 0, 2)),
    )

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "blocked"
    assert "unknown_compatibility_identity" in report["warning_codes"]


def test_legacy_profile_without_compatibility_identity_is_exploratory_only(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    legacy_binding, legacy = _profile()
    legacy.pop("compatibility")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        profile=(legacy_binding, legacy),
        values=_values(("a1", 0, 1)),
    )
    _seed_run(
        storage,
        "current",
        cases=cases,
        profile=(legacy_binding, legacy),
        values=_values(("a1", 0, 2)),
    )

    strict = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")
    exploratory = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "baseline",
        "current",
        mode="exploratory",
    )

    assert strict["status"] == "blocked"
    assert "unknown_compatibility_identity" in strict["warning_codes"]
    assert exploratory["status"] == "exploratory"
    assert exploratory["qualified"] is False


def test_missing_execution_lineage_blocks_strict_comparison(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(
        storage,
        "current",
        cases=cases,
        values=_values(("a1", 0, 2)),
        result_execution_ids={("a1", 0): "current:wrong-execution"},
    )

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "blocked"
    assert report["identity_checks"]["lineage"]["compatible"] is False
    assert "execution_identity_mismatch" in report["warning_codes"]


def test_empty_comparison_cannot_claim_a_quality_delta(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values={})
    _seed_run(storage, "current", cases=cases, values={})

    report = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "baseline",
        "current",
        min_paired_coverage=0,
    )

    assert report["qualified"] is True  # identities are compatible
    assert report["claim_qualified"] is False
    assert report["qualified_metric_deltas"] == []
    assert report["overall_coverage_gate"]["reason_code"] == "no_complete_numeric_pairs"
    assert comparison_exit_code(report) == 1


def test_non_case_scope_is_not_paired_as_case_repetitions(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    episode_binding, episode_profile = _profile(scope="episode")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        profile=(episode_binding, episode_profile),
        values=_values(("a1", 0, 1)),
    )
    _seed_run(
        storage,
        "current",
        cases=cases,
        profile=(episode_binding, episode_profile),
        values=_values(("a1", 0, 2)),
    )

    report = compare_runs(storage, ArtifactStore(tmp_path / "artifacts"), "baseline", "current")

    assert report["status"] == "blocked"
    assert "metric_scope_not_case" in report["warning_codes"]


def test_missing_requested_scoring_pass_is_explicitly_blocked(tmp_path: Path) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1")
    _seed_run(storage, "baseline", cases=cases, values=_values(("a1", 0, 1)))
    _seed_run(storage, "current", cases=cases, values=_values(("a1", 0, 2)))

    report = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "baseline",
        "current",
        baseline_scoring_id="does-not-exist",
    )

    assert report["status"] == "blocked"
    assert "selected_scoring_pass_not_found" in report["warning_codes"]


def test_comparison_validates_inputs_and_missing_runs() -> None:
    storage = Storage(Database.open_in_memory())
    with pytest.raises(ComparisonError):
        compare_runs(storage, None, "missing", "also-missing")
    with pytest.raises(ComparisonError, match="no named baseline"):
        compare_runs(storage, None, "@missing", "also-missing")
    with pytest.raises(ComparisonError):
        compare_runs(storage, None, "b", "c", mode="qualified")  # type: ignore[arg-type]


def test_comparison_reports_performance_and_applies_predeclared_regression_policy(
    tmp_path: Path,
) -> None:
    storage = Storage(Database.open_in_memory())
    cases = _cases("a1", "b1")
    _seed_run(
        storage,
        "baseline",
        cases=cases,
        values=_values(("a1", 0, 1), ("b1", 0, 1)),
        timings={
            ("a1", 0): {
                "wall_ms": 100,
                "started_at": "2024-01-01T00:00:00Z",
                "finished_at": "2024-01-01T00:00:00.100Z",
                "streaming": {
                    "time_to_first_token_ms": 22,
                    "output_tokens": 12,
                    "output_tokens_per_second": 24,
                    "inter_token_latency_ms": {
                        "samples": 3,
                        "observed_intervals": 3,
                        "mean_ms": 5,
                        "p95_ms": 8,
                        "samples_truncated": False,
                    },
                    "integrity": {"complete": True},
                },
            },
            ("b1", 0): {
                "wall_ms": 200,
                "started_at": "2024-01-01T00:00:00.100Z",
                "finished_at": "2024-01-01T00:00:00.300Z",
            },
        },
        costs={("a1", 0): 0.1, ("b1", 0): 0.2},
        warmup_repetitions=1,
        warmup_costs={("a1", 0): 0.03, ("b1", 0): 0.04},
        warmup_timings={
            ("a1", 0): {
                "wall_ms": 50,
                "started_at": "2024-01-01T00:00:00Z",
                "finished_at": "2024-01-01T00:00:00.050Z",
                "streaming": {
                    "time_to_first_token_ms": 11,
                    "output_tokens": 4,
                    "output_tokens_per_second": 16,
                    "inter_token_latency_ms": {"samples": 2, "observed_intervals": 2},
                    "integrity": {"complete": True},
                },
            },
            ("b1", 0): {
                "wall_ms": 50,
                "started_at": "2024-01-01T00:00:00.050Z",
                "finished_at": "2024-01-01T00:00:00.100Z",
            },
        },
    )
    _seed_run(
        storage,
        "current",
        cases=cases,
        application_hash="sha256:app-b-intentional-change",
        values=_values(("a1", 0, 0.5), ("b1", 0, 0.5)),
        timings={
            ("a1", 0): {
                "wall_ms": 180,
                "started_at": "2024-01-01T00:00:01Z",
                "finished_at": "2024-01-01T00:00:01.180Z",
            },
            ("b1", 0): {
                "wall_ms": 280,
                "started_at": "2024-01-01T00:00:01.180Z",
                "finished_at": "2024-01-01T00:00:01.460Z",
            },
        },
        costs={("a1", 0): 0.2, ("b1", 0): 0.35},
        warmup_repetitions=1,
        warmup_costs={("a1", 0): 0.05, ("b1", 0): 0.06},
        warmup_timings={
            ("a1", 0): {
                "wall_ms": 60,
                "started_at": "2024-01-01T00:00:01Z",
                "finished_at": "2024-01-01T00:00:01.060Z",
            },
            ("b1", 0): {
                "wall_ms": 70,
                "started_at": "2024-01-01T00:00:01.060Z",
                "finished_at": "2024-01-01T00:00:01.130Z",
            },
        },
    )

    report = compare_runs(
        storage,
        ArtifactStore(tmp_path / "artifacts"),
        "baseline",
        "current",
        bootstrap_replicates=100,
    )
    performance = report["application_performance"]
    assert report["status"] == "qualified"
    assert performance["baseline"]["latency_p95_ms"] == 200
    assert performance["current"]["latency_p95_ms"] == 280
    assert performance["baseline"]["total_cost_usd"] == 0.37
    assert performance["current"]["total_cost_usd"] == 0.66
    assert performance["baseline"]["warmup_dispatches"] == 2
    assert performance["baseline"]["warmup_cost_usd"] == 0.07
    assert performance["current"]["warmup_cost_usd"] == 0.11
    assert performance["baseline"]["warmup_effect_states"] == {"completed": 2}
    assert performance["baseline"]["latency_statistics"]["p99_ms"] == 200
    assert performance["baseline"]["latency_statistics"]["stddev_ms"] == 50
    assert performance["baseline"]["retry_inclusive_latency"]["p50_ms"] == 100
    assert performance["baseline"]["throughput"]["successful_requests_per_second"] == 6.667
    assert performance["baseline"]["warmup_latency_statistics"]["p99_ms"] == 50
    assert performance["baseline"]["warmup_retry_inclusive_latency"]["p50_ms"] == 50
    assert performance["current"]["warmup_throughput"]["successful_requests_per_second"] == 15.385
    assert performance["baseline"]["streaming_performance"]["requests"] == 1
    assert performance["baseline"]["streaming_performance"]["time_to_first_token_ms"]["p50_ms"] == 22
    assert performance["baseline"]["streaming_performance"]["inter_token_latency_ms"]["mean_ms"] == 5
    assert performance["baseline"]["warmup_streaming_performance"]["requests"] == 1

    policy = parse_regression_policy(
        {
            "schema": "aibench.regression-policy/1",
            "metric_rules": [{"metric_id": "native.exact_match", "max_degradation": 0.4}],
            "max_latency_p95_increase_ms": 79,
            "max_application_cost_increase_usd": 0.28,
        }
    )
    report["regression_gate"] = evaluate_regression_policy(report, policy)
    gate = report["regression_gate"]
    assert gate["status"] == "fail"
    assert gate["rules"][0]["observed"] == pytest.approx(0.5), json.dumps(
        gate["rules"], indent=2
    )
    assert gate["rules"][1]["observed"] == 80
    assert gate["rules"][2]["observed"] == pytest.approx(0.29)
    assert comparison_exit_code(report) == 1


def test_regression_policy_requires_complete_performance_measurements() -> None:
    policy = parse_regression_policy(
        {
            "schema": "aibench.regression-policy/1",
            "max_application_cost_increase_usd": 0,
        }
    )
    report = {
        "status": "qualified",
        "qualified": True,
        "overall_coverage_gate": {"passed": True},
        "application_performance": {
            "baseline": {"total_cost_usd": 0.1},
            "current": {"total_cost_usd": None},
        },
    }

    report["regression_gate"] = evaluate_regression_policy(report, policy)

    assert report["regression_gate"]["status"] == "undetermined"
    assert report["regression_gate"]["rules"][0]["reason"] == (
        "complete_baseline_and_current_measurements_required"
    )
    assert comparison_exit_code(report) == 3
