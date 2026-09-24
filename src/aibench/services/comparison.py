"""Storage-only, qualified comparison of two committed runs.

The comparison service is deliberately a reporting boundary.  It reads the run
manifest, immutable cases, committed scoring records, and (when necessary) a
frozen plan/application artifact.  It does not resolve an evaluator, import a
plugin, start a worker, invoke an application, or mutate storage.  The latter
is part of the contract rather than an implementation detail: a comparison
must be safe to call while another process is reporting a run.

There are two deliberately different paths through this module:

* ``strict`` refuses to qualify a comparison when the frozen contracts are not
  provably compatible.  It can still return structured exploratory diagnostics,
  but it never puts a delta in ``qualified_metric_deltas``.
* ``exploratory`` is always non-qualified.  It may show paired numbers for
  inspection, while retaining the same mismatch and coverage facts.

The service uses :mod:`aibench.reporting.statistics` as the sole numeric
comparison implementation.  The rest of this file is responsible for mapping
committed records to that API, checking identities, and removing content that
does not belong in a machine-readable comparison.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from aibench.core.errors import AibenchError
from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    SCHEMA_VERSION,
    ApplicationSpec,
    BenchmarkCase,
    Decision,
    EvaluationResult,
    ExecutionResult,
    ExecutionStatus,
    RunManifest,
    deep_unfreeze,
)
from aibench.core.plans import ExecutablePlan
from aibench.reporting.aggregation import reason_code
from aibench.reporting.statistics import (
    ComparisonSide,
    ExecutionKey,
    JudgeObservation,
    MetricIdentity,
    NumericObservation,
    PairKey,
    compare_numeric_metric,
    summarize_judge_stability,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import Storage

COMPARISON_SCHEMA = "aibench.run-comparison/1"
_FINISHED_STATUSES = frozenset({"completed", "cancelled", "budget_exhausted"})
ZERO_INVOCATION_BASIS = {
    "basis": "committed storage reads only",
    "application_invocations": 0,
    "evaluator_invocations": 0,
    "judge_invocations": 0,
    "worker_starts": 0,
    "storage_writes": 0,
    "database_migrations": 0,
    "application": 0,
    "evaluator": 0,
    "judge": 0,
    "workers": 0,
    "storage_mutations": 0,
    "migrations": 0,
    "zero_invocations": True,
    "automatic_rescoring": False,
}


class ComparisonError(AibenchError):
    """The requested comparison cannot be started or is malformed."""


@dataclass(slots=True)
class _Pass:
    run_id: str
    scoring_id: str
    kind: Literal["engine", "rescore"]
    sequence: int
    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    results: list[EvaluationResult] = field(default_factory=list)
    source: str = "committed_metric_results"
    complete: bool = True


@dataclass(slots=True)
class _CaseFacts:
    available: bool = False
    by_id: dict[str, str] = field(default_factory=dict)
    groups: dict[str, str | None] = field(default_factory=dict)
    duplicate_ids: tuple[str, ...] = ()
    count: int = 0


@dataclass(slots=True)
class _MetricSpec:
    binding_hash: str
    metric_id: str
    metric_version: str
    label: str
    direction: str
    value_kind: str
    scope: str
    aggregation: str
    semantic_digest: str
    parameters_hash: str | None
    rule_digest: str | None
    plugin_id: str | None
    plugin_version: str | None
    package_name: str | None
    package_version: str | None
    dependency_lock_hash: str | None
    judge: dict[str, Any]
    rubric: dict[str, Any]
    instrumentation: dict[str, Any]
    uses_models: bool
    compatibility_hash: str | None
    binding_verified: bool
    identity_recorded: bool = False
    identity_verified: bool = False
    results: list[EvaluationResult] = field(default_factory=list)
    profile_present: bool = False

    @property
    def concept(self) -> str:
        """The framework-independent label used only for diagnostics."""

        return self.metric_id.partition(".")[2].lower() or self.metric_id.lower()

    @property
    def framework(self) -> str:
        return (self.plugin_id or self.metric_id.partition(".")[0]).lower()

    def fact(self) -> dict[str, Any]:
        """A sanitized identity projection safe for a report or chat response."""

        return {
            "metric_id": _identity_label(self.metric_id, "unknown.metric"),
            "metric_version": _identity_label(self.metric_version, "unknown"),
            "label": _identity_label(self.label, "unknown.metric"),
            "binding_hash": _identity_label(self.binding_hash, "unknown"),
            "binding_verified": self.binding_verified,
            "identity_recorded": self.identity_recorded,
            "identity_verified": self.identity_verified,
            "parameters_hash": _identity_label(self.parameters_hash, "unknown")
            if self.parameters_hash is not None
            else None,
            "rule_digest": _identity_label(self.rule_digest, "unknown")
            if self.rule_digest is not None
            else None,
            "value_kind": _safe_label(self.value_kind, "unknown"),
            "direction": _safe_label(self.direction, "none"),
            "scope": _safe_label(self.scope, "case"),
            "aggregation": _safe_label(self.aggregation, "none"),
            "semantic_digest": _identity_label(self.semantic_digest, "unknown"),
            "plugin": {
                "plugin_id": _identity_label(self.plugin_id, "unknown")
                if self.plugin_id is not None
                else None,
                "plugin_version": _identity_label(self.plugin_version, "unknown")
                if self.plugin_version is not None
                else None,
                "package_name": _identity_label(self.package_name, "unknown")
                if self.package_name is not None
                else None,
                "package_version": _identity_label(self.package_version, "unknown")
                if self.package_version is not None
                else None,
            },
            "dependency_lock_hash": _identity_label(self.dependency_lock_hash, "unknown")
            if self.dependency_lock_hash is not None
            else None,
            "judge": dict(self.judge),
            "rubric": dict(self.rubric),
            "instrumentation": dict(self.instrumentation),
            "uses_models": self.uses_models,
            "compatibility_hash": _identity_label(self.compatibility_hash, "unknown")
            if self.compatibility_hash is not None
            else None,
        }


@dataclass(slots=True)
class _RunFacts:
    record: Any
    manifest: RunManifest
    dataset: Any | None
    events: list[dict[str, Any]]
    cases: _CaseFacts
    work_items: list[Any]
    executions: list[ExecutionResult]
    plan: dict[str, Any]
    application: dict[str, Any]
    all_passes: list[_Pass]
    selected: _Pass
    all_results: list[EvaluationResult]
    all_attempts: list[EvaluationResult]
    selected_keys_by_binding: dict[str, set[PairKey]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Safe projections and small parsing helpers


def _plain(value: Any) -> Any:
    """Return a JSON-compatible copy without exposing arbitrary stored objects."""

    value = deep_unfreeze(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_plain(item) for item in value]
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _plain(dump(mode="json"))
        except TypeError:
            return _plain(dump())
    return str(value)


def _text(value: Any, *, default: str | None = None) -> str | None:
    if isinstance(value, str) and value:
        return value
    return default


def _digest(value: Any) -> str:
    return content_hash(_plain(value))


def _component(value: Any, *, fallback: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Project an identity component to kind/digest/verified only.

    Profiles are historical, untrusted JSON.  In particular, never copy a
    component's arbitrary payload into the result: a future adapter may put a
    secret or case text in a field called ``config``.
    """

    source: Mapping[str, Any]
    if isinstance(value, Mapping):
        source = value
    elif hasattr(value, "model_dump"):
        dumped = value.model_dump(mode="json")
        source = dumped if isinstance(dumped, Mapping) else {}
    else:
        source = fallback or {}
    kind = _text(source.get("kind"), default="unknown") or "unknown"
    digest = _text(source.get("digest"))
    verified = source.get("verified")
    if not isinstance(verified, bool):
        verified = any(
            key in source
            for key in (
                "runner",
                "core_schema",
                "output_binding",
                "output_binding_digest",
                "reset_policy",
                "environment_digest",
            )
        )
    if kind == "unknown" and verified and digest is None:
        contract_fields = {
            key: source[key]
            for key in (
                "runner",
                "core_schema",
                "output_binding",
                "output_binding_digest",
                "reset_policy",
                "environment_digest",
            )
            if key in source
        }
        if contract_fields:
            kind = "observation_contract"
            digest = _digest(contract_fields)
    # Unknown implementations intentionally retain no digest.  A caller can see
    # that the identity was not recorded without learning its configuration.
    if not verified and kind in {"unknown", "not_recorded", "unavailable"}:
        digest = None
    if verified and kind == "not_used":
        # A native no-model evaluator has no judge configuration; any adapter
        # supplied digest is an implementation detail, not a judge identity.
        digest = None
    elif digest is not None and not re.fullmatch(
        r"(?:sha256:)?[0-9a-fA-F]{64}|[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,199}",
        digest,
    ):
        digest = _digest(digest)
    return {"kind": kind, "digest": digest, "verified": verified}


def _safe_reason(value: Any) -> str | None:
    """Use the stored reason's code, never its free text."""

    return reason_code(value if isinstance(value, str) else None)


def _enum_value(value: Any, default: str) -> str:
    return value.value if hasattr(value, "value") and isinstance(value.value, str) else (
        value if isinstance(value, str) else default
    )


def _rule_digest(rule: Any) -> str | None:
    if rule is None:
        return None
    return _digest(rule)


def _status_name(status: Any) -> str:
    return _enum_value(status, "missing")


def _decision_name(decision: Any) -> str:
    return _enum_value(decision, "not_evaluated")


def _numeric_value(result: EvaluationResult) -> tuple[float | None, str | None]:
    """Return a finite scalar value or a stable malformed-result code."""

    if result.status is not ExecutionStatus.OK:
        return None, None
    if result.value is None or result.value.kind != "scalar":
        return None, "non_numeric_result"
    value = deep_unfreeze(result.value.value)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, "non_numeric_result"
    number = float(value)
    if not math.isfinite(number):
        return None, "non_finite_result"
    return number, None


def _decision_for_matrix(result: EvaluationResult | None) -> str | None:
    if result is None or result.status is not ExecutionStatus.OK:
        return None
    decision = _decision_name(result.decision)
    return decision if decision in {"pass", "fail"} else None


def _safe_label(value: Any, fallback: str) -> str:
    label = _text(value)
    if label is None:
        return fallback
    # Metric IDs and plugin IDs are constrained by their models.  Profiles from
    # older records are not; keep a bounded label and remove controls.
    label = re.sub(r"[\x00-\x1f\x7f]", "", label)
    return label[:200] or fallback


def _identity_label(value: Any, fallback: str) -> str:
    """Expose a conventional identity label, hashing an untrusted arbitrary value."""

    label = _safe_label(value, "")
    if not label:
        return fallback
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@+-]{0,199}", label):
        return label
    return _digest(label)


def _metric_label(metric_id: str) -> str:
    return metric_id.partition("@")[0]


def _concept(metric_id: str) -> str:
    return metric_id.partition(".")[2].partition("@")[0].lower()


def _framework(metric_id: str, plugin_id: str | None) -> str:
    return (plugin_id or metric_id.partition(".")[0]).lower()


# ---------------------------------------------------------------------------
# Committed record discovery


