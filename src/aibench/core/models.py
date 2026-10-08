"""Canonical, immutable domain models (specification §2, §5).

This module must not import any evaluator framework, storage engine, or UI package
(see `docs/adr/0001-source-of-truth-and-dependency-direction.md`). It depends only on
pydantic and the standard library.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from math import prod
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


class CandidateStatus(str, Enum):
    CANDIDATE = "candidate"
    REVIEWED = "reviewed"
    VERIFIED = "verified"
    REJECTED = "rejected"
    PROMOTED = "promoted"


class ToolMatchMode(str, Enum):
    CONTAINS_ALL = "contains_all"
    EXACT = "exact"
    ORDERED_SUBSEQUENCE = "ordered_subsequence"


class RunnerKind(str, Enum):
    CLI = "cli"
    HTTP = "http"
    PYTHON = "python"
    CONTAINER = "container"
    OPENAI_COMPATIBLE = "openai_compatible"


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
        return all([self.commit, self.setup_recipe, self.hidden_tests_ref, self.success_criteria])


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
            "fixtures": {f.name: deep_unfreeze(f.content) for f in self.fixtures if f.app_visible},
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


class ExposedApplicationParameter(FrozenModel):
    """An application-owned, finite environment setting safe to vary in an experiment.

    Only names under the dedicated AIBENCH_TUNABLE_ prefix can be exposed. Secret and
    host/runtime variables are never accepted as experiment parameters.
    """

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    environment_key: str = Field(pattern=r"^AIBENCH_TUNABLE_[A-Z0-9_]{1,80}$")
    default_value: str = Field(max_length=256)
    allowed_values: tuple[str, ...] = Field(min_length=2, max_length=16)
    description: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def _finite_domain(self) -> ExposedApplicationParameter:
        if len(set(self.allowed_values)) != len(self.allowed_values):
            raise ValueError("exposed parameter allowed_values must be unique")
        if self.default_value not in self.allowed_values:
            raise ValueError("exposed parameter default_value must be in allowed_values")
        return self


class ExperimentParameterValues(FrozenModel):
    """The finite values selected for one application-exposed parameter."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    values: tuple[str, ...] = Field(min_length=2, max_length=16)

    @model_validator(mode="after")
    def _unique_values(self) -> ExperimentParameterValues:
        if len(set(self.values)) != len(self.values):
            raise ValueError(f"parameter {self.name!r} contains duplicate values")
        return self


class ExperimentObjective(FrozenModel):
    """One fixed case-scoped metric binding to maximize or minimize."""

    binding_index: int = Field(ge=0)


class ExperimentConstraint(FrozenModel):
    """A frozen threshold over a numeric or binary-rate metric binding."""

    binding_index: int = Field(ge=0)
    comparator: Literal[">=", ">", "<=", "<"]
    threshold: float = Field(allow_inf_nan=False)


class ExperimentBudget(FrozenModel):
    """Search budget; each individual run also obeys the frozen plan's BudgetLimits."""

    max_trials: int = Field(ge=1, le=128)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    bootstrap_replicates: int = Field(default=2_000, ge=100, le=20_000)
    min_metric_coverage: float = Field(default=0.95, ge=0, le=1, allow_inf_nan=False)
    min_paired_coverage: float = Field(default=0.95, ge=0, le=1, allow_inf_nan=False)


class ExperimentDefinition(FrozenModel):
    """A controlled finite-grid experiment over a development dataset and fixed plan."""

    experiment_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
    plan: str = Field(min_length=1)
    development_dataset: str = Field(min_length=1)
    holdout_dataset: str = Field(min_length=1)
    intended_change: str = Field(min_length=1, max_length=2_000)
    parameters: tuple[ExperimentParameterValues, ...] = Field(min_length=1, max_length=5)
    objective: ExperimentObjective
    constraints: tuple[ExperimentConstraint, ...] = Field(default_factory=tuple, max_length=16)
    budget: ExperimentBudget

    @model_validator(mode="after")
    def _bounded_unique_space(self) -> ExperimentDefinition:
        names = [parameter.name for parameter in self.parameters]
        if len(set(names)) != len(names):
            raise ValueError("experiment parameters must have unique names")
        combinations = prod(len(parameter.values) for parameter in self.parameters)
        if combinations > 128:
            raise ValueError("experiment parameter space exceeds 128 combinations")
        return self


