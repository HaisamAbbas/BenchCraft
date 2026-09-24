"""Effect-aware retry classification and backoff (§15, 06-T3).

Rules:
- Retry only failures that are both *transient* (timeout, transport, spawn failure,
  HTTP 408/425/429/500/502/503/504) and *provably effect-free*: the application declares no
  effects, or the request never reached it. A failure after an effectful dispatch is
  `unknown_effect` (the app may have acted) and waits for intervention — the harness cannot
  guarantee exactly-once external effects.
- Never retry validation/binding/policy failures, or any evaluation that produced a result:
  a low score is a valid measurement, not a failure (§15).
- Never multiply retries: runners never retry, adapters declare `internal_retries`, and the
  engine does not retry an evaluator that already retries internally.
- Delays grow exponentially, are capped, carry +/- jitter from a seeded generator (so runs
  are reproducible), and honour a server's Retry-After (capped at the maximum backoff).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from aibench.core.models import (
    EffectState,
    ErrorKind,
    EvaluationResult,
    EvaluatorManifest,
    ExecutionResult,
    ExecutionStatus,
    WorkItemState,
    deep_unfreeze,
)
from aibench.core.plans import RetryPolicy

RETRYABLE_ERROR_KINDS = frozenset({ErrorKind.TIMEOUT, ErrorKind.TRANSPORT, ErrorKind.SPAWN_FAILED})
RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
SAFE_TO_REPEAT = frozenset({EffectState.NONE_DECLARED, EffectState.NOT_DISPATCHED})
RETRYABLE_EVALUATION_REASONS = ("timeout:", "worker_failed:", "evaluator_restart_failed:")


@dataclass(frozen=True)
class Verdict:
    """What to do after one attempt."""

    retry: bool
    final_state: WorkItemState  # state if not retried (or if retries are exhausted)
    reason: str
    retry_after_seconds: float | None = None


def _http_status(result: ExecutionResult) -> tuple[int | None, float | None]:
    entry = deep_unfreeze(result.observation_completeness).get("http_status") or {}
    status = entry.get("value")
    retry_after = entry.get("retry_after_seconds")
    return (
        status if isinstance(status, int) else None,
        float(retry_after) if isinstance(retry_after, (int, float)) else None,
    )


def classify_execution(result: ExecutionResult) -> Verdict:
    if result.status is ExecutionStatus.OK:
        return Verdict(False, WorkItemState.SUCCEEDED, "ok")
    if result.status is ExecutionStatus.CANCELLED:
        if result.effect_state is EffectState.UNKNOWN:
            return Verdict(
                False, WorkItemState.UNKNOWN_EFFECT, "cancelled after dispatch; effect unknown"
            )
        return Verdict(False, WorkItemState.CANCELLED, "cancelled")
    kind = result.error_kind
    label = kind.value if kind else "error"
    if result.effect_state is EffectState.UNKNOWN:
        return Verdict(
            False,
            WorkItemState.UNKNOWN_EFFECT,
            f"{label} after the request was dispatched to an effectful application; "
            "the effect may have occurred — reconcile before retrying",
        )
    status, retry_after = _http_status(result)
    transient = kind in RETRYABLE_ERROR_KINDS or (
        kind is ErrorKind.HTTP_STATUS and status in RETRYABLE_HTTP_STATUSES
    )
    if transient and result.effect_state in SAFE_TO_REPEAT:
        return Verdict(True, WorkItemState.FAILED, f"transient {label}", retry_after)
    if transient:
        return Verdict(
            False, WorkItemState.FAILED, f"{label} on an effectful application; not repeated"
        )
    return Verdict(False, WorkItemState.FAILED, f"not retryable: {label}")


def classify_evaluation(result: EvaluationResult, manifest: EvaluatorManifest) -> Verdict:
    if result.status in (
        ExecutionStatus.OK,
        ExecutionStatus.NOT_APPLICABLE,
        ExecutionStatus.SKIPPED,
    ):
        return Verdict(False, WorkItemState.SUCCEEDED, result.status.value)  # low scores stay
    if result.status is ExecutionStatus.CANCELLED:
        return Verdict(False, WorkItemState.CANCELLED, "cancelled")
    reason = result.reason or "error"
    transient = reason.startswith(RETRYABLE_EVALUATION_REASONS)
    if transient and manifest.internal_retries > 0:
        return Verdict(
            False, WorkItemState.FAILED, f"{reason} (evaluator retries internally; not multiplied)"
        )
    if transient:
        return Verdict(True, WorkItemState.FAILED, reason)
    return Verdict(False, WorkItemState.FAILED, reason)


def backoff_delay(
    policy: RetryPolicy, attempt: int, rng: random.Random, retry_after: float | None = None
) -> float:
    """Delay before attempt `attempt + 1`, where `attempt` (>=1) attempts have failed."""
    base = min(policy.max_backoff_seconds, policy.initial_backoff_seconds * 2 ** (attempt - 1))
    jittered = base + base * policy.jitter * (2 * rng.random() - 1)
    delay = max(0.0, min(policy.max_backoff_seconds, jittered))
    if retry_after is not None:
        delay = max(delay, min(retry_after, policy.max_backoff_seconds))
    return delay


def http_status(result: ExecutionResult) -> tuple[int | None, float | None]:
    """The HTTP status and Retry-After seconds an attempt recorded, when it has them."""
    return _http_status(result)
