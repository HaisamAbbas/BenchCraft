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
import contextlib
import json
import math
import random
import re
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from aibench.core.errors import AibenchError
from aibench.core.hashes import bytes_hash, content_hash
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
    WorkItem,
    deep_unfreeze,
)
from aibench.core.plans import BudgetLimits, ExecutablePlan, Quota, ReleaseGate, RetryPolicy
from aibench.engine.budget import BudgetLedger
from aibench.engine.cache import evaluation_from_cache, evaluation_key
from aibench.engine.quota import QuotaGate
from aibench.engine.retry import backoff_delay, classify_evaluation
from aibench.evaluators.protocol import (
    EPISODE_TURNS,
    EXECUTION_TRACE,
    MISSING,
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
    rule_for,
)
from aibench.observations.otel import TREE_FORMAT, Trace, parse_otlp, span_tree
from aibench.registry import EvaluatorRegistry, ResolvedMetric
from aibench.reporting.aggregation import MetricSummary, summarize
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import Storage

FINAL_ATTEMPT_RULE = "highest_attempt_id_per_case_and_repetition"
DEFAULT_EVALUATION_TIMEOUT_SECONDS = 60.0
# A model-judged metric's per-case budget (see `ExecutablePlan.model_evaluation_timeout_seconds`).
DEFAULT_MODEL_EVALUATION_TIMEOUT_SECONDS = 600.0
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
    # Results carried forward from earlier passes instead of evaluated again (see
    # `score_recorded_run(carry_forward=True)`).
    carried: int = 0
    budget: dict[str, Any] = field(default_factory=dict)
    quotas: list[dict[str, object]] = field(default_factory=list)
    stop_reason: str | None = None
    gates: list[dict[str, Any]] = field(default_factory=list)
    outcome: dict[str, Any] = field(default_factory=dict)
    exit_code: int = 0


