"""Evaluator protocol (§9 "Adapter contract", 04-T1).

`describe → validate_binding → prepare → evaluate / evaluate_batch → close`. Evaluators
score *recorded* executions: they receive an `EvaluationView` (the Golden case plus one
stored `ExecutionResult`) and never an application runner, so scoring cannot invoke the
application (04-G3).

Evaluators report a value and an execution status. The pass/fail decision is applied by
the harness from a frozen `DecisionRule` (§10), so an evaluator cannot turn an error into a
pass or a low score into an error.
"""

from __future__ import annotations

import abc
import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

from aibench.core.models import (
    BenchmarkCase,
    DecisionRule,
    EvaluatorManifest,
    ExecutionResult,
    ExecutionStatus,
    FieldRequirement,
    MetricBinding,
    MetricValue,
    deep_unfreeze,
)

MISSING: Any = object()
FieldState = Literal["present", "empty", "missing"]

_EXECUTION_FIELDS = ("output", "retrieved_context", "tool_events", "usage", "cost", "world_state")
_REFERENCE_FIELDS = ("answer", "context", "tools")
EPISODE_TURNS = "episode.turns"


@dataclass(frozen=True)
class EvaluationView:
    """Read-only view of one case and its recorded execution. Field paths:
    `case.input`, `case.case_id`, `case.group_id`, `case.reference.<answer|context|tools>`,
    `case.expectations.<key>...`, `case.metadata.<key>...`, `case.fixtures.<name>`,
    `execution.<output|retrieved_context|tool_events|usage|cost|world_state>`, and
    `episode.turns`: for a case in an episode, the conversation up to and including it.

    `episode` is filled by the scorer only for a metric that requires `episode.turns`: one
    dict per turn, in dataset order, with the turn's `case_id`, `input` and recorded `output`
    (and its `retrieved_context` and `tool_events` when the metric requires those)."""

    case: BenchmarkCase
    execution: ExecutionResult
    episode: tuple[Any, ...] | None = None

    def get(self, path: str) -> Any:
        head, _, rest = path.partition(".")
        parts = rest.split(".") if rest else []
        if head == "execution" and len(parts) == 1 and parts[0] in _EXECUTION_FIELDS:
            value = getattr(self.execution, parts[0])
            # ExecutionResult uses None for "not observed"; () for tool_events means
            # "none observed" only when the capability was actually observed.
            if parts[0] == "tool_events":
                state = self.execution.observation_completeness.get("tool_events", {})
                if state.get("state") != "observed":
                    return MISSING
            if parts[0] == "output":
                # An ok execution always has an output, possibly JSON null: that is the
                # application's answer, not missing evidence.
                return deep_unfreeze(value)
            return MISSING if value is None else deep_unfreeze(value)
        if path == EPISODE_TURNS:
            return MISSING if self.episode is None else [dict(turn) for turn in self.episode]
        return case_field(self.case, path)

    @staticmethod
    def case_state(case: BenchmarkCase, path: str) -> FieldState:
        """State of a `case.*` path on a Golden alone (planning, selection)."""
        return _state_of(case_field(case, path))

    @staticmethod
    def path_problem(path: str) -> str | None:
        """Why `path` is not a valid view path, or None. Lets the registry reject a typo'd
        requirement before scoring instead of failing on the first case."""
        head, _, rest = path.partition(".")
        parts = rest.split(".") if rest else []
        valid = (
            (head == "execution" and len(parts) == 1 and parts[0] in _EXECUTION_FIELDS)
            or path == EPISODE_TURNS
            or (
                head == "case"
                and parts[:1] in (["input"], ["case_id"], ["group_id"])
                and len(parts) == 1
            )
            or (
                head == "case"
                and len(parts) == 2
                and parts[0] == "reference"
                and parts[1] in _REFERENCE_FIELDS
            )
            or (head == "case" and len(parts) >= 2 and parts[0] in ("expectations", "metadata"))
            or (head == "case" and len(parts) == 2 and parts[0] == "fixtures")
        )
        return None if valid else f"unknown evaluation view path {path!r}"

    def state(self, path: str) -> FieldState:
        return _state_of(self.get(path))


def _state_of(value: Any) -> FieldState:
    if value is MISSING:
        return "missing"
    if value in ("", [], {}, ()):
        return "empty"
    return "present"


def case_field(case: BenchmarkCase, path: str) -> Any:
    """Value of a `case.*` evaluation-view path on a Golden, or MISSING."""
    head, _, rest = path.partition(".")
    parts = rest.split(".") if rest else []
    if head != "case" or not parts:
        raise KeyError(f"unknown evaluation view path {path!r}")
    first, remainder = parts[0], parts[1:]
    if first in ("input", "case_id") and not remainder:
        return deep_unfreeze(getattr(case, first))
    if first == "group_id" and not remainder:
        return MISSING if case.group_id is None else case.group_id
    if first == "reference" and len(remainder) == 1 and remainder[0] in _REFERENCE_FIELDS:
        reference = case.reference
        if reference is None:
            return MISSING
        if remainder[0] == "answer":
            return MISSING if reference.answer is None else reference.answer
        if remainder[0] == "context":
            return list(reference.context)
        return MISSING if reference.tools is None else reference.tools.model_dump(mode="json")
    if first in ("expectations", "metadata") and remainder:
        current: Any = deep_unfreeze(getattr(case, first))
        for key in remainder:
            if not isinstance(current, dict) or key not in current:
                return MISSING
            current = current[key]
        return current
    if first == "fixtures" and len(remainder) == 1:
        for fixture in case.fixtures:
            if fixture.name == remainder[0]:
                return deep_unfreeze(fixture.content)
        return MISSING
    raise KeyError(f"unknown evaluation view path {path!r}")


