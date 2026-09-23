"""Canonical, immutable domain models (specification §2, §5).

This module must not import any evaluator framework, storage engine, or UI package
(see `docs/adr/0001-source-of-truth-and-dependency-direction.md`). It depends only on
pydantic and the standard library.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    model_validator,
)

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

# A typed `str -> str` mapping that is read-only after validation (same deep-immutability
# guarantee as `FrozenValue`, but with a real schema for configuration fields).
FrozenStrMap = Annotated[
    dict[str, str],
    AfterValidator(MappingProxyType),
    PlainSerializer(dict, return_type=dict[str, str]),
]

_SECRET_REF_RE = re.compile(r"^[a-z][a-z0-9_]*:\S+$")


def _check_secret_ref(value: str) -> str:
    """Secret references are `source:name` (e.g. `env:APP_TOKEN`), never literal values.
    Resolution happens at use time in `aibench.security.secrets`."""
    if not _SECRET_REF_RE.match(value):
        raise ValueError(f"secret reference must look like 'source:name', got {value!r}")
    return value


SecretRefStr = Annotated[str, AfterValidator(_check_secret_ref)]
FrozenSecretRefMap = Annotated[
    dict[str, SecretRefStr],
    AfterValidator(MappingProxyType),
    PlainSerializer(dict, return_type=dict[str, str]),
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


class ErrorKind(str, Enum):
    """Why an application invocation did not produce a usable output (§7, §15). Separates
    application/transport failures from evaluator failures and from policy denials."""

    BINDING = "binding"  # input/output binding could not be applied (configuration)
    REQUEST_LIMIT = "request_limit"  # request exceeded the configured size before sending
    POLICY_DENIED = "policy_denied"  # endpoint/redirect policy refused the request
    SPAWN_FAILED = "spawn_failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    OUTPUT_LIMIT = "output_limit"
    NONZERO_EXIT = "nonzero_exit"
    HTTP_STATUS = "http_status"
    REDIRECT_REJECTED = "redirect_rejected"
    TRANSPORT = "transport"
    INVALID_OUTPUT = "invalid_output"


class EffectState(str, Enum):
    """What is known about external effects of one invocation (§15: "A timeout does not
    prove a server-side operation did not occur"). Runners never retry; the engine uses this
    to decide whether a retry is safe or intervention is required."""

    NONE_DECLARED = "none_declared"  # the application declares no external effects
    NOT_DISPATCHED = "not_dispatched"  # the request provably never reached the application
    COMPLETED = "completed"  # the application finished/responded; effects are as it reports
    UNKNOWN = "unknown"  # dispatched, then timed out/cancelled/lost: effects may have occurred


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


# Environment variables a trusted-local child process inherits by default: only what
# common runtimes need to start. Everything else (notably evaluator/provider credentials)
# must be passed explicitly via `env` or `secret_env` (§16: "pass only required secrets").
DEFAULT_INHERITED_ENV: tuple[str, ...] = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "HOME",
    "USERPROFILE",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)


class CliTransport(FrozenModel):
    """One-shot CLI protocol (§7): a static argv array (never a shell string, never
    interpolated with case text), JSON on stdin, JSON (or explicit legacy text) on stdout,
    diagnostics on stderr."""

    kind: Literal["cli"] = "cli"
    argv: tuple[str, ...] = Field(min_length=1)
    cwd: str | None = None  # relative to the application config file's directory
    output_mode: Literal["json", "text"] = "json"
    timeout_seconds: float = Field(default=60.0, gt=0, le=86_400)
    max_stdout_bytes: int = Field(default=1_048_576, gt=0)
    max_stderr_bytes: int = Field(default=65_536, ge=0)
    env: FrozenStrMap = Field(default_factory=dict)
    secret_env: FrozenSecretRefMap = Field(default_factory=dict)
    inherit_env: tuple[str, ...] = DEFAULT_INHERITED_ENV
    healthcheck_argv: tuple[str, ...] | None = None


class HttpSecretHeader(FrozenModel):
    ref: SecretRefStr
    prefix: str = ""  # e.g. "Bearer "


class HttpTransport(FrozenModel):
    """JSON request/response protocol (§7). Redirects are refused by default; when enabled,
    only method-preserving redirects (307/308) to URLs that pass the endpoint policy are
    followed."""

    kind: Literal["http"] = "http"
    url: str
    method: Literal["POST", "PUT"] = "POST"
    headers: FrozenStrMap = Field(default_factory=dict)
    secret_headers: Annotated[
        dict[str, HttpSecretHeader],
        AfterValidator(MappingProxyType),
        PlainSerializer(dict, return_type=dict[str, HttpSecretHeader]),
    ] = Field(default_factory=dict)
    verify_tls: bool = True
    ca_bundle: str | None = None  # relative to the application config file's directory
    allow_plaintext_http: bool = False  # plain http is otherwise loopback-only
    allowed_endpoints: tuple[str, ...] = ()  # URL prefixes; empty means the origin of `url`
    follow_redirects: bool = False
    max_redirects: int = Field(default=3, ge=0, le=10)
    timeout_seconds: float = Field(default=60.0, gt=0, le=86_400)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    max_request_bytes: int = Field(default=1_048_576, gt=0)
    max_response_bytes: int = Field(default=1_048_576, gt=0)
    correlation_header: str = "X-Request-ID"
    healthcheck_url: str | None = None
    reset_url: str | None = None


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
    # Optional so application records committed before Prompt 03 still load; a runner
    # cannot be created without it.
    transport: Annotated[CliTransport | HttpTransport, Field(discriminator="kind")] | None = None

    @model_validator(mode="after")
    def _transport_matches_runner(self) -> ApplicationSpec:
        if self.transport is not None and self.transport.kind != self.runner.value:
            raise ValueError(
                f"transport kind {self.transport.kind!r} does not match runner "
                f"{self.runner.value!r}"
            )
        return self


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
    error_kind: ErrorKind | None = None
    effect_state: EffectState | None = None
    correlation_id: str | None = None

    @staticmethod
    def build_id(run_id: str, case_id: str, repetition_id: int, attempt_id: int) -> str:
        return f"{run_id}:{case_id}:r{repetition_id}:a{attempt_id}"


# --------------------------------------------------------------------------- result


ValueKind = Literal["scalar", "boolean", "category", "vector", "distribution", "structured"]


class MetricValue(FrozenModel):
    kind: ValueKind
    value: FrozenValue


class MetricDirection(str, Enum):
    HIGHER = "higher"
    LOWER = "lower"
    TARGET = "target"
    NONE = "none"


class MetricScope(str, Enum):
    CASE = "case"
    EPISODE = "episode"
    COMPONENT = "component"
    SLICE = "slice"
    RUN = "run"


class DecisionRule(FrozenModel):
    """Harness-owned pass/fail rule using frozen thresholds."""

    rule_id: str = "threshold"
    version: str = "1"
    comparator: Literal["is_true", ">=", ">", "<=", "<", "==", "in"]
    # Strict and finite: `True` is not 1.0 and NaN would make every comparison fail.
    threshold: Annotated[float, Field(strict=True, allow_inf_nan=False)] | None = None
    categories: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _operands_match_comparator(self) -> DecisionRule:
        if self.comparator in (">=", ">", "<=", "<", "==") and self.threshold is None:
            raise ValueError(f"comparator {self.comparator!r} needs a numeric threshold")
        if self.comparator == "in" and not self.categories:
            raise ValueError("comparator 'in' needs at least one category")
        return self


class FieldRequirement(FrozenModel):
    """A field an evaluator reads from the evaluation view."""

    path: str
    non_empty: bool = True


class EvaluatorManifest(FrozenModel):
    """What an evaluator is and needs, readable without running it."""

    evaluator_id: str = Field(pattern=r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*$")
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    plugin_id: str
    plugin_version: str
    core_schema: str = ">=1.0.0,<2.0.0"
    description: str
    limitations: tuple[str, ...] = ()
    value_kind: ValueKind
    direction: MetricDirection
    scope: MetricScope = MetricScope.CASE
    aggregation: Literal["rate", "mean", "category_counts", "none"]
    requires: tuple[FieldRequirement, ...] = ()
    default_rule: DecisionRule | None = None
    parameters_schema: FrozenValue = Field(default_factory=dict)
    consumes: Literal["recorded_outputs", "owns_execution"] = "recorded_outputs"
    uses_models: bool = False
    credentials: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
    supports_batch: bool = False
    internal_retries: int = 0
    requires_worker: bool = False


class MetricBinding(FrozenModel):
    """One metric binding with parameters and an optional rule override."""

    metric: str
    params: FrozenValue = Field(default_factory=dict)
    rule: DecisionRule | None = None


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
    # Added in Prompt 04 (optional so earlier records still load; ADR 0003).
    schema_version: str = SCHEMA_VERSION
    scoring_id: str | None = None
    execution_id: str | None = None
    repetition_id: int = 0
    attempt_number: int = 0
    scope: MetricScope = MetricScope.CASE
    direction: MetricDirection | None = None
    rule: DecisionRule | None = None
    binding_hash: str | None = None
    reason: str | None = None


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
    application_id: str | None = None  # added in Prompt 04; lets scoring check applicability


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
    CliTransport,
    HttpSecretHeader,
    HttpTransport,
    ApplicationSpec,
    ObservationClaim,
    EvaluationPlan,
    ExecutionResult,
    MetricValue,
    DecisionRule,
    FieldRequirement,
    EvaluatorManifest,
    MetricBinding,
    EvaluationResult,
    RunManifest,
    ArtifactRef,
    WorkItem,
    UsageEvent,
    Approval,
)
