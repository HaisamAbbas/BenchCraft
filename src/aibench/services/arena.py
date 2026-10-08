"""A judge reads two runs' answers to the same case and says which is better (DeepEval's
ArenaGEval through `/compare BASELINE CURRENT --judge "CRITERIA"`).

The stored comparison says whether scores changed; this says which answers a judge prefers,
by criteria the user states. It reads only recorded answers (the application is never called)
and runs the evaluator like any other: in its plugin's worker, with the project's judge, after
the policy permits it. The verdicts are part of the comparison report, not of either run.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.models import BenchmarkCase, ExecutionResult, ExecutionStatus, deep_unfreeze
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench.registry import ResolvedMetric
from aibench.storage.repositories import Storage

ARENA = "deepeval.arena_g_eval"
VERDICTS = ("current", "baseline", "tie")


class ArenaError(AibenchError):
    """The runs cannot be judged against each other."""


@dataclass(frozen=True)
class Pair:
    case: BenchmarkCase
    baseline: ExecutionResult
    current: ExecutionResult


def _answers(storage: Storage, run_id: str) -> dict[tuple[str, int], ExecutionResult]:
    """The latest successful attempt of each (case, repetition) of a run."""
    found: dict[tuple[str, int], ExecutionResult] = {}
    for attempt in storage.list_execution_attempts(run_id):
        if attempt.status is ExecutionStatus.OK:
            found[(attempt.case_id, attempt.repetition_id)] = attempt
    return found


def pairs(storage: Storage, baseline_run: str, current_run: str) -> list[Pair]:
    """Cases both runs answered, in the current run's dataset order."""
    current = storage.get_run(current_run)
    if current is None or storage.get_run(baseline_run) is None:
        raise ArenaError("both runs must exist to be judged against each other")
    cases = {c.case_id: c for c in storage.list_cases(current.manifest.dataset_hash)}
    theirs, ours = _answers(storage, baseline_run), _answers(storage, current_run)
    found = []
    for key in sorted(ours, key=lambda k: (list(cases).index(k[0]) if k[0] in cases else 0, k)):
        if key in theirs and key[0] in cases:
            found.append(Pair(cases[key[0]], theirs[key], ours[key]))
    return found


async def judge(
    metric: ResolvedMetric, found: list[Pair], *, timeout_seconds: float = 600.0
) -> dict[str, Any]:
    """Each pair judged once by the arena evaluator (which judges both orders itself)."""
    evaluator = metric.factory()
    rows: list[dict[str, Any]] = []
    try:
        await evaluator.prepare(deep_unfreeze(metric.binding.params) or {})
        for pair in found:
            ensure = getattr(evaluator, "ensure_ready", None)
            if ensure is not None:
                await ensure()
            view = EvaluationView(case=pair.case, execution=pair.current, comparison=pair.baseline)
            ctx = EvaluatorContext(run_id=pair.current.run_id, scoring_id="compare-judge")
            try:
                outcome = await asyncio.wait_for(evaluator.evaluate(view, ctx), timeout_seconds)
            except TimeoutError:
                rows.append(_row(pair, "error", f"timeout:no verdict within {timeout_seconds:g} s"))
                continue
            if outcome.status is ExecutionStatus.OK and outcome.value is not None:
                rows.append(_row(pair, str(outcome.value.value), outcome.reason))
            else:
                rows.append(_row(pair, outcome.status.value, outcome.reason))
    finally:
        await evaluator.close()
    counts = Counter(row["verdict"] for row in rows)
    return {
        "judge": metric.binding.params.get("judge", {}).get("model")
        if isinstance(metric.binding.params.get("judge"), dict)
        else None,
        "criteria": metric.binding.params.get("criteria"),
        "pairs": len(found),
        "counts": {name: counts.get(name, 0) for name in (*VERDICTS, "not_applicable", "error")},
        "rows": rows,
    }


def _row(pair: Pair, verdict: str, reason: str | None) -> dict[str, Any]:
    return {
        "case_id": pair.case.case_id,
        "repetition": pair.current.repetition_id,
        "verdict": verdict,
        "reason": (reason or "")[:500],
    }
