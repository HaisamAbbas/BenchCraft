"""Canonical, immutable domain models (specification §2, §5).

This module must not import any evaluator framework, storage engine, or UI package
(see `docs/adr/0001-source-of-truth-and-dependency-direction.md`). It depends only on
pydantic and the standard library.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, PlainSerializer

SCHEMA_VERSION = "1.0.0"


def utcnow() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    """Base for immutable records: no attribute reassignment after construction."""

    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------- deep immutability
#
# `frozen=True` only blocks *attribute reassignment* (`case.input = x`); it does nothing
# about mutating a mutable object already stored in a field (`case.input["k"].append(x)`).
# `FrozenValue` closes that gap for loosely-typed Golden/plan/execution data: dicts become
# read-only `MappingProxyType`, lists/tuples become tuples, sets become frozensets, all the
# way down. JSON export/serialization converts back to plain dict/list via the paired
# `PlainSerializer` so schemas and `model_dump(mode="json")` are unaffected.


def deep_freeze(value: Any) -> Any:
    if isinstance(value, MappingProxyType):
        return value
    if isinstance(value, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(deep_freeze(v) for v in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(deep_freeze(v) for v in value)
    return value


def deep_unfreeze(value: Any) -> Any:
    """Inverse of `deep_freeze`: a plain, independently mutable copy safe for JSON
    serialization or handing to an application runner. Never mutates the frozen source."""
    if isinstance(value, MappingProxyType):
        return {k: deep_unfreeze(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [deep_unfreeze(v) for v in value]
    if isinstance(value, frozenset):
        return [deep_unfreeze(v) for v in value]
    return value


FrozenValue = Annotated[
    Any,
    BeforeValidator(deep_freeze),
    PlainSerializer(deep_unfreeze, return_type=Any, when_used="json"),
]


# --------------------------------------------------------------------------- enums


class ObservationState(str, Enum):
    OBSERVED = "observed"
    DECLARED = "declared"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class ExecutionStatus(str, Enum):
    OK = "ok"
    ERROR = "error"
    NOT_APPLICABLE = "not_applicable"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class Decision(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INDETERMINATE = "indeterminate"
    NOT_EVALUATED = "not_evaluated"


class ReferenceStatus(str, Enum):
    SYNTHETIC_UNVERIFIED = "synthetic_unverified"
    SOURCE_VERIFIED = "source_verified"
    HUMAN_REVIEWED = "human_reviewed"
    EXECUTABLE_ORACLE = "executable_oracle"
    HUMAN_AUTHORED = "human_authored"


class ToolMatchMode(str, Enum):
    CONTAINS_ALL = "contains_all"
    EXACT = "exact"
    ORDERED_SUBSEQUENCE = "ordered_subsequence"


class RunnerKind(str, Enum):
    CLI = "cli"
    HTTP = "http"


class ResetPolicy(str, Enum):
    PER_CASE = "per_case"
    PER_EPISODE = "per_episode"
    SHARED = "shared"


class EffectLevel(str, Enum):
    NONE = "none"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class RedactionClass(str, Enum):
    NONE = "none"
    REDACTED = "redacted"
    RESTRICTED = "restricted"


# --------------------------------------------------------------------------- dataset / case


class ToolExpectation(FrozenModel):
    tool_names: tuple[str, ...] = Field(default_factory=tuple)
    match_mode: ToolMatchMode = ToolMatchMode.CONTAINS_ALL


class ReferenceAnswer(FrozenModel):
    """Judge-only reference data. Never sent to the application unless explicitly bound."""

    answer: str | None = None
    context: tuple[str, ...] = Field(default_factory=tuple)
    tools: ToolExpectation | None = None
    status: ReferenceStatus = ReferenceStatus.HUMAN_AUTHORED


class RepositoryFixture(FrozenModel):
    """Coding-case repository reference. Execution prerequisites are intentionally
    incomplete in MVP (§6): an immutable commit, execution environment, setup recipe,
    hidden tests, and success criteria are required before this is executable."""

    path: str
    commit: str | None = None
    setup_recipe: str | None = None
    hidden_tests_ref: str | None = None
    success_criteria: str | None = None

    @property
    def is_execution_ready(self) -> bool:
        return all(
            [self.commit, self.setup_recipe, self.hidden_tests_ref, self.success_criteria]
        )


class Fixture(FrozenModel):
    """A named piece of case data. `app_visible=True` is an explicit opt-in to expose it
    to the application; it is otherwise judge-only, like `reference`."""

    name: str
    content: FrozenValue = None
    app_visible: bool = False


class Provenance(FrozenModel):
    origin: ReferenceStatus = ReferenceStatus.HUMAN_AUTHORED
    source_refs: tuple[str, ...] = Field(default_factory=tuple)
    generator_identity: str | None = None
    prompt_hash: str | None = None
    reviewer_identity: str | None = None


class BenchmarkCase(FrozenModel):
    """Golden. Facts, input, references, expectations and fixtures known before
    execution. Runtime data must never be written into it (§2)."""

    case_id: str
    input: FrozenValue
    reference: ReferenceAnswer | None = None
    expectations: FrozenValue = Field(default_factory=dict)
    fixtures: tuple[Fixture, ...] = Field(default_factory=tuple)
    repository: RepositoryFixture | None = None
    group_id: str | None = None
    metadata: FrozenValue = Field(default_factory=dict)
    extensions: FrozenValue = Field(default_factory=dict)
    provenance: Provenance = Field(default_factory=Provenance)
    duplicate_of_line: int | None = None
    source_line: int | None = None

    def application_input_projection(self) -> dict[str, Any]:
        """The application-visible envelope: input plus fixtures explicitly marked
        `app_visible`. Never includes `reference` or non-app-visible fixtures (01-G2).
        Returns a plain, independently mutable, JSON-serializable copy: mutating the
        result never mutates this frozen Golden."""
        return {
            "case_id": self.case_id,
            "input": deep_unfreeze(self.input),
            "fixtures": {
                f.name: deep_unfreeze(f.content) for f in self.fixtures if f.app_visible
            },
        }


class DatasetManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    dataset_id: str
    content_hash: str
    case_count: int
    source_refs: tuple[str, ...] = Field(default_factory=tuple)
    split: str | None = None
    duplicate_case_ids: tuple[str, ...] = Field(default_factory=tuple)
    created_at: datetime = Field(default_factory=utcnow)


# --------------------------------------------------------------------------- application


class ApplicationSpec(FrozenModel):
    application_id: str
    runner: RunnerKind
    target: str
    input_binding: FrozenValue = Field(default_factory=dict)
    output_binding: FrozenValue = Field(default_factory=dict)
    revision: str | None = None
    environment_digest: str | None = None
    reset_policy: ResetPolicy = ResetPolicy.PER_CASE
    effects: EffectLevel = EffectLevel.NONE


class ObservationClaim(FrozenModel):
    observation_id: str
    capability: str
    state: ObservationState
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    method: str | None = None
    scope: str | None = None
    limitations: str | None = None


# --------------------------------------------------------------------------- plan


class EvaluationPlan(FrozenModel):
    plan_id: str
    objectives: tuple[str, ...] = Field(default_factory=tuple)
    metric_specs: tuple[FrozenValue, ...] = Field(default_factory=tuple)
    bindings: FrozenValue = Field(default_factory=dict)
    selectors: FrozenValue = Field(default_factory=dict)
    sampling: FrozenValue = Field(default_factory=dict)
    aggregations: FrozenValue = Field(default_factory=dict)
    gates: FrozenValue = Field(default_factory=dict)
    budgets: FrozenValue = Field(default_factory=dict)
    policy_hash: str | None = None


# --------------------------------------------------------------------------- execution


class ExecutionResult(FrozenModel):
    execution_id: str
    run_id: str
    case_id: str
    repetition_id: int = 0
    attempt_id: int = 0
    status: ExecutionStatus
    output: FrozenValue = None
    retrieved_context: tuple[str, ...] | None = None
    tool_events: tuple[FrozenValue, ...] = Field(default_factory=tuple)
    trace_refs: tuple[str, ...] = Field(default_factory=tuple)
    timing: FrozenValue = Field(default_factory=dict)
    usage: FrozenValue = None
    cost: float | None = None
    error: str | None = None
    observation_completeness: FrozenValue = Field(default_factory=dict)

    @staticmethod
    def build_id(run_id: str, case_id: str, repetition_id: int, attempt_id: int) -> str:
        return f"{run_id}:{case_id}:r{repetition_id}:a{attempt_id}"


# --------------------------------------------------------------------------- result


class MetricValue(FrozenModel):
    kind: Literal["scalar", "boolean", "category", "vector", "distribution", "structured"]
    value: FrozenValue


class EvaluationResult(FrozenModel):
    result_id: str
    run_id: str
    case_id: str
    metric_id: str
    metric_version: str
    value: MetricValue | None = None
    status: ExecutionStatus
    decision: Decision
    evidence_refs: tuple[str, ...] = Field(default_factory=tuple)
    provenance: FrozenValue = Field(default_factory=dict)
    uncertainty: FrozenValue = None
    resources: FrozenValue = Field(default_factory=dict)
    raw_artifact_ref: str | None = None


# --------------------------------------------------------------------------- run / artifact


class RunManifest(FrozenModel):
    run_id: str
    dataset_hash: str
    application_hash: str
    plan_hash: str
    plugin_hashes: FrozenValue = Field(default_factory=dict)
    model_identifiers: FrozenValue = Field(default_factory=dict)
    dependency_lock_hash: str | None = None
    parameters: FrozenValue = Field(default_factory=dict)
    seed: int | None = None
    environment: FrozenValue = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class ArtifactRef(FrozenModel):
    artifact_id: str
    digest: str
    uri: str
    mime_type: str
    size_bytes: int
    redaction: RedactionClass = RedactionClass.NONE
    run_id: str | None = None


# --------------------------------------------------------------------------- scheduling / usage


class WorkItemState(str, Enum):
    """§15: "Work states: pending, running, succeeded, failed, blocked, cancelled, and
    unknown-effect." This model records identity/state only — the scheduler that assigns
    and transitions work items is engine scope (Prompt 06), not storage scope (Prompt 02)."""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"
    UNKNOWN_EFFECT = "unknown_effect"


class WorkItem(FrozenModel):
    work_item_id: str
    run_id: str
    task_key: str  # stable logical key; unique per run so a retried commit cannot duplicate it
    kind: str  # e.g. "execution", "evaluation"
    dependency_keys: tuple[str, ...] = Field(default_factory=tuple)
    state: WorkItemState = WorkItemState.PENDING
    attempt: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None


class UsageRole(str, Enum):
    """§2: "Treat model calls in three distinct roles: application model, planning model,
    and judging model. Each has separate credentials, budgets, usage records, and
    provenance." """

    APPLICATION = "application"
    PLANNER = "planner"
    EVALUATOR = "evaluator"


class UsageEvent(FrozenModel):
    usage_event_id: str
    run_id: str
    role: UsageRole
    provider: str | None = None
    tokens: FrozenValue = None
    calls: int | None = None
    cost: float | None = None
    recorded_at: datetime = Field(default_factory=utcnow)


class Approval(FrozenModel):
    """§16: "Bind approvals to target/config/plan/environment hashes and allowed actions.
    Approval rules are evaluated by code." """

    approval_id: str
    scope_hash: str
    allowed_actions: tuple[str, ...] = Field(default_factory=tuple)
    granted_by: str | None = None
    granted_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None


ALL_MODELS: tuple[type[BaseModel], ...] = (
    ToolExpectation,
    ReferenceAnswer,
    RepositoryFixture,
    Fixture,
    Provenance,
    BenchmarkCase,
    DatasetManifest,
    ApplicationSpec,
    ObservationClaim,
    EvaluationPlan,
    ExecutionResult,
    MetricValue,
    EvaluationResult,
    RunManifest,
    ArtifactRef,
    WorkItem,
    UsageEvent,
    Approval,
)
