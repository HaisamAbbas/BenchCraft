from __future__ import annotations

from aibench.core.models import ExecutionResult, ExecutionStatus
from aibench.services.performance import (
    phase_throughput,
    retry_inclusive_latency,
    summarize_latency,
)


def _execution(
    execution_id: str,
    *,
    repetition: int = 0,
    attempt: int = 1,
    warmup: bool = False,
    status: ExecutionStatus = ExecutionStatus.OK,
    start: str | None = None,
    finish: str | None = None,
    wall_ms: float = 100,
) -> ExecutionResult:
    timing = {"wall_ms": wall_ms}
    if start is not None:
        timing["started_at"] = start
    if finish is not None:
        timing["finished_at"] = finish
    return ExecutionResult(
        execution_id=execution_id,
        run_id="run",
        case_id=f"case-{repetition}",
        repetition_id=repetition,
        attempt_id=attempt,
        warmup=warmup,
        status=status,
        timing=timing,
    )


def test_latency_summary_reports_nearest_rank_tail_and_population_dispersion() -> None:
    summary = summarize_latency([1, 2, 3, 4, 5])

    assert summary["samples"] == 5
    assert summary["p50_ms"] == 3
    assert summary["p95_ms"] == summary["p99_ms"] == 5
    assert summary["mean_ms"] == 3
    assert summary["stddev_ms"] == 1.414
    assert summary["iqr_ms"] == 2


def test_retry_inclusive_latency_spans_retry_delay_and_filters_by_phase() -> None:
    first = _execution(
        "first",
        status=ExecutionStatus.ERROR,
        start="2024-01-01T00:00:00Z",
        finish="2024-01-01T00:00:00.100Z",
        wall_ms=100,
    )
    final = _execution(
        "final",
        attempt=2,
        start="2024-01-01T00:00:00.700Z",
        finish="2024-01-01T00:00:01Z",
        wall_ms=300,
    )
    warmup = _execution(
        "warmup",
        repetition=1,
        warmup=True,
        start="2024-01-01T00:00:00Z",
        finish="2024-01-01T00:00:00.050Z",
        wall_ms=50,
    )

    measured = retry_inclusive_latency([first, final, warmup], [final, warmup], warmup=False)
    warmups = retry_inclusive_latency([first, final, warmup], [final, warmup], warmup=True)

    assert measured["p50_ms"] == 1_000
    assert measured["successful_requests"] == 1
    assert measured["missing_measurements"] == 0
    assert warmups["p50_ms"] == 50
    recovered = retry_inclusive_latency(
        [final],
        [final],
        warmup=False,
        incomplete_items={("case-0", 0, False)},
    )
    assert recovered["samples"] == 0
    assert recovered["missing_measurements"] == 1


def test_retry_inclusive_latency_keeps_missing_legacy_timestamps_explicit() -> None:
    final = _execution("legacy")

    summary = retry_inclusive_latency([final], [final], warmup=False)

    assert summary["samples"] == 0
    assert summary["successful_requests"] == 1
    assert summary["missing_measurements"] == 1


def test_retry_inclusive_latency_suppresses_phase_when_recovery_phase_is_unknown() -> None:
    final = _execution(
        "final",
        start="2024-01-01T00:00:00.500Z",
        finish="2024-01-01T00:00:01Z",
    )

    summary = retry_inclusive_latency(
        [final],
        [final],
        warmup=False,
        phase_attribution_incomplete=True,
    )

    assert summary["samples"] == 0
    assert summary["successful_requests"] == 1
    assert summary["missing_measurements"] == 1
    assert summary["phase_attribution_complete"] is False


def test_phase_throughput_counts_successes_over_observed_concurrent_span() -> None:
    first = _execution(
        "first",
        start="2024-01-01T00:00:00Z",
        finish="2024-01-01T00:00:01Z",
    )
    first_retry = _execution(
        "retry",
        repetition=0,
        attempt=2,
        start="2024-01-01T00:00:01.500Z",
        finish="2024-01-01T00:00:02Z",
    )
    second = _execution(
        "second",
        repetition=1,
        start="2024-01-01T00:00:00.500Z",
        finish="2024-01-01T00:00:02Z",
    )

    measured = phase_throughput(
        [first, first_retry, second], [first_retry, second], warmup=False
    )

    assert measured["dispatched_attempts"] == 3
    assert measured["successful_requests"] == 2
    assert measured["wall_seconds"] == 2
    assert measured["successful_requests_per_second"] == 1


def test_phase_throughput_is_unknown_when_legacy_or_uncommitted_calls_lack_timestamps() -> None:
    legacy = _execution("legacy")

    legacy_summary = phase_throughput([legacy], [legacy], warmup=False)
    recovered_summary = phase_throughput(
        [legacy], [legacy], warmup=False, uncommitted_dispatches=1
    )

    assert legacy_summary["successful_requests_per_second"] is None
    assert legacy_summary["missing_dispatch_timestamps"] == 1
    assert legacy_summary["missing_final_timestamps"] == 1
    assert recovered_summary["uncommitted_dispatches"] == 1
    assert recovered_summary["successful_requests_per_second"] is None


def test_phase_throughput_does_not_attribute_unknown_recovery_to_a_phase() -> None:
    known = _execution(
        "known",
        start="2024-01-01T00:00:00Z",
        finish="2024-01-01T00:00:01Z",
    )

    summary = phase_throughput(
        [known],
        [known],
        warmup=True,
        phase_attribution_incomplete=True,
    )

    assert summary["uncommitted_dispatches"] == 0
    assert summary["phase_attribution_complete"] is False
    assert summary["successful_requests_per_second"] is None


def test_latency_summary_excludes_non_finite_negative_and_boolean_samples() -> None:
    summary = summarize_latency([1, float("nan"), float("inf"), -1, True])  # type: ignore[list-item]

    assert summary["samples"] == 1
    assert summary["mean_ms"] == 1
    huge = summarize_latency([1e308, 1e308])
    assert huge["mean_ms"] == 1e308
    assert huge["stddev_ms"] == 0
