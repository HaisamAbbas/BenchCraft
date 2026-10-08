"""DeepEval's ArenaGEval: a judge reads two runs' answers to the same case and says which is
better by the criteria the user states. Run by `/compare BASELINE CURRENT --judge "CRITERIA"`,
never by a plan (`consumes: paired_runs`): the other run's answer comes in as
`comparison.output`, beside this run's `execution.output`.

Upstream always names a winner, and a judge leans towards an answer by its position. Each case
is therefore judged twice, with the two answers in both orders: the same winner both times
decides it; a split verdict is a tie. DeepEval masks the contestants' names from the judge.

Upstream shuffles the answers into a random order on every call, so two calls are not two
orders: half the time both show the same order, and a judge's position bias decides the case.
While the arena judges, that shuffle is held still and the two orders are given here.
"""

from __future__ import annotations

import contextlib
import os
import random
from collections.abc import Iterator
from typing import Any

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricDirection,
    MetricValue,
)
from aibench.evaluators.protocol import (
    MISSING,
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench_deepeval._version import __version__
from aibench_deepeval.faithfulness import (
    DEEPEVAL_ENVIRONMENT,
    PINNED_DEEPEVAL,
    _require_pinned_deepeval,
)
from aibench_deepeval.judges import JUDGE_SCHEMA, build_judge, report_judge_usage
from aibench_deepeval.metrics import _text

BASELINE, CURRENT, TIE = "baseline", "current", "tie"


class _InOrder(random.Random):
    """`random` for upstream's arena module with `shuffle` holding the given order."""

    def shuffle(self, x: Any, *args: Any, **kwargs: Any) -> None:
        return None


@contextlib.contextmanager
def _given_order() -> Iterator[None]:
    """Upstream's arena lists the answers in the order given, not a random one. The worker
    judges one case at a time, so nothing else uses that module meanwhile."""
    import deepeval.metrics.arena_g_eval.utils as upstream

    original = upstream.random
    upstream.random = _InOrder()  # type: ignore[assignment]
    try:
        yield
    finally:
        upstream.random = original


_STRINGS = {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}


class ArenaGEval(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="deepeval.arena_g_eval",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="deepeval",
        package_version=PINNED_DEEPEVAL,
        description=(
            f"DeepEval {PINNED_DEEPEVAL} ArenaGEval: a judge says which of two runs' answers to "
            "the same case is better by the criteria the user states (current, baseline or tie)."
        ),
        limitations=(
            "Judge-dependent: verdicts from different judge models are not comparable.",
            (
                "Each case is judged twice, the answers in both orders; a split verdict is a "
                "tie, so a judge's preference for a position does not decide a case."
            ),
            "Runs from /compare on two stored runs, never as a plan metric.",
        ),
        concepts=("custom_criteria",),
        value_kind="category",
        direction=MetricDirection.NONE,
        aggregation="category_counts",
        requires=(
            FieldRequirement(path="case.input"),
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="comparison.output", non_empty=False),
        ),
        default_rule=DecisionRule(comparator="in", categories=(CURRENT, TIE)),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["judge"],
            "anyOf": [{"required": ["criteria"]}, {"required": ["evaluation_steps"]}],
            "properties": {
                "judge": JUDGE_SCHEMA,
                "name": {"type": "string", "minLength": 1, "maxLength": 80},
                "criteria": {"type": "string", "minLength": 1, "maxLength": 4000},
                "evaluation_steps": _STRINGS,
            },
        },
        uses_models=True,
        credentials=("the judge provider's key, passed explicitly to the plugin environment",),
        network_destinations=(
            "the configured judge model's API (none for a local python_factory judge)",
        ),
        internal_retries=0,
        internal_concurrency=1,
        requires_worker=True,
        consumes="paired_runs",
    )

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        os.environ.update(DEEPEVAL_ENVIRONMENT)
        _require_pinned_deepeval()
        import deepeval.metrics  # noqa: F401 - import once up front so failures surface here

        build_judge(self.params["judge"])  # validate the judge before any case runs

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        from deepeval.metrics import ArenaGEval as UpstreamArena
        from deepeval.test_case import ArenaTestCase, Contestant, LLMTestCase, SingleTurnParams

        other = view.get("comparison.output")
        if other is MISSING:
            return EvaluationOutcome.not_applicable("missing:comparison.output")
        answers = {CURRENT: view.get("execution.output"), BASELINE: other}
        for side, answer in answers.items():
            if not isinstance(answer, str) or not answer.strip():
                return EvaluationOutcome.not_applicable(f"unscorable_output:{side}")
        question = _text(view.get("case.input"))
        judge = build_judge(self.params["judge"])
        winners: list[str] = []
        reasons: list[str] = []
        metric: Any = None
        try:
            for order in ((BASELINE, CURRENT), (CURRENT, BASELINE)):
                metric = UpstreamArena(
                    name=self.params.get("name") or "better answer",
                    evaluation_params=[SingleTurnParams.INPUT, SingleTurnParams.ACTUAL_OUTPUT],
                    criteria=self.params.get("criteria"),
                    evaluation_steps=self.params.get("evaluation_steps"),
                    model=judge,
                    async_mode=True,
                    verbose_mode=False,
                )
                contestants = [
                    Contestant(
                        name=side,
                        test_case=LLMTestCase(input=question, actual_output=answers[side]),
                    )
                    for side in order
                ]
                with _given_order():
                    winner = await metric.a_measure(
                        ArenaTestCase(contestants=contestants), _show_indicator=False
                    )
                winners.append(str(winner))
                reasons.append(str(getattr(metric, "reason", "") or ""))
        finally:
            if metric is not None:
                report_judge_usage(ctx, metric, judge)
        verdict = winners[0] if winners[0] == winners[1] else TIE
        split = "" if verdict != TIE else " (the judge picked a different answer in each order)"
        return EvaluationOutcome(
            ExecutionStatus.OK,
            MetricValue(kind="category", value=verdict),
            f"{verdict}{split}: {reasons[0]}"[:2000] if reasons else verdict,
            ("case.input", "execution.output", "comparison.output"),
            {
                "deepeval_version": PINNED_DEEPEVAL,
                "metric": "ArenaGEval",
                "judge": getattr(metric, "evaluation_model", None),
                "winners_by_order": winners,
                "reasons": reasons,
                "verdict": verdict,
            },
        )
