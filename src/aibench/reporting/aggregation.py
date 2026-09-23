"""Per-metric aggregation (§12 "Aggregation and comparison", 04-T4).

One summary per metric binding; never an average across different metrics. Every summary
states its denominators, and every selected case lands in exactly one status bucket, so
losing observations shows up as lower coverage instead of a better score.

Buckets (per selected case, per binding):
- `completed`       evaluated, status ok
- `errors`          the evaluator failed (not a low score)
- `cancelled`       evaluation was cancelled
- `not_applicable`  execution ok, but required evidence missing or empty
- `unavailable`     no usable execution to score (app failed, or case not recorded)
- `pending`         planned but not yet resolved (a partial snapshot of an unfinished run)

`eligible = selected - not_applicable - unavailable - pending` (known to be eligible);
`attempted = completed + errors + cancelled`. With `planned`, `selected` is the planned
count rather than the number of results so far, so an unfinished run shows the missing
work as `pending` instead of silently shrinking its denominators.
Coverages are relative to `selected`. Rates and means use `completed` as denominator and
say so. Results are deterministic: inputs are ordered and floats rounded to 6 places.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

from aibench.core.models import (
    Decision,
    EvaluationResult,
    EvaluatorManifest,
    ExecutionStatus,
    deep_unfreeze,
)

_PLACES = 6


def reason_code(reason: str | None) -> str | None:
    """The machine code at the start of a result reason (e.g. `not_applicable`), or None.
    Free text (a judge's explanation, an application's error) may quote case content, so
    only a code is shown or tallied where content is withheld."""
    if not reason:
        return None
    code = reason.split(":", 1)[0].strip()
    return code if re.fullmatch(r"[a-z][a-z0-9_]{0,40}", code) else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, _PLACES)


@dataclass(frozen=True)
class MetricSummary:
    metric_id: str
    metric_version: str
    binding_hash: str
    value_kind: str
    direction: str
    aggregation: str
    selected: int
    eligible: int
    attempted: int
    completed: int
    errors: int
    cancelled: int
    not_applicable: int
    unavailable: int
    eligible_coverage: float | None
    completed_coverage: float | None
    decisions: dict[str, int]
    value_summary: dict[str, Any]
    reasons: dict[str, int] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    pending: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(
    results: Sequence[EvaluationResult],
    *,
    manifest: EvaluatorManifest,
    binding_hash: str,
    params: dict[str, Any] | None = None,
    planned: int | None = None,
    missing: Literal["pending", "unavailable"] = "pending",
) -> MetricSummary:
    """Summarize the results of one binding in one scoring pass. `planned` is the number
    of (case, repetition) items the pass was meant to evaluate, when known. Planned items
    without a result are `pending` (an unfinished pass) or, with `missing="unavailable"`,
    unavailable (a rescore of a run whose missing executions will never exist)."""
    ordered = sorted(results, key=lambda r: (r.case_id, r.repetition_id))
    status = Counter(r.status for r in ordered)
    unavailable = status[ExecutionStatus.SKIPPED]
    not_applicable = status[ExecutionStatus.NOT_APPLICABLE]
    completed = status[ExecutionStatus.OK]
    errors = status[ExecutionStatus.ERROR]
    cancelled = status[ExecutionStatus.CANCELLED]
    if completed + errors + cancelled + not_applicable + unavailable != len(ordered):
        raise ValueError(f"unexpected result statuses for {manifest.evaluator_id}: {dict(status)}")
    if planned is not None and planned < len(ordered):
        raise ValueError(
            f"{manifest.evaluator_id}: {len(ordered)} results exceed the {planned} planned items"
        )
    selected = len(ordered) if planned is None else planned
    pending = selected - len(ordered)
    if missing == "unavailable":
        unavailable, pending = unavailable + pending, 0
    eligible = selected - not_applicable - unavailable - pending

    reasons = Counter(
        reason_code(r.reason) or ("unclassified" if r.reason else r.status.value)
        for r in ordered
        if r.status is not ExecutionStatus.OK
    )
    if missing == "unavailable" and selected > len(ordered):
        reasons["not_executed"] += selected - len(ordered)
    decisions = {d.value: 0 for d in Decision}
    decisions.update(Counter(r.decision.value for r in ordered))
    ok_values = [
        deep_unfreeze(r.value.value) for r in ordered if r.status is ExecutionStatus.OK and r.value
    ]

    return MetricSummary(
        metric_id=manifest.evaluator_id,
        metric_version=manifest.version,
        binding_hash=binding_hash,
        value_kind=manifest.value_kind,
        direction=manifest.direction.value,
        aggregation=manifest.aggregation,
        selected=selected,
        eligible=eligible,
        attempted=completed + errors + cancelled,
        completed=completed,
        errors=errors,
        cancelled=cancelled,
        not_applicable=not_applicable,
        unavailable=unavailable,
        eligible_coverage=_ratio(eligible, selected),
        completed_coverage=_ratio(completed, selected),
        decisions=decisions,
        value_summary=_value_summary(manifest.aggregation, ok_values),
        reasons=dict(sorted(reasons.items())),
        params=dict(params or {}),
        pending=pending,
    )


def _value_summary(aggregation: str, values: list[Any]) -> dict[str, Any]:
    n = len(values)
    if aggregation == "rate":
        true = sum(1 for v in values if v is True)
        return {
            "true": true,
            "false": n - true,
            "rate": _ratio(true, n),
            "denominator": "completed",
        }
    if aggregation == "mean":
        numbers = [
            float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)
        ]
        if not numbers:
            return {"n": 0, "mean": None, "min": None, "max": None, "denominator": "completed"}
        return {
            "n": len(numbers),
            "mean": round(sum(numbers) / len(numbers), _PLACES),
            "min": round(min(numbers), _PLACES),
            "max": round(max(numbers), _PLACES),
            "denominator": "completed",
        }
    if aggregation == "category_counts":
        counts = Counter(str(v) for v in values)
        return {"counts": dict(sorted(counts.items())), "denominator": "completed"}
    return {}  # "none": structured values are reported per case, never averaged
