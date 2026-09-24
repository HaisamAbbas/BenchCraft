"""Evidence-backed application profiles (§3 `inspect`, §5 ObservationClaim, 07-T1).

Scope, stated in every profile: the declared application config, plus — when a run is
named — what that run's recorded executions actually contained. No source code is read and
no architecture is claimed (§17: "`inspect` initially validates declared interfaces and
static metadata; advertise its limited scope accurately").

Each capability gets one `ObservationClaim` with a state:

- `observed` — present in recorded executions (self-reported by the application, which is
  stated as a limitation);
- `declared` — the config maps it (e.g. `output_binding.retrieved_context`), but no
  recorded execution has shown it yet — or recordings contradict the declaration, which is
  stated;
- `unknown` — neither declared nor observed.

(`inferred` claims come from dataset metadata; see `dataset_summary`.)
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path

from aibench.core.hashes import content_hash
from aibench.core.models import (
    CliTransport,
    ExecutionResult,
    ExecutionStatus,
    FrozenModel,
    HttpTransport,
    ObservationClaim,
    ObservationState,
)
from aibench.runners import LoadedApplication, create_runner, load_application
from aibench.runners.bindings import OPTIONAL_CAPABILITIES
from aibench.security.endpoints import origin_of

PROFILE_SCHEMA_VERSION = "1.0.0"
SCOPE = (
    "declared application configuration and, when a run is named, its recorded executions; "
    "no source code or architecture discovery"
)

# How to close each gap without rewriting the application (§7: "minimal integration recipe").
_RECIPES = {
    "retrieved_context": (
        "return the passages actually retrieved for this request in the response and map "
        "them with output_binding.retrieved_context"
    ),
    "tool_events": (
        "return the tool calls made for this request in the response and map them with "
        "output_binding.tool_events"
    ),
    "usage": "return token usage in the response and map it with output_binding.usage",
    "world_state": (
        "return the final application state or state assertion in the response and map it "
        "with output_binding.world_state"
    ),
    "cost": "return the request's cost in the response and map it with output_binding.cost",
}
_MAX_EVIDENCE_REFS = 5


class ApplicationProfile(FrozenModel):
    schema_version: str = PROFILE_SCHEMA_VERSION
    application_id: str
    config_path: str
    config_hash: str  # content hash of the declared ApplicationSpec
    runner: str
    endpoint: str  # CLI target, or the HTTP origin (never credentials)
    effects: str
    isolation: str
    claims: tuple[ObservationClaim, ...]
    gaps: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    evidence_runs: tuple[str, ...] = ()
    scope: str = SCOPE
    # Observed in recorded executions, but empty in every one of them (e.g. a retriever
    # that is declared but disabled at runtime, §23): nothing can be measured from it.
    always_empty: tuple[str, ...] = ()

    def claim(self, capability: str) -> ObservationClaim | None:
        return next((c for c in self.claims if c.capability == capability), None)

    def available(self, capability: str) -> bool:
        """Declared or observed: planning may rely on it; execution re-checks per case."""
        claim = self.claim(capability)
        return claim is not None and claim.state in (
            ObservationState.DECLARED,
            ObservationState.OBSERVED,
        )

    @property
    def profile_hash(self) -> str:
        return content_hash(self.model_dump(mode="json"))


MIN_ALWAYS_EMPTY_SAMPLE = 3  # reported-and-empty executions before a field is "always empty"


class _Recorded:
    """Per-capability completeness counts over successful recorded executions."""

    def __init__(self, executions: Iterable[ExecutionResult]) -> None:
        self.total = 0
        self.details: dict[str, Counter[str]] = {}
        self.examples: dict[str, list[str]] = {}
        for execution in executions:
            if execution.status is not ExecutionStatus.OK:
                continue  # a failed attempt says nothing about what a response contains
            self.total += 1
            for name, entry in execution.observation_completeness.items():
                if not isinstance(entry, Mapping):
                    continue
                state, detail = entry.get("state"), entry.get("detail", "")
                key = f"{state}:{detail}"
                self.details.setdefault(name, Counter())[key] += 1
                if state == ObservationState.OBSERVED.value:
                    examples = self.examples.setdefault(name, [])
                    if len(examples) < _MAX_EVIDENCE_REFS:
                        examples.append(f"execution:{execution.execution_id}")

    def observed(self, name: str) -> int:
        return sum(
            n for k, n in self.details.get(name, Counter()).items() if k.startswith("observed:")
        )

    def observed_empty(self, name: str) -> int:
        return self.details.get(name, Counter()).get("observed:empty", 0)


def _endpoint(loaded: LoadedApplication) -> str:
    transport = loaded.spec.transport
    if isinstance(transport, HttpTransport):
        return origin_of(transport.url)
    if isinstance(transport, CliTransport):
        return loaded.spec.target
    return loaded.spec.target


def inspect_application(
    config_path: Path,
    *,
    executions: Iterable[ExecutionResult] = (),
    run_ids: Iterable[str] = (),
) -> ApplicationProfile:
    """Build the profile from the declared config and, optionally, recorded executions of
    this application. Pure: nothing is invoked."""
    loaded = load_application(config_path)
    spec = loaded.spec
    description = create_runner(loaded).describe()
    recorded = _Recorded(executions)
    source = str(config_path)
    claims: list[ObservationClaim] = []
    gaps: list[str] = []
    always_empty: list[str] = []

    def claim(
        capability: str,
        state: ObservationState,
        *,
        method: str,
        evidence_refs: tuple[str, ...] = (),
        scope: str | None = None,
        limitations: str | None = None,
    ) -> None:
        claims.append(
            ObservationClaim(
                observation_id=f"{spec.application_id}:{capability}",
                capability=capability,
                state=state,
                evidence_refs=evidence_refs,
                method=method,
                scope=scope,
                limitations=limitations,
            )
        )

    for capability, declared_state in description.observable.items():
        if capability in OPTIONAL_CAPABILITIES:
            continue
        # output and transport-level measurements (wall time, exit/HTTP status)
        observed = recorded.observed(capability)
        if observed:
            claim(
                capability,
                ObservationState.OBSERVED,
                evidence_refs=tuple(recorded.examples.get(capability, ())),
                method="recorded_executions",
                scope=f"{observed} of {recorded.total} successful recorded executions",
            )
        else:
            state = ObservationState(declared_state)
            claim(
                capability,
                ObservationState.DECLARED if state is ObservationState.OBSERVED else state,
                evidence_refs=(f"{source}#/runner",),
                method="measured_by_harness" if declared_state == "observed" else "declared_config",
            )

    for capability in OPTIONAL_CAPABILITIES:
        declared = description.observable.get(capability) == ObservationState.DECLARED.value
        observed = recorded.observed(capability)
        pointer = f"{source}#/output_binding/{capability}"
        if observed:
            empty = recorded.observed_empty(capability)
            # One empty answer (an unanswerable question) says nothing about the app; only
            # a field that was empty every time it was reported, over enough runs, does.
            if empty == observed >= MIN_ALWAYS_EMPTY_SAMPLE:
                always_empty.append(capability)
                gaps.append(
                    f"{capability}: empty in all {observed} recorded executions that reported "
                    f"it ({recorded.total} successful); it is declared but returns nothing "
                    "at runtime"
                )
            claim(
                capability,
                ObservationState.OBSERVED,
                evidence_refs=tuple(recorded.examples.get(capability, ())),
                method="recorded_executions",
                scope=f"{observed} of {recorded.total} successful recorded executions"
                + (f" ({empty} empty)" if empty else ""),
                limitations="self-reported by the application",
            )
        elif declared:
            contradiction = (
                f"declared, but absent from all {recorded.total} successful recorded executions"
                if recorded.total
                else None
            )
            claim(
                capability,
                ObservationState.DECLARED,
                evidence_refs=(pointer,),
                method="declared_config",
                limitations=contradiction,
            )
            if contradiction:
                gaps.append(f"{capability}: {contradiction}; check the response mapping")
        else:
            claim(capability, ObservationState.UNKNOWN, method="not_declared")
            gaps.append(f"{capability} is not observable: {_RECIPES[capability]}")

    return ApplicationProfile(
        application_id=spec.application_id,
        config_path=source,
        config_hash=content_hash(spec.model_dump(mode="json")),
        runner=spec.runner.value,
        endpoint=_endpoint(loaded),
        effects=spec.effects.value,
        isolation=description.isolation,
        claims=tuple(claims),
        gaps=tuple(gaps),
        limitations=tuple(description.limitations),
        evidence_runs=tuple(run_ids),
        always_empty=tuple(always_empty),
    )
