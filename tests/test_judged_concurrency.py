"""A plan with model-judged metrics scores several cases at once. One at a time, a 15-case
run with five DeepEval metrics took an hour on a paid endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from aibench.core.plans import ConcurrencyLimits
from aibench.sessions.drafting import JUDGED_EVALUATION_CONCURRENCY, with_judged_concurrency


@dataclass
class _Ctx:
    concurrency: ConcurrencyLimits


def _catalog() -> list[SimpleNamespace]:
    return [
        SimpleNamespace(metric="native.exact_match@1.0.0", uses_models=False),
        SimpleNamespace(metric="deepeval.g_eval@1.1.0", uses_models=True),
    ]


def _proposal(*metrics: str) -> SimpleNamespace:
    return SimpleNamespace(metrics=tuple(SimpleNamespace(metric=m) for m in metrics))


def test_a_judged_metric_raises_evaluation_concurrency_and_leaves_the_rest_alone() -> None:
    ctx = _Ctx(ConcurrencyLimits(application=2))
    raised = with_judged_concurrency(ctx, _proposal("deepeval.g_eval@1.1.0"), _catalog())
    assert raised.concurrency.evaluation == JUDGED_EVALUATION_CONCURRENCY
    assert raised.concurrency.application == 2  # the application's own limit is untouched
    assert ctx.concurrency.evaluation == 1  # the input is not modified


def test_native_metrics_keep_one_case_at_a_time() -> None:
    ctx = _Ctx(ConcurrencyLimits())
    same = with_judged_concurrency(ctx, _proposal("native.exact_match@1.0.0"), _catalog())
    assert same is ctx


def test_a_higher_limit_already_set_is_never_lowered() -> None:
    ctx = _Ctx(ConcurrencyLimits(evaluation=8))
    same = with_judged_concurrency(ctx, _proposal("deepeval.g_eval@1.1.0"), _catalog())
    assert same.concurrency.evaluation == 8