def _event_scoring_passes(events: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    passes: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.get("event_type") != "scoring_pass":
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        scoring_id = _text(payload.get("scoring_id"))
        if scoring_id is None:
            continue
        raw_profiles = payload.get("metric_profiles")
        profiles: dict[str, dict[str, Any]] = {}
        if isinstance(raw_profiles, Mapping):
            for key, value in raw_profiles.items():
                if isinstance(key, str) and isinstance(value, Mapping):
                    profiles[key] = dict(value)
        passes[scoring_id] = {
            "sequence": int(event.get("sequence", 0)),
            "profiles": profiles,
            "kind": _text(payload.get("kind")),
        }
    return passes


def _completed_scoring_passes(events: Sequence[Mapping[str, Any]]) -> set[str]:
    completed: set[str] = set()
    for event in events:
        if event.get("event_type") != "scoring_pass_completed":
            continue
        payload = event.get("payload")
        if isinstance(payload, Mapping):
            scoring_id = _text(payload.get("scoring_id"))
            if scoring_id:
                completed.add(scoring_id)
    return completed


def _result_passes(results: Sequence[EvaluationResult], attempts: Sequence[EvaluationResult]) -> set[str]:
    ids = {r.scoring_id for r in (*results, *attempts) if r.scoring_id}
    return ids


def _dedupe_results(results: Sequence[EvaluationResult]) -> list[EvaluationResult]:
    """Choose one final result for each (binding, case, repetition) deterministically."""

    chosen: dict[tuple[str, str, int], EvaluationResult] = {}
    for result in results:
        binding = result.binding_hash or f"{result.metric_id}@{result.metric_version}"
        key = (binding, result.case_id, result.repetition_id)
        old = chosen.get(key)
        if old is None or result.attempt_number > old.attempt_number or (
            result.attempt_number == old.attempt_number and result.result_id > old.result_id
        ):
            chosen[key] = result
    return [chosen[key] for key in sorted(chosen)]


def _profile_for_result(
    profiles: Mapping[str, Mapping[str, Any]], result: EvaluationResult
) -> dict[str, Any] | None:
    if result.binding_hash and result.binding_hash in profiles:
        return dict(profiles[result.binding_hash])
    candidates = [
        value
        for key, value in profiles.items()
        if result.binding_hash and key and (
            result.binding_hash.startswith(key) or key.startswith(result.binding_hash)
        )
    ]
    if len(candidates) == 1:
        return dict(candidates[0])
    candidates = [
        value
        for value in profiles.values()
        if value.get("metric") in {result.metric_id, f"{result.metric_id}@{result.metric_version}"}
    ]
    return dict(candidates[0]) if len(candidates) == 1 else None


def _manifest_profiles(manifest: RunManifest) -> dict[str, dict[str, Any]]:
    params = deep_unfreeze(manifest.parameters) or {}
    raw = params.get("metric_profiles")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if isinstance(key, str) and isinstance(value, Mapping)
    }


