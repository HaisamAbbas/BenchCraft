"""Example domain-authored evaluator: does the answer state the correct refund window?

A support team knows its refund window (a case's `expectations.refund_days`). This oracle
classifies each answer instead of scoring it on a made-up scale:

- `correct`          states exactly the expected number of days
- `wrong_window`     states a different number of days
- `no_window_stated` gives no number of days at all
- `unusable_output`  the answer is not text (null, object, list)

Load it with `--custom-evaluator examples/evaluators/refund_window.py --trust-local-code`
(loading a file executes it, so it must be trusted explicitly).
"""

from __future__ import annotations

import re

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)

_DAYS = re.compile(r"\b(\d{1,4})\s*(?:-\s*)?(?:calendar\s+|business\s+)?days?\b", re.IGNORECASE)


class RefundWindowOracle(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="acme.refund_window",
        version="1.0.0",
        plugin_id="acme.support_evaluators",
        plugin_version="0.1.0",
        description="Classifies whether an answer states the expected refund window in days.",
        limitations=("Only recognises windows written as '<N> days'; 'a month' is not parsed.",),
        value_kind="category",
        direction=MetricDirection.NONE,
        aggregation="category_counts",
        requires=(
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="case.expectations.refund_days"),
        ),
        default_rule=DecisionRule(comparator="in", categories=("correct",)),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        output = view.get("execution.output")
        expected = view.get("case.expectations.refund_days")
        if not isinstance(output, str):
            # The app answered without text: a failed answer, never a skipped case.
            return EvaluationOutcome.ok(
                "category",
                "unusable_output",
                evidence=("execution.output",),
                raw={"output_type": type(output).__name__},
            )
        if isinstance(expected, bool) or not isinstance(expected, int):
            return EvaluationOutcome.error("expectations.refund_days must be an integer")
        stated = sorted({int(n) for n in _DAYS.findall(output)})
        if not stated:
            category = "no_window_stated"
        elif stated == [expected]:
            category = "correct"
        else:
            category = "wrong_window"
        return EvaluationOutcome.ok(
            "category",
            category,
            evidence=("execution.output", "case.expectations.refund_days"),
            raw={"stated_days": stated, "expected_days": expected},
        )


EVALUATORS = (RefundWindowOracle,)
