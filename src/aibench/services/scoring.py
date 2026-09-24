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
from aibench.core.hashes import content_hash
from aibench.core.models import (
    SCHEMA_VERSION,
    ApplicationSpec,
    BenchmarkCase,
    Decision,
    DecisionRule,
    EvaluationCompatibilityIdentity,
    EvaluationResult,
    ExecutionResult,
    ExecutionStatus,
    IdentityComponent,
    MetricBinding,
    MetricValue,
    RedactionClass,
    UsageEvent,
    UsageRole,
    deep_unfreeze,
)
from aibench.engine.cache import evaluation_from_cache, evaluation_key
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


_JUDGE_CONFIG_KEYS = ("judge", "model", "llm", "provider", "factory")
_RUBRIC_CONFIG_KEYS = ("rubric", "rubric_hash", "prompt", "prompt_hash", "criteria")


def _configured_component(
    params: dict[str, Any], keys: tuple[str, ...], *, uses_models: bool
) -> IdentityComponent:
    selected = {key: deep_unfreeze(params[key]) for key in keys if key in params}
    if not uses_models:
        return IdentityComponent(kind="not_used", verified=True)
    if selected:
        return IdentityComponent(
            kind="configured", digest=content_hash(selected), verified=True
        )
    return IdentityComponent(kind="unknown", verified=False)


def _rubric_component(
    params: dict[str, Any], manifest: ResolvedMetric
) -> IdentityComponent:
    selected = {key: params[key] for key in _RUBRIC_CONFIG_KEYS if key in params}
    if selected:
        return IdentityComponent(
            kind="configured", digest=content_hash(selected), verified=True
        )
    return IdentityComponent(
        kind="framework_internal",
        digest=content_hash(
            {
                "plugin_id": manifest.manifest.plugin_id,
                "plugin_version": manifest.manifest.plugin_version,
                "metric_id": manifest.manifest.evaluator_id,
                "metric_version": manifest.manifest.version,
            }
        ),
        verified=True,
    )


def _instrumentation_component(
    metric: ResolvedMetric, application: ApplicationSpec | None
) -> IdentityComponent:
    requirements = sorted(requirement.path for requirement in metric.requirements)
    if application is None:
        return IdentityComponent(
            kind="unknown",
            digest=content_hash({"requirements": requirements, "schema_version": SCHEMA_VERSION}),
            verified=False,
        )
    execution_fields = sorted(
        path.split(".", 1)[1] for path in requirements if path.startswith("execution.")
    )
    output_binding = deep_unfreeze(application.output_binding) or {}
    return IdentityComponent(
        kind="observation_contract",
        digest=content_hash(
            {
                "schema_version": SCHEMA_VERSION,
                "runner": application.runner.value,
                "requirements": requirements,
                "input_binding": deep_unfreeze(application.input_binding) or {},
                "output_binding": {
                    field: output_binding.get(field) for field in execution_fields
                },
                "reset_policy": application.reset_policy.value,
                "environment_digest": application.environment_digest,
            }
        ),
        verified=True,
    )


def evaluation_compatibility_identity(
    metric: ResolvedMetric,
    *,
    application: ApplicationSpec | None = None,
    dependency_lock_hash: str | None = None,
) -> EvaluationCompatibilityIdentity:
    """Build the strict-comparison identity frozen with one scoring pass.

    Parameters are represented by a digest, not copied into chat. Explicit judge/rubric
    configuration is recognized; a model-backed binding with no recognizable judge identity
    is deliberately marked unverified. Instrumentation is the extraction contract, not the
    application target or implementation hash, so an intended application change remains
    comparable when it reports the same observations.
    """
    manifest = metric.manifest
    params = deep_unfreeze(metric.binding.params) or {}
    rule = rule_for(metric.binding, manifest)
    identity: dict[str, Any] = {
        "metric_id": manifest.evaluator_id,
        "metric_version": manifest.version,
        "value_kind": manifest.value_kind,
        "direction": manifest.direction.value,
        "scope": manifest.scope.value,
        "aggregation": manifest.aggregation,
        "binding_hash": metric.binding_hash,
        "parameters_hash": content_hash(params),
        "rule": rule.model_dump(mode="json") if rule else None,
        "plugin_id": manifest.plugin_id,
        "plugin_version": manifest.plugin_version,
        "package_name": manifest.package_name,
        "package_version": manifest.package_version,
        "dependency_lock_hash": dependency_lock_hash,
        "judge": _configured_component(
            params, _JUDGE_CONFIG_KEYS, uses_models=manifest.uses_models
        ).model_dump(mode="json"),
        "rubric": _rubric_component(params, metric).model_dump(mode="json"),
        "instrumentation": _instrumentation_component(metric, application).model_dump(mode="json"),
        "required_fields": sorted(requirement.path for requirement in metric.requirements),
        "final_attempt_rule": FINAL_ATTEMPT_RULE,
    }
    return EvaluationCompatibilityIdentity(
        **identity, compatibility_hash=content_hash(identity)
    )


