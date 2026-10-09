"""Rescore dispatch bounds, real CLI paths, and persisted per-pass accounting."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import EvaluatorManifest, ExecutionStatus, MetricBinding
from aibench.core.plans import BudgetLimits, Quota, RetryPolicy
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from aibench.security.policy import ExecutionPolicy
from aibench.services.scoring import ScoringError
from tests.engine_support import Harness
from tests.scoring_support import RUN_ID, Seeded, case, execution


class Judge(Evaluator):
    manifest = EvaluatorManifest.model_validate(
        {
            "evaluator_id": "test.budget_judge",
            "version": "1.0.0",
            "plugin_id": "tests",
            "plugin_version": "1",
            "description": "local test judge",
            "value_kind": "scalar",
            "direction": "higher",
            "aggregation": "mean",
            "uses_models": True,
            "requires": [{"path": "execution.output", "non_empty": False}],
        }
    )
    calls: ClassVar[list[float]] = []
    prepare_delay: ClassVar[float] = 0
    delay: ClassVar[float] = 0
    failures: ClassVar[int] = 0
    silent: ClassVar[bool] = False

    async def prepare(self, params: dict[str, Any]) -> None:
        await asyncio.sleep(self.prepare_delay)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        self.calls.append(time.monotonic())
        await asyncio.sleep(self.delay)
        if not self.silent:
            ctx.report_usage(provider="fake", calls=1, tokens={"input": 3, "output": 2}, cost=0.1)
        if Judge.failures:
            Judge.failures -= 1
            return EvaluationOutcome.error("worker_failed:HTTP 429: local fixture")
        return EvaluationOutcome.ok("scalar", 0.9)


class NeedsEvidence(Judge):
    manifest = Judge.manifest.model_copy(
        update={
            "evaluator_id": "test.needs_evidence",
            "requires": (
                Judge.manifest.requires[0].model_copy(
                    update={"path": "execution.retrieved_context", "non_empty": True}
                ),
            ),
        }
    )


@pytest.fixture
def seeded(tmp_path: Path) -> Seeded:
    Judge.calls.clear()
    Judge.prepare_delay = Judge.delay = 0
    Judge.failures = 0
    Judge.silent = False
    seeded = Seeded(tmp_path)
    seeded.seed([case(c, "yes") for c in "abc"], [execution(c, "yes") for c in "abc"])
    yield seeded
    seeded.storage.db.close()


def score(seeded: Seeded, **kwargs: Any):  # type: ignore[no-untyped-def]
    registry = EvaluatorRegistry.with_native()
    registry.register(Judge)
    return seeded.score([{"metric": Judge.manifest.evaluator_id}], registry=registry, **kwargs)


def test_call_ceiling_is_shared_across_bindings_and_saved(seeded: Seeded) -> None:
    report = seeded.score(
        [
            {"metric": "native.exact_match"},
            {"metric": "native.exact_match", "params": {"case_sensitive": False}},
        ],
        budgets=BudgetLimits(max_evaluator_calls=1),
    )
    assert report.budget["evaluator"]["calls"] == 1
    assert sum(r.status is ExecutionStatus.OK for r in report.results) == 1
    assert len(report.results) == 6
    assert "max_evaluator_calls=1" in (report.stop_reason or "")
    finished = seeded.storage.list_run_events(RUN_ID)[-1]
    assert finished["event_type"] == "scoring_pass_completed"
    assert finished["payload"]["budget"] == report.budget
    assert finished["payload"]["stop_reason"] == report.stop_reason


@pytest.mark.parametrize(
    "limits",
    [
        BudgetLimits(max_evaluator_calls=1),
        BudgetLimits(max_judge_tokens=5),
        BudgetLimits(
            max_cost_usd=0.15,
            estimated_cost_per_application_call_usd=0,
            estimated_cost_per_evaluation_usd=0.1,
        ),
    ],
)
def test_call_token_and_projected_cost_stop_new_invocations(
    seeded: Seeded, limits: BudgetLimits
) -> None:
    report = score(seeded, budgets=limits)
    assert len(Judge.calls) == 1
    assert [r.status for r in report.results] == [
        ExecutionStatus.OK,
        ExecutionStatus.SKIPPED,
        ExecutionStatus.SKIPPED,
    ]
    assert report.budget["evaluator"]["calls"] == 1
    assert report.budget["evaluator"]["known_cost_usd"] == 0.1
    assert report.budget["evaluator"]["reported_tokens"] == 5
    assert report.budget["application"]["calls"] == 0
    assert report.stop_reason


def test_unknown_usage_is_reported_and_cost_estimates_stop_dispatch(seeded: Seeded) -> None:
    Judge.silent = True
    report = score(
        seeded,
        budgets=BudgetLimits(
            max_cost_usd=0.15,
            max_judge_tokens=1,
            estimated_cost_per_application_call_usd=0,
            estimated_cost_per_evaluation_usd=0.1,
        ),
    )
    assert len(Judge.calls) == 1
    assert report.budget["evaluator"]["calls_with_unknown_cost"] == 1
    assert report.budget["projected_cost_usd_estimate"] == 0.1
    assert any("did not report tokens" in note for note in report.budget["unenforced"])


def test_cost_estimate_is_required_before_any_call_or_pass_event(seeded: Seeded) -> None:
    with pytest.raises(ScoringError, match="estimated_cost_per_evaluation_usd"):
        score(
            seeded, budgets=BudgetLimits(max_cost_usd=1, estimated_cost_per_application_call_usd=0)
        )
    assert not Judge.calls
    assert not seeded.storage.list_run_events(RUN_ID)


def test_carried_results_do_not_spend_or_prepare_again(seeded: Seeded) -> None:
    score(seeded)
    Judge.calls.clear()
    Judge.prepare_delay = 30
    report = score(seeded, budgets=BudgetLimits(max_evaluator_calls=1), carry_forward=True)
    assert report.carried == 3
    assert not Judge.calls
    assert report.budget["evaluator"]["calls"] == 0
    assert report.stop_reason is None


def test_missing_evidence_does_not_consume_the_next_eligible_call(seeded: Seeded) -> None:
    registry = EvaluatorRegistry.with_native()
    registry.register(Judge)
    registry.register(NeedsEvidence)
    report = seeded.score(
        [{"metric": NeedsEvidence.manifest.evaluator_id}, {"metric": Judge.manifest.evaluator_id}],
        registry=registry,
        budgets=BudgetLimits(max_evaluator_calls=1),
    )
    assert all(r.status is ExecutionStatus.NOT_APPLICABLE for r in report.results[:3])
    assert len(Judge.calls) == 1


def test_wall_limit_stops_after_current_evaluation(seeded: Seeded) -> None:
    Judge.delay = 0.05
    report = score(seeded, budgets=BudgetLimits(max_wall_seconds=0.02))
    assert len(Judge.calls) == 1
    assert "max_wall_seconds" in (report.stop_reason or "")


def test_wall_limit_expiring_during_prepare_prevents_evaluation(seeded: Seeded) -> None:
    Judge.prepare_delay = 0.05
    report = score(seeded, budgets=BudgetLimits(max_wall_seconds=0.02))
    assert not Judge.calls
    assert report.budget["evaluator"]["calls"] == 0
    assert "max_wall_seconds" in (report.stop_reason or "")


def test_rate_quota_is_shared_and_backpressure_delays_retry(seeded: Seeded) -> None:
    Judge.failures = 1
    report = score(
        seeded,
        retry=RetryPolicy(max_attempts=2, initial_backoff_seconds=0, jitter=0),
        quotas=(
            Quota(
                name="fake", applies_to="evaluator:*", requests_per_second=20, backoff_seconds=0.08
            ),
        ),
    )
    assert len(Judge.calls) == 4
    assert Judge.calls[1] - Judge.calls[0] >= 0.07
    assert all(b - a >= 0.04 for a, b in zip(Judge.calls[1:], Judge.calls[2:], strict=False))
    assert report.quotas[0]["started"] == 4
    assert report.quotas[0]["backpressure_events"] == 1
    assert report.budget["evaluator"]["calls"] == 4
    assert len(seeded.storage.list_evaluation_attempts(RUN_ID)) == 4
    assert len(seeded.storage.list_metric_results(RUN_ID, scoring_id=report.scoring_id)) == 3


def test_retry_cannot_bypass_call_ceiling(seeded: Seeded) -> None:
    Judge.failures = 1
    report = score(
        seeded,
        budgets=BudgetLimits(max_evaluator_calls=1),
        retry=RetryPolicy(max_attempts=3, initial_backoff_seconds=0, jitter=0),
    )
    assert len(Judge.calls) == 1
    assert report.budget["evaluator"]["calls"] == 1
    assert len(seeded.storage.list_evaluation_attempts(RUN_ID)) == 4
    assert report.stop_reason


def test_cancel_while_waiting_for_quota_stops_promptly(seeded: Seeded) -> None:
    async def go():  # type: ignore[no-untyped-def]
        registry = EvaluatorRegistry.with_native()
        registry.register(Judge)
        from aibench.services.scoring import score_recorded_run

        cancel = asyncio.Event()

        async def stop():  # type: ignore[no-untyped-def]
            await asyncio.sleep(0.04)
            cancel.set()

        task = asyncio.create_task(stop())
        try:
            return await asyncio.wait_for(
                score_recorded_run(
                    storage=seeded.storage,
                    artifacts=seeded.artifacts,
                    registry=registry,
                    run_id=RUN_ID,
                    bindings=[MetricBinding(metric=Judge.manifest.evaluator_id)],
                    cancel=cancel,
                    quotas=(
                        Quota(name="slow", applies_to="evaluator:*", requests_per_second=0.01),
                    ),
                ),
                1,
            )
        finally:
            await task

    report = asyncio.run(go())
    assert len(Judge.calls) == 1
    assert [r.status for r in report.results[1:]] == [ExecutionStatus.CANCELLED] * 2


@pytest.mark.parametrize("command", ["evaluate", "score"])
def test_cli_rescore_obeys_declared_or_frozen_budget_without_rerunning_app(
    tmp_path: Path, command: str
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi", "b": "hi"}),
        application=h.cli_app(),
        budgets={"max_evaluator_calls": 1},
        plan_id="bounded-測試",
    )
    run_id = h.create(plan)
    h.execute(run_id)
    assert h.count() == 2
    if command == "evaluate":
        policy = tmp_path / "bounded-policy.json"
        policy.write_text(
            ExecutionPolicy(ceilings=BudgetLimits(max_evaluator_calls=1)).model_dump_json(),
            encoding="utf-8",
        )
        args = ["evaluate", run_id, "--plan", str(plan), "--policy", str(policy)]
    else:
        metrics = tmp_path / "metrics.json"
        metrics.write_text(
            json.dumps({"metrics": [{"metric": "native.exact_match"}]}), encoding="utf-8"
        )
        args = ["score", run_id, "--metrics", str(metrics)]
    result = CliRunner().invoke(app, [*args, "--workspace", str(h.workspace.root.parent), "--json"])
    assert result.exit_code == 3, result.output
    data = json.loads(result.output)
    assert data["budget"]["evaluator"]["calls"] == 1
    assert data["summaries"][0]["completed"] == 1
    assert "max_evaluator_calls=1" in data["stop_reason"]
    assert h.count() == 2
    stored = CliRunner().invoke(
        app,
        [
            "report",
            run_id,
            "--workspace",
            str(h.workspace.root.parent),
            "--format",
            "json",
            "--out",
            "-",
        ],
    )
    assert stored.exit_code == 0, stored.output
    rescored_pass = next(
        p
        for p in json.loads(stored.output)["scoring_passes"]
        if p["scoring_id"] == data["scoring_id"]
    )
    assert rescored_pass["budget"] == data["budget"]
    assert rescored_pass["stop_reason"] == data["stop_reason"]