@dataclass(frozen=True)
class EvaluationOutcome:
    """What an evaluator returns for one view. `status` is ok, error or not_applicable;
    `raw` is any JSON-serializable payload worth keeping (stored as an artifact)."""

    status: ExecutionStatus
    value: MetricValue | None = None
    reason: str | None = None
    evidence: tuple[str, ...] = ()
    raw: Any = None

    @classmethod
    def ok(
        cls, kind: Any, value: Any, *, evidence: Sequence[str] = (), raw: Any = None
    ) -> EvaluationOutcome:
        return cls(
            ExecutionStatus.OK, MetricValue(kind=kind, value=value), None, tuple(evidence), raw
        )

    @classmethod
    def not_applicable(cls, reason: str) -> EvaluationOutcome:
        return cls(ExecutionStatus.NOT_APPLICABLE, reason=reason)

    @classmethod
    def error(cls, reason: str, *, raw: Any = None) -> EvaluationOutcome:
        return cls(ExecutionStatus.ERROR, reason=reason, raw=raw)


@dataclass
class UsageReport:
    provider: str | None = None
    calls: int | None = None  # None: the evaluator could not count its calls
    tokens: dict[str, int] = field(default_factory=dict)
    cost: float | None = None


class EvaluatorContext:
    """Services handed to an evaluator: cancellation, artifact writing and usage
    accounting. Model-backed evaluators must call `report_usage`; if they do not, their
    cost is recorded as unknown, never as zero."""

    def __init__(
        self,
        *,
        run_id: str,
        scoring_id: str,
        cancel: asyncio.Event | None = None,
        write_artifact: Callable[[bytes, str], str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.scoring_id = scoring_id
        self.cancel = cancel or asyncio.Event()
        self._write_artifact = write_artifact
        self.usage: list[UsageReport] = []

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def write_artifact(self, data: bytes, mime_type: str) -> str:
        """Persist bytes (verified artifact commit); returns the artifact ID."""
        if self._write_artifact is None:
            raise RuntimeError("this context has no artifact store")
        return self._write_artifact(data, mime_type)

    def report_usage(
        self,
        *,
        provider: str | None,
        calls: int | None,
        tokens: Mapping[str, int] | None = None,
        cost: float | None = None,
    ) -> None:
        self.usage.append(UsageReport(provider, calls, dict(tokens or {}), cost))


class Evaluator(abc.ABC):
    """Subclass, set `manifest`, implement `evaluate`. Keep instances per scoring pass;
    they are never shared across concurrent passes."""

    manifest: ClassVar[EvaluatorManifest]

    def __init__(self) -> None:
        self.params: dict[str, Any] = {}

    def describe(self) -> EvaluatorManifest:
        return self.manifest

    def validate_binding(self, binding: MetricBinding) -> list[str]:
        """Problems with this binding (bad parameters, incompatible rule), found before
        anything is evaluated. The default checks parameters against the manifest's JSON
        Schema and the rule against the value kind."""
        from aibench.evaluators.validation import check_params, check_rule

        params = deep_unfreeze(binding.params) or {}
        return check_params(self.manifest, params) + check_rule(
            self.manifest, binding.rule or self.manifest.default_rule
        )

    def required_fields(self, params: Mapping[str, Any]) -> tuple[FieldRequirement, ...]:
        """The manifest's requirements plus those its `parameter_requirements` add for these
        parameters (so a worker-run evaluator gets them too, and they are checked before
        scoring)."""
        extra: list[FieldRequirement] = []
        declared = deep_unfreeze(self.manifest.parameter_requirements) or {}
        for name, by_value in declared.items():
            listed = params.get(name)
            values = listed if isinstance(listed, (list, tuple)) else [listed]
            for value in values:
                requirement = by_value.get(value) if isinstance(value, str) else None
                if requirement is not None:
                    extra.append(FieldRequirement.model_validate(requirement))
        paths = {r.path for r in self.manifest.requires}
        return (*self.manifest.requires, *(r for r in extra if r.path not in paths))

    async def prepare(self, params: Mapping[str, Any]) -> None:
        self.params = dict(params)

    async def ensure_ready(self) -> None:
        """Called before each case, outside that case's time budget. Evaluators that can
        lose their runtime (e.g. a worker killed after a timeout) rebuild it here, so
        startup cost is never charged to the next case."""
        return

    @abc.abstractmethod
    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome: ...

    async def evaluate_batch(
        self, views: Sequence[EvaluationView], ctx: EvaluatorContext
    ) -> list[EvaluationOutcome]:
        """Optional batch contract; the default evaluates one view at a time."""
        return [await self.evaluate(view, ctx) for view in views]

    async def close(self) -> None:
        return None


def rule_for(binding: MetricBinding, manifest: EvaluatorManifest) -> DecisionRule | None:
    return binding.rule or manifest.default_rule