def _merge_profile(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        previous = merged.get(key)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[key] = {**previous, **value}
        else:
            merged[key] = value
    return merged


def _passes_for_run(
    manifest: RunManifest,
    events: Sequence[Mapping[str, Any]],
    results: Sequence[EvaluationResult],
    attempts: Sequence[EvaluationResult],
) -> list[_Pass]:
    event_info = _event_scoring_passes(events)
    completed_ids = _completed_scoring_passes(events)
    result_ids = _result_passes(results, attempts)
    params = deep_unfreeze(manifest.parameters) or {}
    engine_id = _text(params.get("scoring_id")) or _text(params.get("engine_scoring_id"))
    scoring_ids = set(event_info) | result_ids
    if engine_id:
        scoring_ids.add(engine_id)
    passes: list[_Pass] = []
    for scoring_id in scoring_ids:
        info = event_info.get(scoring_id, {})
        kind = info.get("kind")
        if kind not in {"engine", "rescore"}:
            kind = "engine" if scoring_id == engine_id else "rescore"
        pass_kind = cast(Literal["engine", "rescore"], kind)
        sequence = int(info.get("sequence", 0))
        profile_map = dict(info.get("profiles") or {})
        if scoring_id == engine_id:
            # A run's frozen manifest is authoritative for its engine pass.  A
            # sparse event profile does not erase fields frozen at run creation.
            manifest_profiles = _manifest_profiles(manifest)
            profile_map = {
                binding: _merge_profile(manifest_profiles.get(binding, {}), profile)
                for binding, profile in profile_map.items()
            }
            for binding, profile in manifest_profiles.items():
                profile_map.setdefault(binding, profile)
        selected_results = [r for r in results if r.scoring_id == scoring_id]
        if not selected_results:
            selected_results = [r for r in attempts if r.scoring_id == scoring_id]
            source = "committed_evaluation_attempts" if selected_results else "no_committed_results"
        else:
            source = "committed_metric_results"
        passes.append(
            _Pass(
                run_id=manifest.run_id,
                scoring_id=scoring_id,
                kind=pass_kind,
                sequence=sequence,
                profiles=profile_map,
                results=_dedupe_results(selected_results),
                source=source,
                complete=(
                    pass_kind == "engine"
                    or (scoring_id in completed_ids and source == "committed_metric_results")
                ),
            )
        )
    # If the run manifest does not name an engine pass, the service uses the
    # latest committed pass.  A pass kind is never guessed from an ID prefix
    # alone: older records without an explicit kind remain rescore/unknown
    # until the caller selects a pass explicitly.
    # Engine first, then event order, with a deterministic ID tie-breaker.
    return sorted(
        passes,
        key=lambda p: (p.kind != "engine", p.sequence, p.scoring_id),
    )


def _select_pass(passes: Sequence[_Pass], requested: str | None) -> _Pass | None:
    if requested is not None:
        return next((p for p in passes if p.scoring_id == requested), None)
    # A normal run has one frozen engine pass. If historical data contains only
    # several rescore passes, choosing the newest silently would change the
    # estimand; require an explicit pass ID instead. With no engine identity,
    # one lone pass remains a safe historical fallback.
    engine = next((p for p in passes if p.kind == "engine"), None)
    if engine is not None:
        return engine
    if len(passes) <= 1:
        return passes[-1] if passes else None
    return _Pass(
        run_id=passes[0].run_id,
        scoring_id="",
        kind="rescore",
        sequence=-1,
        source="pass_selection_required",
        complete=False,
    )


def _case_content(case: BenchmarkCase) -> str:
    # Source line and duplicate bookkeeping describe storage placement, not the
    # normalized Golden.  Excluding them makes the identity stable across a
    # file re-ingestion while retaining every semantic case field.
    data = case.model_dump(mode="json", exclude={"source_line", "duplicate_of_line"})
    return _digest(data)


def _case_facts(storage: Storage, manifest: RunManifest) -> _CaseFacts:
    try:
        cases = storage.list_cases(manifest.dataset_hash)
    except Exception:  # noqa: BLE001 - a damaged read must not mutate/claim compatibility
        return _CaseFacts()
    grouped: dict[str, list[BenchmarkCase]] = defaultdict(list)
    for case in cases:
        grouped[case.case_id].append(case)
    by_id: dict[str, str] = {}
    groups: dict[str, str | None] = {}
    duplicates: list[str] = []
    for case_id, records in sorted(grouped.items()):
        digests = {_case_content(case) for case in records}
        if len(digests) > 1:
            # Distinct normalized contents under one case_id are ambiguous.
            duplicates.append(case_id)
            continue
        # Re-committing an identical dataset can leave byte-identical rows in
        # SQLite's nullable composite key.  They represent one normalized case,
        # not an ambiguity, so collapse them deterministically.
        by_id[case_id] = next(iter(digests))
        groups[case_id] = records[0].group_id
    return _CaseFacts(
        available=True,
        by_id=by_id,
        groups=groups,
        duplicate_ids=tuple(sorted(duplicates)),
        count=len(cases),
    )


def _artifact_json(
    storage: Storage, artifacts: ArtifactStore | None, artifact_id: Any
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    if not isinstance(artifact_id, str) or not artifact_id or artifacts is None:
        return None, "artifact_unavailable", None
    ref = storage.get_artifact(artifact_id)
    if ref is None:
        return None, "artifact_missing", None
    try:
        raw = artifacts.read_bytes(ref)
        value = json.loads(raw.decode("utf-8"))
    except Exception:  # noqa: BLE001 - artifact implementations may raise backend-specific errors
        return None, "artifact_unreadable", None
    return (value if isinstance(value, dict) else None), None, bytes_hash(raw)


def _plan_facts(
    storage: Storage,
    artifacts: ArtifactStore | None,
    manifest: RunManifest,
    *,
    observed_repetitions: set[int],
    declared_repetitions: set[int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    params = deep_unfreeze(manifest.parameters) or {}
    artifact_id = params.get("plan_artifact_id")
    raw, problem, raw_digest = _artifact_json(storage, artifacts, artifact_id)
    warnings: list[dict[str, Any]] = []
    repetitions: int | None = None
    selection_digest: str | None = None
    source = "unknown"
    artifact_expected = isinstance(artifact_id, str) and bool(artifact_id)
    plan_hash_verified = raw is not None and raw_digest == manifest.plan_hash
    plan_content_verified = False

    if artifact_expected and raw is None:
        plan_hash_verified = False
        warnings.append(
            {
                "code": (
                    "frozen_plan_unavailable"
                    if problem in {None, "artifact_missing", "artifact_unreadable"}
                    else f"frozen_plan_{problem}"
                ),
                "scope": "frozen_plan",
                "blocking": True,
            }
        )
    elif raw is not None and not plan_hash_verified:
        warnings.append(
            {"code": "frozen_plan_hash_mismatch", "scope": "frozen_plan", "blocking": True}
        )
    elif raw is not None:
        try:
            plan = ExecutablePlan.model_validate(raw)
        except Exception:  # noqa: BLE001 - malformed frozen data must fail closed
            warnings.append(
                {"code": "frozen_plan_invalid", "scope": "frozen_plan", "blocking": True}
            )
        else:
            plan_content_verified = True
            repetitions = plan.repetitions
            source = "frozen_plan_artifact"
            selection_digest = _digest(plan.selection.model_dump(mode="json"))

    # Historical records may retain repetitions in the run parameters or a
    # run-created event. Those are acceptable only when no artifact was expected;
    # an absent/malformed frozen artifact is never silently replaced by inference.
    if repetitions is None and not artifact_expected:
        candidate = params.get("repetitions")
        if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate > 0:
            repetitions = candidate
            source = "run_manifest_parameters"
        elif declared_repetitions:
            candidate_values = sorted(declared_repetitions)
            if len(candidate_values) == 1 and candidate_values[0] > 0:
                repetitions = candidate_values[0]
                source = "run_event"
        elif observed_repetitions:
            expected = set(range(max(observed_repetitions) + 1))
            if observed_repetitions == expected:
                repetitions = max(observed_repetitions) + 1
                source = "observed_work_items"

    # Without a frozen plan, an inferred repetition count is useful context but
    # cannot satisfy a strict comparison's repetition-policy identity.
    verified = (
        repetitions is not None
        and plan_hash_verified
        and plan_content_verified
        if artifact_expected
        else repetitions is not None and source in {"run_manifest_parameters", "run_event"}
    )
    return (
        {
            "repetitions": repetitions,
            "selection_digest": selection_digest,
            "source": source,
            "verified": verified,
            "artifact_hash_verified": plan_hash_verified if artifact_expected else None,
            "artifact_expected": artifact_expected,
            "observed_repetition_ids": sorted(observed_repetitions),
            "warnings": warnings,
        },
        warnings,
    )


def _application_facts(
    storage: Storage, artifacts: ArtifactStore | None, manifest: RunManifest
) -> dict[str, Any]:
    params = deep_unfreeze(manifest.parameters) or {}
    raw: dict[str, Any] | None = None
    source = "unknown"
    artifact_id = params.get("application_artifact_id")
    raw, problem, _raw_digest = _artifact_json(storage, artifacts, artifact_id)
    if raw is not None:
        source = "frozen_application_artifact"
    else:
        application_id = manifest.application_id
        if application_id:
            try:
                spec = storage.get_application(application_id)
            except Exception:  # noqa: BLE001 - treat an unavailable catalog as unknown
                spec = None
            if spec is not None:
                raw = _plain(spec.model_dump(mode="json"))
                source = "application_catalog"
        if raw is None and problem is not None and artifact_id:
            return {
                "available": False,
                "source": source,
                "code": problem,
                "application_id": manifest.application_id,
                "application_hash": manifest.application_hash,
                "manifest_hash_verified": False,
            }
    if raw is None:
        environment = deep_unfreeze(manifest.environment) or {}
        contract = environment.get("instrumentation") if isinstance(environment, Mapping) else None
        if not isinstance(contract, Mapping):
            contract = environment if isinstance(environment, Mapping) else {}
        contract_keys = {
            "runner",
            "core_schema",
            "output_binding",
            "output_binding_digest",
            "reset_policy",
            "environment_digest",
        }
        if contract_keys & set(contract):
            runner_fact = _text(contract.get("runner"), default="unknown") or "unknown"
            input_fact = _digest(contract.get("input_binding") or {})
            output_value = contract.get("output_binding_digest")
            output_fact = _text(output_value) or _digest(contract.get("output_binding") or {})
            reset_fact = _text(contract.get("reset_policy"), default="unknown") or "unknown"
            env_fact = _text(contract.get("environment_digest"))
            return {
                "available": True,
                "source": "run_manifest_environment",
                "code": None,
                "application_id": manifest.application_id,
                "application_hash": manifest.application_hash,
                "manifest_hash_verified": True,
                "revision": None,
                "runner": runner_fact,
                "core_schema": _text(contract.get("core_schema"), default=SCHEMA_VERSION),
                "input_binding_digest": input_fact,
                "output_binding_digest": output_fact,
                "reset_policy": reset_fact,
                "environment_digest": env_fact,
                "instrumentation_digest": _digest(
                    {
                        "core_schema": _text(contract.get("core_schema"), default=SCHEMA_VERSION),
                        "runner": runner_fact,
                        "input_binding_digest": input_fact,
                        "output_binding_digest": output_fact,
                        "reset_policy": reset_fact,
                        "environment_digest": env_fact,
                    }
                ),
            }
        return {
            "available": False,
            "source": source,
            "code": "application_unavailable",
            "application_id": manifest.application_id,
            "application_hash": manifest.application_hash,
            "manifest_hash_verified": False,
        }
    manifest_hash_verified = False
    try:
        spec = ApplicationSpec.model_validate(raw)
        runner = spec.runner.value
        input_binding = _digest(deep_unfreeze(spec.input_binding) or {})
        output_binding = _digest(deep_unfreeze(spec.output_binding) or {})
        reset_policy = spec.reset_policy.value
        environment_digest = (
            _identity_label(spec.environment_digest, "unknown")
            if spec.environment_digest is not None
            else None
        )
        revision = spec.revision
        application_id = spec.application_id
        manifest_hash_verified = (
            content_hash(spec.model_dump(mode="json")) == manifest.application_hash
        )
    except Exception:  # noqa: BLE001 - retain only the safe contract fallback
        runner = _text(raw.get("runner"), default="unknown") or "unknown"
        input_binding = _digest(raw.get("input_binding") or {})
        output_binding = _digest(raw.get("output_binding") or {})
        reset_policy = _text(raw.get("reset_policy"), default="unknown") or "unknown"
        environment_digest = _text(raw.get("environment_digest"))
        if environment_digest is not None:
            environment_digest = _identity_label(environment_digest, "unknown")
        revision = _text(raw.get("revision"))
        application_id = _text(raw.get("application_id"), default=manifest.application_id)
    if not manifest_hash_verified:
        return {
            "available": False,
            "source": source,
            "code": "application_manifest_hash_mismatch",
            "application_id": application_id,
            "application_hash": manifest.application_hash,
            "manifest_hash_verified": False,
        }
    return {
        "available": True,
        "source": source,
        "code": None,
        "application_id": application_id,
        "application_hash": manifest.application_hash,
        "manifest_hash_verified": manifest_hash_verified,
        "revision": revision,
        "runner": runner,
        "core_schema": SCHEMA_VERSION,
        "input_binding_digest": input_binding,
        "output_binding_digest": output_binding,
        "reset_policy": reset_policy,
        "environment_digest": environment_digest,
        "instrumentation_digest": _digest(
            {
                "core_schema": SCHEMA_VERSION,
                "runner": runner,
                "input_binding_digest": input_binding,
                "output_binding_digest": output_binding,
                "reset_policy": reset_policy,
                "environment_digest": environment_digest,
            }
        ),
    }


def _run_facts(
    storage: Storage,
    artifacts: ArtifactStore | None,
    run_id: str,
    requested_scoring_id: str | None,
) -> _RunFacts:
    record = storage.get_run(run_id)
    if record is None:
        raise ComparisonError(f"no run committed with run_id={run_id!r}")
    manifest = record.manifest
    events = storage.list_run_events(run_id)
    results = storage.list_metric_results(run_id)
    attempts = storage.list_evaluation_attempts(run_id)
    passes = _passes_for_run(manifest, events, results, attempts)
    selected = _select_pass(passes, requested_scoring_id)
    if selected is None:
        # Keep the run facts usable so the caller can return a structured
        # blocked result rather than manufacturing a pass.
        selected = _Pass(
            run_id=run_id,
            scoring_id=requested_scoring_id or "",
            kind="rescore",
            sequence=-1,
            source=(
                "requested_pass_not_found"
                if requested_scoring_id is not None
                else "pass_selection_required"
            ),
            complete=False,
        )
    cases = _case_facts(storage, manifest)
    work_items = storage.list_work_items(run_id)
    executions = storage.list_execution_attempts(run_id)
    observed_reps = {
        int(item.task_key.partition(":r")[2].partition(":")[0])
        for item in work_items
        if item.kind in {"execution", "evaluation"}
        and ":r" in item.task_key
        and item.task_key.partition(":r")[2].partition(":")[0].isdigit()
    }
    declared_reps = {
        int(event["payload"]["repetitions"])
        for event in events
        if event.get("event_type") == "run_created"
        and isinstance(event.get("payload"), Mapping)
        and isinstance(event["payload"].get("repetitions"), int)
        and not isinstance(event["payload"].get("repetitions"), bool)
        and event["payload"]["repetitions"] > 0
    }
    plan, plan_warnings = _plan_facts(
        storage,
        artifacts,
        manifest,
        observed_repetitions=observed_reps | {r.repetition_id for r in results},
        declared_repetitions=declared_reps,
    )
    if plan_warnings:
        # These are facts about an optional frozen artifact.  Keep them in a
        # private side channel until the final report; never expose raw errors.
        events = [*events, {"event_type": "_comparison_warning", "payload": plan_warnings}]
    application = _application_facts(storage, artifacts, manifest)
    dataset = storage.get_dataset(manifest.dataset_hash)
    return _RunFacts(
        record=record,
        manifest=manifest,
        dataset=dataset,
        events=events,
        cases=cases,
        work_items=work_items,
        executions=executions,
        plan=plan,
        application=application,
        all_passes=passes,
        selected=selected,
        all_results=results,
        all_attempts=attempts,
    )


# ---------------------------------------------------------------------------
# Metric identity reconstruction


def _fallback_judge(metric_id: str, metric_version: str, uses_models: bool) -> dict[str, Any]:
    if not uses_models:
        return {
            "kind": "not_used",
            "digest": _digest({"kind": "not_used", "metric_id": metric_id, "version": metric_version}),
            "verified": True,
        }
    return {"kind": "unknown", "digest": None, "verified": False}


def _fallback_rubric(metric_id: str, metric_version: str, plugin_id: str | None) -> dict[str, Any]:
    return {
        "kind": "framework_internal",
        "digest": _digest(
            {
                "plugin_id": plugin_id,
                "metric_id": metric_id,
                "metric_version": metric_version,
            }
        ),
        "verified": True,
    }


def _fallback_instrumentation(application: Mapping[str, Any]) -> dict[str, Any]:
    if application.get("available") and application.get("instrumentation_digest"):
        return {
            "kind": "observation_contract",
            "digest": application["instrumentation_digest"],
            "verified": True,
        }
    return {"kind": "unknown", "digest": None, "verified": False}


def _profile_value(
    profile: Mapping[str, Any] | None,
    result: EvaluationResult,
    *,
    manifest: RunManifest,
    application: Mapping[str, Any],
) -> dict[str, Any]:
    raw = dict(profile or {})
    provenance = deep_unfreeze(result.provenance) or {}
    binding = provenance.get("binding") if isinstance(provenance, Mapping) else {}
    if not isinstance(binding, Mapping):
        binding = {}
    compatibility = raw.get("compatibility")
    compatibility_recorded = isinstance(compatibility, Mapping) and bool(compatibility)
    if compatibility is None and isinstance(provenance, Mapping):
        compatibility = provenance.get("compatibility")
        compatibility_recorded = isinstance(compatibility, Mapping) and bool(compatibility)
    if compatibility is None:
        compatibility = {
            key: raw[key]
            for key in (
                "parameters_hash",
                "rule",
                "package_name",
                "package_version",
                "dependency_lock_hash",
                "dependency_implementation",
                "judge",
                "rubric",
                "instrumentation",
                "required_fields",
                "compatibility_hash",
            )
            if key in raw
        }
        compatibility_recorded = bool(compatibility.get("compatibility_hash"))
    if not isinstance(compatibility, Mapping):
        compatibility = {}
    manifest_data = raw.get("manifest")
    if not isinstance(manifest_data, Mapping):
        manifest_data = {}
    else:
        manifest_data = dict(manifest_data)
    # Accept the compact identity shape used by a few early Prompt 14
    # records, while keeping the canonical nested manifest shape as the
    # preferred source.
    for field_name in (
        "evaluator_id",
        "version",
        "plugin_id",
        "plugin_version",
        "package_name",
        "package_version",
        "value_kind",
        "direction",
        "scope",
        "aggregation",
        "uses_models",
        "description",
        "limitations",
        "requires",
    ):
        if field_name not in manifest_data and field_name in raw:
            manifest_data[field_name] = raw[field_name]
    if "evaluator_id" not in manifest_data and "metric_id" in raw:
        manifest_data["evaluator_id"] = raw["metric_id"]
    if "version" not in manifest_data and "metric_version" in raw:
        manifest_data["version"] = raw["metric_version"]
    metric_id = _text(manifest_data.get("evaluator_id"), default=result.metric_id) or result.metric_id
    metric_version = (
        _text(manifest_data.get("version"), default=result.metric_version) or result.metric_version
    )
    uses_models = manifest_data.get("uses_models")
    if not isinstance(uses_models, bool):
        uses_models = not metric_id.startswith("native.")
    params = binding.get("params")
    if not isinstance(params, Mapping):
        params = raw.get("params")
    if not isinstance(params, Mapping):
        params = {}
    params = _plain(params)
    parameters_hash = _text(compatibility.get("parameters_hash"))
    if parameters_hash is None:
        parameters_hash = _digest(params)
    rule = raw.get("rule")
    if rule is None:
        rule = binding.get("rule")
    if rule is None and result.rule is not None:
        rule = result.rule.model_dump(mode="json")
    direction = _enum_value(
        manifest_data.get("direction", result.direction),
        _enum_value(result.direction, "none"),
    )
    value_kind = _text(manifest_data.get("value_kind"), default="scalar") or "scalar"
    if result.value is not None:
        value_kind = result.value.kind
    scope = _enum_value(manifest_data.get("scope", result.scope), "case")
    aggregation = _text(manifest_data.get("aggregation"), default="")
    if not aggregation:
        aggregation = {"scalar": "mean", "boolean": "rate", "category": "category_counts"}.get(
            value_kind, "none"
        )
    plugin_id = _text(manifest_data.get("plugin_id"))
    if plugin_id is None:
        plugin_id = _text(provenance.get("plugin_id")) if isinstance(provenance, Mapping) else None
    plugin_version = _text(manifest_data.get("plugin_version"))
    if plugin_version is None and isinstance(provenance, Mapping):
        plugin_version = _text(provenance.get("plugin_version"))
    package_name = _text(manifest_data.get("package_name"))
    package_version = _text(manifest_data.get("package_version"))
    dependency: str | None
    dependency_value = compatibility.get("dependency_lock_hash")
    if dependency_value is None:
        dependency_value = compatibility.get("dependency_implementation")
    if isinstance(dependency_value, Mapping):
        dependency = _digest(dependency_value)
    else:
        dependency = _text(dependency_value)
    if dependency is None:
        dependency = _text(manifest.dependency_lock_hash)
    judge = _component(compatibility.get("judge"), fallback=_fallback_judge(metric_id, metric_version, uses_models))
    rubric = _component(compatibility.get("rubric"), fallback=_fallback_rubric(metric_id, metric_version, plugin_id))
    instrumentation = _component(
        compatibility.get("instrumentation"), fallback=_fallback_instrumentation(application)
    )
    if not instrumentation.get("verified") and application.get("available"):
        # Older engine manifests froze an unverified placeholder.  A verified
        # frozen application artifact is sufficient to recover the observation
        # contract without importing or executing anything.
        instrumentation = _fallback_instrumentation(application)
    required = manifest_data.get("requires")
    if required is None:
        required = compatibility.get("required_fields")
    if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
        required = []
    required_paths: list[str] = []
    for item in required:
        if isinstance(item, Mapping):
            path = _text(item.get("path"))
            if path is not None:
                required_paths.append(path)
    required_paths.sort()
    semantic_digest = _digest(
        {
            "metric_id": metric_id,
            "metric_version": metric_version,
            "description": manifest_data.get("description"),
            "limitations": manifest_data.get("limitations", []),
            "required_fields": required_paths,
            "value_kind": value_kind,
            "direction": direction,
            "scope": scope,
            "aggregation": aggregation,
        }
    )
    rule_digest = _rule_digest(rule)
    identity_core = {
        "metric_id": metric_id,
        "metric_version": metric_version,
        "value_kind": value_kind,
        "direction": direction,
        "scope": scope,
        "aggregation": aggregation,
        "binding_hash": result.binding_hash
        or _text(raw.get("binding_hash"))
        or _text(compatibility.get("binding_hash")),
        "parameters_hash": parameters_hash,
        "rule_digest": rule_digest,
        "semantic_digest": semantic_digest,
        "plugin_id": plugin_id,
        "plugin_version": plugin_version,
        "package_name": package_name,
        "package_version": package_version,
        "dependency_lock_hash": dependency,
        "judge": judge,
        "rubric": rubric,
        "instrumentation": instrumentation,
    }
    compatibility_hash = _text(compatibility.get("compatibility_hash"))
    if compatibility_hash is None:
        compatibility_hash = _digest(identity_core)
    binding_hash = result.binding_hash or _text(raw.get("binding_hash")) or _text(
        compatibility.get("binding_hash")
    )
    identity_recorded = bool(compatibility_recorded and compatibility_hash and binding_hash)
    binding_verified = bool(binding_hash)
    identity_verified = identity_recorded
    canonical_identity = compatibility.get("schema_version") == "aibench.evaluation-identity/1"
    if canonical_identity:
        expected_binding = content_hash(
            {
                "evaluator_id": metric_id,
                "version": metric_version,
                "params": params,
                "rule": rule,
            }
        )
        binding_verified = binding_verified and expected_binding == binding_hash
        # ``EvaluationCompatibilityIdentity`` adds schema_version as a
        # model-level default; scoring hashes the identity payload without that
        # defaulted field.  Verify the same canonical payload here.
        identity_payload = {
            key: compatibility.get(key)
            for key in (
                "metric_id",
                "metric_version",
                "value_kind",
                "direction",
                "scope",
                "aggregation",
                "binding_hash",
                "parameters_hash",
                "rule",
                "plugin_id",
                "plugin_version",
                "package_name",
                "package_version",
                "dependency_lock_hash",
                "judge",
                "rubric",
                "instrumentation",
                "required_fields",
                "final_attempt_rule",
            )
            if key in compatibility
        }
        identity_verified = (
            identity_recorded
            and set(identity_payload) >= {
                "metric_id",
                "metric_version",
                "binding_hash",
                "parameters_hash",
                "rule",
                "plugin_id",
                "plugin_version",
                "judge",
                "rubric",
                "instrumentation",
                "required_fields",
                "final_attempt_rule",
            }
            and _digest(identity_payload) == compatibility_hash
        )
        if not binding_verified:
            identity_verified = False
    if not binding_hash:
        binding_hash = _digest(
            {
                "metric_id": metric_id,
                "metric_version": metric_version,
                "parameters_hash": parameters_hash,
                "rule_digest": rule_digest,
            }
        )
    return {
        **identity_core,
        "binding_hash": binding_hash,
        "binding_verified": binding_verified and identity_verified,
        "compatibility_hash": compatibility_hash,
        "identity_recorded": identity_recorded,
        "identity_verified": identity_verified,
        "uses_models": uses_models,
        "profile_present": profile is not None,
    }


def _specs_for_pass(run: _RunFacts, selected: _Pass) -> list[_MetricSpec]:
    by_binding: dict[str, list[EvaluationResult]] = defaultdict(list)
    for stored_result in selected.results:
        binding = stored_result.binding_hash
        if not binding:
            matching = [
                key
                for key, value in selected.profiles.items()
                if value.get("metric") in {stored_result.metric_id, f"{stored_result.metric_id}@{stored_result.metric_version}"}
            ]
            binding = matching[0] if len(matching) == 1 else _digest(
                {"metric_id": stored_result.metric_id, "metric_version": stored_result.metric_version}
            )
        by_binding[binding].append(stored_result)
    for binding in selected.profiles:
        by_binding.setdefault(binding, [])
    specs: list[_MetricSpec] = []
    for binding in sorted(by_binding):
        results = by_binding[binding]
        result = results[0] if results else None
        if result is not None:
            result_profile = _profile_for_result(selected.profiles, result)
            values = _profile_value(
                result_profile, result, manifest=run.manifest, application=run.application
            )
        else:
            profile_only = selected.profiles[binding]
            values = _profile_from_profile(
                binding, profile_only, run.manifest, run.application
            )
        specs.append(
            _MetricSpec(
                binding_hash=binding,
                metric_id=values["metric_id"],
                metric_version=values["metric_version"],
                label=_metric_label(values["metric_id"]),
                direction=values["direction"],
                value_kind=values["value_kind"],
                scope=values["scope"],
                aggregation=values["aggregation"],
                semantic_digest=values["semantic_digest"],
                parameters_hash=values["parameters_hash"],
                rule_digest=values["rule_digest"],
                plugin_id=values["plugin_id"],
                plugin_version=values["plugin_version"],
                package_name=values["package_name"],
                package_version=values["package_version"],
                dependency_lock_hash=values["dependency_lock_hash"],
                judge=values["judge"],
                rubric=values["rubric"],
                instrumentation=values["instrumentation"],
                uses_models=values["uses_models"],
                compatibility_hash=values["compatibility_hash"],
                binding_verified=values["binding_verified"],
                identity_recorded=values["identity_recorded"],
                identity_verified=values["identity_verified"],
                results=results,
                profile_present=values["profile_present"],
            )
        )
    return specs


def _profile_from_profile(
    binding: str,
    profile: Mapping[str, Any],
    manifest: RunManifest,
    application: Mapping[str, Any],
) -> dict[str, Any]:
    # A profile without a result still has enough manifest information to be
    # checked.  Use a small synthetic result only as a value carrier; no
    # placeholder result is ever persisted.
    synthetic = EvaluationResult(
        result_id=f"profile:{binding}",
        run_id=manifest.run_id,
        case_id="__profile__",
        metric_id=_text(profile.get("metric"), default="unknown.metric") or "unknown.metric",
        metric_version="0.0.0",
        status=ExecutionStatus.NOT_APPLICABLE,
        decision=Decision.NOT_EVALUATED,
        binding_hash=binding,
    )
    return _profile_value(profile, synthetic, manifest=manifest, application=application)


def _specs_by_label(specs: Sequence[_MetricSpec]) -> dict[str, list[_MetricSpec]]:
    grouped: dict[str, list[_MetricSpec]] = defaultdict(list)
    for spec in specs:
        grouped[spec.label].append(spec)
    return grouped


# ---------------------------------------------------------------------------
# Identity checks


def _check(
    name: str,
    *,
    ok: bool | None,
    code: str | None = None,
    baseline: Any = None,
    current: Any = None,
    blocking: bool = True,
) -> dict[str, Any]:
    status = "unknown" if ok is None else ("match" if ok else "mismatch")
    return {
        "name": name,
        "status": status,
        "compatible": ok,
        "blocking": blocking,
        "reason_code": code,
        "baseline": _plain(baseline),
        "current": _plain(current),
    }


def _case_check(baseline: _CaseFacts, current: _CaseFacts) -> dict[str, Any]:
    if not baseline.available or not current.available:
        return _check(
            "case_content_identity",
            ok=None,
            code="case_content_unavailable",
            baseline={"available": baseline.available, "count": baseline.count},
            current={"available": current.available, "count": current.count},
        )
    if baseline.duplicate_ids or current.duplicate_ids:
        return _check(
            "case_content_identity",
            ok=False,
            code="duplicate_case_identity",
            baseline={"duplicate_count": len(baseline.duplicate_ids)},
            current={"duplicate_count": len(current.duplicate_ids)},
        )
    common = sorted(set(baseline.by_id) & set(current.by_id))
    changed = [case_id for case_id in common if baseline.by_id[case_id] != current.by_id[case_id]]
    only_baseline = sorted(set(baseline.by_id) - set(current.by_id))
    only_current = sorted(set(current.by_id) - set(baseline.by_id))
    ok = not changed and not only_baseline and not only_current
    return _check(
        "case_content_identity",
        ok=ok,
        code=None if ok else "case_content_changed",
        baseline={
            "case_count": baseline.count,
            "content_digest_count": len(baseline.by_id),
            "changed_case_count": len(changed),
            "baseline_only_case_count": len(only_baseline),
            "changed_case_ids": changed,
            "baseline_only_case_ids": only_baseline,
        },
        current={
            "case_count": current.count,
            "content_digest_count": len(current.by_id),
            "changed_case_count": len(changed),
            "current_only_case_count": len(only_current),
            "changed_case_ids": changed,
            "current_only_case_ids": only_current,
        },
    )


def _instrumentation_check(
    baseline: _RunFacts, current: _RunFacts, specs: Sequence[tuple[_MetricSpec, _MetricSpec]]
) -> dict[str, Any]:
    left = baseline.application
    right = current.application
    if left.get("available") and right.get("available"):
        fields = (
            "runner",
            "core_schema",
            "input_binding_digest",
            "output_binding_digest",
            "reset_policy",
            "environment_digest",
        )
        field_mismatches = [field for field in fields if left.get(field) != right.get(field)]
        ok = not field_mismatches
        return _check(
            "observation_instrumentation",
            ok=ok,
            code=None if ok else "instrumentation_contract_changed",
            baseline={field: left.get(field) for field in fields},
            current={field: right.get(field) for field in fields},
        )
    if not specs:
        return _check(
            "observation_instrumentation",
            ok=None,
            code="instrumentation_unavailable",
            baseline={"available": bool(left.get("available"))},
            current={"available": bool(right.get("available"))},
        )
    # A frozen metric profile is also an observation contract.  It can stand in
    # for a missing application artifact, but an unverified component is not
    # enough for strict qualification.
    profile_mismatches: list[str] = []
    unknown = False
    for left_spec, right_spec in specs:
        if (
            not left_spec.instrumentation.get("verified")
            or not right_spec.instrumentation.get("verified")
            or left_spec.instrumentation.get("kind") in {"unknown", "unavailable", "not_recorded"}
            or right_spec.instrumentation.get("kind") in {"unknown", "unavailable", "not_recorded"}
        ):
            unknown = True
        elif left_spec.instrumentation.get("digest") != right_spec.instrumentation.get("digest"):
            profile_mismatches.append(left_spec.label)
    return _check(
        "observation_instrumentation",
        ok=None if unknown else not profile_mismatches,
        code="instrumentation_unavailable" if unknown else (
            None if not profile_mismatches else "instrumentation_contract_changed"
        ),
        baseline={"available": bool(left.get("available"))},
        current={"available": bool(right.get("available"))},
        blocking=True,
    ) | {
        "mismatched_metric_count": len(profile_mismatches),
    }


def _spec_component_checks(left: _MetricSpec, right: _MetricSpec) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def add(name: str, a: Any, b: Any, *, unknown: bool = False, code: str = "") -> None:
        if unknown:
            checks.append(
                _check(name, ok=None, code=code or f"unknown_{name}", baseline=a, current=b)
            )
        else:
            mismatch_code = f"{name}_changed"
            if code.startswith("unknown_"):
                mismatch_code = f"{name}_changed"
            checks.append(
                _check(
                    name,
                    ok=a == b,
                    code=None if a == b else (code if unknown else mismatch_code),
                    baseline=a,
                    current=b,
                )
            )

    identity_known = left.identity_recorded and right.identity_recorded
    add(
        "compatibility_identity",
        {
            "recorded": left.identity_recorded,
            "verified": left.identity_verified,
        },
        {
            "recorded": right.identity_recorded,
            "verified": right.identity_verified,
        },
        unknown=not identity_known,
        code="unknown_compatibility_identity",
    )
    binding_known = left.binding_verified and right.binding_verified and identity_known
    add(
        "exact_binding",
        {"binding_hash": left.binding_hash},
        {"binding_hash": right.binding_hash},
        unknown=not binding_known,
        code="unknown_exact_binding",
    )
    for name, a, b in (
        ("metric_semantics", left.semantic_digest, right.semantic_digest),
        ("rule", left.rule_digest, right.rule_digest),
        ("value_kind", left.value_kind, right.value_kind),
        ("direction", left.direction, right.direction),
        ("aggregation", left.aggregation, right.aggregation),
        ("parameters", left.parameters_hash, right.parameters_hash),
    ):
        unknown = a is None or b is None
        if name == "rule" and a is None and b is None:
            unknown = False
        add(name, a, b, unknown=unknown, code=f"unknown_{name}")
    plugin_left = (left.plugin_id, left.plugin_version, left.package_name, left.package_version)
    plugin_right = (right.plugin_id, right.plugin_version, right.package_name, right.package_version)
    add("plugin_implementation", plugin_left, plugin_right, unknown=not all((left.plugin_id, left.plugin_version, right.plugin_id, right.plugin_version)), code="unknown_plugin_implementation")
    for name, left_component, right_component in (
        ("judge", left.judge, right.judge),
        ("rubric", left.rubric, right.rubric),
        ("instrumentation", left.instrumentation, right.instrumentation),
    ):
        unknown = (
            not left_component.get("verified")
            or not right_component.get("verified")
            or left_component.get("kind") in {"unknown", "unavailable", "not_recorded"}
            or right_component.get("kind") in {"unknown", "unavailable", "not_recorded"}
        )
        if (
            left_component.get("kind") == "not_used"
            and right_component.get("kind") == "not_used"
        ):
            unknown = False
        add(
            name,
            left_component,
            right_component,
            unknown=unknown,
            code=f"unknown_{name}",
        )
    native_judge = (
        left.judge.get("kind") == "not_used"
        and right.judge.get("kind") == "not_used"
    )
    add(
        "compatibility_hash",
        "native_judge_hash_not_identity" if native_judge else left.compatibility_hash,
        "native_judge_hash_not_identity" if native_judge else right.compatibility_hash,
        unknown=(
            not native_judge
            and (left.compatibility_hash is None or right.compatibility_hash is None)
        ),
        code="unknown_compatibility_hash",
    )
    dependency_required = left.uses_models or right.uses_models
    add(
        "dependency_implementation",
        left.dependency_lock_hash,
        right.dependency_lock_hash,
        unknown=dependency_required and (left.dependency_lock_hash is None or right.dependency_lock_hash is None),
        code="unknown_dependency_implementation",
    )
    return checks


def _global_identity(
    baseline: _RunFacts,
    current: _RunFacts,
    common_specs: Sequence[tuple[_MetricSpec, _MetricSpec]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bm = baseline.manifest
    cm = current.manifest
    warnings: list[dict[str, Any]] = []
    dataset_left = {
        "dataset_hash": bm.dataset_hash,
        "dataset_id": _text(getattr(baseline.dataset, "dataset_id", None)),
        "schema_version": _text(getattr(baseline.dataset, "schema_version", None)),
        "case_count": getattr(baseline.dataset, "case_count", baseline.cases.count),
    }
    dataset_right = {
        "dataset_hash": cm.dataset_hash,
        "dataset_id": _text(getattr(current.dataset, "dataset_id", None)),
        "schema_version": _text(getattr(current.dataset, "schema_version", None)),
        "case_count": getattr(current.dataset, "case_count", current.cases.count),
    }
    dataset_available = baseline.dataset is not None and current.dataset is not None
    dataset_ok = (
        dataset_available
        and bm.dataset_hash == cm.dataset_hash
        and dataset_left["dataset_id"] == dataset_right["dataset_id"]
        and dataset_left["schema_version"] == dataset_right["schema_version"]
        and dataset_left["case_count"] == dataset_right["case_count"]
    )
    dataset_check = _check(
        "frozen_dataset_identity",
        ok=dataset_ok,
        code=None if dataset_ok else (
            "dataset_identity_unavailable" if not dataset_available else "dataset_identity_changed"
        ),
        baseline=dataset_left,
        current=dataset_right,
    )
    case_check = _case_check(baseline.cases, current.cases)
    plan_ok = (
        baseline.plan.get("verified")
        and current.plan.get("verified")
        and not baseline.plan.get("warnings")
        and not current.plan.get("warnings")
        and baseline.plan.get("repetitions") == current.plan.get("repetitions")
        and baseline.plan.get("selection_digest") == current.plan.get("selection_digest")
    )
    plan_check = _check(
        "repetition_policy",
        ok=plan_ok,
        code=None if plan_ok else "repetition_policy_changed_or_unknown",
        baseline=_plain(baseline.plan),
        current=_plain(current.plan),
    )
    instrumentation = _instrumentation_check(baseline, current, common_specs)
    run_state_ok = (
        baseline.record.status in _FINISHED_STATUSES
        and current.record.status in _FINISHED_STATUSES
    )
    run_state_check = _check(
        "run_state",
        ok=run_state_ok,
        code=None if run_state_ok else "run_not_finished",
        baseline={"status": baseline.record.status},
        current={"status": current.record.status},
        blocking=True,
    )
    baseline_app = baseline.application
    current_app = current.application
    baseline_app_expected = bool((deep_unfreeze(baseline.manifest.parameters) or {}).get("application_artifact_id"))
    current_app_expected = bool((deep_unfreeze(current.manifest.parameters) or {}).get("application_artifact_id"))
    app_evidence_ok = (
        (not baseline_app_expected or baseline_app.get("manifest_hash_verified") is True)
        and (not current_app_expected or current_app.get("manifest_hash_verified") is True)
    )
    app_same = baseline.manifest.application_hash == current.manifest.application_hash
    app_check = _check(
        "application_identity",
        ok=app_same,
        code=None if app_same else "application_identity_changed",
        baseline={
            "application_hash": baseline.manifest.application_hash,
            "application_id": baseline.manifest.application_id,
        },
        current={
            "application_hash": current.manifest.application_hash,
            "application_id": current.manifest.application_id,
        },
        blocking=False,
    )
    app_artifact_check = _check(
        "application_artifact",
        ok=app_evidence_ok,
        code=None if app_evidence_ok else "application_artifact_unverified",
        baseline={
            "artifact_expected": baseline_app_expected,
            "artifact_verified": baseline_app.get("manifest_hash_verified"),
        },
        current={
            "artifact_expected": current_app_expected,
            "artifact_verified": current_app.get("manifest_hash_verified"),
        },
        blocking=True,
    )
    checks = {
        "dataset": dataset_check,
        "case_content": case_check,
        "repetition_policy": plan_check,
        "instrumentation": instrumentation,
        "run_state": run_state_check,
        "application": app_check,
        "application_artifact": app_artifact_check,
    }
    for name, check in checks.items():
        if check["blocking"] and check["compatible"] is not True:
            warnings.append(
                {
                    "code": check["reason_code"] or f"{name}_incompatible",
                    "scope": name,
                    "blocking": True,
                }
            )
    return checks, warnings


# ---------------------------------------------------------------------------
# Pairing and numeric diagnostics


def _parse_work_keys(run: _RunFacts) -> dict[str, set[PairKey]]:
    by_binding: dict[str, set[PairKey]] = defaultdict(set)
    for item in run.work_items:
        if item.kind != "evaluation" or not item.task_key.startswith("eval:"):
            continue
        # Case IDs are user-defined and may contain colons (including ``:r``).
        # Decode the generated repetition and binding suffixes from the right edge;
        # splitting the whole key on ``:`` would silently pair the wrong case.
        body = item.task_key[len("eval:") :]
        case_and_repetition, separator, binding_key = body.rpartition(":")
        if not separator or not case_and_repetition or not binding_key:
            continue
        case_id, repetition_separator, repetition_text = case_and_repetition.rpartition(":r")
        if not repetition_separator or not case_id or not repetition_text.isdecimal():
            continue
        # Work-item keys retain a short binding prefix.  Keep it as the map key;
        # _selected_keys matches it to the full frozen binding hash.
        by_binding[binding_key].add(PairKey(case_id, int(repetition_text)))
    return by_binding


def _selected_keys(run: _RunFacts, spec: _MetricSpec) -> set[PairKey]:
    keys: set[PairKey] = set()
    matched_work = False
    work_map = _parse_work_keys(run)
    for binding, values in work_map.items():
        if binding == spec.binding_hash or spec.binding_hash.startswith(binding) or binding.startswith(spec.binding_hash):
            keys.update(values)
            matched_work = True
    for result in spec.results:
        keys.add(PairKey(result.case_id, result.repetition_id))
    if not matched_work:
        final: dict[tuple[str, int], ExecutionResult] = {}
        for execution in run.executions:
            key = (execution.case_id, execution.repetition_id)
            old = final.get(key)
            if old is None or execution.attempt_id > old.attempt_id:
                final[key] = execution
        keys.update(PairKey(case_id, repetition) for case_id, repetition in final)
        repetitions = run.plan.get("repetitions")
        if isinstance(repetitions, int) and repetitions > 0 and run.cases.available:
            keys.update(
                PairKey(case_id, repetition)
                for case_id in run.cases.by_id
                for repetition in range(repetitions)
            )
    return keys


def _result_map(spec: _MetricSpec) -> dict[PairKey, EvaluationResult]:
    return {PairKey(r.case_id, r.repetition_id): r for r in spec.results}


def _lineage_mismatch_count(run: _RunFacts, spec: _MetricSpec) -> int:
    final: dict[tuple[str, int], ExecutionResult] = {}
    for execution in run.executions:
        key = (execution.case_id, execution.repetition_id)
        old = final.get(key)
        if old is None or execution.attempt_id > old.attempt_id:
            final[key] = execution
    mismatches = 0
    for result in spec.results:
        final_execution = final.get((result.case_id, result.repetition_id))
        if (
            not result.execution_id
            or final_execution is None
            or result.execution_id != final_execution.execution_id
        ):
            mismatches += 1
    return mismatches


def _matching_case_ids(
    baseline: _RunFacts,
    current: _RunFacts,
) -> tuple[set[str], str | None]:
    if not baseline.cases.available or not current.cases.available:
        return set(), "case_content_unavailable"
    if baseline.cases.duplicate_ids or current.cases.duplicate_ids:
        return set(), "duplicate_case_identity"
    return {
        case_id
        for case_id in set(baseline.cases.by_id) & set(current.cases.by_id)
        if baseline.cases.by_id[case_id] == current.cases.by_id[case_id]
    }, None


def _side(
    run: _RunFacts,
    spec: _MetricSpec,
    *,
    allowed_case_ids: set[str] | None,
) -> tuple[ComparisonSide, list[dict[str, Any]]]:
    keys = _selected_keys(run, spec)
    if allowed_case_ids is not None:
        keys = {key for key in keys if key.case_id in allowed_case_ids}
    result_map = _result_map(spec)
    observations: list[NumericObservation] = []
    invalid: list[dict[str, Any]] = []
    for key in sorted(keys):
        result = result_map.get(key)
        if result is None:
            continue
        value, malformed = _numeric_value(result)
        if malformed is not None:
            invalid.append(
                {
                    "case_id": key.case_id,
                    "repetition_id": key.repetition_id,
                    "reason_code": malformed,
                }
            )
        status = "ok" if result.status is ExecutionStatus.OK and malformed is None else (
            _status_name(result.status) if result.status is not ExecutionStatus.OK else "error"
        )
        observations.append(
            NumericObservation(
                key,
                cast(Any, status),
                value if status == "ok" else None,
            )
        )
    identity = MetricIdentity(
        metric_id=spec.metric_id,
        metric_version=spec.metric_version,
        binding_hash=spec.binding_hash,
        direction=cast(Any, spec.direction),
        value_kind="scalar",
    )
    return (
        ComparisonSide(
            run_id=run.manifest.run_id,
            metric=identity,
            selected_keys=tuple(sorted(keys)),
            observations=tuple(observations),
        ),
        invalid,
    )


def _coverage_gate(stats: Mapping[str, Any], threshold: float) -> dict[str, Any]:
    value = stats.get("denominators", {}).get("coverage", {}).get(
        "complete_numeric_pairs_over_paired_selected"
    )
    ratio = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None
    complete = stats.get("denominators", {}).get("complete_numeric_pairs")
    has_complete = isinstance(complete, int) and not isinstance(complete, bool) and complete > 0
    passed = ratio is not None and has_complete and ratio >= threshold
    return {
        "status": "pass" if passed else "fail",
        "passed": passed,
        "minimum": threshold,
        "paired_complete_over_paired_selected": ratio,
        "paired_selected": stats.get("denominators", {}).get("paired_selected"),
        "complete_numeric_pairs": complete,
        "reason_code": None if passed else (
            "no_complete_numeric_pairs" if not has_complete else "paired_coverage_below_minimum"
        ),
    }


def _numeric_comparison(
    left_run: _RunFacts,
    right_run: _RunFacts,
    left: _MetricSpec,
    right: _MetricSpec,
    *,
    threshold: float,
    seed: int,
    replicates: int,
    allowed_case_ids: set[str] | None,
) -> tuple[dict[str, Any] | None, dict[str, Any], list[dict[str, Any]]]:
    if left.scope != "case" or right.scope != "case":
        return None, {
            "status": "not_applicable",
            "passed": False,
            "reason_code": "metric_scope_not_case",
        }, []
    if left.value_kind != "scalar" or right.value_kind != "scalar" or left.aggregation == "none" or right.aggregation == "none":
        return None, {
            "status": "not_applicable",
            "passed": True,
            "reason_code": "metric_has_no_scalar_mean",
        }, []
    left_side, left_invalid = _side(left_run, left, allowed_case_ids=allowed_case_ids)
    right_side, right_invalid = _side(right_run, right, allowed_case_ids=allowed_case_ids)
    try:
        case_groups = {
            case_id: left_run.cases.groups.get(case_id) or right_run.cases.groups.get(case_id)
            for case_id in set(left_run.cases.by_id) | set(right_run.cases.by_id)
        }
        stats = compare_numeric_metric(
            left_side,
            right_side,
            case_groups=case_groups,
            bootstrap_replicates=replicates,
            seed=seed,
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        # Do not return exception text: historical records may contain sensitive
        # values in a validation message.
        del exc
        return None, {
            "status": "fail",
            "passed": False,
            "reason_code": "numeric_comparison_unavailable",
        }, [*left_invalid, *right_invalid]
    return stats, _coverage_gate(stats, threshold), [*left_invalid, *right_invalid]


# ---------------------------------------------------------------------------
# Judge stability


def _judge_observations(
    run: _RunFacts, spec_binding: str, scoring_ids: Sequence[str]
) -> tuple[list[JudgeObservation], list[ExecutionKey]]:
    observations: list[JudgeObservation] = []
    units: set[ExecutionKey] = set()
    for selected in run.all_passes:
        if scoring_ids and selected.scoring_id not in scoring_ids:
            continue
        for result in selected.results:
            binding_matches = (result.binding_hash or "") == spec_binding
            if not binding_matches and not result.binding_hash:
                binding_matches = any(
                    spec.binding_hash == spec_binding and result in spec.results
                    for spec in _specs_for_pass(run, selected)
                )
            if not binding_matches or not result.execution_id:
                continue
            key = ExecutionKey(result.execution_id, result.repetition_id)
            units.add(key)
            value, malformed = _numeric_value(result)
            if malformed is not None:
                value = None
            if result.status is ExecutionStatus.OK:
                decision = _decision_name(result.decision)
                if decision not in {"pass", "fail", "indeterminate"}:
                    decision = "indeterminate"
                observations.append(
                    JudgeObservation(key, selected.scoring_id, "ok", value, decision)  # type: ignore[arg-type]
                )
            else:
                observations.append(
                    JudgeObservation(key, selected.scoring_id, _status_name(result.status), None, "not_evaluated")  # type: ignore[arg-type]
                )
    # Include final stored executions even if a pass has no result for them;
    # this makes missing repeats visible rather than silently shrinking units.
    # Retries are attempts of one application execution, not independent judge
    # repetition units.
    final: dict[tuple[str, int], ExecutionResult] = {}
    for execution in run.executions:
        unit_key = (execution.case_id, execution.repetition_id)
        old = final.get(unit_key)
        if old is None or execution.attempt_id > old.attempt_id:
            final[unit_key] = execution
    for execution in final.values():
        if execution.execution_id:
            units.add(ExecutionKey(execution.execution_id, execution.repetition_id))
    return observations, sorted(units)


# ---------------------------------------------------------------------------
# Cross-framework diagnostics


def _score_summary(results: Sequence[EvaluationResult]) -> dict[str, Any]:
    values: list[float] = []
    for result in results:
        value, malformed = _numeric_value(result)
        if result.status is ExecutionStatus.OK and malformed is None and value is not None:
            values.append(value)
    return {
        "measured_count": len(values),
        "minimum": round(min(values), 12) if values else None,
        "maximum": round(max(values), 12) if values else None,
        "mean": round(math.fsum(values) / len(values), 12) if values else None,
        "denominator": "ok_numeric_results_on_this_side_only",
    }


def _decision_summary(results: Sequence[EvaluationResult]) -> dict[str, Any]:
    statuses = Counter(_status_name(r.status) for r in results)
    decisions = Counter(_decision_name(r.decision) for r in results)
    return {
        "status_counts": dict(sorted(statuses.items())),
        "decision_counts": {
            decision: decisions.get(decision, 0)
            for decision in ("pass", "fail", "indeterminate", "not_evaluated")
        },
    }


def _cross_framework(
    left_run: _RunFacts,
    right_run: _RunFacts,
    left_specs: Sequence[_MetricSpec],
    right_specs: Sequence[_MetricSpec],
    *,
    allowed_case_ids: set[str] | None,
) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for left in left_specs:
        for right in right_specs:
            if left.concept != right.concept or _framework(left.metric_id, left.plugin_id) == _framework(
                right.metric_id, right.plugin_id
            ):
                continue
            left_results = _result_map(left)
            right_results = _result_map(right)
            left_keys = _selected_keys(left_run, left)
            right_keys = _selected_keys(right_run, right)
            keys = left_keys & right_keys
            if allowed_case_ids is not None:
                keys = {key for key in keys if key.case_id in allowed_case_ids}
            matrix = Counter({"pass_pass": 0, "pass_fail": 0, "fail_pass": 0, "fail_fail": 0, "unmeasured": 0})
            left_observed: list[EvaluationResult] = []
            right_observed: list[EvaluationResult] = []
            execution_identity_mismatches = 0
            execution_identity_unknown = 0
            for key in sorted(keys):
                lresult = left_results.get(key)
                rresult = right_results.get(key)
                if lresult is not None:
                    left_observed.append(lresult)
                if rresult is not None:
                    right_observed.append(rresult)
                ld = _decision_for_matrix(lresult)
                rd = _decision_for_matrix(rresult)
                if lresult is not None and rresult is not None:
                    if not lresult.execution_id or not rresult.execution_id:
                        execution_identity_unknown += 1
                        ld = rd = None
                    elif lresult.execution_id != rresult.execution_id:
                        execution_identity_mismatches += 1
                        ld = rd = None
                if ld == "pass" and rd == "pass":
                    matrix["pass_pass"] += 1
                elif ld == "pass" and rd == "fail":
                    matrix["pass_fail"] += 1
                elif ld == "fail" and rd == "pass":
                    matrix["fail_pass"] += 1
                elif ld == "fail" and rd == "fail":
                    matrix["fail_fail"] += 1
                else:
                    matrix["unmeasured"] += 1
            diagnostics.append(
                {
                    "baseline": {
                        "metric_id": _identity_label(left.metric_id, "unknown.metric"),
                        "metric_version": _identity_label(left.metric_version, "unknown"),
                        "binding_hash": left.binding_hash,
                        "framework": _identity_label(
                            _framework(left.metric_id, left.plugin_id), "unknown"
                        ),
                    },
                    "current": {
                        "metric_id": _identity_label(right.metric_id, "unknown.metric"),
                        "metric_version": _identity_label(right.metric_version, "unknown"),
                        "binding_hash": right.binding_hash,
                        "framework": _identity_label(
                            _framework(right.metric_id, right.plugin_id), "unknown"
                        ),
                    },
                    "concept": _identity_label(left.concept, "unknown.concept"),
                    "pairing": "same_case_id_and_repetition_with_matching_case_content",
                    "paired_count": len(keys),
                    "matrix": dict(matrix),
                    "decision_matrix": dict(matrix),
                    "unmeasured_count": matrix["unmeasured"],
                    "execution_identity_mismatch_count": execution_identity_mismatches,
                    "execution_identity_unknown_count": execution_identity_unknown,
                    "baseline_summary": {
                        **_decision_summary(left_observed),
                        "score_summary": _score_summary(left_observed),
                    },
                    "current_summary": {
                        **_decision_summary(right_observed),
                        "score_summary": _score_summary(right_observed),
                    },
                    "cross_framework_difference_calculated": False,
                    "combined_score_calculated": False,
                    "scale_equivalence": "not_claimed",
                }
            )
    return diagnostics


# ---------------------------------------------------------------------------
# Public API


def _available_pass_facts(run: _RunFacts) -> list[dict[str, Any]]:
    return [
        {
            "scoring_id": item.scoring_id,
            "kind": item.kind,
            "sequence": item.sequence,
            "source": item.source,
            "complete": item.complete,
            "result_count": len(item.results),
        }
        for item in run.all_passes
    ]


def _selected_pass_fact(run: _RunFacts) -> dict[str, Any]:
    return {
        "run_id": run.manifest.run_id,
        "scoring_id": run.selected.scoring_id,
        "kind": run.selected.kind,
        "source": run.selected.source,
        "complete": run.selected.complete,
        "available_passes": _available_pass_facts(run),
    }


def _metric_entry(
    left: _MetricSpec,
    right: _MetricSpec,
    checks: Sequence[Mapping[str, Any]],
    *,
    mode: str,
) -> dict[str, Any]:
    compatible = all(check.get("compatible") is True for check in checks)
    return {
        "label": _identity_label(left.label, "unknown.metric"),
        "metric_id": _identity_label(left.metric_id, "unknown.metric"),
        "metric_version": _identity_label(left.metric_version, "unknown"),
        "binding_hash": left.binding_hash,
        "baseline_identity": left.fact(),
        "current_identity": right.fact(),
        "identity_checks": [dict(check) for check in checks],
        "compatible": compatible,
        "qualified": compatible and mode == "strict",
        "comparison": None,
        "coverage_gate": None,
        "diagnostic_comparison": None,
    }


def _add_warning(warnings: list[dict[str, Any]], code: str, scope: str, *, blocking: bool) -> None:
    item = {
        "code": _safe_label(code, "comparison_warning"),
        "scope": _safe_label(scope, "comparison"),
        "blocking": blocking,
    }
    if item not in warnings:
        warnings.append(item)


def compare_runs(
    storage: Storage,
    artifacts: ArtifactStore | None,
    baseline_run_id: str,
    current_run_id: str,
    *,
    baseline_scoring_id: str | None = None,
    current_scoring_id: str | None = None,
    mode: Literal["strict", "exploratory"] = "strict",
    min_paired_coverage: float = 0.95,
    bootstrap_seed: int = 0,
    bootstrap_replicates: int = 2_000,
) -> dict[str, Any]:
    """Compare two committed runs without invoking or changing anything.

    The return value is a sanitized dictionary suitable for JSON output.  A
    strict compatibility failure is represented as ``status="blocked"`` and
    still contains identity facts and non-qualified diagnostics; callers should
    use :func:`comparison_exit_code` rather than inferring a verdict from text.
    """

    if not isinstance(baseline_run_id, str) or not baseline_run_id:
        raise ComparisonError("baseline_run_id must be a non-empty string")
    if not isinstance(current_run_id, str) or not current_run_id:
        raise ComparisonError("current_run_id must be a non-empty string")
    if baseline_scoring_id is not None and (
        not isinstance(baseline_scoring_id, str) or not baseline_scoring_id
    ):
        raise ComparisonError("baseline_scoring_id must be a non-empty string or None")
    if current_scoring_id is not None and (
        not isinstance(current_scoring_id, str) or not current_scoring_id
    ):
        raise ComparisonError("current_scoring_id must be a non-empty string or None")
    if mode not in {"strict", "exploratory"}:
        raise ComparisonError("mode must be 'strict' or 'exploratory'")
    if isinstance(min_paired_coverage, bool) or not isinstance(min_paired_coverage, (int, float)):
        raise ComparisonError("min_paired_coverage must be numeric")
    if not math.isfinite(float(min_paired_coverage)) or not 0.0 <= float(min_paired_coverage) <= 1.0:
        raise ComparisonError("min_paired_coverage must be between 0 and 1")
    if isinstance(bootstrap_seed, bool) or not isinstance(bootstrap_seed, int):
        raise ComparisonError("bootstrap_seed must be an integer")
    if isinstance(bootstrap_replicates, bool) or not isinstance(bootstrap_replicates, int):
        raise ComparisonError("bootstrap_replicates must be an integer")
    if bootstrap_replicates < 1:
        raise ComparisonError("bootstrap_replicates must be at least 1")

    baseline = _run_facts(storage, artifacts, baseline_run_id, baseline_scoring_id)
    current = _run_facts(storage, artifacts, current_run_id, current_scoring_id)
    baseline_specs = _specs_for_pass(baseline, baseline.selected)
    current_specs = _specs_for_pass(current, current.selected)
    left_groups = _specs_by_label(baseline_specs)
    right_groups = _specs_by_label(current_specs)
    common_specs: list[tuple[_MetricSpec, _MetricSpec]] = []
    metric_entries: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    same_label_binding_block = False

    # A same-label collision is a strict identity problem even when one exact
    # binding happens to be present.  It prevents an arbitrary binding choice.
    for label in sorted(set(left_groups) & set(right_groups)):
        left_options = left_groups[label]
        right_options = right_groups[label]
        if len(left_options) > 1 or len(right_options) > 1:
            same_label_binding_block = True
            _add_warning(warnings, "same_metric_label_different_binding", label, blocking=True)
        candidates: list[tuple[_MetricSpec, _MetricSpec]] = []
        for left in left_options:
            for right in right_options:
                if left.binding_hash == right.binding_hash:
                    candidates.append((left, right))
        if not candidates:
            same_label_binding_block = True
            _add_warning(warnings, "same_metric_label_different_binding", label, blocking=True)
            metric_entries.append(
                {
                    "label": _identity_label(label, "unknown.metric"),
                    "metric_id": _identity_label(left_options[0].metric_id, "unknown.metric"),
                    "metric_version": _identity_label(left_options[0].metric_version, "unknown"),
                    "binding_hash": None,
                    "baseline_identity": left_options[0].fact(),
                    "current_identity": right_options[0].fact(),
                    "identity_checks": [
                        _check(
                            "metric_binding",
                            ok=False,
                            code="metric_binding_mismatch",
                            baseline={"binding_hash": left_options[0].binding_hash},
                            current={"binding_hash": right_options[0].binding_hash},
                        )
                    ],
                    "compatible": False,
                    "qualified": False,
                    "comparison": None,
                    "coverage_gate": None,
                    "diagnostic_comparison": None,
                }
            )
            continue
        # Duplicate exact hashes are malformed history; retain deterministic
        # diagnostics but never treat them as an unqualified match.
        seen: set[tuple[str, str]] = set()
        for left, right in candidates:
            pair_key = (left.binding_hash, right.binding_hash)
            if pair_key in seen:
                continue
            seen.add(pair_key)
            common_specs.append((left, right))
            checks = _spec_component_checks(left, right)
            entry = _metric_entry(left, right, checks, mode=mode)
            metric_entries.append(entry)
            for check in checks:
                if check.get("blocking") and check.get("compatible") is not True:
                    _add_warning(
                        warnings,
                        str(check.get("reason_code") or f"{check.get('name')}_incompatible"),
                        label,
                        blocking=True,
                    )

    # Unmatched labels are informational: a run may add a diagnostic metric.  A
    # same-label mismatch above is the case that blocks strict qualification.
    for label in sorted(set(left_groups) - set(right_groups)):
        _add_warning(warnings, "metric_missing_on_current", label, blocking=False)
    for label in sorted(set(right_groups) - set(left_groups)):
        _add_warning(warnings, "metric_missing_on_baseline", label, blocking=False)

    global_checks, global_warnings = _global_identity(baseline, current, common_specs)
    for warning in global_warnings:
        if warning not in warnings:
            warnings.append(warning)
    for run_facts in (baseline, current):
        for warning in run_facts.plan.get("warnings", []):
            if isinstance(warning, Mapping):
                _add_warning(
                    warnings,
                    str(warning.get("code", "frozen_plan_unavailable")),
                    "frozen_plan",
                    blocking=bool(warning.get("blocking", True)),
                )
    allowed_case_ids, case_pair_code = _matching_case_ids(baseline, current)
    if case_pair_code is not None:
        allowed_case_ids = set()

    # Compute diagnostics for exact identity matches even when another global
    # check blocks strict qualification.  They are never promoted to qualified
    # deltas unless the complete strict gate succeeds.
    for entry in metric_entries:
        if entry.get("binding_hash") is None:
            continue
        left = next(
            spec for spec in baseline_specs if spec.binding_hash == entry["binding_hash"]
        )
        right = next(
            spec for spec in current_specs if spec.binding_hash == entry["binding_hash"]
        )
        stats, gate, invalid = _numeric_comparison(
            baseline,
            current,
            left,
            right,
            threshold=float(min_paired_coverage),
            seed=bootstrap_seed,
            replicates=bootstrap_replicates,
            allowed_case_ids=allowed_case_ids,
        )
        entry["coverage_gate"] = gate
        if stats is not None:
            # A diagnostic can be useful even when an identity is unknown.  It
            # is deliberately kept out of qualified_metric_deltas unless the
            # entry itself is compatible and the overall strict gate passes.
            entry["diagnostic_comparison"] = stats
            if entry["compatible"]:
                entry["comparison"] = stats
        for invalid_row in invalid:
            _add_warning(warnings, "invalid_stored_metric_value", str(entry["label"]), blocking=False)

    all_checks = list(global_checks.values())
    for entry in metric_entries:
        all_checks.extend(entry["identity_checks"])
    baseline_lineage = sum(
        _lineage_mismatch_count(baseline, left) for left, _ in common_specs
    )
    current_lineage = sum(
        _lineage_mismatch_count(current, right) for _, right in common_specs
    )
    lineage_mismatches = baseline_lineage + current_lineage
    lineage_check = _check(
        "execution_lineage",
        ok=lineage_mismatches == 0,
        code=None if lineage_mismatches == 0 else "execution_identity_mismatch",
        baseline={"mismatch_count": baseline_lineage},
        current={"mismatch_count": current_lineage},
        blocking=True,
    )
    all_checks.append(lineage_check)
    if lineage_mismatches:
        _add_warning(warnings, "execution_identity_mismatch", "lineage", blocking=True)
    strict_blocked = (
        same_label_binding_block
        or lineage_mismatches > 0
        or any(
            check.get("blocking") and check.get("compatible") is not True for check in all_checks
        )
    )
    # An explicitly requested pass that is not present is a blocking selection
    # error, not an excuse to compare an arbitrary latest pass.
    if baseline.selected.source in {"requested_pass_not_found", "pass_selection_required"} or current.selected.source in {"requested_pass_not_found", "pass_selection_required"}:
        strict_blocked = True
        _add_warning(
            warnings,
            "selected_scoring_pass_not_found"
            if "requested_pass_not_found" in {baseline.selected.source, current.selected.source}
            else "multiple_scoring_passes_require_explicit_id",
            "pass_selection",
            blocking=True,
        )
    if not baseline.selected.complete or not current.selected.complete:
        strict_blocked = True
        _add_warning(warnings, "scoring_pass_incomplete", "pass_selection", blocking=True)
    if not metric_entries:
        strict_blocked = True
        _add_warning(warnings, "no_compatible_metric_binding", "metrics", blocking=True)
    if any(left.scope != "case" or right.scope != "case" for left, right in common_specs):
        strict_blocked = True
        _add_warning(warnings, "metric_scope_not_case", "metrics", blocking=True)

    if strict_blocked or mode == "exploratory":
        for entry in metric_entries:
            entry["qualified"] = False
            if mode == "exploratory" or strict_blocked:
                entry["comparison"] = None
    if mode == "exploratory":
        status = "exploratory"
        qualified = False
    elif strict_blocked:
        status = "blocked"
        qualified = False
    else:
        status = "qualified"
        qualified = True

    # The overall gate is calculated only for qualified/strict comparisons.  It
    # is still reported in blocked/exploratory output as a diagnostic.
    numeric_entries = [
        entry
        for entry in metric_entries
        if entry.get("comparison") is not None or entry.get("diagnostic_comparison") is not None
    ]
    gates = [entry["coverage_gate"] for entry in numeric_entries if entry.get("coverage_gate")]
    overall_passed = bool(gates) and all(gate.get("passed") is True for gate in gates)
    if not numeric_entries:
        overall_gate = {
            "status": "not_applicable",
            "passed": True,
            "minimum": float(min_paired_coverage),
            "reason_code": "no_numeric_compatible_metrics",
            "metric_count": 0,
        }
    else:
        overall_gate = {
            "status": "pass" if overall_passed else "fail",
            "passed": overall_passed,
            "minimum": float(min_paired_coverage),
            "reason_code": None
            if overall_passed
            else (
                "no_complete_numeric_pairs"
                if any(
                    gate.get("reason_code") == "no_complete_numeric_pairs"
                    for gate in gates
                )
                else "paired_coverage_below_minimum"
            ),
            "metric_count": len(gates),
            "failed_metric_count": sum(gate.get("passed") is not True for gate in gates),
        }
    if qualified and not overall_gate["passed"]:
        _add_warning(warnings, "overall_paired_coverage_gate_failed", "coverage", blocking=False)

    # Judge stability is over all committed passes, not just the selected pass.
    judge_stability: list[dict[str, Any]] = []
    for run in (baseline, current):
        run_specs_by_pass = {
            selected.scoring_id: _specs_for_pass(run, selected) for selected in run.all_passes
        }
        run_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
        group_specs: dict[tuple[str, str], _MetricSpec] = {}
        for scoring_id, specs in run_specs_by_pass.items():
            for spec in specs:
                key = (spec.binding_hash, spec.compatibility_hash or "unknown")
                run_groups[key].append(scoring_id)
                group_specs.setdefault(key, spec)
        for binding, compatibility_hash in sorted(run_groups):
            pass_ids = sorted(set(run_groups[(binding, compatibility_hash)]))
            observations, units = _judge_observations(run, binding, pass_ids)
            observations = _dedupe_judge_observations(observations)
            if not observations and not units:
                continue
            try:
                stability_report = summarize_judge_stability(
                    observations,
                    expected_units=sorted(units),
                    expected_scoring_ids=pass_ids,
                )
            except (TypeError, ValueError):
                continue
            spec = group_specs[(binding, compatibility_hash)]
            judge_stability.append(
                {
                    "run_id": run.manifest.run_id,
                    "binding_hash": _identity_label(binding, "unknown"),
                    "compatibility_hash": _identity_label(compatibility_hash, "unknown"),
                    "label": _identity_label(spec.label, "unknown.metric"),
                    "stability_kind": "judge" if spec.uses_models else "decision",
                    "scoring_ids": pass_ids,
                    "automatic_rescoring": False,
                    **stability_report,
                }
            )

    cross_framework = _cross_framework(
        baseline,
        current,
        baseline_specs,
        current_specs,
        allowed_case_ids=allowed_case_ids,
    )
    left_execution_ids = {
        result.execution_id for result in baseline.selected.results if result.execution_id
    }
    right_execution_ids = {
        result.execution_id for result in current.selected.results if result.execution_id
    }
    overlap = left_execution_ids & right_execution_ids
    same_stored = bool(left_execution_ids and right_execution_ids and left_execution_ids == right_execution_ids)
    execution_identity = {
        "basis": "evaluation_result_execution_ids",
        "baseline_execution_count": len(left_execution_ids),
        "current_execution_count": len(right_execution_ids),
        "shared_execution_count": len(overlap),
        "baseline_only_execution_count": len(left_execution_ids - right_execution_ids),
        "current_only_execution_count": len(right_execution_ids - left_execution_ids),
        "same_stored_executions": same_stored,
        "same_stored_execution_ids": same_stored,
        "execution_ids_equal": same_stored,
        "classification": "same_stored_executions" if same_stored else "fresh_or_unknown_execution_identity",
    }

    has_complete_numeric_pair = any(
        isinstance((entry.get("comparison") or {}).get("denominators"), Mapping)
        and (entry["comparison"]["denominators"].get("complete_numeric_pairs", 0) or 0) > 0
        for entry in metric_entries
    )
    claim_qualified = bool(qualified and overall_gate.get("passed") is True and has_complete_numeric_pair)
    qualified_deltas = [
        entry["comparison"]
        for entry in metric_entries
        if (
            qualified
            and entry.get("qualified")
            and entry.get("comparison") is not None
            and (
                entry["comparison"].get("denominators", {}).get("complete_numeric_pairs", 0) or 0
            )
            > 0
        )
    ]
    exploratory_diagnostics = [
        entry.get("diagnostic_comparison")
        for entry in metric_entries
        if entry.get("diagnostic_comparison") is not None
    ]
    identity_checks = {
        **global_checks,
        "lineage": lineage_check,
        "metric_bindings": [entry["identity_checks"] for entry in metric_entries],
    }
    report: dict[str, Any] = {
        "schema": COMPARISON_SCHEMA,
        "mode": mode,
        "status": status,
        "qualified": qualified,
        "claim_qualified": claim_qualified,
        "identity_qualified": qualified,
        "baseline_run_id": baseline_run_id,
        "current_run_id": current_run_id,
        "runs": {
            "baseline": {
                "run_id": baseline.manifest.run_id,
                "status": baseline.record.status,
                "dataset_hash": baseline.manifest.dataset_hash,
                "application_hash": baseline.manifest.application_hash,
                "plan_hash": baseline.manifest.plan_hash,
            },
            "current": {
                "run_id": current.manifest.run_id,
                "status": current.record.status,
                "dataset_hash": current.manifest.dataset_hash,
                "application_hash": current.manifest.application_hash,
                "plan_hash": current.manifest.plan_hash,
            },
        },
        "passes": {
            "baseline": _selected_pass_fact(baseline),
            "current": _selected_pass_fact(current),
        },
        "selected_passes": {
            "baseline_scoring_id": baseline.selected.scoring_id,
            "baseline_kind": baseline.selected.kind,
            "current_scoring_id": current.selected.scoring_id,
            "current_kind": current.selected.kind,
        },
        "execution_identity": execution_identity,
        "identity_checks": identity_checks,
        "compatibility_checks": {
            "global": [*global_checks.values(), lineage_check],
            "metrics": [entry["identity_checks"] for entry in metric_entries],
        },
        "application_identity": global_checks["application"],
        "global_checks": list(global_checks.values()),
        "warnings": sorted(warnings, key=lambda item: (item["scope"], item["code"])),
        "warning_codes": sorted({item["code"] for item in warnings}),
        "metrics": metric_entries,
        "qualified_metric_deltas": qualified_deltas,
        "qualified_deltas": qualified_deltas,
        "metric_deltas": qualified_deltas,
        "claim_qualified_metric_deltas": qualified_deltas if claim_qualified else [],
        "qualified_metric_diagnostics": [
            entry["comparison"]
            for entry in metric_entries
            if entry.get("comparison") is not None and not claim_qualified
        ],
        "exploratory_diagnostics": exploratory_diagnostics,
        "coverage_gate": overall_gate,
        "overall_coverage_gate": overall_gate,
        "overall_gate": overall_gate,
        "judge_stability": judge_stability,
        "cross_framework": cross_framework,
        "invocation_basis": {
            **ZERO_INVOCATION_BASIS,
            "invocations": {
                "application": 0,
                "evaluator": 0,
                "judge": 0,
                "workers": 0,
            },
        },
        "zero_invocation_basis": {
            **ZERO_INVOCATION_BASIS,
            "invocations": {
                "application": 0,
                "evaluator": 0,
                "judge": 0,
                "workers": 0,
            },
        },
        "basis": "committed storage reads only; no application/evaluator/judge invocation",
    }
    return _json_safe(report)


def _dedupe_judge_observations(observations: Sequence[JudgeObservation]) -> list[JudgeObservation]:
    chosen: dict[tuple[str, str, int], JudgeObservation] = {}
    for observation in observations:
        key = (observation.key.execution_id, observation.scoring_id, observation.key.repetition_id)
        chosen[key] = observation
    return [chosen[key] for key in sorted(chosen)]


def _json_safe(value: Any) -> Any:
    """Final guard for accidental enums, model objects, or non-finite floats."""

    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _json_safe(dump(mode="json"))
        except TypeError:
            return _json_safe(dump())
    return str(value)


def comparison_exit_code(report: Mapping[str, Any]) -> int:
    """Map a comparison document to the shared comparison exit codes.

    ``2`` is reserved for a blocked strict comparison, ``1`` for a qualified
    comparison whose declared paired-coverage gate fails, and ``0`` otherwise
    (including an explicitly exploratory diagnostic).
    """

    if report.get("status") == "blocked" and report.get("mode") == "strict":
        return 2
    if report.get("qualified") is True:
        gate = report.get("overall_coverage_gate")
        if not isinstance(gate, Mapping):
            gate = report.get("coverage_gate")
        if isinstance(gate, Mapping) and gate.get("passed") is False:
            return 1
    return 0


__all__ = [
    "COMPARISON_SCHEMA",
    "ZERO_INVOCATION_BASIS",
    "ComparisonError",
    "compare_runs",
    "comparison_exit_code",
]
