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
            round(normalized_stddev / normalized_mean * 100, 3) if normalized_mean > 0 else None
        ),
    }


def _summarize_rate(values: Sequence[float]) -> dict[str, float | int | None]:
    """Summarize rates without assigning the latency unit suffix to their values."""
    return {
        (key.removesuffix("_ms") if key.endswith("_ms") else key): value
        for key, value in summarize_latency(values).items()
    }


def successful_latency_values(finals: Sequence[ExecutionResult], *, warmup: bool) -> list[float]:
    return [
        value
        for execution in finals
        if execution.warmup is warmup
        and execution.status is ExecutionStatus.OK
        and not execution.cache
        and (value := wall_milliseconds(execution)) is not None
    ]


def stream_performance_summary(
    finals: Sequence[ExecutionResult], *, warmup: bool
) -> dict[str, Any]:
    """Summarize stream observations while preserving their request-level denominators.

    Providers may combine tokenizer tokens in one content delta, and receive buffering may
    coalesce events. Each request therefore records its own interval summary; this function
    weights the overall mean by observed intervals and summarizes request means/p95 values.
    """
    streams: list[Mapping[str, Any]] = []
    for execution in finals:
        if execution.warmup is not warmup or execution.cache:
            continue
        timing = deep_unfreeze(execution.timing) or {}
        stream = timing.get("streaming") if isinstance(timing, Mapping) else None
        if isinstance(stream, Mapping):
            streams.append(stream)

    ttft: list[float] = []
    output_rates: list[float] = []
    request_gap_means: list[float] = []
    request_gap_p95s: list[float] = []
    summarized_intervals = 0
    observed_intervals = 0
    weighted_gap_total = 0.0
    truncated_gap_requests = 0
    known_tokens = 0
    requests_with_tokens = 0
    complete = incomplete = unknown = 0
    for stream in streams:
        if (value := finite_nonnegative(stream.get("time_to_first_token_ms"))) is not None:
            ttft.append(value)
        if (value := finite_nonnegative(stream.get("output_tokens_per_second"))) is not None:
            output_rates.append(value)
        token_count = stream.get("output_tokens")
        if isinstance(token_count, int) and not isinstance(token_count, bool) and token_count >= 0:
            known_tokens += token_count
            requests_with_tokens += 1
        integrity = stream.get("integrity")
        if isinstance(integrity, Mapping) and isinstance(integrity.get("complete"), bool):
            if integrity["complete"]:
                complete += 1
            else:
                incomplete += 1
        else:
            unknown += 1
        gaps = stream.get("inter_token_latency_ms")
        if not isinstance(gaps, Mapping):
            continue
        sample_count = gaps.get("samples")
        mean = finite_nonnegative(gaps.get("mean_ms"))
        observed_count = gaps.get("observed_intervals", sample_count)
        if isinstance(observed_count, int) and not isinstance(observed_count, bool):
            observed_intervals += max(0, observed_count)
        if (
            isinstance(sample_count, int)
            and not isinstance(sample_count, bool)
            and sample_count > 0
        ):
            if mean is not None:
                summarized_intervals += sample_count
                weighted_gap_total += mean * sample_count
                request_gap_means.append(mean)
            if (value := finite_nonnegative(gaps.get("p95_ms"))) is not None:
                request_gap_p95s.append(value)
        if gaps.get("samples_truncated") is True:
            truncated_gap_requests += 1

    return {
        "requests": len(streams),
        "integrity": {"complete": complete, "incomplete": incomplete, "unknown": unknown},
        "time_to_first_token_ms": {
            **summarize_latency(ttft),
            "eligible_streams": len(streams),
            "missing_measurements": len(streams) - len(ttft),
        },
        "inter_token_latency_ms": {
            "observed_intervals": observed_intervals,
            "summarized_intervals": summarized_intervals,
            "mean_ms": (
                round(weighted_gap_total / summarized_intervals, 3)
                if summarized_intervals
                else None
            ),
            "request_mean_ms": summarize_latency(request_gap_means),
            "request_p95_ms": summarize_latency(request_gap_p95s),
            "requests_with_intervals": len(request_gap_means),
            "requests_with_truncated_samples": truncated_gap_requests,
            "definition": (
                "Intervals are client receive-time gaps between non-empty content delta events. "
                "Providers may combine tokens in a delta and network buffering may coalesce events. "
                "The overall mean is interval-weighted; request summaries preserve each stream's "
                "sample distribution."
            ),
        },
        "output_tokens": {
            "total": known_tokens if requests_with_tokens else None,
            "requests_with_usage": requests_with_tokens,
            "requests_without_usage": len(streams) - requests_with_tokens,
        },
        "output_tokens_per_second": {
            **_summarize_rate(output_rates),
            "eligible_streams": len(streams),
            "missing_measurements": len(streams) - len(output_rates),
            "definition": (
                "reported completion_tokens divided by time from first content delta to the "
                "stream completion marker; only complete streams with reported usage are included"
            ),
        },
    }


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
        execution for execution in finals if execution.warmup is warmup and not execution.cache
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