class ExperimentStatus(str, Enum):
    READY = "ready"
    RUNNING = "running"
    BUDGET_EXHAUSTED = "budget_exhausted"
    SELECTED = "selected"
    NO_FEASIBLE_TRIAL = "no_feasible_trial"
    HOLDOUT_RUNNING = "holdout_running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExperimentTrialStatus(str, Enum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ExperimentEventKind(str, Enum):
    CREATED = "created"
    STARTED = "started"
    BUDGET_EXTENDED = "budget_extended"
    BUDGET_EXHAUSTED = "budget_exhausted"
    TRIAL_STARTED = "trial_started"
    TRIAL_COMPLETED = "trial_completed"
    TRIAL_FAILED = "trial_failed"
    SELECTED = "selected"
    NO_FEASIBLE_TRIAL = "no_feasible_trial"
    HOLDOUT_STARTED = "holdout_started"
    HOLDOUT_COMPLETED = "holdout_completed"
    ADOPTION_PROPOSED = "adoption_proposed"


class ExperimentRecord(FrozenModel):
    """Durable frozen experiment contract and its separately managed phase transitions."""

    experiment_id: str
    definition: ExperimentDefinition
    definition_hash: str
    definition_artifact_id: str
    plan_hash: str
    plan_artifact_id: str
    application_hash: str
    application_code_hash: str
    evaluator_contract_hash: str
    objective_metric_id: str
    objective_direction: str
    objective_binding_hash: str
    policy_hash: str
    effective_policy: FrozenValue
    trusted_local: bool = False
    development_dataset_hash: str
    holdout_dataset_hash: str
    spec_path: str
    trial_limit: int = Field(ge=1, le=128)
    status: ExperimentStatus = ExperimentStatus.READY
    selected_trial_id: str | None = None
    selection_locked_at: datetime | None = None
    holdout_plan_hash: str | None = None
    holdout_plan_artifact_id: str | None = None
    holdout_baseline_run_id: str | None = None
    holdout_run_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ExperimentTrial(FrozenModel):
    experiment_id: str
    trial_id: str
    ordinal: int = Field(ge=0, le=127)
    run_id: str
    parameters: FrozenValue
    parameter_hash: str
    status: ExperimentTrialStatus = ExperimentTrialStatus.PENDING
    objective_value: float | None = Field(default=None, allow_inf_nan=False)
    metrics: FrozenValue = Field(default_factory=dict)
    constraints_passed: bool | None = None
    comparison_to_baseline: FrozenValue = None
    failure: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ExperimentEvent(FrozenModel):
    event_id: str
    experiment_id: str
    kind: ExperimentEventKind
    actor: str
    details: FrozenValue = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class CandidateSourceDocument(FrozenModel):
    """One bounded, user-selected development source and any exact-content aliases."""

    source_ref: str
    digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    character_count: int = Field(ge=1)
    line_count: int = Field(ge=1)
    duplicate_of: str | None = None


class CandidateSourceSpan(FrozenModel):
    """A precise quote location in an unchanged source document.

    Offsets are Unicode code-point offsets, end-exclusive. The source digest makes a stale
    path fail closed during review or executable verification.
    """

    source_ref: str
    source_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    start_offset: int = Field(ge=0)
    end_offset: int = Field(gt=0)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @model_validator(mode="after")
    def _span_is_ordered(self) -> CandidateSourceSpan:
        if self.end_offset <= self.start_offset:
            raise ValueError("source span end_offset must be greater than start_offset")
        if self.end_line < self.start_line:
            raise ValueError("source span end_line must not precede start_line")
        return self


class CandidateVerification(FrozenModel):
    method: Literal["human", "executable"]
    outcome: Literal["passed", "failed"]
    verifier_id: str = Field(min_length=1, max_length=200)
    actor: str | None = Field(default=None, max_length=200)
    detail: str = Field(min_length=1, max_length=2000)
    recorded_at: datetime = Field(default_factory=utcnow)


