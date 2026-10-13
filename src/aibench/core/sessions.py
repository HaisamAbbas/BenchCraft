"""Benchmark session records (§2, §5, 08-T1).

A session is a persistent conversation linked to a project, the user's decisions, draft plan
revisions and runs. Everything a session changes goes through these typed records:

- `SessionChoices`: what the user has decided at one session revision (objectives, case
  selection, repetitions, budgets, metric parameters and rules, dataset). The draft plan is
  derived from it; a conversation never edits a plan directly.
- `PlanPatch`: a typed change to those choices. It is the only way a turn — the user's own
  command or a model's interpretation of a message — changes a draft.
- `DecisionRecord`: one accepted patch, linked to the turn it came from, the revision it
  produced, the revision it supersedes and the validated draft.
- `PendingQuestion`: a clarification tied to the revision it was asked against.
- `ActionRequest`: a typed request to act on a run (start, pause, resume, cancel), with a
  stable ID so redelivery cannot act twice.

Like `core.models`, this module depends only on pydantic and other core modules.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import AfterValidator, Field, PlainSerializer, model_validator

from aibench.core.models import DecisionRule, FrozenModel, FrozenValue, deep_unfreeze, utcnow
from aibench.core.plans import BudgetLimits, CaseSelection, ReleaseGate

_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"

# Each evaluator's parameters are themselves frozen (nested read-only mappings), so the
# whole value is unwrapped recursively: `dict` alone left inner mappings unserializable.
FrozenParams = Annotated[
    dict[str, FrozenValue],
    AfterValidator(MappingProxyType),
    PlainSerializer(deep_unfreeze, return_type=dict[str, object]),
]
FrozenConcepts = Annotated[
    dict[str, tuple[str, ...]],
    AfterValidator(MappingProxyType),
    PlainSerializer(dict, return_type=dict[str, tuple[str, ...]]),
]
FrozenCursors = Annotated[
    dict[str, int],
    AfterValidator(MappingProxyType),
    PlainSerializer(dict, return_type=dict[str, int]),
]
FrozenRules = Annotated[
    dict[str, DecisionRule],
    AfterValidator(MappingProxyType),
    PlainSerializer(dict, return_type=dict[str, DecisionRule]),
]


class PendingQuestion(FrozenModel):
    """§5 PendingQuestion: a structured clarification, tied to the draft revision it was
    asked against so a later answer cannot silently apply to a newer draft."""

    question_id: str
    prompt: str
    required_fields: tuple[str, ...]
    choices: tuple[str, ...]
    blocking_scope: str
    draft_revision: int
    status: Literal["open", "answered", "stale"] = "open"


class SessionReleaseGate(FrozenModel):
    """A session gate targets a stable metric identity, not a draft's moving index."""

    gate_id: str = Field(min_length=1, max_length=100)
    metric: str = Field(min_length=1)
    occurrence: int = Field(default=0, ge=0)
    min_pass_rate: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    min_completed_coverage: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def _has_threshold(self) -> SessionReleaseGate:
        if self.min_pass_rate is None and self.min_completed_coverage is None:
            raise ValueError(f"gate {self.gate_id!r} needs min_pass_rate or min_completed_coverage")
        return self


class SessionChoices(FrozenModel):
    """The user's benchmark decisions at one session revision."""

    application: str  # absolute path of the application config
    dataset: str  # absolute path of the dataset
    objectives: tuple[str, ...] = ()
    # Concepts the user chose for an objective's text (answers "Which concept does ... mean?").
    objective_concepts: FrozenConcepts = Field(default_factory=dict)
    selection: CaseSelection = Field(default_factory=CaseSelection)
    repetitions: int = Field(default=1, ge=1, le=10)
    budgets: BudgetLimits = Field(default_factory=BudgetLimits)
    params: FrozenParams = Field(default_factory=dict)  # evaluator_id -> parameters
    rules: FrozenRules = Field(default_factory=dict)  # evaluator_id -> pass/fail rule
    gates: tuple[SessionReleaseGate, ...] = ()
    # A test world the application declares (15-T3); the policy must approve it.
    test_world: str | None = None


class SamplePatch(FrozenModel):
    """A seeded sample of `size` cases (§3 "a seeded 20-case pilot"). Without a seed, the
    current one is kept, or the session's own stable seed is used; either way the sample
    is recorded with its seed and is reproducible."""

    size: int = Field(ge=1)
    seed: int | None = Field(default=None, ge=0)


