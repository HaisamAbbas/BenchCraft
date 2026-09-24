"""Executable manual plans (§13 `run`, §15; 06-T1).

A plan says *what* to run — dataset, application, metric bindings, repetitions, selection —
and the bounds to run it under: concurrency caps, retry policy and budgets. It is frozen
into the run manifest by content hash; `resume` continues under exactly this plan.

Budget semantics (06-T2):
- hard limits: `max_application_calls`, `max_evaluator_calls`, `max_judge_tokens`,
  `max_wall_seconds` — dispatch stops before they would be exceeded.
- soft limit: `max_cost_usd` — enforced on known cost plus declared estimates, because
  providers do not report enforceable monetary bounds. Reported as an estimate, never as a
  guarantee.
Like all core modules, this imports only pydantic and the standard library.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from aibench.core.models import (
    SCHEMA_VERSION,
    FrozenModel,
    FrozenSecretRefMap,
    FrozenValue,
    MetricBinding,
)


class RetryPolicy(FrozenModel):
    """Engine-level retries for retryable failures only (§15). Total attempts per work item,
    including the first; delays grow exponentially with jitter and are capped."""

    max_attempts: int = Field(default=3, ge=1, le=10)
    initial_backoff_seconds: float = Field(default=0.5, ge=0, le=60)
    max_backoff_seconds: float = Field(default=30.0, ge=0, le=600)
    jitter: float = Field(default=0.25, ge=0, le=1)  # +/- fraction of each delay


class ConcurrencyLimits(FrozenModel):
    application: int = Field(default=1, ge=1, le=64)
    evaluation: int = Field(default=1, ge=1, le=64)


class BudgetLimits(FrozenModel):
    max_application_calls: int | None = Field(default=None, ge=1)
    max_evaluator_calls: int | None = Field(default=None, ge=1)
    max_judge_tokens: int | None = Field(default=None, ge=1)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)  # soft
    estimated_cost_per_application_call_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    estimated_cost_per_evaluation_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def _cost_limit_needs_an_estimate(self) -> BudgetLimits:
        # Applications rarely report cost; without an estimate the projection would count
        # every unknown-cost call as $0 and the soft limit would never trigger.
        if self.max_cost_usd is not None and self.estimated_cost_per_application_call_usd is None:
            raise ValueError(
                "max_cost_usd needs estimated_cost_per_application_call_usd "
                "(unknown costs are never counted as zero)"
            )
        return self


class CasePredicate(FrozenModel):
    """A selector over Golden fields only (never execution data): `path` is a `case.*`
    evaluation-view path, e.g. `case.metadata.category` or `case.reference.answer`.
    `exists` means present and non-empty."""

    path: str = Field(pattern=r"^case\.")
    op: Literal["exists", "equals", "in"] = "exists"
    value: FrozenValue = None
    values: tuple[FrozenValue, ...] = ()

    @model_validator(mode="after")
    def _operands(self) -> CasePredicate:
        if self.op == "equals" and self.value is None:
            raise ValueError("op 'equals' needs a value")
        if self.op == "in" and not self.values:
            raise ValueError("op 'in' needs values")
        return self


class CaseSelection(FrozenModel):
    """Which cases of the dataset the run selects, applied in this order:
    `case_ids` (empty means all), then every `where` predicate, then either a seeded
    random `sample_size` (kept in dataset order) or the first `limit` cases."""

    case_ids: tuple[str, ...] = ()
    where: tuple[CasePredicate, ...] = ()
    limit: int | None = Field(default=None, ge=1)
    sample_size: int | None = Field(default=None, ge=1)
    seed: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _sampling(self) -> CaseSelection:
        if self.sample_size is not None and self.seed is None:
            raise ValueError("selection.sample_size needs an explicit selection.seed")
        if self.seed is not None and self.sample_size is None:
            raise ValueError("selection.seed needs selection.sample_size")
        if self.sample_size is not None and self.limit is not None:
            raise ValueError("use selection.limit or selection.sample_size, not both")
        return self


class PluginEnvironmentRef(FrozenModel):
    """A plugin environment whose evaluators run in workers (see ADR 0004)."""

    python: str
    paths: tuple[str, ...] = ()
    secret_env: FrozenSecretRefMap = Field(default_factory=dict)
    startup_timeout_seconds: float = Field(default=120.0, gt=0, le=3_600)


class ReleaseGate(FrozenModel):
    """A predeclared release gate on one metric binding (§12), checked against the run's
    stored results. Both thresholds use every selected case as the denominator, so losing
    observations can only fail a gate, never pass it:
    - `min_pass_rate`: passing decisions / selected;
    - `min_completed_coverage`: completed evaluations / selected.
    A gate is decided only for a finished run; a partial snapshot leaves it undecided."""

    gate_id: str = Field(min_length=1, max_length=100)
    binding: int = Field(ge=0)  # index into the plan's `metrics`
    min_pass_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    min_completed_coverage: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def _has_threshold(self) -> ReleaseGate:
        if self.min_pass_rate is None and self.min_completed_coverage is None:
            raise ValueError(f"gate {self.gate_id!r} needs min_pass_rate or min_completed_coverage")
        return self


class Quota(FrozenModel):
    """A provider-aware limit shared by the work it applies to (§15, 16-T4): the
    application, or evaluators whose ID matches `evaluator:<glob>`."""

    name: str = Field(min_length=1, max_length=100)
    applies_to: str = Field(pattern=r"^(application|evaluator:\S+)$")
    max_in_flight: int | None = Field(default=None, ge=1, le=64)
    requests_per_second: float | None = Field(default=None, gt=0, le=10_000)
    burst: int = Field(default=1, ge=1, le=1_000)
    # Pause after "slow down" (HTTP 429/503) when the provider gives no Retry-After.
    backoff_seconds: float = Field(default=1.0, gt=0, le=600)
    # The longest pause a provider's Retry-After can impose on the whole quota.
    max_backpressure_seconds: float = Field(default=60.0, gt=0, le=3600)


class CachePolicy(FrozenModel):
    """Opt-in cross-run caches (§14, 16-T3). Off by default: every record is fresh."""

    executions: bool = False
    evaluations: bool = False


class ExecutablePlan(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    plan_id: str = Field(min_length=1, max_length=200)
    dataset: str  # path, relative to the plan file
    application: str  # application config path, relative to the plan file
    metrics: tuple[MetricBinding, ...] = ()
    repetitions: int = Field(default=1, ge=1, le=100)
    selection: CaseSelection = Field(default_factory=CaseSelection)
    concurrency: ConcurrencyLimits = Field(default_factory=ConcurrencyLimits)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    budgets: BudgetLimits = Field(default_factory=BudgetLimits)
    evaluation_timeout_seconds: float = Field(default=60.0, gt=0, le=3600)
    plugin_environments: tuple[PluginEnvironmentRef, ...] = ()
    gates: tuple[ReleaseGate, ...] = ()
    # A test world the application declares; its seed is loaded before each case/episode.
    test_world: str | None = Field(default=None, min_length=1, max_length=200)
    cache: CachePolicy = Field(default_factory=CachePolicy)
    quotas: tuple[Quota, ...] = ()

    @model_validator(mode="after")
    def _backoff_bounds(self) -> ExecutablePlan:
        if self.retry.initial_backoff_seconds > self.retry.max_backoff_seconds:
            raise ValueError("retry.initial_backoff_seconds exceeds retry.max_backoff_seconds")
        if len(set(self.selection.case_ids)) != len(self.selection.case_ids):
            raise ValueError("selection.case_ids contains duplicates")
        ids = [g.gate_id for g in self.gates]
        if len(set(ids)) != len(ids):
            raise ValueError("gates contain duplicate gate_id values")
        names = [q.name for q in self.quotas]
        if len(set(names)) != len(names):
            raise ValueError("quotas contain duplicate names")
        for gate in self.gates:
            if gate.binding >= len(self.metrics):
                raise ValueError(
                    f"gate {gate.gate_id!r} refers to metrics[{gate.binding}], but the plan "
                    f"has {len(self.metrics)} metric binding(s)"
                )
        return self
