"""Runner lifecycle contract (§7, 03-T1).

`describe → prepare → healthcheck → invoke* → reset → close`. Every operation is time
bounded. `invoke` is cancellable cooperatively (`InvocationContext.cancel`, which produces a
recorded `cancelled` outcome) and by asyncio task cancellation (which cleans up and
re-raises). Runners never retry: one `invoke` call is one attempt, and its `effect_state`
tells the engine whether a retry would be safe.
"""

from __future__ import annotations

import abc
import asyncio
import time
import uuid
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, Self, TypeVar

from aibench.core.errors import AibenchError
from aibench.core.models import (
    ApplicationSpec,
    EffectLevel,
    EffectState,
    ErrorKind,
    ExecutionResult,
    ExecutionStatus,
    ObservationState,
)
from aibench.runners.bindings import (
    MISSING,
    OPTIONAL_CAPABILITIES,
    AppInputEnvelope,
    ExtractedObservations,
    InputBinding,
    OutputBinding,
    completeness,
    extract_optional,
    resolve_pointer,
    unknown_everywhere,
)
from aibench.security.secrets import Redactor

DEFAULT_LIFECYCLE_TIMEOUT_SECONDS = 10.0
_T = TypeVar("_T")


class RunnerLifecycleError(AibenchError):
    """A lifecycle operation was called out of order (e.g. invoke before prepare)."""


@dataclass(frozen=True)
class InvocationContext:
    run_id: str
    case_id: str
    repetition_id: int = 0
    attempt_id: int = 0
    correlation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cancel: asyncio.Event | None = None

    @property
    def execution_id(self) -> str:
        return ExecutionResult.build_id(
            self.run_id, self.case_id, self.repetition_id, self.attempt_id
        )


@dataclass(frozen=True)
class Capture:
    """Raw bytes exchanged with the application, persisted as a restricted artifact."""

    name: str  # "request" | "stdout" | "stderr" | "exchange" | "response_body"
    data: bytes
    mime_type: str
    truncated: bool = False


@dataclass(frozen=True)
class InvocationOutcome:
    status: ExecutionStatus
    effect_state: EffectState
    correlation_id: str
    timing: dict[str, Any]
    completeness: dict[str, dict[str, Any]]
    output: Any = None
    observations: ExtractedObservations = field(default_factory=ExtractedObservations)
    error_kind: ErrorKind | None = None
    error: str | None = None
    captures: tuple[Capture, ...] = ()


@dataclass(frozen=True)
class HealthReport:
    status: Literal["healthy", "unhealthy", "unknown"]
    detail: str


@dataclass(frozen=True)
class ResetReport:
    status: Literal["reset", "not_needed", "unsupported", "failed"]
    detail: str


@dataclass(frozen=True)
class RunnerDescription:
    kind: str
    target: str
    isolation: str
    effects: str
    observable: dict[str, str]  # capability -> ObservationState value before invocation
    limitations: tuple[str, ...]


class Stopwatch:
    def __init__(self) -> None:
        self.started_at = datetime.now(UTC)
        self._t0 = time.perf_counter()  # monotonic() ticks ~15.6 ms on Windows

    def timing(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": datetime.now(UTC).isoformat(),
            "wall_ms": round((time.perf_counter() - self._t0) * 1000, 3),
        }


async def race(
    work: asyncio.Task[_T], *, timeout: float, cancel: asyncio.Event | None
) -> Literal["done", "timeout", "cancelled"]:
    """Wait for `work` until it finishes, `timeout` elapses, or `cancel` is set. Never
    cancels `work` itself — the caller owns cleanup, which differs per transport."""
    waiters: set[asyncio.Future[Any]] = {work}
    cancel_waiter: asyncio.Task[Any] | None = None
    if cancel is not None:
        cancel_waiter = asyncio.ensure_future(cancel.wait())
        waiters.add(cancel_waiter)
    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when="FIRST_COMPLETED")
    finally:
        if cancel_waiter is not None and not cancel_waiter.done():
            cancel_waiter.cancel()
    if work in done:
        return "done"
    if cancel_waiter is not None and cancel_waiter in done:
        return "cancelled"
    return "timeout"


