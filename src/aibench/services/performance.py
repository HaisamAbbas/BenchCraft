"""Stable latency, retry-lifecycle, and throughput summaries for recorded app calls."""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from math import fsum, sqrt
from typing import Any

from aibench.core.models import ExecutionResult, ExecutionStatus, deep_unfreeze


def finite_nonnegative(value: Any) -> float | None:
    """Accept finite numeric measurements, excluding bools and negative durations."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def timing_value(execution: ExecutionResult, name: str) -> Any:
    timing = deep_unfreeze(execution.timing) or {}
    return timing.get(name) if isinstance(timing, Mapping) else None


def wall_milliseconds(execution: ExecutionResult) -> float | None:
    return finite_nonnegative(timing_value(execution, "wall_ms"))


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    try:
        return instant.astimezone(UTC)
    except (OverflowError, ValueError):
        return None


def started_at(execution: ExecutionResult) -> datetime | None:
    return _instant(timing_value(execution, "started_at"))


def finished_at(execution: ExecutionResult) -> datetime | None:
    return _instant(timing_value(execution, "finished_at"))


def nearest_rank(values: Sequence[float], percentile: float) -> float | None:
    """Nearest-rank percentile; returns an observed sample without interpolation."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def summarize_latency(values: Sequence[float]) -> dict[str, float | int | None]:
    """Summarize milliseconds with explicit population-dispersion definitions."""
    finite = [number for value in values if (number := finite_nonnegative(value)) is not None]
    if not finite:
        return {
            "samples": 0,
            "min_ms": None,
            "mean_ms": None,
            "p50_ms": None,
            "p75_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
            "stddev_ms": None,
            "iqr_ms": None,
            "coefficient_of_variation_percent": None,
        }
    scale = max(finite)
    normalized = [value / scale for value in finite] if scale else finite
    normalized_mean = fsum(normalized) / len(normalized)
    mean = scale * normalized_mean if scale else 0.0
    normalized_stddev = sqrt(
        fsum((value - normalized_mean) ** 2 for value in normalized) / len(normalized)
    )
    stddev = scale * normalized_stddev if scale else 0.0
    p25 = nearest_rank(finite, 25)
    p75 = nearest_rank(finite, 75)
    return {
        "samples": len(finite),
        "min_ms": round(min(finite), 3),
        "mean_ms": round(mean, 3),
        "p50_ms": round(nearest_rank(finite, 50) or 0.0, 3),
        "p75_ms": round(p75, 3) if p75 is not None else None,
        "p95_ms": round(nearest_rank(finite, 95) or 0.0, 3),
        "p99_ms": round(nearest_rank(finite, 99) or 0.0, 3),
        "max_ms": round(max(finite), 3),
        "stddev_ms": round(stddev, 3),
        "iqr_ms": round(p75 - p25, 3) if p25 is not None and p75 is not None else None,
        "coefficient_of_variation_percent": (
            round(normalized_stddev / normalized_mean * 100, 3)
            if normalized_mean > 0
            else None
        ),
    }


def successful_latency_values(
    finals: Sequence[ExecutionResult], *, warmup: bool
) -> list[float]:
    return [
        value
        for execution in finals
        if execution.warmup is warmup
        and execution.status is ExecutionStatus.OK
        and not execution.cache
        and (value := wall_milliseconds(execution)) is not None
    ]


def _work_key(execution: ExecutionResult) -> tuple[str, int, bool]:
    return execution.case_id, execution.repetition_id, execution.warmup


def retry_inclusive_latency(
    dispatched_attempts: Sequence[ExecutionResult],
    finals: Sequence[ExecutionResult],
    *,
    warmup: bool,
    incomplete_items: set[tuple[str, int, bool]] | None = None,
    phase_attribution_incomplete: bool = False,
) -> dict[str, Any]:
    """Measure successful work from its first dispatched attempt to final completion.

    UTC timestamps make the measurement resumable across processes and include retry
    delays and session gaps. Missing/invalid historical timestamps stay explicit.
    """
    eligible = [
        execution
        for execution in finals
        if execution.warmup is warmup
        and execution.status is ExecutionStatus.OK
        and not execution.cache
    ]
    attempts_by_item: dict[tuple[str, int, bool], list[ExecutionResult]] = defaultdict(list)
    for attempt in dispatched_attempts:
        if attempt.warmup is warmup and not attempt.cache:
            attempts_by_item[_work_key(attempt)].append(attempt)
    values: list[float] = []
    if not phase_attribution_incomplete:
        for final in eligible:
            if _work_key(final) in (incomplete_items or set()):
                continue
            attempts = attempts_by_item.get(_work_key(final), [])
            if not attempts:
                continue
            first = min(attempts, key=lambda item: item.attempt_id)
            start = started_at(first)
            finish = finished_at(final)
            if start is None or finish is None:
                continue
            elapsed = (finish - start).total_seconds() * 1000
            if math.isfinite(elapsed) and elapsed >= 0:
                values.append(elapsed)
    return {
        **summarize_latency(values),
        "successful_requests": len(eligible),
        "missing_measurements": len(eligible) - len(values),
        "phase_attribution_complete": not phase_attribution_incomplete,
        "definition": (
            "first dispatched attempt start through final successful attempt completion; "
            "includes retries, retry delays, and time between resumed sessions; summaries are "
            "incomplete when recovered dispatches cannot be assigned to this phase"
        ),
    }


def phase_throughput(
    dispatched_attempts: Sequence[ExecutionResult],
    finals: Sequence[ExecutionResult],
    *,
    warmup: bool,
    uncommitted_dispatches: int = 0,
    phase_attribution_incomplete: bool = False,
) -> dict[str, Any]:
    """Observed successful requests per second across the phase's wall-clock span."""
    phase_attempts = [
        execution
        for execution in dispatched_attempts
        if execution.warmup is warmup and not execution.cache
    ]
    phase_finals = [
        execution
        for execution in finals
        if execution.warmup is warmup and not execution.cache
    ]
    starts = [instant for item in phase_attempts if (instant := started_at(item)) is not None]
    finishes = [instant for item in phase_finals if (instant := finished_at(item)) is not None]
    missing_dispatch_timestamps = len(phase_attempts) - len(starts) + uncommitted_dispatches
    missing_final_timestamps = len(phase_finals) - len(finishes)
    duration = (
        (max(finishes) - min(starts)).total_seconds()
        if starts and finishes and max(finishes) >= min(starts)
        else None
    )
    successful = sum(item.status is ExecutionStatus.OK for item in phase_finals)
    complete = (
        missing_dispatch_timestamps == 0
        and missing_final_timestamps == 0
        and not phase_attribution_incomplete
    )
    return {
        "successful_requests": successful,
        "dispatched_attempts": len(phase_attempts),
        "failed_requests": len(phase_finals) - successful,
        "uncommitted_dispatches": uncommitted_dispatches,
        "phase_attribution_complete": not phase_attribution_incomplete,
        "missing_dispatch_timestamps": missing_dispatch_timestamps,
        "missing_final_timestamps": missing_final_timestamps,
        "wall_seconds": round(duration, 3) if duration is not None else None,
        "successful_requests_per_second": (
            round(successful / duration, 3)
            if complete and duration is not None and duration > 0
            else None
        ),
        "definition": (
            "successful final requests divided by the observed phase span from first dispatch "
            "to last final completion; retries and concurrent calls are included"
        ),
    }