class CandidatePoolManifest(FrozenModel):
    """Metadata for a development-only generation job.

    Holdout cases do not have a representable pool type, so a generation service cannot
    accidentally accept them as prompt context.
    """

    pool_id: str
    split_id: Literal["development"] = "development"
    generator_identity: str = Field(min_length=1, max_length=300)
    prompt_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    sources: tuple[CandidateSourceDocument, ...] = Field(min_length=1)
    candidate_ids: tuple[str, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _candidate_ids_unique(self) -> CandidatePoolManifest:
        if len(self.candidate_ids) != len(set(self.candidate_ids)):
            raise ValueError("candidate_ids must be unique")
        return self


class DatasetCandidate(FrozenModel):
    """A generated case and its review lifecycle, separate from trusted datasets."""

    candidate_id: str
    pool_id: str
    split_id: Literal["development"] = "development"
    case: BenchmarkCase
    source_spans: tuple[CandidateSourceSpan, ...] = Field(min_length=1)
    status: CandidateStatus = CandidateStatus.CANDIDATE
    verifications: tuple[CandidateVerification, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _lifecycle_matches_reference(self) -> DatasetCandidate:
        if self.case.reference is None:
            raise ValueError("a generated candidate must have a judge-only reference")
        reference_status = self.case.reference.status
        if self.status is CandidateStatus.CANDIDATE and reference_status is not ReferenceStatus.SYNTHETIC_UNVERIFIED:
            raise ValueError("an unreviewed candidate must have synthetic_unverified reference status")
        if self.status is CandidateStatus.REVIEWED and reference_status not in (
            ReferenceStatus.SOURCE_VERIFIED,
            ReferenceStatus.HUMAN_REVIEWED,
        ):
            raise ValueError("a reviewed candidate needs a recorded trusted human reference status")
        if self.status is CandidateStatus.VERIFIED and reference_status is not ReferenceStatus.EXECUTABLE_ORACLE:
            raise ValueError("an executable-verified candidate needs executable_oracle reference status")
        if self.status is CandidateStatus.PROMOTED and reference_status not in (
            ReferenceStatus.SOURCE_VERIFIED,
            ReferenceStatus.HUMAN_REVIEWED,
            ReferenceStatus.EXECUTABLE_ORACLE,
        ):
            raise ValueError("a promoted candidate must have a trusted reference status")
        if self.status is CandidateStatus.REVIEWED and not any(
            item.method == "human" and item.outcome == "passed" for item in self.verifications
        ):
            raise ValueError("a reviewed candidate needs a passed human verification record")
        if self.status is CandidateStatus.VERIFIED and not any(
            item.method == "executable" and item.outcome == "passed"
            for item in self.verifications
        ):
            raise ValueError("an executable-verified candidate needs a passed oracle record")
        if self.status is CandidateStatus.REJECTED and not any(
            item.method == "human" and item.outcome == "failed" for item in self.verifications
        ):
            raise ValueError("a rejected candidate needs a failed human review record")
        if self.status is CandidateStatus.PROMOTED and not any(
            item.outcome == "passed" for item in self.verifications
        ):
            raise ValueError("a promoted candidate needs a passed verification record")
        return self


CandidateEventKind = Literal[
    "generated",
    "reviewed_source",
    "reviewed_human",
    "review_rejected",
    "executable_check_passed",
    "executable_check_failed",
    "promoted",
]


class CandidateEvent(FrozenModel):
    event_id: str
    candidate_id: str
    kind: CandidateEventKind
    actor: str
    details: FrozenValue = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class EpisodeSimulatorProvenance(FrozenModel):
    kind: Literal["human", "scripted", "model"]
    identity: str = Field(min_length=1, max_length=300)
    model: str | None = None
    prompt_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    seed: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _model_simulator_has_provenance(self) -> EpisodeSimulatorProvenance:
        if self.kind == "model" and (not self.model or not self.prompt_hash):
            raise ValueError("a model user simulator requires model and prompt_hash")
        return self


class EpisodeSuccessCriterion(FrozenModel):
    """Independent final-state assertions evaluated against captured world state."""

    evaluator_id: Literal["native.final_state"] = "native.final_state"
    evidence_field: Literal["execution.world_state"] = "execution.world_state"
    assertions: tuple[FrozenValue, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _assertions_are_executable(self) -> EpisodeSuccessCriterion:
        allowed = {"equals", "not_equals", "in", "matches", "min", "max", "length", "present", "absent"}
        for assertion in self.assertions:
            assertion = deep_unfreeze(assertion)
            if not isinstance(assertion, dict) or not isinstance(assertion.get("path"), str):
                raise ValueError(  # noqa: TRY004 -- Pydantic turns this into a schema validation error.
                    "each final-state assertion needs a JSON-pointer path"
                )
            constraints = set(assertion) - {"path"}
            if not constraints or constraints - allowed:
                raise ValueError("each final-state assertion needs supported constraints")
        return self


class MultiTurnTextEpisode(FrozenModel):
    """An ordered, resettable application conversation, distinct from harness chat."""

    episode_id: str
    split_id: Literal["development", "validation", "holdout"]
    case_ids: tuple[str, ...] = Field(min_length=2)
    simulator: EpisodeSimulatorProvenance
    test_world_id: str
    success_criterion: EpisodeSuccessCriterion

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> MultiTurnTextEpisode:
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("episode case_ids must be unique and ordered")
        return self


class TextEpisodeManifest(FrozenModel):
    schema_version: str = SCHEMA_VERSION
    episodes: tuple[MultiTurnTextEpisode, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _episode_ids_are_unique(self) -> TextEpisodeManifest:
        ids = [episode.episode_id for episode in self.episodes]
        if len(ids) != len(set(ids)):
            raise ValueError("episode IDs must be unique")
        return self


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
    # Resets state the application keeps outside its process (files, a local database):
    # run with the selected test world's seed as JSON on stdin (null when none is selected).
    reset_argv: tuple[str, ...] | None = None


class PythonTransport(FrozenModel):
    """A Python callable (§7 "Python callable"), `module:function` or `path/file.py:function`.
    It runs in a fresh interpreter process per invocation through a small standard-library
    shim, so timeouts, cancellation and process-tree cleanup are those of the CLI protocol.
    The function receives the bound input and returns a JSON document (an object is used as
    the response document; any other value becomes `{"output": value}`)."""

    kind: Literal["python"] = "python"
    callable: str = Field(pattern=r"^.+:[A-Za-z_][A-Za-z0-9_]*$")
    python: str = "python"  # interpreter of the application's environment
    paths: tuple[str, ...] = ()  # extra import paths, relative to the config file's directory
    cwd: str | None = None
    timeout_seconds: float = Field(default=60.0, gt=0, le=86_400)
    max_stdout_bytes: int = Field(default=1_048_576, gt=0)
    max_stderr_bytes: int = Field(default=65_536, ge=0)
    env: FrozenStrMap = Field(default_factory=dict)
    secret_env: FrozenSecretRefMap = Field(default_factory=dict)
    inherit_env: tuple[str, ...] = DEFAULT_INHERITED_ENV
    reset_callable: str | None = Field(default=None, pattern=r"^.+:[A-Za-z_][A-Za-z0-9_]*$")


_IMAGE_DIGEST = r"^[a-z0-9][a-z0-9._/:-]*@sha256:[0-9a-f]{64}$"
# Container-side paths: absolute, plain characters only (no ',' '=' or quotes that could
# add fields to the engine's `--mount` value), no `.` or `..` segments.
_CONTAINER_PATH = r"^/(?:[A-Za-z0-9_.-]+/?)*$"
_CONTAINER_ENGINE_ENV_NAMES = frozenset(
    {
        "PATH",
        "PATHEXT",
        "COMSPEC",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "XDG_RUNTIME_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "LD_AUDIT",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "PYTHONSTARTUP",
        "NODE_OPTIONS",
        "RUBYOPT",
        "PERL5OPT",
        "JAVA_TOOL_OPTIONS",
        "_JAVA_OPTIONS",
        "BASH_ENV",
        "ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    }
)


def _container_engine_env_problem(name: str) -> str | None:
    normalized = name.upper()
    if not name.isascii() or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return "environment variable names must use letters, digits and underscores"
    if normalized in _CONTAINER_ENGINE_ENV_NAMES or normalized.startswith(
        ("DOCKER_", "PODMAN_", "CONTAINER_", "CONTAINERS_")
    ):
        return (
            f"{name} can change or execute code in the host container-engine client; "
            "use a different name"
        )
    return None


def _engine_socket_path(path: str) -> bool:
    """Whether a host path is, or is a directory holding, a container engine socket, after
    normalizing separators, repeated slashes and `.`/`..` segments."""
    import posixpath

    normalized = posixpath.normpath(path.replace("\\", "/")).lower()
    name = posixpath.basename(normalized)
    return (
        name in ("docker.sock", "docker_engine", "podman.sock", "containerd.sock")
        or normalized in ("/var/run", "/run", "/var/run/docker", "/run/docker")
        or "pipe/docker_engine" in normalized
    )


class ContainerMount(FrozenModel):
    """A read-only bind mount. Writable bind mounts are not offered (§16: read-only source
    mounts; writable space is tmpfs only)."""

    source: str  # host path, relative to the config file's directory
    target: str = Field(pattern=_CONTAINER_PATH)

    @model_validator(mode="after")
    def _not_the_engine_socket(self) -> ContainerMount:
        if _engine_socket_path(self.source):
            raise ValueError("mounting the container engine socket is not allowed")
        if any(part in ("..", ".") for part in self.target.split("/")):
            raise ValueError("a mount target cannot contain '.' or '..' segments")
        return self


class ContainerTransport(FrozenModel):
    """One container per invocation (§7 "Container", §16): an image pinned by digest, a
    non-root user, a read-only root filesystem, read-only bind mounts, tmpfs for scratch
    space, no Linux capabilities, resource limits, and no network unless the policy allows
    it. JSON on stdin and stdout, as in the CLI protocol. Not a hostile multi-tenant
    sandbox."""

    kind: Literal["container"] = "container"
    image: str = Field(pattern=_IMAGE_DIGEST)
    argv: tuple[str, ...] = Field(min_length=1)
    workdir: str | None = Field(default=None, pattern=_CONTAINER_PATH)
    mounts: tuple[ContainerMount, ...] = ()
    tmpfs: tuple[Annotated[str, Field(pattern=_CONTAINER_PATH)], ...] = ("/tmp",)
    tmpfs_size_mb: int = Field(default=64, gt=0, le=4096)
    user: str = Field(default="65534:65534", pattern=r"^[0-9]+(:[0-9]+)?$")
    network: Literal["none", "bridge"] = "none"
    memory_mb: int = Field(default=512, ge=16, le=65_536)
    cpus: float = Field(default=1.0, gt=0, le=64)
    pids_limit: int = Field(default=128, ge=8, le=65_536)
    output_mode: Literal["json", "text"] = "json"
    timeout_seconds: float = Field(default=120.0, gt=0, le=86_400)
    max_stdout_bytes: int = Field(default=1_048_576, gt=0)
    max_stderr_bytes: int = Field(default=65_536, ge=0)
    env: FrozenStrMap = Field(default_factory=dict)
    secret_env: FrozenSecretRefMap = Field(default_factory=dict)
    # This is a fixed, host-installed client, never an executable path from the app config.
    engine: Literal["docker"] = "docker"

    @model_validator(mode="after")
    def _container_process_is_safe(self) -> ContainerTransport:
        ids = [int(part) for part in self.user.split(":")]  # "00" is uid 0 too
        if 0 in ids:
            raise ValueError("containers run as a non-root user and group; id 0 is not allowed")
        names = set(self.env) | set(self.secret_env)
        overlap = set(self.env) & set(self.secret_env)
        if overlap:
            raise ValueError(
                "container environment variables cannot be both plain and secret: "
                + ", ".join(sorted(overlap))
            )
        for name in sorted(names):
            problem = _container_engine_env_problem(name)
            if problem:
                raise ValueError(problem)
        return self


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
    # POSTed with the selected test world's seed as the JSON body ({} when none is selected).
    reset_url: str | None = None


class OpenAICompatibleTransport(FrozenModel):
    """An OpenAI-compatible chat-completions endpoint as the application (§7). Observes the
    model response and the usage the endpoint reports, never hidden application internals.
    Tool calls in a response are requests by the model, not executed effects. Unrelated to
    any OpenAI evaluator plugin."""

    kind: Literal["openai_compatible"] = "openai_compatible"
    base_url: str  # e.g. "https://api.openai.com/v1"; requests go to {base_url}/chat/completions
    model: str = Field(min_length=1)
    api_key: SecretRefStr | None = None
    system_prompt: str | None = None
    parameters: FrozenValue = Field(default_factory=dict)  # temperature, max_tokens, seed, ...
    tools: tuple[FrozenValue, ...] = ()  # tool schemas offered to the model
    headers: FrozenStrMap = Field(default_factory=dict)
    verify_tls: bool = True
    allow_plaintext_http: bool = False
    timeout_seconds: float = Field(default=120.0, gt=0, le=86_400)
    connect_timeout_seconds: float = Field(default=10.0, gt=0, le=600)
    max_request_bytes: int = Field(default=1_048_576, gt=0)
    max_response_bytes: int = Field(default=4_194_304, gt=0)


class TestWorldSpec(FrozenModel):
    """A named, versioned starting state for a stateful application (§7 "State"): the seed
    is sent to the application's reset hook before each case or episode. Seeds describe a
    test double or an ephemeral environment, never production."""

    __test__ = False  # not a pytest test class

    seed_file: str | None = None  # JSON, relative to the config file's directory
    seed: FrozenValue = None
    description: str = ""

    @model_validator(mode="after")
    def _one_seed(self) -> TestWorldSpec:
        if (self.seed_file is None) == (self.seed is None):
            raise ValueError("a test world needs exactly one of seed_file or seed")
        return self


ApplicationTransport = Annotated[
    CliTransport | HttpTransport | PythonTransport | ContainerTransport | OpenAICompatibleTransport,
    Field(discriminator="kind"),
]


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
    transport: ApplicationTransport | None = None
    # Starting states the application's reset hook accepts; a plan selects one by name.
    test_worlds: Annotated[
        dict[str, TestWorldSpec],
        AfterValidator(MappingProxyType),
        PlainSerializer(dict, return_type=dict[str, TestWorldSpec]),
    ] = Field(default_factory=dict)
    # The application owner explicitly selects a finite set of environment values that a
    # controlled experiment may vary. No arbitrary config paths or source files are tunable.
    exposed_parameters: tuple[ExposedApplicationParameter, ...] = ()

    @model_validator(mode="after")
    def _transport_matches_runner(self) -> ApplicationSpec:
        if self.transport is not None and self.transport.kind != self.runner.value:
            raise ValueError(
                f"transport kind {self.transport.kind!r} does not match runner "
                f"{self.runner.value!r}"
            )
        parameter_names = [parameter.name for parameter in self.exposed_parameters]
        environment_keys = [parameter.environment_key for parameter in self.exposed_parameters]
        if len(set(parameter_names)) != len(parameter_names):
            raise ValueError("application exposed_parameters have duplicate names")
        if len(set(environment_keys)) != len(environment_keys):
            raise ValueError("application exposed_parameters have duplicate environment keys")
        if self.exposed_parameters:
            environment = getattr(self.transport, "env", None)
            if environment is None:
                raise ValueError(
                    "exposed_parameters require a runner transport with a configured env mapping"
                )
            for parameter in self.exposed_parameters:
                if environment.get(parameter.environment_key) != parameter.default_value:
                    raise ValueError(
                        f"exposed parameter {parameter.name!r} must match its transport env "
                        "default_value"
                    )
        return self

    def effective_output_binding(self) -> dict[str, Any]:
        """The output binding in force: the transport's defaults (an OpenAI-compatible
        endpoint's documented response fields) overlaid by what the config declares."""
        declared = deep_unfreeze(self.output_binding) or {}
        if isinstance(self.transport, OpenAICompatibleTransport):
            return {**OPENAI_COMPATIBLE_OUTPUT_BINDING, **declared}
        return dict(declared)


# The chat-completions response fields an OpenAI-compatible endpoint documents.
OPENAI_COMPATIBLE_OUTPUT_BINDING: dict[str, str] = {
    "output": "/choices/0/message/content",
    "usage": "/usage",
    "tool_events": "/choices/0/message/tool_calls",
}


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
    # The test world's state after the invocation, when the application reports it.
    world_state: FrozenValue = None
    # Set when this record is a copy from the execution cache (16-T3): its source and key.
    cache: FrozenValue = None
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
    # Installed distribution identity when the adapter can declare it. These fields were
    # added in Prompt 14; older manifests remain loadable and report them as unknown.
    package_name: str | None = None
    package_version: str | None = None
    core_schema: str = ">=1.0.0,<2.0.0"
    description: str
    limitations: tuple[str, ...] = ()
    # What the metric measures, in the planner's concept vocabulary (planning.catalog). When
    # empty, concepts are read from the fields the metric requires.
    concepts: tuple[str, ...] = ()
    value_kind: ValueKind
    direction: MetricDirection
    scope: MetricScope = MetricScope.CASE
    aggregation: Literal["rate", "mean", "category_counts", "none"]
    requires: tuple[FieldRequirement, ...] = ()
    # Fields a parameter adds, when the metric reads what the plan names: parameter ->
    # listed value -> requirement, e.g. {"evaluation_params": {"expected_output":
    # {"path": "case.reference.answer", "non_empty": true}}}. Read by `required_fields`.
    parameter_requirements: FrozenValue = Field(default_factory=dict)
    # A field a parameter's TEXT adds, for a metric whose criteria name what they read:
    # parameter -> {"pattern": regex, "requires": requirement, "unless_set": other
    # parameter}. The requirement applies when the regex matches the parameter's text (or
    # any item of a list) and `unless_set`, if given, is absent. Read by `required_fields`.
    parameter_patterns: FrozenValue = Field(default_factory=dict)
    default_rule: DecisionRule | None = None
    parameters_schema: FrozenValue = Field(default_factory=dict)
    # recorded_outputs: scores stored executions per case (the default).
    # owns_execution: runs the application itself (a delegated suite).
    # remote_job: results arrive from an external job (submit/poll/fetch), never from a
    # per-case evaluate call; a plan cannot bind it (17-T2).
    # paired_runs: judges two runs' answers to the same case against each other
    # (`comparison.output` beside `execution.output`); run by a comparison, never by a plan.
    consumes: Literal["recorded_outputs", "owns_execution", "remote_job", "paired_runs"] = (
        "recorded_outputs"
    )
    uses_models: bool = False
    credentials: tuple[str, ...] = ()
    network_destinations: tuple[str, ...] = ()
    supports_batch: bool = False
    internal_retries: int = Field(default=0, ge=0)  # retries it performs itself, per evaluation
    internal_concurrency: int = Field(default=1, ge=1)  # parallel judge calls per evaluation
    requires_worker: bool = False


class MetricBinding(FrozenModel):
    """One metric binding with parameters and an optional rule override."""

    metric: str
    params: FrozenValue = Field(default_factory=dict)
    rule: DecisionRule | None = None


class IdentityComponent(FrozenModel):
    """One compatibility component without exposing its configuration to chat.

    `verified=False` means the historical record did not contain enough evidence for a
    strict comparison. The digest is for equality checks, not as a substitute for the
    underlying frozen manifest/profile.
    """

    kind: str
    digest: str | None = None
    verified: bool


class EvaluationCompatibilityIdentity(FrozenModel):
    """Canonical identity frozen with a scoring pass and copied into each result (§12).

    This is additive metadata, not a replacement for `binding_hash`: that hash identifies
    metric semantics, while this record also separates judge, rubric, plugin implementation,
    dependency and instrumentation identities. Unknown required components fail closed in
    strict comparison but remain available to explicitly exploratory diagnostics.
    """

    schema_version: str = "aibench.evaluation-identity/1"
    metric_id: str
    metric_version: str
    value_kind: ValueKind
    direction: MetricDirection
    scope: MetricScope
    aggregation: str
    binding_hash: str
    parameters_hash: str
    rule: DecisionRule | None = None
    plugin_id: str
    plugin_version: str
    package_name: str | None = None
    package_version: str | None = None
    dependency_lock_hash: str | None = None
    judge: IdentityComponent
    rubric: IdentityComponent
    instrumentation: IdentityComponent
    required_fields: tuple[str, ...] = Field(default_factory=tuple)
    final_attempt_rule: str
    compatibility_hash: str


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
    last_error: str | None = None  # why the item is failed/blocked/unknown_effect (Prompt 06)


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
    ExposedApplicationParameter,
    ExperimentParameterValues,
    ExperimentObjective,
    ExperimentConstraint,
    ExperimentBudget,
    ExperimentDefinition,
    ExperimentRecord,
    ExperimentTrial,
    ExperimentEvent,
    CandidateSourceDocument,
    CandidateSourceSpan,
    CandidateVerification,
    CandidatePoolManifest,
    DatasetCandidate,
    CandidateEvent,
    EpisodeSimulatorProvenance,
    EpisodeSuccessCriterion,
    MultiTurnTextEpisode,
    TextEpisodeManifest,
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
    IdentityComponent,
    EvaluationCompatibilityIdentity,
    EvaluationResult,
    RunManifest,
    ArtifactRef,
    WorkItem,
    UsageEvent,
    Approval,
)