class BaseRunner(abc.ABC):
    """Lifecycle state, time bounds and declared-observation bookkeeping shared by the CLI
    and HTTP transports."""

    kind: str

    def __init__(
        self,
        spec: ApplicationSpec,
        *,
        lifecycle_timeout_seconds: float = DEFAULT_LIFECYCLE_TIMEOUT_SECONDS,
    ) -> None:
        self.spec = spec
        self.input_binding = InputBinding.from_spec(spec.input_binding)
        self.output_binding = OutputBinding.from_spec(spec.output_binding)
        self.lifecycle_timeout_seconds = lifecycle_timeout_seconds
        self.redactor = Redactor()  # replaced in `_prepare` once secrets are resolved
        self._state: Literal["new", "prepared", "closed"] = "new"

    # ------------------------------------------------------------------ public lifecycle

    def describe(self) -> RunnerDescription:
        """Pure: what this runner can honestly observe, before anything runs."""
        observable = {"output": ObservationState.DECLARED.value}
        observable.update(dict.fromkeys(self._transport_observables(), "observed"))
        for name in OPTIONAL_CAPABILITIES:
            bound = getattr(self.output_binding, name) is not None
            observable[name] = (
                ObservationState.DECLARED if bound else ObservationState.UNKNOWN
            ).value
        return RunnerDescription(
            kind=self.kind,
            target=self.spec.target,
            isolation=self._isolation(),
            effects=self.spec.effects.value,
            observable=observable,
            limitations=self._limitations(),
        )

    async def prepare(self) -> None:
        if self._state == "closed":
            raise RunnerLifecycleError("runner is closed")
        await self._bounded(self._prepare())
        self._state = "prepared"

    async def healthcheck(self) -> HealthReport:
        self._require_prepared()
        try:
            return await self._bounded(self._healthcheck())
        except TimeoutError:
            return HealthReport("unhealthy", "healthcheck timed out")

    async def invoke(self, envelope: AppInputEnvelope, ctx: InvocationContext) -> InvocationOutcome:
        self._require_prepared()
        return await self._invoke(envelope, ctx)

    async def reset(self) -> ResetReport:
        self._require_prepared()
        try:
            return await self._bounded(self._reset())
        except TimeoutError:
            return ResetReport("failed", "reset timed out")

    async def close(self) -> None:
        if self._state == "closed":
            return
        self._state = "closed"
        await self._bounded(self._close())

    async def __aenter__(self) -> Self:
        await self.prepare()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------ helpers

    def effect(self, dispatch: EffectState) -> EffectState:
        if self.spec.effects is EffectLevel.NONE:
            return EffectState.NONE_DECLARED
        return dispatch

    def outcome(
        self,
        clock: Stopwatch,
        ctx: InvocationContext,
        status: ExecutionStatus,
        dispatch: EffectState,
        *,
        error_kind: ErrorKind | None = None,
        error: str | None = None,
        output: Any = None,
        output_method: str | None = None,
        output_detail: str = "missing",
        captures: tuple[Capture, ...] = (),
        extra: dict[str, dict[str, Any]] | None = None,
    ) -> InvocationOutcome:
        """An outcome with no structured response document: a failure, or plain-text
        output. Optional capabilities stay unknown unless `extra` says otherwise."""
        if status is ExecutionStatus.OK:
            output_entry = completeness(ObservationState.OBSERVED, "present", method=output_method)
        else:
            output_entry = completeness(ObservationState.UNKNOWN, output_detail)
        return InvocationOutcome(
            status=status,
            effect_state=self.effect(dispatch),
            correlation_id=ctx.correlation_id,
            timing=clock.timing(),
            completeness={
                **unknown_everywhere("no_structured_output"),
                "output": output_entry,
                "wall_time": completeness(ObservationState.OBSERVED, "present"),
                **(extra or {}),
            },
            output=output,
            error_kind=error_kind,
            error=self.redactor.text(error) if error else None,
            captures=captures,
        )

    def outcome_from_document(
        self,
        clock: Stopwatch,
        ctx: InvocationContext,
        document: Any,
        *,
        source: str,
        captures: tuple[Capture, ...],
        extra: dict[str, dict[str, Any]],
    ) -> InvocationOutcome:
        """Apply the output binding to a parsed JSON response."""
        pointer = self.output_binding.output
        observations = extract_optional(document, self.output_binding)
        output = resolve_pointer(document, pointer)
        if output is MISSING:
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.COMPLETED,
                error_kind=ErrorKind.INVALID_OUTPUT,
                error=f"output binding {pointer!r} not present in the JSON response",
                captures=captures,
                extra={**extra, **observations.completeness},
            )
        return InvocationOutcome(
            status=ExecutionStatus.OK,
            effect_state=self.effect(EffectState.COMPLETED),
            correlation_id=ctx.correlation_id,
            timing=clock.timing(),
            completeness={
                "output": completeness(
                    ObservationState.OBSERVED, "present", method=f"{source}:{pointer}"
                ),
                "wall_time": completeness(ObservationState.OBSERVED, "present"),
                **extra,
                **observations.completeness,
            },
            output=output,
            observations=observations,
            captures=captures,
        )

    async def _bounded(self, awaitable: Awaitable[_T]) -> _T:
        return await asyncio.wait_for(awaitable, timeout=self.lifecycle_timeout_seconds)

    def _require_prepared(self) -> None:
        if self._state != "prepared":
            raise RunnerLifecycleError(f"runner must be prepared first (state={self._state})")

    # ------------------------------------------------------------------ transport hooks

    @abc.abstractmethod
    def _transport_observables(self) -> tuple[str, ...]: ...

    @abc.abstractmethod
    def _isolation(self) -> str: ...

    @abc.abstractmethod
    def _limitations(self) -> tuple[str, ...]: ...

    @abc.abstractmethod
    async def _prepare(self) -> None: ...

    @abc.abstractmethod
    async def _healthcheck(self) -> HealthReport: ...

    @abc.abstractmethod
    async def _invoke(
        self, envelope: AppInputEnvelope, ctx: InvocationContext
    ) -> InvocationOutcome: ...

    @abc.abstractmethod
    async def _reset(self) -> ResetReport: ...

    @abc.abstractmethod
    async def _close(self) -> None: ...
