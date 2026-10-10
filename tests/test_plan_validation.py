"""Plan compiler/validator (07-T2; gate 07-G1): classified findings — invalid, missing
objective information, missing permission — per-case field requirements, selectors,
seeded sampling, aggregation semantics, DAG checks and budgets. All outside any model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import EvaluatorManifest, MetricBinding, MetricDirection
from aibench.core.plans import CaseSelection, ExecutablePlan
from aibench.engine.compile import (
    PlanInvalid,
    analyze_plan,
    compile_plan,
    dag_problems,
    freeze_plan,
    work_graph,
)
from aibench.security.policy import ExecutionPolicy
from tests.planning_support import TRUSTED, registry_with, write_app, write_dataset

ROWS = [
    {"case_id": "a", "input": "q1", "expected_output": "x", "metadata": {"lang": "en"}},
    {"case_id": "b", "input": "q2", "expected_output": "y", "metadata": {"lang": "fr"}},
    {"case_id": "c", "input": "q3", "metadata": {"lang": "en"}},
    {"case_id": "d", "input": "q4", "metadata": {"lang": "de"}},
]


def _plan(**fields: Any) -> ExecutablePlan:
    base: dict[str, Any] = {
        "plan_id": "p",
        "dataset": "data.jsonl",
        "application": "app.json",
        "metrics": [{"metric": "native.exact_match"}],
    }
    return ExecutablePlan.model_validate({**base, **fields})


def _analyze(tmp_path: Path, plan: ExecutablePlan, **kwargs: Any) -> Any:
    write_dataset(tmp_path, ROWS)
    if not (tmp_path / "app.json").exists():
        write_app(tmp_path)
    return analyze_plan(plan, tmp_path, policy=kwargs.pop("policy", TRUSTED), **kwargs)


def _kinds(analysis: Any) -> set[tuple[str, bool]]:
    return {(f.kind, f.blocking) for f in analysis.findings}


def test_zero_warmup_preserves_legacy_frozen_plan_bytes() -> None:
    plan = _plan()
    expected = plan.model_dump(mode="json")
    expected.pop("warmup_repetitions")
    expected_bytes = json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()

    frozen, _ = freeze_plan(plan)
    assert frozen == expected_bytes
    warmed, _ = freeze_plan(plan.model_copy(update={"warmup_repetitions": 1}))
    assert b'"warmup_repetitions":1' in warmed


def test_unknown_evaluator_is_invalid_and_rejected_outside_any_model(tmp_path: Path) -> None:
    analysis = _analyze(tmp_path, _plan(metrics=[{"metric": "deepeval.hallucination_magic"}]))
    [finding] = analysis.findings
    assert finding.kind == "invalid" and "unknown evaluator" in finding.message
    assert not analysis.executable


def test_unavailable_golden_input_is_missing_information_and_blocks(tmp_path: Path) -> None:
    analysis = _analyze(tmp_path, _plan(selection={"case_ids": ["c", "d"]}))
    blocking = analysis.blocking("missing_information")
    expected = (
        "native.exact_match: requires case.reference.answer, but none of the 2 selected "
        "case(s) has it"
    )
    assert [f.message for f in blocking] == [expected]
    # The execution gate refuses it too: zero calls, not "not_applicable" for every case.
    plan_file = tmp_path / "plan.json"
    plan_file.write_text(
        _plan(selection={"case_ids": ["c", "d"]}).model_dump_json(), encoding="utf-8"
    )
    with pytest.raises(PlanInvalid, match="none of the 2 selected"):
        compile_plan(plan_file, policy=TRUSTED)


def test_partial_coverage_is_a_warning_with_counts(tmp_path: Path) -> None:
    analysis = _analyze(tmp_path, _plan())
    assert analysis.executable
    [warning] = [f for f in analysis.findings if not f.blocking]
    assert "present in 2 of 4 selected case(s)" in warning.message
    [coverage] = analysis.coverage
    assert (coverage.eligible_cases, coverage.selected_cases) == (2, 4)


def test_unexposed_observation_is_missing_information(tmp_path: Path) -> None:
    grounded = EvaluatorManifest(
        evaluator_id="fixture.grounded",
        version="1.0.0",
        plugin_id="fixture",
        plugin_version="1",
        description="d",
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=({"path": "execution.retrieved_context"},),  # type: ignore[arg-type]
        default_rule={"comparator": ">=", "threshold": 0.5},  # type: ignore[arg-type]
    )
    policy = ExecutionPolicy(allow_trusted_local=True, allowed_evaluators=("native.*", "fixture.*"))
    analysis = _analyze(
        tmp_path,
        _plan(metrics=[{"metric": "fixture.grounded"}]),
        policy=policy,
        registry=registry_with(grounded),
    )
    assert _kinds(analysis) == {("missing_information", True)}
    assert "does not expose it (output_binding.retrieved_context" in analysis.findings[0].message


def test_permission_and_information_are_reported_separately(tmp_path: Path) -> None:
    write_app(tmp_path, runner="cli")
    analysis = _analyze(
        tmp_path,
        _plan(selection={"case_ids": ["c"]}),
        policy=ExecutionPolicy(),  # no trusted-local grant
    )
    kinds = {f.kind for f in analysis.findings if f.blocking}
    assert kinds == {"missing_permission", "missing_information"}


def test_selectors_filter_on_golden_fields_and_reject_undefined_paths(tmp_path: Path) -> None:
    where = [{"path": "case.metadata.lang", "op": "in", "values": ["en", "fr"]}]
    analysis = _analyze(tmp_path, _plan(selection={"where": where}))
    assert [c.case_id for c in analysis.cases] == ["a", "b", "c"]
    exists = _analyze(tmp_path, _plan(selection={"where": [{"path": "case.reference.answer"}]}))
    assert [c.case_id for c in exists.cases] == ["a", "b"] and not exists.findings

    undefined = _analyze(tmp_path, _plan(selection={"where": [{"path": "case.referenc.answer"}]}))
    assert any(
        f.kind == "invalid" and "unknown evaluation view path" in f.message
        for f in undefined.findings
    )
    empty = _analyze(
        tmp_path,
        _plan(selection={"where": [{"path": "case.metadata.lang", "op": "equals", "value": "jp"}]}),
    )
    assert any(f.message == "the selection contains no cases" for f in empty.findings)


def test_seeded_sampling_is_deterministic_kept_in_order_and_needs_a_seed(tmp_path: Path) -> None:
    picks = [
        [
            c.case_id
            for c in _analyze(tmp_path, _plan(selection={"sample_size": 2, "seed": 7})).cases
        ]
        for _ in range(3)
    ]
    assert picks[0] == picks[1] == picks[2] and len(picks[0]) == 2
    assert picks[0] == sorted(picks[0])  # dataset order preserved
    others = {
        tuple(
            c.case_id
            for c in _analyze(tmp_path, _plan(selection={"sample_size": 2, "seed": s})).cases
        )
        for s in range(10)
    }
    assert len(others) > 1  # the seed matters
    with pytest.raises(ValueError, match="needs an explicit selection.seed"):
        CaseSelection(sample_size=2)
    with pytest.raises(ValueError, match="selection.seed needs selection.sample_size"):
        CaseSelection(seed=7)
    too_many = _analyze(tmp_path, _plan(selection={"sample_size": 9, "seed": 1}))
    assert any("exceeds the 4 case(s)" in f.message for f in too_many.findings)


def test_aggregation_semantics_must_match_the_value_kind(tmp_path: Path) -> None:
    broken = EvaluatorManifest(
        evaluator_id="fixture.broken",
        version="1.0.0",
        plugin_id="fixture",
        plugin_version="1",
        description="d",
        value_kind="category",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
    )
    policy = ExecutionPolicy(allow_trusted_local=True, allowed_evaluators=("native.*", "fixture.*"))
    analysis = _analyze(
        tmp_path,
        _plan(metrics=[{"metric": "fixture.broken"}]),
        policy=policy,
        registry=registry_with(broken),
    )
    assert any(
        f.kind == "invalid" and "aggregation 'mean' needs scalar values" in f.message
        for f in analysis.findings
    )


def test_work_graph_is_a_dag_and_the_checker_finds_cycles_and_undefined_deps() -> None:
    graph = work_graph(["a", "b"], 2, ["sha256:" + "1" * 64])
    assert len(graph) == 8 and dag_problems(graph) == []
    assert all(len(deps) == 1 for key, deps in graph.items() if key.startswith("eval:"))
    assert dag_problems({"x": ("y",), "y": ("x",)}) == ["dependency cycle through x"]
    assert dag_problems({"x": ("missing",)}) == ["x depends on undefined missing"]


def test_budgets_that_cannot_cover_the_plan_are_reported(tmp_path: Path) -> None:
    analysis = _analyze(tmp_path, _plan(budgets={"max_application_calls": 2}))
    assert any(
        f.subject == "budget" and "covers at most 2 of 4 planned executions" in f.message
        for f in analysis.findings
    )


def test_duplicate_and_bad_parameter_bindings_are_invalid(tmp_path: Path) -> None:
    analysis = _analyze(
        tmp_path,
        _plan(
            metrics=[
                {"metric": "native.exact_match"},
                {"metric": "native.exact_match@1.0.0"},
                {"metric": "native.json_schema", "params": {"typo": 1}},
            ]
        ),
    )
    messages = [f.message for f in analysis.findings if f.kind == "invalid"]
    assert any("duplicate binding" in m for m in messages)
    assert any("Additional properties" in m for m in messages)
    assert MetricBinding  # imported for readers: bindings are the plan's metric entries