class BudgetPatch(FrozenModel):
    """Budget limits to set; a field left out keeps its current value."""

    max_application_calls: int | None = Field(default=None, ge=1)
    max_evaluator_calls: int | None = Field(default=None, ge=1)
    max_judge_tokens: int | None = Field(default=None, ge=1)
    max_wall_seconds: float | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    estimated_cost_per_application_call_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False
    )
    estimated_cost_per_evaluation_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class PlanPatch(FrozenModel):
    """A typed change to the session's choices. Selection edits are mutually exclusive:
    `sample`, `limit` or `all_cases`."""

    add_objectives: tuple[Annotated[str, Field(min_length=1, max_length=500)], ...] = ()
    remove_objectives: tuple[str, ...] = ()
    objective_concepts: FrozenConcepts = Field(default_factory=dict)  # objective text -> concepts
    sample: SamplePatch | None = None
    limit: int | None = Field(default=None, ge=1)
    all_cases: bool = False
    repetitions: int | None = Field(default=None, ge=1, le=10)
    budgets: BudgetPatch | None = None
    params: FrozenParams = Field(default_factory=dict)  # evaluator_id -> parameters
    rules: FrozenRules = Field(default_factory=dict)  # evaluator_id -> pass/fail rule
    gates: tuple[ReleaseGate, ...] | None = Field(
        default=None,
        description=(
            "Replace all release gates for this draft; [] clears them. Each binding is the "
            "zero-based index into the current draft's metrics list."
        ),
    )
    dataset: str | None = Field(default=None, min_length=1)  # relative to the project root
    # Select one of the application's declared test worlds, or clear the selection.
    test_world: str | None = Field(default=None, min_length=1, max_length=200)
    clear_test_world: bool = False
    answers: tuple[str, ...] = ()  # question IDs this patch answers

    @model_validator(mode="after")
    def _one_selection_edit(self) -> PlanPatch:
        edits = [self.sample is not None, self.limit is not None, self.all_cases]
        if sum(edits) > 1:
            raise ValueError("use one of sample, limit or all_cases")
        if self.test_world is not None and self.clear_test_world:
            raise ValueError("use one of test_world or clear_test_world")
        return self

    def is_empty(self) -> bool:
        return not self.scope_fields() and not self.answers

    def scope_fields(self) -> set[str]:
        """The benchmark-definition fields this patch changes (§8: such changes create a
        new draft revision; they never touch a running run's frozen plan)."""
        fields = set()
        if self.add_objectives or self.remove_objectives or self.objective_concepts:
            fields.add("objectives")
        if self.sample is not None or self.limit is not None or self.all_cases:
            fields.add("selection")
        if self.repetitions is not None:
            fields.add("repetitions")
        if self.budgets is not None:
            fields.add("budgets")
        fields.update(f"params.{key}" for key in self.params)
        fields.update(f"rule.{key}" for key in self.rules)
        if self.gates is not None:
            fields.add("gates")
        if self.dataset is not None:
            fields.add("dataset")
        if self.test_world is not None or self.clear_test_world:
            fields.add("test_world")
        return fields


class DecisionRecord(FrozenModel):
    """§5 DecisionRecord: one accepted change, linked to its source turn and the exact
    draft revision it produced."""

    decision_id: str
    session_id: str
    source_turn_id: str | None  # None for the draft made when the session was created
    source: Literal["session", "user", "assistant"]
    revision: int = Field(ge=1)
    supersedes: str | None = None  # the previous decision's ID
    structured_change: FrozenValue = Field(default_factory=dict)  # the PlanPatch applied
    choices: SessionChoices
    plan_file: str  # relative to the session directory; content-addressed, never rewritten
    plan_hash: str
    executable: bool
    draft: FrozenValue  # the validated draft document (planning.drafts.PlanDraft)
    created_at: datetime = Field(default_factory=utcnow)


class ActionKind(str, Enum):
    START_RUN = "start_run"
    PAUSE_RUN = "pause_run"
    RESUME_RUN = "resume_run"
    CANCEL_RUN = "cancel_run"


class ActionState(str, Enum):
    REQUESTED = "requested"  # recorded, not yet decided
    DONE = "done"  # carried out (a run started, or a control request applied)
    BLOCKED = "blocked"  # needs information first (the draft is not executable)
    DENIED = "denied"  # needs a permission the policy does not grant
    REJECTED = "rejected"  # stale, duplicate-in-effect, unauthorized or inapplicable


class ActionRequest(FrozenModel):
    """§5 ActionRequest: a typed, policy-checked request to act on a run."""

    action_id: str = Field(pattern=_ID)
    session_id: str
    source_turn_id: str | None = None
    source: Literal["user", "assistant"]
    kind: ActionKind
    expected_revision: int | None = None  # start_run: the reviewed revision it targets
    run_id: str | None = None
    authorization: str | None = None  # an assistant request: the user's words it acts on
    state: ActionState = ActionState.REQUESTED
    reason: str | None = None
    findings: tuple[FrozenValue, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class BenchmarkSession(FrozenModel):
    """§5 BenchmarkSession."""

    session_id: str
    project_root: str
    policy_path: str | None = None
    trusted_local: bool = False  # the user's explicit grant when the session was opened
    plugin_environments: tuple[FrozenValue, ...] = ()  # PluginEnvironmentRef dumps
    # Project-configured parameters by evaluator-ID pattern (e.g. the judge for
    # `deepeval.*`), filled into plans where the user gave none (planning.catalog).
    evaluator_defaults: FrozenValue = Field(default_factory=dict)
    revision: int = Field(ge=0)
    decision_id: str | None = None  # the current draft's decision
    presented_revision: int | None = None  # the latest revision shown to the user
    active_run_id: str | None = None
    # Per run, the last run-event sequence shown to the user, so a reconnecting client
    # replays only what it missed (§14) — display state, never an action.
    event_cursors: FrozenCursors = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ConversationTurn(FrozenModel):
    """§5 ConversationTurn: a user message or command, or the assistant's response. Never
    an executable plan by itself; its effects are the decision and action records it
    references."""

    turn_id: str
    session_id: str
    sequence: int = Field(ge=1)
    role: Literal["user", "assistant"]
    kind: Literal["message", "command", "reply"]
    content: str
    message_id: str | None = None  # the client's delivery ID (user turns)
    replies_to: str | None = None  # assistant turns: the user turn they answer
    decision_refs: tuple[str, ...] = ()
    action_refs: tuple[str, ...] = ()
    outcome: FrozenValue = None  # assistant turns: the structured TurnOutcome
    created_at: datetime = Field(default_factory=utcnow)


SESSION_MODELS = (
    PendingQuestion,
    SessionReleaseGate,
    SessionChoices,
    PlanPatch,
    DecisionRecord,
    ActionRequest,
    BenchmarkSession,
    ConversationTurn,
)
