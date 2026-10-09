"""Typed plan overrides remain subject to the executable-plan validators."""

from __future__ import annotations

import pytest

from aibench.core.plans import BudgetLimits, ExecutablePlan
from aibench.engine.compile import (
    PlanInvalid,
    PolicyDenied,
    RunPlanOverrides,
    apply_run_plan_overrides,
    compile_plan,
)
from aibench.security.policy import ExecutionPolicy
from tests.engine_support import Harness


def _plan(**fields: object) -> ExecutablePlan:
    return ExecutablePlan.model_validate(
        {
            "plan_id": "controls",
            "dataset": "cases.jsonl",
            "application": "app.json",
            "selection": {"case_ids": ["a", "b"], "limit": 10},
            **fields,
        }
    )


def test_direct_selection_overrides_preserve_plan_predicates_and_ids() -> None:
    plan = _plan(
        selection={
            "case_ids": ["a", "b"],
            "where": [{"path": "case.metadata.team", "op": "equals", "value": "blue"}],
            "limit": 10,
        }
    )

    limited = apply_run_plan_overrides(plan, RunPlanOverrides(limit=1))
    sampled = apply_run_plan_overrides(
        plan, RunPlanOverrides(sample_size=1, selection_seed=42)
    )

    assert limited.selection.limit == 1
    assert limited.selection.sample_size is None
    assert limited.selection.case_ids == ("a", "b")
    assert limited.selection.where == plan.selection.where
    assert sampled.selection.limit is None
    assert sampled.selection.sample_size == 1 and sampled.selection.seed == 42
    assert sampled.selection.case_ids == ("a", "b")


def test_direct_overrides_update_plan_and_respect_plan_validation() -> None:
    plan = _plan()
    updated = apply_run_plan_overrides(
        plan,
        RunPlanOverrides(
            repetitions=3,
            application_concurrency=4,
            evaluation_concurrency=2,
            max_attempts=1,
            evaluation_timeout_seconds=10,
            max_application_calls=6,
            max_cost_usd=1.5,
            estimated_cost_per_application_call_usd=0.25,
            cache_executions=True,
            cache_evaluations=False,
        ),
    )

    assert updated.repetitions == 3
    assert updated.concurrency.application == 4
    assert updated.concurrency.evaluation == 2
    assert updated.retry.max_attempts == 1
    assert updated.evaluation_timeout_seconds == 10
    assert updated.budgets.max_application_calls == 6
    assert updated.budgets.max_cost_usd == 1.5
    assert updated.budgets.estimated_cost_per_application_call_usd == 0.25
    assert updated.cache.executions is True and updated.cache.evaluations is False

    with pytest.raises(PlanInvalid, match="max_cost_usd needs"):
        apply_run_plan_overrides(plan, RunPlanOverrides(max_cost_usd=1))
    with pytest.raises(PlanInvalid, match="concurrency.application"):
        apply_run_plan_overrides(plan, RunPlanOverrides(application_concurrency=65))
    with pytest.raises(PlanInvalid, match="finite number"):
        apply_run_plan_overrides(plan, RunPlanOverrides(max_wall_seconds=float("inf")))


def test_selection_override_combinations_are_explicit() -> None:
    plan = _plan()
    with pytest.raises(PlanInvalid, match="cannot be used together"):
        apply_run_plan_overrides(plan, RunPlanOverrides(limit=1, sample_size=1))
    with pytest.raises(PlanInvalid, match="requires --sample-size"):
        apply_run_plan_overrides(plan, RunPlanOverrides(selection_seed=9))


def test_direct_budget_override_cannot_exceed_policy_ceiling(tmp_path) -> None:
    harness = Harness(tmp_path)
    plan_path = harness.plan(
        dataset=harness.dataset({"a": "hi", "b": "hi"}),
        application=harness.cli_app(),
    )
    policy = ExecutionPolicy(
        allow_trusted_local=True,
        ceilings=BudgetLimits(max_application_calls=1),
    )

    with pytest.raises(PolicyDenied, match="exceeds the policy ceiling"):
        compile_plan(
            plan_path,
            policy=policy,
            overrides=RunPlanOverrides(max_application_calls=2),
        )
    assert harness.count() == 0
