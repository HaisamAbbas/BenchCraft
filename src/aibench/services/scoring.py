"""Score recorded executions (04-T4), shared by commands, the engine and conversation.

This service has no access to an application runner: it reads stored `ExecutionResult`s
and Golden cases, so rescoring can never invoke the application (04-G3). Bindings are
resolved and validated before any case is evaluated (04-G2).

Per case and binding, exactly one result is persisted, with one status:
- `skipped`        no usable execution (the app failed, or the case is not recorded)
- `not_applicable` required evidence is missing or empty
- `ok` / `error` / `cancelled` from evaluation itself
The decision comes from the frozen rule, never from the evaluator.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.models import (
    BenchmarkCase,
    Decision,
    DecisionRule,
    EvaluationResult,
    ExecutionResult,
    ExecutionStatus,
    MetricBinding,
    MetricValue,
    RedactionClass,
    UsageEvent,
    UsageRole,
    deep_unfreeze,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
    rule_for,
)
from aibench.registry import EvaluatorRegistry, ResolvedMetric
from aibench.reporting.aggregation import MetricSummary, summarize
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import Storage

FINAL_ATTEMPT_RULE = "highest_attempt_id_per_case_and_repetition"
DEFAULT_EVALUATION_TIMEOUT_SECONDS = 60.0
# Evaluator startup (prepare, and rebuilding a worker after a timeout) has its own bound, so
# a slow framework import never eats into a case's evaluation budget.
DEFAULT_PREPARE_TIMEOUT_SECONDS = 300.0
_VALUE_TYPES: dict[str, tuple[type, ...]] = {
    "boolean": (bool,),
    "scalar": (int, float),
    "category": (str,),
}


def _all_finite(value: Any) -> bool:
    stack = [deep_unfreeze(value)]
    while stack:
        item = stack.pop()
        if isinstance(item, float) and not math.isfinite(item):
            return False
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return True


class ScoringError(AibenchError):
    """Scoring could not start (unknown run, nothing recorded)."""


@dataclass
class ScoringReport:
    scoring_id: str
    run_id: str
    summaries: list[MetricSummary] = field(default_factory=list)
    results: list[EvaluationResult] = field(default_factory=list)
    # Problems that could not change any recorded result, e.g. an evaluator's close() failed.
    warnings: list[str] = field(default_factory=list)


def select_final_executions(executions: Sequence[ExecutionResult]) -> list[ExecutionResult]:
    """One execution per (case, repetition): the highest attempt. Earlier attempts stay
    stored; they are simply not the scored one (§15: declare the final-attempt rule)."""
    final: dict[tuple[str, int], ExecutionResult] = {}
    for execution in executions:
        key = (execution.case_id, execution.repetition_id)
        if key not in final or execution.attempt_id > final[key].attempt_id:
            final[key] = execution
    return [final[key] for key in sorted(final)]


def decide(
    status: ExecutionStatus, value: MetricValue | None, rule: DecisionRule | None
) -> Decision:
    if status is not ExecutionStatus.OK or value is None:
        return Decision.NOT_EVALUATED
    if rule is None:
        return Decision.INDETERMINATE
    v = value.value
    if rule.comparator == "is_true":
        passed = v is True
    elif rule.comparator == "in":
        passed = v in rule.categories
    else:
        threshold = rule.threshold
        assert threshold is not None  # enforced by DecisionRule
        passed = {
            ">=": v >= threshold,
            ">": v > threshold,
            "<=": v <= threshold,
            "<": v < threshold,
            "==": v == threshold,
        }[rule.comparator]
    return Decision.PASS if passed else Decision.FAIL


async def score_recorded_run(
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    registry: EvaluatorRegistry,
    run_id: str,
    bindings: Sequence[MetricBinding],
    timeout_seconds: float = DEFAULT_EVALUATION_TIMEOUT_SECONDS,
    prepare_timeout_seconds: float = DEFAULT_PREPARE_TIMEOUT_SECONDS,
    cancel: asyncio.Event | None = None,
) -> ScoringReport:
    record = storage.get_run(run_id)
    if record is None:
        raise ScoringError(f"no run committed with run_id={run_id!r}")
    application_id = record.manifest.application_id
    application = storage.get_application(application_id) if application_id else None
    resolved = registry.validate(bindings, application=application)  # raises on any problem

    executions = select_final_executions(storage.list_execution_attempts(run_id))
    if not executions:
        raise ScoringError(f"run {run_id!r} has no recorded executions to score")
    cases: dict[str, list[BenchmarkCase]] = {}
    for stored in storage.list_cases(record.manifest.dataset_hash):
        cases.setdefault(stored.case_id, []).append(stored)

    report = ScoringReport(scoring_id=f"score-{uuid.uuid4().hex[:12]}", run_id=run_id)
    for metric in resolved:
        scorer = _Scorer(
            storage,
            artifacts,
            report.scoring_id,
            metric,
            timeout_seconds,
            cancel,
            prepare_timeout_seconds=prepare_timeout_seconds,
        )
        metric_results = await scorer.score_all(executions, cases, report.warnings)
        report.results.extend(metric_results)
        report.summaries.append(
            summarize(
                metric_results,
                manifest=metric.manifest,
                binding_hash=metric.binding_hash,
                params=deep_unfreeze(metric.binding.params) or {},
            )
        )
    return report


class _Scorer:
    def __init__(
        self,
        storage: Storage,
        artifacts: ArtifactStore,
        scoring_id: str,
        metric: ResolvedMetric,
        timeout_seconds: float,
        cancel: asyncio.Event | None,
        *,
        prepare_timeout_seconds: float = DEFAULT_PREPARE_TIMEOUT_SECONDS,
    ) -> None:
        self.storage = storage
        self.artifacts = artifacts
        self.scoring_id = scoring_id
        self.metric = metric
        self.manifest = metric.manifest
        self.rule = rule_for(metric.binding, metric.manifest)
        self.timeout_seconds = timeout_seconds
        self.prepare_timeout_seconds = prepare_timeout_seconds
        self.cancel = cancel or asyncio.Event()
        self.prepare_error: str | None = None

    async def score_all(
        self,
        executions: Sequence[ExecutionResult],
        cases: dict[str, list[BenchmarkCase]],
        warnings: list[str],
    ) -> list[EvaluationResult]:
        label = f"{self.manifest.evaluator_id}@{self.manifest.version}"
        evaluator: Evaluator | None = None
        # Construction and prepare() are evaluator code too: a failure there becomes a
        # recorded error for every case it would have evaluated, never an aborted pass.
        try:
            evaluator = self.metric.factory()
            await asyncio.wait_for(
                evaluator.prepare(deep_unfreeze(self.metric.binding.params) or {}),
                self.prepare_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - see comment above
            self.prepare_error = f"evaluator_prepare_failed:{type(exc).__name__}: {exc}"[:500]
        results = []
        try:
            for execution in executions:
                result = await self._score_one(
                    evaluator, execution, cases.get(execution.case_id, [])
                )
                self.storage.commit_evaluation_attempt(result, attempt_number=result.attempt_number)
                self.storage.commit_metric_result(result)
                results.append(result)
        finally:
            if evaluator is not None:
                # Results are already recorded; a failed cleanup cannot change them, so it
                # is reported as a warning instead of discarding the pass.
                try:
                    await asyncio.wait_for(evaluator.close(), self.timeout_seconds)
                except Exception as exc:  # noqa: BLE001
                    warnings.append(f"{label}: close() failed: {type(exc).__name__}: {exc}"[:500])
        return results

    async def _score_one(
        self,
        evaluator: Evaluator | None,
        execution: ExecutionResult,
        candidates: list[BenchmarkCase],
    ) -> EvaluationResult:
        if not candidates:
            return self._result(
                execution, EvaluationOutcome(ExecutionStatus.SKIPPED, reason="case_not_recorded")
            )
        if len(candidates) > 1:
            # Two stored Goldens share this case_id: scoring against either would be a guess.
            return self._result(
                execution, EvaluationOutcome(ExecutionStatus.SKIPPED, reason="duplicate_case_id")
            )
        case = candidates[0]
        if execution.status is not ExecutionStatus.OK:
            kind = f":{execution.error_kind.value}" if execution.error_kind else ""
            return self._result(
                execution,
                EvaluationOutcome(
                    ExecutionStatus.SKIPPED, reason=f"execution_{execution.status.value}{kind}"
                ),
            )
        view = EvaluationView(case=case, execution=execution)
        for requirement in self.metric.requirements:
            state = view.state(requirement.path)
            if state == "missing" or (state == "empty" and requirement.non_empty):
                return self._result(
                    execution, EvaluationOutcome.not_applicable(f"{state}:{requirement.path}")
                )
        if self.prepare_error is not None or evaluator is None:
            return self._result(
                execution, EvaluationOutcome.error(self.prepare_error or "evaluator_unavailable")
            )
        if self.cancel.is_set():
            return self._result(
                execution, EvaluationOutcome(ExecutionStatus.CANCELLED, reason="cancelled")
            )

        ctx = EvaluatorContext(
            run_id=execution.run_id,
            scoring_id=self.scoring_id,
            cancel=self.cancel,
            write_artifact=lambda data, mime: self._write(data, mime, execution.run_id),
        )
        try:
            await asyncio.wait_for(evaluator.ensure_ready(), self.prepare_timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - a lost runtime is an evaluator error
            return self._result(
                execution,
                EvaluationOutcome.error(
                    f"evaluator_restart_failed:{type(exc).__name__}: {exc}"[:500]
                ),
            )
        started = time.perf_counter()
        try:
            outcome = await asyncio.wait_for(evaluator.evaluate(view, ctx), self.timeout_seconds)
            outcome = self._conform(outcome)
            raw = self._serialize_raw(outcome)
        except TimeoutError:
            outcome = EvaluationOutcome.error(
                f"timeout:evaluation exceeded {self.timeout_seconds}s"
            )
        # Deliberately broad: any evaluator bug must become a recorded evaluator error,
        # never a crash of the scoring pass and never a score.
        except Exception as exc:  # noqa: BLE001
            outcome = EvaluationOutcome.error(
                f"evaluator_exception:{type(exc).__name__}: {exc}"[:500]
            )
        else:
            latency_ms = round((time.perf_counter() - started) * 1000, 3)
            return self._result(execution, outcome, ctx=ctx, latency_ms=latency_ms, raw=raw)
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        return self._result(execution, outcome, ctx=ctx, latency_ms=latency_ms)

    @staticmethod
    def _serialize_raw(outcome: EvaluationOutcome) -> bytes | None:
        if outcome.raw is None:
            return None
        return json.dumps(outcome.raw, sort_keys=True, ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )

    def _conform(self, outcome: EvaluationOutcome) -> EvaluationOutcome:
        """Reject an outcome that breaks the manifest's contract instead of storing it."""
        if not isinstance(outcome, EvaluationOutcome):
            return EvaluationOutcome.error(
                f"conformance:evaluator returned {type(outcome).__name__}, not an outcome"
            )
        if outcome.status not in (
            ExecutionStatus.OK,
            ExecutionStatus.ERROR,
            ExecutionStatus.NOT_APPLICABLE,
        ):
            return EvaluationOutcome.error(
                f"conformance:evaluator returned status {outcome.status.value}"
            )
        if outcome.status is not ExecutionStatus.OK:
            return outcome
        value = outcome.value
        kind = self.manifest.value_kind
        if value is None or value.kind != kind:
            got = value.kind if value else None
            return EvaluationOutcome.error(f"conformance:expected a {kind} value, got {got}")
        expected = _VALUE_TYPES.get(kind)
        v = value.value
        if expected and (not isinstance(v, expected) or (kind == "scalar" and isinstance(v, bool))):
            return EvaluationOutcome.error(f"conformance:{kind} value has type {type(v).__name__}")
        if not _all_finite(v):
            return EvaluationOutcome.error("conformance:value contains NaN or infinity")
        return outcome

    def _write(self, data: bytes, mime_type: str, run_id: str) -> str:
        ref = self.artifacts.write_bytes(
            data, mime_type=mime_type, run_id=run_id, redaction=RedactionClass.RESTRICTED
        )
        commit_verified_artifact(self.artifacts, self.storage, ref)
        return ref.artifact_id

    def _resources(self, ctx: EvaluatorContext | None, latency_ms: float | None) -> dict[str, Any]:
        resources: dict[str, Any] = {"latency_ms": latency_ms}
        if ctx is None:
            resources["accounting"] = "not_evaluated"
        elif not self.manifest.uses_models:
            resources.update(model_calls=0, cost=0.0, accounting="complete")
        elif not ctx.usage:
            resources.update(
                model_calls=None,
                cost=None,
                accounting="unknown",
                reason="evaluator reported no usage",
            )
        else:
            costs = [u.cost for u in ctx.usage]
            resources.update(
                model_calls=(
                    None
                    if any(u.calls is None for u in ctx.usage)
                    else sum(u.calls or 0 for u in ctx.usage)
                ),
                cost=None if None in costs else round(sum(c for c in costs if c is not None), 6),
                accounting="reported" if None not in costs else "partial",
            )
        return resources

    def _result(
        self,
        execution: ExecutionResult,
        outcome: EvaluationOutcome,
        *,
        ctx: EvaluatorContext | None = None,
        latency_ms: float | None = None,
        raw: bytes | None = None,
    ) -> EvaluationResult:
        manifest = self.manifest
        result_id = (
            f"{self.scoring_id}:{execution.case_id}:r{execution.repetition_id}:"
            f"{manifest.evaluator_id}@{manifest.version}:{self.metric.binding_hash[7:19]}"
        )
        raw_ref = None if raw is None else self._write(raw, "application/json", execution.run_id)
        if ctx is not None:
            for report in ctx.usage:
                self.storage.commit_usage_event(
                    UsageEvent(
                        usage_event_id=uuid.uuid4().hex,
                        run_id=execution.run_id,
                        role=UsageRole.EVALUATOR,
                        provider=report.provider,
                        tokens=report.tokens or None,
                        calls=report.calls,
                        cost=report.cost,
                    )
                )
        evidence = tuple(
            f"{execution.execution_id}#{path}"
            if path.startswith("execution.")
            else f"case:{execution.case_id}#{path}"
            for path in outcome.evidence
        )
        return EvaluationResult(
            result_id=result_id,
            run_id=execution.run_id,
            case_id=execution.case_id,
            metric_id=manifest.evaluator_id,
            metric_version=manifest.version,
            value=outcome.value,
            status=outcome.status,
            decision=decide(outcome.status, outcome.value, self.rule),
            evidence_refs=evidence,
            provenance={
                "plugin_id": manifest.plugin_id,
                "plugin_version": manifest.plugin_version,
                "binding": self.metric.binding.model_dump(mode="json"),
                "final_attempt_rule": FINAL_ATTEMPT_RULE,
            },
            resources=self._resources(ctx, latency_ms),
            raw_artifact_ref=raw_ref,
            scoring_id=self.scoring_id,
            execution_id=execution.execution_id,
            repetition_id=execution.repetition_id,
            attempt_number=self.storage.next_evaluation_attempt_number(
                execution.run_id,
                execution.case_id,
                execution.repetition_id,
                manifest.evaluator_id,
                self.metric.binding_hash,
            ),
            scope=manifest.scope,
            direction=manifest.direction,
            rule=self.rule,
            binding_hash=self.metric.binding_hash,
            reason=outcome.reason,
        )