def metric_profiles(
    metrics: Sequence[ResolvedMetric],
    *,
    application: ApplicationSpec | None = None,
    dependency_lock_hash: str | None = None,
) -> dict[str, dict[str, Any]]:
    """What reports/comparison need to interpret each binding, frozen at scoring time."""
    profiles: dict[str, dict[str, Any]] = {}
    for metric in metrics:
        rule = rule_for(metric.binding, metric.manifest)
        compatibility = evaluation_compatibility_identity(
            metric,
            application=application,
            dependency_lock_hash=dependency_lock_hash,
        )
        profiles[metric.binding_hash] = {
            "metric": metric.binding.metric,
            "manifest": metric.manifest.model_dump(mode="json"),
            "params": deep_unfreeze(metric.binding.params) or {},
            "rule": rule.model_dump(mode="json") if rule else None,
            "compatibility": compatibility.model_dump(mode="json"),
            "source": "frozen_with_run",
        }
    return profiles


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
    application: ApplicationSpec | None = None,
) -> ScoringReport:
    """`application` overrides the catalog lookup (the engine passes the run's frozen spec)."""
    record = storage.get_run(run_id)
    if record is None:
        raise ScoringError(f"no run committed with run_id={run_id!r}")
    application_id = record.manifest.application_id
    if application is None and application_id:
        application = storage.get_application(application_id)
    resolved = registry.validate(bindings, application=application)  # raises on any problem

    executions = select_final_executions(storage.list_execution_attempts(run_id))
    if not executions:
        raise ScoringError(f"run {run_id!r} has no recorded executions to score")
    cases: dict[str, list[BenchmarkCase]] = {}
    for stored in storage.list_cases(record.manifest.dataset_hash):
        cases.setdefault(stored.case_id, []).append(stored)

    report = ScoringReport(scoring_id=f"score-{uuid.uuid4().hex[:12]}", run_id=run_id)
    # Recorded first, so a report can interpret this pass's results even if it is cut short.
    storage.append_run_event(
        run_id,
        "scoring_pass",
        {
            "scoring_id": report.scoring_id,
            "metric_profiles": metric_profiles(
                resolved,
                application=application,
                dependency_lock_hash=record.manifest.dependency_lock_hash,
            ),
            "repeat_reason": "explicit_stored_output_rescore",
            "independent_judge_repeat": "not_proven",
        },
    )
    for metric in resolved:
        scorer = BindingScorer(
            storage,
            artifacts,
            report.scoring_id,
            metric,
            timeout_seconds,
            cancel,
            prepare_timeout_seconds=prepare_timeout_seconds,
            application=application,
            dependency_lock_hash=record.manifest.dependency_lock_hash,
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
    # A pass is complete only after every resolved binding has returned.  The
    # marker lets a later comparison distinguish a finished rescore from a
    # process that crashed after writing the opening scoring_pass event.
    storage.append_run_event(
        run_id,
        "scoring_pass_completed",
        {
            "scoring_id": report.scoring_id,
            "result_count": len(report.results),
            "status": "completed",
        },
    )
    return report


@dataclass(frozen=True)
class MissingExecution:
    """A planned (case, repetition) with no execution to score, e.g. never dispatched
    because a budget ran out, or cancelled. Scored as `skipped` so it stays in the
    denominator as lost coverage (ADR 0003 decision 4)."""

    run_id: str
    case_id: str
    repetition_id: int
    execution_id: str | None = None


class BindingScorer:
    """Scores executions for one metric binding within one scoring pass. Used whole by
    `score_recorded_run`, and item by item (with engine-level retries) by the engine:
    `open` → `score` per attempt (committed to evaluation_attempts) → `finalize` the
    chosen attempt (committed to metric_results) → `close`."""

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
        application: ApplicationSpec | None = None,
        dependency_lock_hash: str | None = None,
    ) -> None:
        self.storage = storage
        self.artifacts = artifacts
        self.scoring_id = scoring_id
        self.metric = metric
        self.manifest = metric.manifest
        self.rule = rule_for(metric.binding, metric.manifest)
        self.compatibility = evaluation_compatibility_identity(
            metric,
            application=application,
            dependency_lock_hash=dependency_lock_hash,
        )
        self.timeout_seconds = timeout_seconds
        self.prepare_timeout_seconds = prepare_timeout_seconds
        # Set to the run's policy hash to enable the evaluation cache (16-T3).
        self.cache_policy_hash: str | None = None
        self._cache_pending: dict[str, str] = {}
        self.cancel = cancel or asyncio.Event()
        self.prepare_error: str | None = None
        self._evaluator: Evaluator | None = None

    async def open(self) -> None:
        """Construct and prepare the evaluator. Construction and prepare() are evaluator
        code too: a failure there becomes a recorded error for every case it would have
        evaluated, never an aborted pass."""
        try:
            self._evaluator = self.metric.factory()
            await asyncio.wait_for(
                self._evaluator.prepare(deep_unfreeze(self.metric.binding.params) or {}),
                self.prepare_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            self.prepare_error = f"evaluator_prepare_failed:{type(exc).__name__}: {exc}"[:500]

    async def score(
        self, execution: ExecutionResult, candidates: list[BenchmarkCase]
    ) -> EvaluationResult:
        """Evaluate one execution and commit the attempt (evaluation_attempts). With the
        evaluation cache on (`cache_policy_hash`), a stored result for the same key is
        copied instead of calling the evaluator (16-T3)."""
        key = self._cache_key(execution, candidates)
        result = self._from_cache(key, execution) if key else None
        if result is None:
            result = await self._score_one(self._evaluator, execution, candidates)
            if key and result.status is ExecutionStatus.OK:
                self._cache_pending[result.result_id] = key
        self.storage.commit_evaluation_attempt(result, attempt_number=result.attempt_number)
        return result

    def _cache_key(self, execution: ExecutionResult, candidates: list[BenchmarkCase]) -> str | None:
        if (
            self.cache_policy_hash is None
            or len(candidates) != 1
            or execution.status is not ExecutionStatus.OK
        ):
            return None
        manifest = self.manifest
        return evaluation_key(
            execution,
            candidates[0],
            binding_hash=self.metric.binding_hash,
            evaluator=f"{manifest.evaluator_id}@{manifest.version}",
            plugin=f"{manifest.plugin_id}=={manifest.plugin_version}",
            policy_hash=self.cache_policy_hash,
        )

    def _from_cache(self, key: str, execution: ExecutionResult) -> EvaluationResult | None:
        entry = self.storage.get_cache_entry("evaluation", key)
        if entry is None:
            return None
        source_run, record = entry
        case_id, _, result_id = record.partition("\t")
        source = next(
            (r for r in self.storage.list_metric_results(source_run, case_id)
             if r.result_id == result_id),
            None,
        )  # fmt: skip
        if source is None:
            return None
        fresh = self._result(execution, EvaluationOutcome(ExecutionStatus.OK))
        return evaluation_from_cache(source, fresh, key=key)

    def score_missing(
        self,
        target: MissingExecution,
        reason: str,
        *,
        status: ExecutionStatus = ExecutionStatus.SKIPPED,
    ) -> EvaluationResult:
        """Record a planned (case, repetition) that was not evaluated: no usable execution
        (`skipped`), or evaluation cancelled (`cancelled`). No evaluator call is made."""
        result = self._result(target, EvaluationOutcome(status, reason=reason))
        self.storage.commit_evaluation_attempt(result, attempt_number=result.attempt_number)
        return result

    def finalize(self, result: EvaluationResult) -> None:
        """Commit the attempt chosen as this item's result (metric_results), and make a
        fresh successful result the cache source for its key."""
        self.storage.commit_metric_result(result)
        key = self._cache_pending.pop(result.result_id, None)
        if key is not None:
            self.storage.put_cache_entry(
                "evaluation", key, result.run_id, f"{result.case_id}\t{result.result_id}"
            )

    async def close(self, warnings: list[str]) -> None:
        """Results are already recorded; a failed cleanup cannot change them, so it is
        reported as a warning instead of discarding the pass."""
        if self._evaluator is None:
            return
        label = f"{self.manifest.evaluator_id}@{self.manifest.version}"
        try:
            await asyncio.wait_for(self._evaluator.close(), self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"{label}: close() failed: {type(exc).__name__}: {exc}"[:500])

    async def score_all(
        self,
        executions: Sequence[ExecutionResult],
        cases: dict[str, list[BenchmarkCase]],
        warnings: list[str],
    ) -> list[EvaluationResult]:
        await self.open()
        results = []
        try:
            for execution in executions:
                result = await self.score(execution, cases.get(execution.case_id, []))
                self.finalize(result)
                results.append(result)
        finally:
            await self.close(warnings)
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
            tokens: dict[str, int] = {}
            for usage in ctx.usage:
                for name, count in usage.tokens.items():
                    tokens[name] = tokens.get(name, 0) + count
            if tokens:
                resources["tokens"] = tokens  # absent means unknown, never zero
        return resources

    def _result(
        self,
        execution: ExecutionResult | MissingExecution,
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
                "compatibility": self.compatibility.model_dump(mode="json"),
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
