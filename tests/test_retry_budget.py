"""Retry classification, backoff and budget ledger semantics (06-T2, 06-T3)."""

from __future__ import annotations

import random

import pytest

from aibench.core.models import (
    Decision,
    EvaluationResult,
    EvaluatorManifest,
    ExecutionResult,
    MetricDirection,
    WorkItemState,
)
from aibench.core.plans import BudgetLimits, RetryPolicy
from aibench.engine.budget import BudgetLedger
from aibench.engine.retry import backoff_delay, classify_evaluation, classify_execution


def _execution(
    status: str = "error",
    kind: str | None = None,
    effect: str = "none_declared",
    http: int | None = None,
    retry_after: float | None = None,
) -> ExecutionResult:
    completeness = (
        {"http_status": {"value": http, "retry_after_seconds": retry_after}} if http else {}
    )
    return ExecutionResult(
        execution_id="e",
        run_id="r",
        case_id="c",
        status=status,
        error_kind=kind,
        effect_state=effect,
        observation_completeness=completeness,
    )


@pytest.mark.parametrize(
    ("result", "retry", "state"),
    [
        (_execution("ok", effect="completed"), False, WorkItemState.SUCCEEDED),
        (_execution(kind="timeout"), True, WorkItemState.FAILED),
        (_execution(kind="transport", effect="not_dispatched"), True, WorkItemState.FAILED),
        (_execution(kind="http_status", http=503), True, WorkItemState.FAILED),
        (_execution(kind="http_status", http=404), False, WorkItemState.FAILED),
        (_execution(kind="binding"), False, WorkItemState.FAILED),
        (_execution(kind="nonzero_exit"), False, WorkItemState.FAILED),
        (_execution(kind="timeout", effect="unknown"), False, WorkItemState.UNKNOWN_EFFECT),
        (_execution(kind="http_status", effect="completed", http=500), False, WorkItemState.FAILED),
        (
            _execution("cancelled", kind="cancelled", effect="unknown"),
            False,
            WorkItemState.UNKNOWN_EFFECT,
        ),
        (_execution("cancelled", kind="cancelled"), False, WorkItemState.CANCELLED),
    ],
)
def test_execution_retry_classification(
    result: ExecutionResult, retry: bool, state: WorkItemState
) -> None:
    verdict = classify_execution(result)
    assert (verdict.retry, verdict.final_state) == (retry, state)


def _evaluation(status: str, reason: str | None = None) -> EvaluationResult:
    return EvaluationResult(
        result_id="x",
        run_id="r",
        case_id="c",
        metric_id="m.x",
        metric_version="1.0.0",
        status=status,
        decision=Decision.NOT_EVALUATED,
        reason=reason,
    )


def _manifest(retries: int = 0) -> EvaluatorManifest:
    return EvaluatorManifest(
        evaluator_id="m.x",
        version="1.0.0",
        plugin_id="p",
        plugin_version="1",
        description="d",
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        internal_retries=retries,
    )


def test_evaluation_retries_never_repeat_valid_results_or_multiply() -> None:
    assert classify_evaluation(_evaluation("ok"), _manifest()).retry is False  # low score stays
    assert (
        classify_evaluation(_evaluation("not_applicable", "missing:x"), _manifest()).retry is False
    )
    assert (
        classify_evaluation(
            _evaluation("error", "timeout:evaluation exceeded 1s"), _manifest()
        ).retry
        is True
    )
    assert (
        classify_evaluation(_evaluation("error", "worker_failed:exited"), _manifest()).retry is True
    )
    assert classify_evaluation(_evaluation("error", "conformance:bad"), _manifest()).retry is False
    multiplied = classify_evaluation(_evaluation("error", "timeout:x"), _manifest(retries=2))
    assert multiplied.retry is False and "not multiplied" in multiplied.reason


def test_backoff_is_exponential_capped_jittered_seeded_and_honours_retry_after() -> None:
    policy = RetryPolicy(initial_backoff_seconds=1, max_backoff_seconds=5, jitter=0)
    assert [backoff_delay(policy, n, random.Random(0)) for n in (1, 2, 3, 4)] == [1, 2, 4, 5]
    jittered = RetryPolicy(initial_backoff_seconds=1, max_backoff_seconds=100, jitter=0.25)
    delays = [backoff_delay(jittered, 3, random.Random(seed)) for seed in range(50)]
    assert all(3.0 <= d <= 5.0 for d in delays) and len(set(delays)) > 1
    assert backoff_delay(jittered, 3, random.Random(7)) == backoff_delay(
        jittered, 3, random.Random(7)
    )
    assert backoff_delay(policy, 1, random.Random(0), retry_after=3) == 3
    assert backoff_delay(policy, 1, random.Random(0), retry_after=60) == 5  # capped