def scoring_pass_outcome(
    summaries: Sequence[MetricSummary], gates: Sequence[ReleaseGate] = ()
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Shared automation result. A low score is complete; errors, missing work or
    cancellation are incomplete. Release gates fail with exit 1 only after all work has
    been evaluated. The returned exit code is shared by CLI and chat rescoring.
    """
    unhealthy = {
        "no_metrics": int(not summaries),
        "errors": sum(summary.errors for summary in summaries),
        "cancelled": sum(summary.cancelled for summary in summaries),
        "unavailable": sum(summary.unavailable for summary in summaries),
        "pending": sum(summary.pending for summary in summaries),
    }
    unhealthy = {key: value for key, value in unhealthy.items() if value}
    complete = not unhealthy
    gate_results: list[dict[str, Any]] = []
    for gate in gates:
        summary = summaries[gate.binding] if gate.binding < len(summaries) else None
        entry: dict[str, Any] = {
            "gate_id": gate.gate_id,
            "binding": gate.binding,
            "min_pass_rate": gate.min_pass_rate,
            "min_completed_coverage": gate.min_completed_coverage,
            "denominator": "selected",
        }
        if summary is None:
            gate_results.append(
                {**entry, "status": "undecided", "reason": "no results for this binding"}
            )
        elif not complete:
            gate_results.append(
                {**entry, "status": "undecided", "reason": "the scoring pass is incomplete"}
            )
        elif summary.selected == 0:
            gate_results.append(
                {**entry, "selected": 0, "status": "fail", "reason": "no selected cases"}
            )
        else:
            passes = summary.decisions.get("pass", 0)
            failures = []
            if gate.min_pass_rate is not None and passes / summary.selected < gate.min_pass_rate:
                failures.append(f"pass rate {passes}/{summary.selected} below {gate.min_pass_rate}")
            if (
                gate.min_completed_coverage is not None
                and summary.completed / summary.selected < gate.min_completed_coverage
            ):
                failures.append(
                    f"completed coverage {summary.completed}/{summary.selected} below "
                    f"{gate.min_completed_coverage}"
                )
            gate_results.append(
                {
                    **entry,
                    "selected": summary.selected,
                    "passes": passes,
                    "completed": summary.completed,
                    "pass_rate": round(passes / summary.selected, 6),
                    "completed_coverage": summary.completed_coverage,
                    "status": "fail" if failures else "pass",
                    "reason": "; ".join(failures) or None,
                }
            )
    failed = [gate["gate_id"] for gate in gate_results if gate["status"] == "fail"]
    undecided = [gate["gate_id"] for gate in gate_results if gate["status"] == "undecided"]
    outcome = {
        "complete": complete,
        "unhealthy_work": unhealthy,
        "gates_failed": failed,
        "gates_undecided": undecided,
    }
    exit_code = 3 if not complete else 1 if failed else 0
    return outcome, gate_results, exit_code


class RescoreDispatch:
    """Serial stored-output dispatch using the engine's budget and quota primitives.

    Every scoring pass has its own allowance, separate from the original run's spend.
    Only work that reaches an evaluator is settled as spend; carried and inapplicable
    results never reserve a call. Quota waits and retry backoff consume wall allowance.
    """

    def __init__(self, budgets: BudgetLimits, quotas: Sequence[Quota], retry: RetryPolicy) -> None:
        self.ledger = BudgetLedger(budgets)
        self.gates = [QuotaGate(quota) for quota in quotas]
        self.retry = retry
        self.rng = random.Random(0)
        self.stop_reason: str | None = None

    async def pause(self, seconds: float, cancel: asyncio.Event) -> None:
        deadline = time.monotonic() + seconds
        while not cancel.is_set() and time.monotonic() < deadline:
            wall = self.ledger.limits.max_wall_seconds
            if wall is not None and self.ledger.elapsed() >= wall:
                return
            try:
                await asyncio.wait_for(cancel.wait(), min(0.1, deadline - time.monotonic()))
            except TimeoutError:
                pass

    async def acquire(self, evaluator_id: str, cancel: asyncio.Event) -> str | None:
        gates = [gate for gate in self.gates if gate.applies_to("evaluation", evaluator_id)]
        while not cancel.is_set():
            denial = self.ledger.reserve_evaluation()
            if denial is not None:
                self.stop_reason = self.stop_reason or denial
                return f"not_evaluated:{denial}"
            wait = max((gate.wait_seconds() for gate in gates), default=0.0)
            if wait <= 0:
                for gate in gates:
                    gate.start()
                return None
            self.ledger.settle_evaluation(None)
            await self.pause(wait, cancel)
        return "cancelled"

    def wall_denial(self) -> str | None:
        wall = self.ledger.limits.max_wall_seconds
        if wall is not None and self.ledger.elapsed() >= wall:
            self.stop_reason = self.stop_reason or f"max_wall_seconds={wall} reached"
            return f"not_evaluated:{self.stop_reason}"
        return None

    def finish(self, evaluator_id: str, result: EvaluationResult | None) -> None:
        # An unexpected interruption after reservation may have reached the evaluator.
        resources = dict(result.resources) if result is not None else {"latency_ms": 0}
        self.ledger.settle_evaluation(resources)
        status = (
            re.search(
                r"\b(?:HTTP(?:_status)?|status)[ :=]*(429|503)\b",
                result.reason or "",
                re.IGNORECASE,
            )
            if result
            else None
        )
        for gate in self.gates:
            if gate.applies_to("evaluation", evaluator_id):
                gate.finish()
                if status is not None:
                    gate.backpressure(None)


CARRIED_NOTE = (
    "an earlier pass's finished result for the same stored answer and metric settings, "
    "carried forward instead of evaluated again; nothing was called for it in this pass"
)


def is_carried(result: EvaluationResult) -> bool:
    return "carried_forward" in (deep_unfreeze(result.provenance) or {})


_JUDGE_CONFIG_KEYS = ("judge", "model", "llm", "provider", "factory")
_RUBRIC_CONFIG_KEYS = ("rubric", "rubric_hash", "prompt", "prompt_hash", "criteria")


def _configured_component(
    params: dict[str, Any], keys: tuple[str, ...], *, uses_models: bool
) -> IdentityComponent:
    selected = {key: deep_unfreeze(params[key]) for key in keys if key in params}
    if not uses_models:
        return IdentityComponent(kind="not_used", verified=True)
    if selected:
        return IdentityComponent(kind="configured", digest=content_hash(selected), verified=True)
    return IdentityComponent(kind="unknown", verified=False)


def _rubric_component(params: dict[str, Any], manifest: ResolvedMetric) -> IdentityComponent:
    selected = {key: params[key] for key in _RUBRIC_CONFIG_KEYS if key in params}
    if selected:
        return IdentityComponent(kind="configured", digest=content_hash(selected), verified=True)
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
                "output_binding": {field: output_binding.get(field) for field in execution_fields},
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
    return EvaluationCompatibilityIdentity(**identity, compatibility_hash=content_hash(identity))


def declared_dependency_identity(metrics: Sequence[ResolvedMetric]) -> str | None:
    """Freeze the declared plugin/package and worker-environment identity.

    This is deliberately a direct-dependency identity, not a claim that transitive wheels
    are reproducible.  Scoring passes recompute it from the metrics/worker specs they
    actually resolved; a rescore therefore cannot inherit a stale lock from the original
    run manifest or silently use a different declared interpreter/target.
    """

    entries = []
    for metric in metrics:
        worker_spec = getattr(metric.factory, "spec", None)
        if not metric.manifest.uses_models and worker_spec is None:
            continue
        worker_lock_hash = getattr(worker_spec, "dependency_lock_hash", None)
        worker_extra_paths = getattr(worker_spec, "extra_paths", ())
        worker_extra_paths_hash = getattr(worker_spec, "extra_paths_hash", None)
        worker_runtime_identity = getattr(worker_spec, "python_runtime_identity", None)
        if worker_extra_paths and not worker_extra_paths_hash:
            # Configured import roots are executable implementation inputs too.
            return None
        if worker_spec is not None and not worker_lock_hash:
            # A worker path/target identifies where code runs, not its installed dependencies.
            return None
        if worker_spec is not None and not worker_runtime_identity:
            # The same venv path can be upgraded to a different Python runtime in place.
            return None
        if worker_spec is None and metric.manifest.uses_models and not (
            metric.manifest.package_name and metric.manifest.package_version
        ):
            return None
        environment = None
        if worker_spec is not None:
            environment = {
                "python": str(worker_spec.python),
                "target": worker_spec.target,
                "extra_paths": [str(path) for path in worker_spec.extra_paths],
            }
        entries.append(
            {
                "metric_id": metric.manifest.evaluator_id,
                "plugin_id": metric.manifest.plugin_id,
                "plugin_version": metric.manifest.plugin_version,
                "package_name": metric.manifest.package_name,
                "package_version": metric.manifest.package_version,
                "worker_dependency_lock_hash": worker_lock_hash,
                "worker_extra_paths_hash": worker_extra_paths_hash,
                "worker_python_runtime_identity": worker_runtime_identity,
                "environment": environment,
            }
        )
    return content_hash(sorted(entries, key=lambda item: item["metric_id"])) if entries else None


def _verified_producer_identity(
    result: EvaluationResult,
    earlier_by_id: Mapping[str, EvaluationResult],
    seen: frozenset[str] = frozenset(),
) -> EvaluationCompatibilityIdentity | None:
    """Resolve and validate the identity of the evaluator that produced a stored value.

    Earlier carry-forward records created before producer identity was recorded link to a
    source result; follow that chain instead of trusting their pass's possibly relabelled
    compatibility identity. Missing, malformed, incomplete or cyclic lineage fails closed.
    """
    if result.result_id in seen:
        return None
    provenance = deep_unfreeze(result.provenance) or {}
    raw_identity: Mapping[str, Any] | None = None
    if "carried_forward" in provenance:
        lineage = provenance["carried_forward"]
        if not isinstance(lineage, Mapping):
            return None
        producer = lineage.get("producer_compatibility")
        if isinstance(producer, Mapping):
            raw_identity = producer
        else:
            source_id = lineage.get("source_result_id")
            source = earlier_by_id.get(source_id) if isinstance(source_id, str) else None
            if source is None:
                return None
            identity = _verified_producer_identity(source, earlier_by_id, seen | {result.result_id})
            claimed_hash = lineage.get("producer_compatibility_hash")
            if identity is None or (
                claimed_hash is not None and claimed_hash != identity.compatibility_hash
            ):
                return None
            return identity
    else:
        candidate = provenance.get("compatibility")
        if isinstance(candidate, Mapping):
            raw_identity = candidate
    if not isinstance(raw_identity, Mapping):
        return None
    try:
        identity = EvaluationCompatibilityIdentity.model_validate(raw_identity)
    except ValidationError:
        return None
    # The historical record stores the canonical content hash without the schema marker
    # and hash fields themselves. Verify it before relying on its claimed producer identity.
    payload = {
        key: value
        for key, value in raw_identity.items()
        if key not in {"schema_version", "compatibility_hash"}
    }
    if content_hash(payload) != identity.compatibility_hash:
        return None
    if not all(
        component.verified
        for component in (identity.judge, identity.rubric, identity.instrumentation)
    ):
        return None
    return identity


def _carry_identity_is_complete(
    identity: EvaluationCompatibilityIdentity, *, requires_dependency_identity: bool
) -> bool:
    """Only reuse values when every relevant producer component has a known identity."""
    return (
        bool(identity.plugin_id and identity.plugin_version)
        and identity.judge.verified
        and identity.rubric.verified
        and identity.instrumentation.verified
        and (not requires_dependency_identity or identity.dependency_lock_hash is not None)
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


def rescore_selected_count(items: Sequence[WorkItem], recorded_count: int) -> int:
    """Keep every frozen execution item selected, including work that never ran.

    Legacy runs without a work graph use final recorded executions. The observed count
    also prevents dropping historical outputs whose work records are unavailable.
    """
    return max(sum(item.kind == "execution" for item in items), recorded_count)


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


def _application_for_scoring(
    storage: Storage, artifacts: ArtifactStore, record: Any
) -> ApplicationSpec | None:
    """Load the run's frozen application, never silently substituting the catalog.

    Runs created by the engine carry an application artifact.  A missing or tampered
    artifact is a hard scoring error: using the mutable catalog would change the
    observation contract without changing the run identity.  Catalog lookup remains only
    for older runs that have no frozen artifact reference at all.
    """

    manifest = record.manifest
    params = deep_unfreeze(manifest.parameters) or {}
    artifact_id = params.get("application_artifact_id")
    if "application_artifact_id" not in params:
        return storage.get_application(manifest.application_id) if manifest.application_id else None
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ScoringError("the run's frozen application artifact reference is invalid")
    ref = storage.get_artifact(artifact_id)
    if ref is None:
        raise ScoringError("the run's frozen application artifact is missing")
    try:
        raw = artifacts.read_bytes(ref)
        spec = ApplicationSpec.model_validate_json(raw)
    except Exception as exc:
        raise ScoringError("the run's frozen application artifact failed verification") from exc
    if content_hash(spec.model_dump(mode="json")) != manifest.application_hash:
        raise ScoringError("the run's frozen application does not match its manifest")
    return spec


def _plan_for_scoring(
    storage: Storage, artifacts: ArtifactStore, record: Any
) -> ExecutablePlan | None:
    """Direct scoring inherits verified frozen bounds when the run has a plan."""
    params = deep_unfreeze(record.manifest.parameters) or {}
    if "plan_artifact_id" not in params:
        return None
    artifact_id = params.get("plan_artifact_id")
    if not isinstance(artifact_id, str) or not artifact_id:
        raise ScoringError("the run's frozen plan artifact reference is invalid")
    ref = storage.get_artifact(artifact_id)
    if ref is None:
        raise ScoringError("the run's frozen plan artifact is missing")
    try:
        raw = artifacts.read_bytes(ref)
        plan = ExecutablePlan.model_validate_json(raw)
    except Exception as exc:
        raise ScoringError("the run's frozen plan artifact failed verification") from exc
    if bytes_hash(raw) != record.manifest.plan_hash:
        raise ScoringError("the run's frozen plan does not match its manifest")
    return plan


async def score_recorded_run(
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    registry: EvaluatorRegistry,
    run_id: str,
    bindings: Sequence[MetricBinding],
    timeout_seconds: float = DEFAULT_EVALUATION_TIMEOUT_SECONDS,
    model_timeout_seconds: float = DEFAULT_MODEL_EVALUATION_TIMEOUT_SECONDS,
    prepare_timeout_seconds: float = DEFAULT_PREPARE_TIMEOUT_SECONDS,
    cancel: asyncio.Event | None = None,
    application: ApplicationSpec | None = None,
    carry_forward: bool = False,
    budgets: BudgetLimits | None = None,
    quotas: Sequence[Quota] | None = None,
    retry: RetryPolicy | None = None,
    gates: Sequence[ReleaseGate] = (),
) -> ScoringReport:
    """`application` overrides the catalog lookup (the engine passes the run's frozen spec).

    `carry_forward`: bring the pass up to date instead of repeating it. A finished result of
    an earlier pass of this run (same stored answer, same metric settings) is carried into
    this pass as it is, and only what is missing or failed is evaluated: a judge that hit a
    rate limit on 2 of 15 cases is asked about those 2, not all 15. Every pass stays complete
    and each carried result says so in its provenance. Results that depend on an imported
    trace are always evaluated again, since a trace can change between passes."""
    record = storage.get_run(run_id)
    if record is None:
        raise ScoringError(f"no run committed with run_id={run_id!r}")
    if application is None:
        application = _application_for_scoring(storage, artifacts, record)
    resolved = registry.validate(bindings, application=application)  # raises on any problem
    frozen = (
        _plan_for_scoring(storage, artifacts, record)
        if budgets is None or quotas is None or retry is None
        else None
    )
    limits = budgets if budgets is not None else frozen.budgets if frozen else BudgetLimits()
    if (
        limits.max_cost_usd is not None
        and limits.estimated_cost_per_evaluation_usd is None
        and any(metric.manifest.uses_models for metric in resolved)
    ):
        raise ScoringError(
            "max_cost_usd with model-backed evaluators needs estimated_cost_per_evaluation_usd "
            "(unknown costs are never counted as zero)"
        )
    dispatch = RescoreDispatch(
        limits,
        quotas if quotas is not None else frozen.quotas if frozen else (),
        retry if retry is not None else frozen.retry if frozen else RetryPolicy(max_attempts=1),
    )
    # A direct rescore may use a different plugin environment from the original run.
    # Freeze the identity of the metrics/environment actually resolved for this pass.
    dependency_lock_hash = declared_dependency_identity(resolved)

    executions = select_final_executions(storage.list_execution_attempts(run_id))
    selected_count = rescore_selected_count(storage.list_work_items(run_id), len(executions))
    if not selected_count:
        raise ScoringError(f"run {run_id!r} has no recorded executions to score")
    cases: dict[str, list[BenchmarkCase]] = {}
    stored_cases = storage.list_cases(record.manifest.dataset_hash)
    for stored in stored_cases:
        cases.setdefault(stored.case_id, []).append(stored)
    episodes = episode_prefixes(stored_cases)

    report = ScoringReport(scoring_id=f"score-{uuid.uuid4().hex[:12]}", run_id=run_id)
    earlier = storage.list_metric_results(run_id) if carry_forward else []
    # Recorded first, so a report can interpret this pass's results even if it is cut short.
    storage.append_run_event(
        run_id,
        "scoring_pass",
        {
            "scoring_id": report.scoring_id,
            "metric_profiles": metric_profiles(
                resolved,
                application=application,
                dependency_lock_hash=dependency_lock_hash,
            ),
            "repeat_reason": (
                "carry_forward_unfinished" if carry_forward else "explicit_stored_output_rescore"
            ),
            "independent_judge_repeat": "not_proven",
            "selected_count": selected_count,
            "release_gates": [gate.model_dump(mode="json") for gate in gates],
            "budget_scope": "this_scoring_pass",
            "budgets": limits.model_dump(mode="json"),
            "quotas": [gate.quota.model_dump(mode="json") for gate in dispatch.gates],
            "retry": dispatch.retry.model_dump(mode="json"),
        },
    )
    for metric in resolved:
        scorer = BindingScorer(
            storage,
            artifacts,
            report.scoring_id,
            metric,
            model_timeout_seconds if metric.manifest.uses_models else timeout_seconds,
            cancel,
            prepare_timeout_seconds=prepare_timeout_seconds,
            application=application,
            dependency_lock_hash=dependency_lock_hash,
            episodes=episodes,
            dispatch=dispatch,
        )
        scorer.carry_from(earlier)
        metric_results = await scorer.score_all(executions, cases, report.warnings)
        report.results.extend(metric_results)
        report.carried += sum(1 for r in metric_results if is_carried(r))
        report.summaries.append(
            summarize(
                metric_results,
                manifest=metric.manifest,
                binding_hash=metric.binding_hash,
                params=deep_unfreeze(metric.binding.params) or {},
                planned=selected_count,
                missing="unavailable",
            )
        )
    # A pass is complete only after every resolved binding has returned.  The
    # marker lets a later comparison distinguish a finished rescore from a
    # process that crashed after writing the opening scoring_pass event.
    report.outcome, report.gates, report.exit_code = scoring_pass_outcome(report.summaries, gates)
    report.budget = dispatch.ledger.summary()
    report.quotas = [gate.summary() for gate in dispatch.gates]
    report.stop_reason = dispatch.stop_reason
    storage.append_run_event(
        run_id,
        "scoring_pass_completed",
        {
            "scoring_id": report.scoring_id,
            "result_count": len(report.results),
            "status": "completed",
            "budget": report.budget,
            "quotas": report.quotas,
            "stop_reason": report.stop_reason,
            "outcome": report.outcome,
            "exit_code": report.exit_code,
            "gates": report.gates,
        },
    )
    return report


def episode_prefixes(cases: Iterable[BenchmarkCase]) -> dict[str, tuple[BenchmarkCase, ...]]:
    """For each case in an episode (cases sharing a `group_id`), that episode's cases up to
    and including it, in dataset order: the conversation a conversational metric scores."""
    groups: dict[str, list[BenchmarkCase]] = {}
    prefixes: dict[str, tuple[BenchmarkCase, ...]] = {}
    for case in cases:
        if case.group_id is None:
            continue
        group = groups.setdefault(case.group_id, [])
        group.append(case)
        prefixes[case.case_id] = tuple(group)
    return prefixes


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
        episodes: Mapping[str, tuple[BenchmarkCase, ...]] | None = None,
        dispatch: RescoreDispatch | None = None,
    ) -> None:
        self.storage = storage
        # Only a metric that reads the conversation gets one assembled (`episode.turns`).
        self.episodes = episodes or {}
        self._paths = {requirement.path for requirement in metric.requirements}
        self._conversational = EPISODE_TURNS in self._paths
        self._traced = EXECUTION_TRACE in self._paths
        self._observations: dict[str, list[dict[str, Any]]] = {}  # run_id -> trace rows
        self._parsed: dict[str, list[Trace]] = {}  # raw artifact id -> its traces
        self.artifacts = artifacts
        self.scoring_id = scoring_id
        self.metric = metric
        self.manifest = metric.manifest
        self._worker_backed = getattr(metric.factory, "spec", None) is not None
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
        self.dispatch = dispatch
        self._opened = False
        self._evaluator: Evaluator | None = None
        # An evaluator that serves one request at a time (a worker process) takes cases
        # in turn, so each case's time limit counts its own turn, not the line.
        self._turn = asyncio.Lock()
        # Finished results of earlier passes to reuse, by (case, repetition); see `carry_from`.
        self._carry: dict[
            tuple[str, int], tuple[EvaluationResult, EvaluationCompatibilityIdentity]
        ] = {}

    async def open(self) -> None:
        """Construct and prepare the evaluator. Construction and prepare() are evaluator
        code too: a failure there becomes a recorded error for every case it would have
        evaluated, never an aborted pass."""
        self._opened = True
        try:
            self._evaluator = self.metric.factory()
            await asyncio.wait_for(
                self._evaluator.prepare(deep_unfreeze(self.metric.binding.params) or {}),
                self.prepare_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001 - see docstring
            self.prepare_error = f"evaluator_prepare_failed:{type(exc).__name__}: {exc}"[:500]

    def carry_from(self, earlier: Sequence[EvaluationResult]) -> None:
        """Offer finished results of earlier passes (oldest first): one for the same case,
        repetition, stored answer, metric settings, and verified producer identity is carried
        into this pass instead of being evaluated again. A result that depends on an imported
        trace is never carried."""
        if self._traced:
            return
        manifest = self.manifest
        earlier_by_id = {result.result_id: result for result in earlier}
        current_identity = self.compatibility
        if not _carry_identity_is_complete(
            current_identity,
            requires_dependency_identity=manifest.uses_models or self._worker_backed,
        ):
            return
        for old in earlier:
            if (
                old.status is ExecutionStatus.OK
                and old.execution_id is not None
                and old.metric_id == manifest.evaluator_id
                and old.metric_version == manifest.version
                and old.binding_hash == self.metric.binding_hash
            ):
                producer = _verified_producer_identity(old, earlier_by_id)
                if producer == current_identity:
                    self._carry[(old.case_id, old.repetition_id)] = (old, producer)
                    # A later compatible result replaces an older one; an incompatible result
                    # cannot shadow an older value from this same implementation.

    def _carried(self, execution: ExecutionResult) -> EvaluationResult | None:
        offered = self._carry.get((execution.case_id, execution.repetition_id))
        if (
            offered is None
            or execution.status is not ExecutionStatus.OK
            or offered[0].execution_id != execution.execution_id
        ):
            return None
        source, producer = offered
        fresh = self._result(execution, EvaluationOutcome(ExecutionStatus.OK))
        copied = evaluation_from_cache(source, fresh, key=f"carried:{source.result_id}")
        provenance = deep_unfreeze(copied.provenance)
        provenance.pop("cache", None)
        provenance["carried_forward"] = {
            "source_result_id": source.result_id,
            "source_scoring_id": source.scoring_id,
            "producer_compatibility_hash": producer.compatibility_hash,
            "note": CARRIED_NOTE,
        }
        return copied.model_copy(
            update={
                "provenance": provenance,
                # The value is the old one; whether it passes is decided by this pass's rule.
                "decision": decide(source.status, source.value, self.rule),
                "resources": {
                    "latency_ms": None,
                    "model_calls": 0,
                    "cost": 0.0,
                    "accounting": "complete",
                    "carried_forward": True,
                },
            }
        )

    async def score(
        self, execution: ExecutionResult, candidates: list[BenchmarkCase]
    ) -> EvaluationResult:
        """Evaluate one execution and commit the attempt (evaluation_attempts). With the
        evaluation cache on (`cache_policy_hash`), a stored result for the same key is
        copied instead of calling the evaluator (16-T3). A result offered by `carry_from`
        is reused as it is."""
        carried = self._carried(execution)
        if carried is not None:
            self.storage.commit_evaluation_attempt(carried, attempt_number=carried.attempt_number)
            return carried
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
            or self._conversational  # the result depends on earlier turns, not in the key
            or self._traced  # likewise the imported trace
        ):
            return None
        manifest = self.manifest
        if not _carry_identity_is_complete(
            self.compatibility,
            requires_dependency_identity=manifest.uses_models or self._worker_backed,
        ):
            return None
        return evaluation_key(
            execution,
            candidates[0],
            binding_hash=self.metric.binding_hash,
            evaluator=f"{manifest.evaluator_id}@{manifest.version}",
            plugin=f"{manifest.plugin_id}=={manifest.plugin_version}",
            compatibility_hash=self.compatibility.compatibility_hash,
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

    def record_outcome(
        self, execution: ExecutionResult | MissingExecution, outcome: EvaluationOutcome
    ) -> EvaluationResult:
        """Record an outcome produced outside this process (a remote job's result, 17-T2):
        the same attempt, provenance and final-result path as an evaluated one, without
        calling any evaluator."""
        raw = (
            self._serialize_raw(outcome) if outcome.status is not ExecutionStatus.SKIPPED else None
        )
        result = self._result(execution, outcome, raw=raw)
        self.storage.commit_evaluation_attempt(result, attempt_number=result.attempt_number)
        self.finalize(result)
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
        if self.dispatch is None:
            await self.open()
        results = []
        try:
            for execution in executions:
                result = await self.score(execution, cases.get(execution.case_id, []))
                if self.dispatch is not None:
                    for attempt in range(1, self.dispatch.retry.max_attempts):
                        verdict = classify_evaluation(result, self.manifest)
                        if not verdict.retry or self.cancel.is_set():
                            break
                        await self.dispatch.pause(
                            backoff_delay(self.dispatch.retry, attempt, self.dispatch.rng),
                            self.cancel,
                        )
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
        if self._conversational:
            turns, incomplete = self._episode_turns(case, execution)
            if incomplete is not None:
                return self._result(execution, EvaluationOutcome.not_applicable(incomplete))
            view = EvaluationView(case=case, execution=execution, episode=turns)
        if self._traced:
            tree, unusable = self._trace_for(execution)
            if unusable is not None:
                return self._result(execution, EvaluationOutcome.not_applicable(unusable))
            view = EvaluationView(case=case, execution=execution, episode=view.episode, trace=tree)
        for requirement in self.metric.requirements:
            state = view.state(requirement.path)
            if state == "missing" or (state == "empty" and requirement.non_empty):
                return self._result(
                    execution, EvaluationOutcome.not_applicable(f"{state}:{requirement.path}")
                )
        if self.dispatch is not None:
            denial = await self.dispatch.acquire(self.manifest.evaluator_id, self.cancel)
            if denial is not None:
                return self._result(
                    execution,
                    EvaluationOutcome(
                        ExecutionStatus.CANCELLED
                        if denial == "cancelled"
                        else ExecutionStatus.SKIPPED,
                        reason=denial,
                    ),
                )
            result: EvaluationResult | None = None
            try:
                if not self._opened:
                    await self.open()
                result = await self._evaluate(self._evaluator, execution, view)
                return result
            finally:
                self.dispatch.finish(self.manifest.evaluator_id, result)
        return await self._evaluate(evaluator, execution, view)

    async def _evaluate(
        self, evaluator: Evaluator | None, execution: ExecutionResult, view: EvaluationView
    ) -> EvaluationResult:
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
        # Held from the restart check to the end of the call, so a worker killed by one
        # case's timeout is restarted for the next case in line instead of failing it.
        turn = (
            self._turn if getattr(evaluator, "one_at_a_time", False) else contextlib.nullcontext()
        )
        async with turn:
            try:
                await asyncio.wait_for(evaluator.ensure_ready(), self.prepare_timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - a lost runtime is an evaluator error
                return self._result(
                    execution,
                    EvaluationOutcome.error(
                        f"evaluator_restart_failed:{type(exc).__name__}: {exc}"[:500]
                    ),
                )
            if self.cancel.is_set():
                return self._result(
                    execution, EvaluationOutcome(ExecutionStatus.CANCELLED, reason="cancelled")
                )
            if self.dispatch is not None and (denial := self.dispatch.wall_denial()):
                return self._result(
                    execution, EvaluationOutcome(ExecutionStatus.SKIPPED, reason=denial)
                )
            started = time.perf_counter()
            try:
                outcome = await asyncio.wait_for(
                    evaluator.evaluate(view, ctx), self.timeout_seconds
                )
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

    def _episode_turns(
        self, case: BenchmarkCase, execution: ExecutionResult
    ) -> tuple[tuple[dict[str, Any], ...] | None, str | None]:
        """The conversation up to and including this turn, from this run's final recorded
        executions of the same repetition; or the reason it cannot be scored. A case in no
        episode gets None (the metric's `episode.turns` requirement makes it not
        applicable). A conversation with an earlier turn that did not complete is not
        scored: judging it without that turn would judge a conversation that never took
        place."""
        prefix = self.episodes.get(case.case_id)
        if not prefix:
            return None, None
        turns = []
        for turn_case in prefix:
            if turn_case.case_id == case.case_id:
                recorded: ExecutionResult | None = execution
            else:
                attempts = self.storage.list_execution_attempts(execution.run_id, turn_case.case_id)
                same = [a for a in attempts if a.repetition_id == execution.repetition_id]
                final = select_final_executions(same)
                recorded = final[0] if final else None
            if recorded is None or recorded.status is not ExecutionStatus.OK:
                return None, f"episode_incomplete:{turn_case.case_id}"
            turn: dict[str, Any] = {
                "case_id": turn_case.case_id,
                "input": deep_unfreeze(turn_case.input),
                "output": deep_unfreeze(recorded.output),
            }
            earlier = EvaluationView(case=turn_case, execution=recorded)
            for name in ("retrieved_context", "tool_events"):
                if f"execution.{name}" in self._paths:
                    value = earlier.get(f"execution.{name}")
                    turn[name] = None if value is MISSING else value
            turns.append(turn)
        return tuple(turns), None

    def _trace_for(self, execution: ExecutionResult) -> tuple[dict[str, Any] | None, str | None]:
        """The execution's imported trace as a span tree, or the reason it cannot be used.
        No trace gives None (the metric's `execution.trace` requirement makes it not
        applicable). A partial trace, or more than one trace for the execution, is not
        scored: an agent judged on part of what it did, or on a guess between two
        records, is judged on evidence that is not there."""
        if execution.run_id not in self._observations:
            self._observations[execution.run_id] = self.storage.list_trace_observations(
                execution.run_id
            )
        rows = [
            row
            for row in self._observations[execution.run_id]
            if row["execution_id"] == execution.execution_id
        ]
        if not rows:
            return None, None
        if len(rows) > 1:
            return None, f"trace_ambiguous:{len(rows)} traces"
        row = rows[0]
        if not row["complete"]:
            return None, "trace_partial:" + ",".join(row["partial_reasons"])
        trace = Trace(trace_id=row["trace_id"])
        for artifact_id in row.get("raw_artifact_ids") or []:
            if artifact_id not in self._parsed:
                stored = self.storage.get_artifact(artifact_id)
                self._parsed[artifact_id] = (
                    parse_otlp(self.artifacts.read_bytes(stored)) if stored else []
                )
            for parsed in self._parsed[artifact_id]:
                if parsed.trace_id == row["trace_id"]:
                    for span in parsed.spans:
                        trace.add(span)
        if not trace.spans:
            return None, "trace_unreadable"
        return {"format": TREE_FORMAT, "spans": span_tree(trace)}, None

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