def test_retry_after_is_read_from_the_http_observation() -> None:
    verdict = classify_execution(_execution(kind="http_status", http=429, retry_after=7.0))
    assert verdict.retry and verdict.retry_after_seconds == 7.0


def test_ledger_hard_limits_count_in_flight_reservations() -> None:
    ledger = BudgetLedger(BudgetLimits(max_application_calls=2))
    assert ledger.reserve_application() is None
    assert ledger.reserve_application() is None
    assert ledger.reserve_application() == "max_application_calls=2 reached"  # in flight counts
    ledger.settle_application(dispatched=False, cost=None)  # never reached the app: refunded
    assert ledger.reserve_application() is None
    assert ledger.summary()["application"]["calls"] == 0


def test_ledger_soft_cost_and_unknown_accounting() -> None:
    ledger = BudgetLedger(
        BudgetLimits(
            max_cost_usd=1.0,
            estimated_cost_per_application_call_usd=0.0,
            estimated_cost_per_evaluation_usd=0.4,
        )
    )
    assert ledger.reserve_evaluation() is None
    ledger.settle_evaluation({"latency_ms": 5, "cost": None, "accounting": "unknown"})
    assert ledger.reserve_evaluation() is None
    assert ledger.reserve_evaluation() == "max_cost_usd=1.0 (soft estimate) reached"  # 3 x 0.4
    summary = ledger.summary()
    assert summary["evaluator"]["calls_with_unknown_cost"] == 1
    assert summary["limits"]["soft"] == {"max_cost_usd": 1.0}


def test_ledger_token_limit_is_honest_about_unreported_tokens() -> None:
    ledger = BudgetLedger(BudgetLimits(max_judge_tokens=100))
    for resources in (
        {"latency_ms": 1, "tokens": {"input": 40, "output": 30}},
        {"latency_ms": 1, "accounting": "unknown"},  # counted as unenforced, never as zero
        {"latency_ms": 1, "tokens": {"input": 20, "output": 15}},
    ):
        assert ledger.reserve_evaluation() is None
        ledger.settle_evaluation(resources)
    assert ledger.reserve_evaluation() == "max_judge_tokens=100 reached"
    assert ledger.summary()["unenforced"] == [
        "max_judge_tokens: some evaluations did not report tokens"
    ]
    native = BudgetLedger(BudgetLimits(max_judge_tokens=100))
    native.reserve_evaluation()
    native.settle_evaluation({"latency_ms": 1, "accounting": "complete", "model_calls": 0})
    assert native.summary()["unenforced"] == []  # model-free: zero tokens is measured


def test_wall_time_limit() -> None:
    ledger = BudgetLedger(BudgetLimits(max_wall_seconds=10), elapsed_before=10.5)
    assert ledger.reserve_application() == "max_wall_seconds=10.0 reached"


def test_prior_spend_carries_into_a_resumed_session() -> None:
    """Review finding: tokens and known costs were dropped on resume."""
    limits = BudgetLimits(
        max_application_calls=3,
        max_judge_tokens=100,
        max_cost_usd=10.0,
        estimated_cost_per_application_call_usd=1.0,
    )
    ledger = BudgetLedger(limits)
    ledger.record_prior_application(5.0)  # known cost survives a later unknown one
    ledger.record_prior_application(None)
    ledger.record_prior_application(None)
    ledger.record_prior_evaluation({"latency_ms": 1, "tokens": {"input": 150}})
    ledger.record_prior_elapsed(1.0)
    assert ledger.reserve_application() == "max_application_calls=3 reached"
    assert ledger.reserve_evaluation() == "max_judge_tokens=100 reached"
    summary = ledger.summary()
    assert summary["application"]["known_cost_usd"] == 5.0
    assert summary["application"]["calls_with_unknown_cost"] == 2
    assert summary["projected_cost_usd_estimate"] == 7.0  # 5 known + 2 x 1.0 estimated
    assert summary["elapsed_seconds"] >= 1.0


def test_cost_limit_requires_an_estimate_and_never_counts_unknown_as_zero() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError, match="unknown costs are never counted as zero"):
        BudgetLimits(max_cost_usd=0.01)
    # Model-free evaluators cost a measured zero; unknown judge cost without an estimate
    # is reported as unenforced rather than silently treated as $0.
    ledger = BudgetLedger(
        BudgetLimits(max_cost_usd=0.01, estimated_cost_per_application_call_usd=0.0)
    )
    ledger.record_prior_evaluation({"latency_ms": 1, "accounting": "complete", "model_calls": 0})
    assert ledger.summary()["unenforced"] == []
    ledger.record_prior_evaluation({"latency_ms": 1, "accounting": "unknown"})
    expected = (
        "max_cost_usd: 1 evaluator call(s) had unknown cost and no estimate, "
        "so they are not in the projection"
    )
    assert ledger.summary()["unenforced"] == [expected]
