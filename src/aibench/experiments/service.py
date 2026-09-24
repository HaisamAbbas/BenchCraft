"""Controlled, development-only experiment search (Prompt 19).

The optimizer is a finite deterministic grid sweep over environment values explicitly
exposed by an application owner. Every trial uses the same frozen plan and the existing
run engine. Holdout cases are not parsed until selection is locked, and their first result
cannot reopen the trial grid.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import tempfile
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from aibench.config.resolve import load_mapping_file
from aibench.core.errors import AibenchError, ConflictError
from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    ApplicationSpec,
    ExperimentDefinition,
    ExperimentEvent,
    ExperimentEventKind,
    ExperimentRecord,
    ExperimentStatus,
    ExperimentTrial,
    ExperimentTrialStatus,
    MetricDirection,
    MetricScope,
    RedactionClass,
    deep_unfreeze,
    utcnow,
)
from aibench.core.plans import CaseSelection
from aibench.datasets.ingest import iter_jsonl_lines
from aibench.engine.cache import application_code_identity, code_identity_problem
from aibench.engine.compile import CompiledRun, PlanInvalid, PolicyDenied, compile_plan
from aibench.runners import LoadedApplication
from aibench.security.policy import ExecutionPolicy
from aibench.services.comparison import compare_runs
from aibench.services.runs import RunError, create_run, execute_run
from aibench.services.scoring import declared_dependency_identity
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import Storage


class ExperimentError(AibenchError):
    """An experiment contract, trial or protected-evaluation boundary is invalid."""


@dataclass(frozen=True)
class PreparedExperiment:
    definition: ExperimentDefinition
    compiled: CompiledRun
    raw_definition: bytes
    definition_hash: str
    holdout_digest: str
    spec_path: Path


def _dataset_content_digest(path: Path) -> str:
    """Match dataset identity while streaming lines without parsing case fields."""
    digest = hashlib.sha256()
    for _line_number, line in iter_jsonl_lines(path):
        digest.update(line.encode("utf-8"))
    return "sha256:" + digest.hexdigest()


def _metric_contract_hash(compiled: CompiledRun) -> str:
    """Identity of the fixed bindings and evaluator implementations, independent of cases."""
    return content_hash(
        {
            "bindings": [
                {
                    "binding_hash": metric.binding_hash,
                    "manifest": metric.manifest.model_dump(mode="json"),
                }
                for metric in compiled.metrics
            ],
            "dependencies": declared_dependency_identity(compiled.metrics),
        }
    )


def _application_code_hash(compiled: CompiledRun) -> str:
    return content_hash(
        application_code_identity(
            compiled.application.spec,
            compiled.application.base_dir,
            dict(os.environ),
        )
    )


def _checked_metric(compiled: CompiledRun, index: int, *, subject: str) -> Any:
    if index >= len(compiled.metrics):
        raise ExperimentError(
            f"{subject} refers to metric binding {index}, but the frozen plan has "
            f"{len(compiled.metrics)} binding(s)"
        )
    metric = compiled.metrics[index]
    manifest = metric.manifest
    if manifest.scope is not MetricScope.CASE:
        raise ExperimentError(f"{subject} must use a case-scoped metric")
    if manifest.value_kind not in {"scalar", "boolean"}:
        raise ExperimentError(f"{subject} must use a scalar or boolean-rate metric")
    if manifest.aggregation not in {"mean", "rate"}:
        raise ExperimentError(f"{subject} metric does not have a numeric mean or rate")
    return metric


def _resolve_definition(definition: ExperimentDefinition, spec_path: Path) -> ExperimentDefinition:
    base = spec_path.resolve().parent

    def absolute(value: str) -> str:
        return str((base / value).resolve())

    return ExperimentDefinition.model_validate(
        {
            **definition.model_dump(mode="json"),
            "plan": absolute(definition.plan),
            "development_dataset": absolute(definition.development_dataset),
            "holdout_dataset": absolute(definition.holdout_dataset),
        }
    )


def _parameter_grid(
    definition: ExperimentDefinition, spec: ApplicationSpec
) -> list[dict[str, str]]:
    exposures = {item.name: item for item in spec.exposed_parameters}
    names = [item.name for item in definition.parameters]
    unknown = sorted(set(names) - set(exposures))
    if unknown:
        raise ExperimentError(
            "parameter space contains names not exposed by the application: " + ", ".join(unknown)
        )
    domains: list[tuple[str, ...]] = []
    for parameter in definition.parameters:
        exposed = exposures[parameter.name]
        values = set(parameter.values)
        unsupported = sorted(values - set(exposed.allowed_values))
        if unsupported:
            raise ExperimentError(
                f"parameter {parameter.name!r} contains values outside its exposed domain"
            )
        if exposed.default_value not in values:
            raise ExperimentError(
                f"parameter {parameter.name!r} must include its declared default for a baseline"
            )
        domains.append(parameter.values)

    grid = [dict(zip(names, values, strict=True)) for values in itertools.product(*domains)]
    defaults = {parameter.name: exposures[parameter.name].default_value for parameter in definition.parameters}
    grid.sort(key=lambda values: (values != defaults, tuple(values.items())))
    if definition.budget.max_trials > len(grid):
        raise ExperimentError(
            f"budget.max_trials ({definition.budget.max_trials}) exceeds the "
            f"{len(grid)} parameter combinations"
        )
    return grid


def prepare_experiment(
    spec_path: Path,
    *,
    policy: ExecutionPolicy,
    trusted_local: bool = False,
) -> PreparedExperiment:
    """Validate the development plan and finite parameter space without opening a workspace.

    The protected dataset is streamed only to compute its byte digest. It is not JSON-parsed,
    converted into cases or passed to any objective, trial or comparison code here.
    """
    resolved_spec = spec_path.resolve()
    if not resolved_spec.is_file():
        raise ExperimentError(f"experiment definition not found: {resolved_spec}")
    raw_definition = resolved_spec.read_bytes()
    try:
        definition = ExperimentDefinition.model_validate(load_mapping_file(resolved_spec))
    except (ValueError, AibenchError) as exc:
        raise ExperimentError(f"invalid experiment definition: {exc}") from exc
    definition = _resolve_definition(definition, resolved_spec)
    plan_path = Path(definition.plan)
    dev_path = Path(definition.development_dataset)
    holdout_path = Path(definition.holdout_dataset)
    if not dev_path.is_file():
        raise ExperimentError(f"development dataset not found: {dev_path}")
    if not holdout_path.is_file():
        raise ExperimentError(f"protected holdout dataset not found: {holdout_path}")
    if policy.data_roots and not any(
        holdout_path.resolve().is_relative_to(Path(root).resolve()) for root in policy.data_roots
    ):
        raise ExperimentError("protected holdout dataset is outside the policy's data_roots")
    try:
        compiled = compile_plan(plan_path, policy=policy, trusted_local=trusted_local)
    except (PlanInvalid, PolicyDenied) as exc:
        raise ExperimentError(f"development plan is not executable: {exc}") from exc
    identity_problem = code_identity_problem(
        compiled.application.spec, compiled.application.base_dir
    )
    if identity_problem:
        raise ExperimentError(
            "application code identity cannot be frozen for controlled trials: "
            f"{identity_problem}"
        )
    actual_dataset = (compiled.plan_dir / compiled.plan.dataset).resolve()
    if actual_dataset != dev_path.resolve():
        raise ExperimentError(
            "the plan's dataset must be the explicitly declared development_dataset"
        )
    if compiled.plan.cache.executions or compiled.plan.cache.evaluations:
        raise ExperimentError("controlled trials require fresh executions and evaluations; disable cache")
    if not compiled.application.spec.exposed_parameters:
        raise ExperimentError("the application exposes no experiment parameters")
    if _dataset_content_digest(dev_path) != compiled.dataset.content_hash:
        raise ExperimentError("development dataset changed while it was being validated")
    holdout_digest = _dataset_content_digest(holdout_path)
    if holdout_digest == compiled.dataset.content_hash:
        raise ExperimentError("development and holdout datasets must have different content")

    _parameter_grid(definition, compiled.application.spec)
    objective = _checked_metric(compiled, definition.objective.binding_index, subject="objective")
    if objective.manifest.direction not in {MetricDirection.HIGHER, MetricDirection.LOWER}:
        raise ExperimentError("objective metric must declare higher-is-better or lower-is-better")
    for constraint in definition.constraints:
        _checked_metric(compiled, constraint.binding_index, subject="constraint")

    resolved_definition = definition.model_copy(
        update={
            "plan": str(plan_path.resolve()),
            "development_dataset": str(dev_path.resolve()),
            "holdout_dataset": str(holdout_path.resolve()),
        }
    )
    return PreparedExperiment(
        definition=resolved_definition,
        compiled=compiled,
        raw_definition=raw_definition,
        definition_hash=content_hash(resolved_definition.model_dump(mode="json")),
        holdout_digest=holdout_digest,
        spec_path=resolved_spec,
    )


def _event(
    experiment_id: str,
    kind: ExperimentEventKind,
    actor: str,
    details: dict[str, Any] | None = None,
) -> ExperimentEvent:
    return ExperimentEvent(
        event_id=f"{experiment_id}:{uuid.uuid4().hex}",
        experiment_id=experiment_id,
        kind=kind,
        actor=actor,
        details=details or {},
    )


def _commit_snapshot(
    storage: Storage,
    artifacts: ArtifactStore,
    content: bytes,
    mime_type: str,
    *,
    artifact_id: str | None = None,
) -> str:
    if artifact_id is not None:
        existing = storage.get_artifact(artifact_id)
        if (
            existing is None
            or existing.mime_type != mime_type
            or existing.redaction is not RedactionClass.NONE
            or artifacts.read_bytes(existing) != content
        ):
            raise ExperimentError("stored experiment snapshot failed content verification")
        return existing.artifact_id
    existing = storage.get_artifact_by_digest(bytes_hash(content), mime_type=mime_type)
    if existing is not None and existing.redaction is RedactionClass.NONE:
        if artifacts.read_bytes(existing) != content:
            raise ExperimentError("stored experiment snapshot failed content verification")
        return existing.artifact_id
    ref = artifacts.write_bytes(content, mime_type=mime_type, redaction=RedactionClass.NONE)
    commit_verified_artifact(artifacts, storage, ref)
    return ref.artifact_id


def create_experiment(
    prepared: PreparedExperiment,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    actor: str = "cli",
) -> ExperimentRecord:
    """Persist the contract, deterministic trial grid and protected holdout digest atomically."""
    if storage.is_protected_dataset_digest(prepared.compiled.dataset.content_hash):
        raise ExperimentError("a dataset already protected as holdout cannot be used for trials")
    if storage.is_protected_dataset_digest(prepared.holdout_digest):
        raise ExperimentError("this holdout dataset is already protected by another experiment")
    definition = prepared.definition
    if storage.get_experiment(definition.experiment_id) is not None:
        raise ExperimentError(
            f"experiment {definition.experiment_id!r} already exists; use experiments resume"
        )
    grid = _parameter_grid(definition, prepared.compiled.application.spec)
    definition_artifact = _commit_snapshot(
        storage, artifacts, prepared.raw_definition, "application/yaml"
    )
    plan_artifact = _commit_snapshot(
        storage, artifacts, prepared.compiled.plan_bytes, "application/json"
    )
    spec = prepared.compiled.application.spec
    objective = _checked_metric(
        prepared.compiled, definition.objective.binding_index, subject="objective"
    )
    code_hash = _application_code_hash(prepared.compiled)
    record = ExperimentRecord(
        experiment_id=definition.experiment_id,
        definition=definition,
        definition_hash=prepared.definition_hash,
        definition_artifact_id=definition_artifact,
        plan_hash=prepared.compiled.plan_hash,
        plan_artifact_id=plan_artifact,
        application_hash=content_hash(spec.model_dump(mode="json")),
        application_code_hash=code_hash,
        evaluator_contract_hash=_metric_contract_hash(prepared.compiled),
        objective_metric_id=objective.manifest.evaluator_id,
        objective_direction=objective.manifest.direction.value,
        objective_binding_hash=objective.binding_hash,
        policy_hash=prepared.compiled.policy_hash,
        effective_policy=prepared.compiled.policy.model_dump(mode="json"),
        trusted_local=prepared.compiled.policy.allow_trusted_local,
        development_dataset_hash=prepared.compiled.dataset.content_hash,
        holdout_dataset_hash=prepared.holdout_digest,
        spec_path=str(prepared.spec_path),
        trial_limit=definition.budget.max_trials,
    )
    trials: list[ExperimentTrial] = []
    for ordinal, parameters in enumerate(grid):
        parameter_hash = content_hash(parameters)
        trial_id = f"{definition.experiment_id}:t{ordinal:03d}:{parameter_hash[7:15]}"
        run_digest = hashlib.sha256(f"{definition.experiment_id}\0{trial_id}".encode()).hexdigest()
        trials.append(
            ExperimentTrial(
                experiment_id=definition.experiment_id,
                trial_id=trial_id,
                ordinal=ordinal,
                run_id=f"run-exp-{run_digest[:16]}",
                parameters=parameters,
                parameter_hash=parameter_hash,
            )
        )
    created = _event(
        definition.experiment_id,
        ExperimentEventKind.CREATED,
        actor,
        {
            "definition_hash": record.definition_hash,
            "plan_hash": record.plan_hash,
            "development_dataset_hash": record.development_dataset_hash,
            "holdout_dataset_hash": record.holdout_dataset_hash,
            "trial_count": len(trials),
            "initial_trial_budget": record.trial_limit,
        },
    )
    try:
        storage.commit_experiment(record, trials, created)
    except ConflictError as exc:
        raise ExperimentError(str(exc)) from exc
    return record


def _compiled_from_record(record: ExperimentRecord) -> CompiledRun:
    policy = ExecutionPolicy.model_validate(deep_unfreeze(record.effective_policy))
    try:
        compiled = compile_plan(
            Path(record.definition.plan),
            policy=policy,
            trusted_local=record.trusted_local,
        )
    except (PlanInvalid, PolicyDenied) as exc:
        raise ExperimentError(f"the frozen development plan is no longer executable: {exc}") from exc
    actual_dataset = (compiled.plan_dir / compiled.plan.dataset).resolve()
    if actual_dataset != Path(record.definition.development_dataset).resolve():
        raise ExperimentError("the plan no longer refers to the experiment's development dataset")
    if compiled.plan_hash != record.plan_hash:
        raise ExperimentError("the frozen evaluation plan changed since experiment creation")
    if compiled.dataset.content_hash != record.development_dataset_hash:
        raise ExperimentError("the development dataset changed since experiment creation")
    if content_hash(compiled.application.spec.model_dump(mode="json")) != record.application_hash:
        raise ExperimentError("the base application config changed since experiment creation")
    if _application_code_hash(compiled) != record.application_code_hash:
        raise ExperimentError("application code or inherited environment changed since experiment creation")
    if _metric_contract_hash(compiled) != record.evaluator_contract_hash:
        raise ExperimentError("evaluator identity changed since experiment creation")
    if compiled.policy_hash != record.policy_hash:
        raise ExperimentError("execution policy changed since experiment creation")
    if compiled.plan.cache.executions or compiled.plan.cache.evaluations:
        raise ExperimentError("cache was enabled after experiment creation")
    return compiled


def _variant(compiled: CompiledRun, parameters: Any) -> CompiledRun:
    source = compiled.application.spec
    raw = source.model_dump(mode="json")
    transport = raw.get("transport")
    if not isinstance(transport, dict) or not isinstance(transport.get("env"), dict):
        raise ExperimentError("application transport no longer exposes its environment mapping")
    exposures = {item.name: item for item in source.exposed_parameters}
    parameter_values = dict(deep_unfreeze(parameters))
    for name, value in parameter_values.items():
        exposure = exposures.get(name)
        if exposure is None or value not in exposure.allowed_values:
            raise ExperimentError(f"trial uses a non-exposed application parameter {name!r}")
        transport["env"][exposure.environment_key] = value
    for exposed in raw.get("exposed_parameters", []):
        if exposed["name"] in parameter_values:
            exposed["default_value"] = parameter_values[exposed["name"]]
    spec = ApplicationSpec.model_validate(raw)
    return replace(
        compiled,
        application=LoadedApplication(spec=spec, base_dir=compiled.application.base_dir),
    )


def _run_context(
    record: ExperimentRecord,
    trial: ExperimentTrial,
    *,
    stage: Literal["development_trial", "final_holdout_evaluation"],
) -> dict[str, Any]:
    return {
        "experiment_id": record.experiment_id,
        "trial_id": trial.trial_id,
        "stage": stage,
        "parameter_hash": trial.parameter_hash,
        "parameters": deep_unfreeze(trial.parameters),
        "run_seed": record.definition.budget.seed,
        "development_plan_hash": record.plan_hash,
        "development_dataset_hash": record.development_dataset_hash,
        "selection_locked": stage == "final_holdout_evaluation",
        **(
            {"protected_holdout_dataset_hash": record.holdout_dataset_hash}
            if stage == "final_holdout_evaluation"
            else {}
        ),
    }


def _holdout_run_id(experiment_id: str, role: Literal["baseline", "selected"]) -> str:
    digest = hashlib.sha256(f"{experiment_id}\0{role}".encode()).hexdigest()[:16]
    return f"run-exp-holdout-{role}-{digest}"


def _compile_holdout_plan(
    record: ExperimentRecord, base: CompiledRun, storage: Storage
) -> CompiledRun:
    """Compile the protected split with the exact evaluator plan and all holdout cases.

    This is called only after development selection has moved the experiment into
    HOLDOUT_RUNNING. Relative plan references are resolved against the original plan
    directory, then the temporary plan is compiled in the system temp directory and
    removed immediately. Run execution consumes its frozen artifact thereafter.
    """
    holdout_path = Path(record.definition.holdout_dataset).resolve()
    if _dataset_content_digest(holdout_path) != record.holdout_dataset_hash:
        raise ExperimentError("protected holdout dataset changed since experiment creation")
    if not storage.is_protected_dataset_digest(record.holdout_dataset_hash):
        raise ExperimentError("protected holdout registration is missing or inconsistent")

    plan_dir = base.plan_dir.resolve()

    def absolute(reference: str) -> str:
        path = Path(reference)
        return str(path.resolve() if path.is_absolute() else (plan_dir / path).resolve())

    plugins = []
    for item in base.plan.plugin_environments:
        plugins.append(
            item.model_copy(
                update={
                    "python": absolute(item.python),
                    "paths": tuple(absolute(path) for path in item.paths),
                }
            )
        )
    holdout_plan = base.plan.model_copy(
        update={
            "plan_id": f"{base.plan.plan_id[:155]}-holdout-{record.experiment_id[:24]}",
            "application": absolute(base.plan.application),
            "dataset": str(holdout_path),
            "selection": CaseSelection(),
            "plugin_environments": tuple(plugins),
        }
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".aibench-holdout-", suffix=".json", delete=False,
        ) as handle:
            handle.write(holdout_plan.model_dump_json().encode("utf-8"))
            temporary_path = Path(handle.name)
        compiled = compile_plan(
            temporary_path,
            policy=ExecutionPolicy.model_validate(deep_unfreeze(record.effective_policy)),
            trusted_local=record.trusted_local,
        )
    except (PlanInvalid, PolicyDenied) as exc:
        raise ExperimentError(f"protected holdout plan is not executable: {exc}") from exc
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    if compiled.dataset.content_hash != record.holdout_dataset_hash:
        raise ExperimentError("holdout bytes changed while the protected plan was compiled")
    if _metric_contract_hash(compiled) != record.evaluator_contract_hash:
        raise ExperimentError("evaluator contract changed before final holdout evaluation")
    if content_hash(compiled.application.spec.model_dump(mode="json")) != record.application_hash:
        raise ExperimentError("application configuration changed before final holdout evaluation")
    if _application_code_hash(compiled) != record.application_code_hash:
        raise ExperimentError("application code changed before final holdout evaluation")
    if compiled.policy_hash != record.policy_hash:
        raise ExperimentError("execution policy changed before final holdout evaluation")
    if compiled.plan.cache.executions or compiled.plan.cache.evaluations:
        raise ExperimentError("cache must remain disabled for final holdout evaluation")
    return compiled


def _mean_value(result: Any) -> float | None:
    if result.value is None:
        return None
    value = deep_unfreeze(result.value.value)
    if result.value.kind == "boolean" and isinstance(value, bool):
        return float(value)
    if result.value.kind == "scalar" and isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number):
            return number
    return None


def _metric_summaries(
    storage: Storage,
    run_id: str,
    binding_indexes: set[int],
    selected_items: int,
) -> dict[str, dict[str, Any]]:
    run = storage.get_run(run_id)
    if run is None:
        raise ExperimentError(f"trial run {run_id!r} disappeared from storage")
    params = deep_unfreeze(run.manifest.parameters) or {}
    hashes = list(params.get("binding_hashes") or [])
    scoring_id = params.get("scoring_id")
    results = storage.list_metric_results(run_id, scoring_id=scoring_id)
    output: dict[str, dict[str, Any]] = {}
    for index in sorted(binding_indexes):
        if index >= len(hashes):
            raise ExperimentError("run manifest is missing a frozen metric binding")
        binding_hash = hashes[index]
        matched = [result for result in results if result.binding_hash == binding_hash]
        completed = [result for result in matched if result.status.value == "ok"]
        values = [value for result in completed if (value := _mean_value(result)) is not None]
        profile = (params.get("metric_profiles") or {}).get(binding_hash, {})
        manifest = profile.get("manifest", {}) if isinstance(profile, dict) else {}
        output[str(index)] = {
            "binding_hash": binding_hash,
            "metric_id": manifest.get("evaluator_id"),
            "direction": manifest.get("direction"),
            "value_kind": manifest.get("value_kind"),
            "aggregation": manifest.get("aggregation"),
            "selected": selected_items,
            "completed": len(completed),
            "coverage": (len(completed) / selected_items) if selected_items else None,
            "mean": (sum(values) / len(values)) if values else None,
            "valid_value_count": len(values),
        }
    return output


def _constraint_results(
    definition: ExperimentDefinition,
    metrics: dict[str, dict[str, Any]],
) -> tuple[bool, list[dict[str, Any]]]:
    outcomes: list[dict[str, Any]] = []
    for constraint in definition.constraints:
        summary = metrics.get(str(constraint.binding_index), {})
        value = summary.get("mean")
        coverage = summary.get("coverage")
        numeric_value = (
            float(value)
            if isinstance(value, (int, float)) and not isinstance(value, bool)
            else None
        )
        passed = (
            numeric_value is not None
            and isinstance(coverage, (int, float))
            and not isinstance(coverage, bool)
            and coverage >= definition.budget.min_metric_coverage
        )
        if passed:
            assert numeric_value is not None
            if constraint.comparator == ">=":
                passed = numeric_value >= constraint.threshold
            elif constraint.comparator == ">":
                passed = numeric_value > constraint.threshold
            elif constraint.comparator == "<=":
                passed = numeric_value <= constraint.threshold
            else:
                passed = numeric_value < constraint.threshold
        outcomes.append(
            {
                "binding_index": constraint.binding_index,
                "comparator": constraint.comparator,
                "threshold": constraint.threshold,
                "observed": value,
                "coverage": coverage,
                "passed": bool(passed),
            }
        )
    return all(result["passed"] for result in outcomes), outcomes


def _compare_to_baseline(
    storage: Storage,
    artifacts: ArtifactStore,
    record: ExperimentRecord,
    baseline: ExperimentTrial,
    trial: ExperimentTrial,
) -> dict[str, Any]:
    report = compare_runs(
        storage,
        artifacts,
        baseline.run_id,
        trial.run_id,
        mode="strict",
        min_paired_coverage=record.definition.budget.min_paired_coverage,
        bootstrap_seed=record.definition.budget.seed + trial.ordinal,
        bootstrap_replicates=record.definition.budget.bootstrap_replicates,
    )
    metric = next(
        (
            entry
            for entry in report.get("metrics", [])
            if entry.get("binding_hash") == record.objective_binding_hash
        ),
        None,
    )
    comparison = metric.get("comparison") if isinstance(metric, dict) else None
    if comparison is None and isinstance(metric, dict):
        comparison = metric.get("diagnostic_comparison")
    qualified = bool(report.get("qualified") and isinstance(metric, dict) and metric.get("qualified"))
    claim_qualified = bool(
        report.get("claim_qualified")
        and isinstance(metric, dict)
        and metric.get("qualified")
        and isinstance(metric.get("coverage_gate"), dict)
        and metric["coverage_gate"].get("passed")
    )
    return {
        "baseline_trial_id": baseline.trial_id,
        "qualified": qualified,
        "claim_qualified": claim_qualified,
        "warning_codes": report.get("warning_codes", []),
        "binding_hash": record.objective_binding_hash,
        "comparison": comparison,
    }


def _trial_metrics(
    storage: Storage,
    artifacts: ArtifactStore,
    record: ExperimentRecord,
    compiled: CompiledRun,
    trial: ExperimentTrial,
) -> tuple[ExperimentTrial, list[dict[str, Any]]]:
    indexes = {record.definition.objective.binding_index} | {
        item.binding_index for item in record.definition.constraints
    }
    selected = len(compiled.cases) * compiled.plan.repetitions
    metrics = _metric_summaries(storage, trial.run_id, indexes, selected)
    objective = metrics[str(record.definition.objective.binding_index)]
    value = objective.get("mean")
    coverage = objective.get("coverage")
    objective_value = (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool)
        else None
    )
    constraints_passed, constraints = _constraint_results(record.definition, metrics)
    feasible = (
        objective_value is not None
        and isinstance(coverage, (int, float))
        and not isinstance(coverage, bool)
        and coverage >= record.definition.budget.min_metric_coverage
        and constraints_passed
    )
    updated = trial.model_copy(
        update={
            "status": ExperimentTrialStatus.COMPLETED,
            "objective_value": objective_value if feasible else None,
            "metrics": metrics,
            "constraints_passed": constraints_passed and feasible,
            "updated_at": utcnow(),
        }
    )
    trials = storage.list_experiment_trials(record.experiment_id)
    baseline = next((item for item in trials if item.ordinal == 0), None)
    if baseline is not None and trial.ordinal > 0 and baseline.status is ExperimentTrialStatus.COMPLETED:
        updated = updated.model_copy(
            update={
                "comparison_to_baseline": _compare_to_baseline(
                    storage, artifacts, record, baseline, trial
                )
            }
        )
    return updated, constraints


def _trial_event(
    trial: ExperimentTrial, kind: ExperimentEventKind, details: dict[str, Any]
) -> ExperimentEvent:
    return _event(trial.experiment_id, kind, "aibench-experiment", details)


def _transition_trial(
    storage: Storage,
    trial: ExperimentTrial,
    *,
    from_status: ExperimentTrialStatus,
    to_status: ExperimentTrialStatus,
    kind: ExperimentEventKind,
    details: dict[str, Any],
    **updates: Any,
) -> ExperimentTrial:
    changed = trial.model_copy(
        update={"status": to_status, "updated_at": utcnow(), **updates}
    )
    storage.transition_experiment_trial(
        changed,
        from_status=from_status,
        event=_trial_event(changed, kind, details),
    )
    return changed


def _transition_experiment(
    storage: Storage,
    record: ExperimentRecord,
    *,
    to_status: ExperimentStatus,
    kind: ExperimentEventKind,
    details: dict[str, Any],
    actor: str = "aibench-experiment",
    **updates: Any,
) -> ExperimentRecord:
    changed = record.model_copy(
        update={"status": to_status, "updated_at": utcnow(), **updates}
    )
    storage.transition_experiment(
        changed,
        from_status=record.status,
        event=_event(record.experiment_id, kind, actor, details),
    )
    return changed


async def execute_experiment(
    experiment_id: str,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    environ: dict[str, str] | None = None,
) -> ExperimentRecord:
    """Run or resume the development grid under its immutable contract and trial budget."""
    record = storage.get_experiment(experiment_id)
    if record is None:
        raise ExperimentError(f"experiment {experiment_id!r} does not exist")
    if record.status in {
        ExperimentStatus.SELECTED,
        ExperimentStatus.NO_FEASIBLE_TRIAL,
        ExperimentStatus.COMPLETED,
        ExperimentStatus.FAILED,
    }:
        return record
    if record.status is ExperimentStatus.HOLDOUT_RUNNING:
        raise ExperimentError("final holdout evaluation is in progress; use evaluate-holdout to resume")
    compiled = _compiled_from_record(record)
    if record.status is ExperimentStatus.READY:
        record = _transition_experiment(
            storage,
            record,
            to_status=ExperimentStatus.RUNNING,
            kind=ExperimentEventKind.STARTED,
            details={"trial_limit": record.trial_limit},
        )

    trials = storage.list_experiment_trials(experiment_id)
    for trial in trials:
        if trial.ordinal >= record.trial_limit or trial.status is ExperimentTrialStatus.COMPLETED:
            continue
        if trial.status is ExperimentTrialStatus.FAILED:
            return _transition_experiment(
                storage,
                record,
                to_status=ExperimentStatus.FAILED,
                kind=ExperimentEventKind.TRIAL_FAILED,
                details={"trial_id": trial.trial_id, "reason": trial.failure or "trial failed"},
            )
        if trial.status is ExperimentTrialStatus.PENDING:
            trial = _transition_trial(
                storage,
                trial,
                from_status=ExperimentTrialStatus.PENDING,
                to_status=ExperimentTrialStatus.RUNNING,
                kind=ExperimentEventKind.TRIAL_STARTED,
                details={
                    "trial_id": trial.trial_id,
                    "run_id": trial.run_id,
                    "parameter_hash": trial.parameter_hash,
                    "ordinal": trial.ordinal,
                },
            )
        variant = _variant(compiled, trial.parameters)
        context = _run_context(record, trial, stage="development_trial")
        try:
            create_run(
                variant,
                storage=storage,
                artifacts=artifacts,
                granted_by=f"experiment:{experiment_id}",
                run_id=trial.run_id,
                run_seed=record.definition.budget.seed,
                experiment_context=context,
            )
            current_run = storage.get_run(trial.run_id)
            if current_run is None:
                raise RunError(f"trial run {trial.run_id!r} was not committed")
            if current_run.status in {
                "created",
                "running",
                "pausing",
                "paused",
                "cancelling",
                "interrupting",
                "interrupted",
            }:
                await execute_run(
                    trial.run_id,
                    storage=storage,
                    artifacts=artifacts,
                    environ=environ,
                )
            current_run = storage.get_run(trial.run_id)
            if current_run is None:
                raise RunError(f"trial run {trial.run_id!r} disappeared")
            if current_run.status in {
                "created",
                "running",
                "pausing",
                "paused",
                "cancelling",
                "interrupting",
                "interrupted",
            }:
                # A user pause, stop or process interruption leaves the trial resumable.
                return storage.get_experiment(experiment_id) or record
            if current_run.status != "completed":
                raise ExperimentError(
                    f"trial run ended {current_run.status}; no candidate will be selected"
                )
            completed, _constraints = _trial_metrics(
                storage, artifacts, record, compiled, trial
            )
            _transition_trial(
                storage,
                completed,
                from_status=ExperimentTrialStatus.RUNNING,
                to_status=ExperimentTrialStatus.COMPLETED,
                kind=ExperimentEventKind.TRIAL_COMPLETED,
                details={
                    "trial_id": trial.trial_id,
                    "run_id": trial.run_id,
                    "parameter_hash": trial.parameter_hash,
                    "objective_value": completed.objective_value,
                    "constraints_passed": completed.constraints_passed,
                },
                objective_value=completed.objective_value,
                metrics=completed.metrics,
                constraints_passed=completed.constraints_passed,
                comparison_to_baseline=completed.comparison_to_baseline,
            )
        except (AibenchError, OSError, ValueError) as exc:
            current = storage.get_experiment_trial(trial.trial_id)
            if current is not None and current.status is ExperimentTrialStatus.RUNNING:
                _transition_trial(
                    storage,
                    current,
                    from_status=ExperimentTrialStatus.RUNNING,
                    to_status=ExperimentTrialStatus.FAILED,
                    kind=ExperimentEventKind.TRIAL_FAILED,
                    details={"trial_id": current.trial_id, "reason": str(exc)[:500]},
                    failure=str(exc)[:500],
                )
            latest = storage.get_experiment(experiment_id)
            if latest is not None and latest.status is ExperimentStatus.RUNNING:
                return _transition_experiment(
                    storage,
                    latest,
                    to_status=ExperimentStatus.FAILED,
                    kind=ExperimentEventKind.TRIAL_FAILED,
                    details={"trial_id": trial.trial_id, "reason": str(exc)[:500]},
                )
            raise

    record = storage.get_experiment(experiment_id) or record
    trials = storage.list_experiment_trials(experiment_id)
    pending = [trial for trial in trials if trial.status is ExperimentTrialStatus.PENDING]
    if pending:
        return _transition_experiment(
            storage,
            record,
            to_status=ExperimentStatus.BUDGET_EXHAUSTED,
            kind=ExperimentEventKind.BUDGET_EXHAUSTED,
            details={
                "trial_limit": record.trial_limit,
                "completed_trials": sum(
                    trial.status is ExperimentTrialStatus.COMPLETED for trial in trials
                ),
                "remaining_trials": len(pending),
            },
        )
    if any(trial.status is ExperimentTrialStatus.FAILED for trial in trials):
        return _transition_experiment(
            storage,
            record,
            to_status=ExperimentStatus.FAILED,
            kind=ExperimentEventKind.TRIAL_FAILED,
            details={"reason": "one or more planned trials failed"},
        )
    return _select_development_trial(storage, artifacts, record, trials)


def _select_development_trial(
    storage: Storage,
    artifacts: ArtifactStore,
    record: ExperimentRecord,
    trials: list[ExperimentTrial],
) -> ExperimentRecord:
    baseline = next((trial for trial in trials if trial.ordinal == 0), None)

    def demonstrated_improvement(trial: ExperimentTrial) -> bool:
        comparison = deep_unfreeze(trial.comparison_to_baseline)
        if not isinstance(comparison, dict) or not comparison.get("claim_qualified"):
            return False
        stats = comparison.get("comparison")
        if not isinstance(stats, dict):
            return False
        difference = stats.get("case_macro", {}).get("mean_current_minus_baseline")
        interval = stats.get("uncertainty", {})
        lower, upper = interval.get("lower"), interval.get("upper")
        if not all(isinstance(value, (int, float)) and math.isfinite(float(value)) for value in (lower, upper)):
            return False
        if not isinstance(difference, (int, float)) or not math.isfinite(float(difference)):
            return False
        if record.objective_direction == MetricDirection.HIGHER.value:
            return lower > 0
        return upper < 0

    feasible = [
        trial
        for trial in trials
        if trial.status is ExperimentTrialStatus.COMPLETED
        and trial.constraints_passed is True
        and trial.objective_value is not None
        and (trial.ordinal == 0 or demonstrated_improvement(trial))
    ]
    if not feasible:
        return _transition_experiment(
            storage,
            record,
            to_status=ExperimentStatus.NO_FEASIBLE_TRIAL,
            kind=ExperimentEventKind.NO_FEASIBLE_TRIAL,
            details={"trial_count": len(trials), "objective": record.objective_metric_id},
        )
    reverse = record.objective_direction == MetricDirection.HIGHER.value
    selected = min(
        feasible,
        key=lambda trial: (-float(trial.objective_value or 0.0), trial.ordinal)
        if reverse
        else (float(trial.objective_value or 0.0), trial.ordinal),
    )
    return _transition_experiment(
        storage,
        record,
        to_status=ExperimentStatus.SELECTED,
        kind=ExperimentEventKind.SELECTED,
        details={
            "selected_trial_id": selected.trial_id,
            "baseline_trial_id": baseline.trial_id if baseline else None,
            "objective_metric_id": record.objective_metric_id,
            "objective_direction": record.objective_direction,
            "objective_value": selected.objective_value,
            "selection_split": "development",
            "selection_basis": "feasible objective with paired uncertainty interval excluding zero",
        },
        selected_trial_id=selected.trial_id,
        selection_locked_at=utcnow(),
    )


def extend_trial_budget(
    experiment_id: str,
    *,
    additional_trials: int,
    storage: Storage,
    actor: str = "cli",
) -> ExperimentRecord:
    if additional_trials < 1:
        raise ExperimentError("additional_trials must be at least 1")
    record = storage.get_experiment(experiment_id)
    if record is None:
        raise ExperimentError(f"experiment {experiment_id!r} does not exist")
    if record.status is not ExperimentStatus.BUDGET_EXHAUSTED:
        raise ExperimentError("trial budget can be extended only when the experiment is budget_exhausted")
    total = len(storage.list_experiment_trials(experiment_id))
    new_limit = min(total, record.trial_limit + additional_trials)
    if new_limit == record.trial_limit:
        raise ExperimentError("no untried parameter combinations remain")
    return _transition_experiment(
        storage,
        record,
        to_status=ExperimentStatus.RUNNING,
        kind=ExperimentEventKind.BUDGET_EXTENDED,
        actor=actor,
        details={"old_trial_limit": record.trial_limit, "new_trial_limit": new_limit},
        trial_limit=new_limit,
    )


async def evaluate_protected_holdout(
    experiment_id: str,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    environ: dict[str, str] | None = None,
) -> ExperimentRecord:
    """Run the locked baseline and selected configuration once on the protected split.

    No development trial or candidate ranking can be resumed after selection. When the
    selected trial differs from baseline, both are evaluated under the same frozen
    holdout plan so the final paired uncertainty report can support an adoption proposal.
    """
    record = storage.get_experiment(experiment_id)
    if record is None:
        raise ExperimentError(f"experiment {experiment_id!r} does not exist")
    if record.status is ExperimentStatus.COMPLETED:
        return record
    if record.status is ExperimentStatus.SELECTED:
        selected = storage.get_experiment_trial(record.selected_trial_id or "")
        baseline = next(
            (item for item in storage.list_experiment_trials(experiment_id) if item.ordinal == 0),
            None,
        )
        if selected is None or baseline is None:
            raise ExperimentError("selected trial or baseline lineage is missing")
        if selected.status is not ExperimentTrialStatus.COMPLETED:
            raise ExperimentError("only a completed development selection can enter holdout")
        baseline_run_id = _holdout_run_id(experiment_id, "baseline")
        selected_run_id = (
            baseline_run_id
            if selected.trial_id == baseline.trial_id
            else _holdout_run_id(experiment_id, "selected")
        )
        record = _transition_experiment(
            storage,
            record,
            to_status=ExperimentStatus.HOLDOUT_RUNNING,
            kind=ExperimentEventKind.HOLDOUT_STARTED,
            details={
                "selected_trial_id": selected.trial_id,
                "baseline_trial_id": baseline.trial_id,
                "selection_split": "development",
                "holdout_digest": record.holdout_dataset_hash,
            },
            selection_locked_at=record.selection_locked_at or utcnow(),
            holdout_baseline_run_id=baseline_run_id,
            holdout_run_id=selected_run_id,
        )
    elif record.status is not ExperimentStatus.HOLDOUT_RUNNING:
        raise ExperimentError(
            f"protected evaluation requires a development selection; current state is {record.status.value}"
        )

    selected = storage.get_experiment_trial(record.selected_trial_id or "")
    baseline = next(
        (item for item in storage.list_experiment_trials(experiment_id) if item.ordinal == 0), None
    )
    if selected is None or baseline is None or selected.status is not ExperimentTrialStatus.COMPLETED:
        raise ExperimentError("selected trial or baseline lineage is missing or incomplete")
    if not record.holdout_baseline_run_id or not record.holdout_run_id:
        raise ExperimentError("locked holdout run identities are missing")
    baseline_holdout_run_id = record.holdout_baseline_run_id
    selected_holdout_run_id = record.holdout_run_id
    assert baseline_holdout_run_id is not None and selected_holdout_run_id is not None

    base = _compiled_from_record(record)
    try:
        holdout = _compile_holdout_plan(record, base, storage)
    except (ExperimentError, AibenchError, OSError, ValueError) as exc:
        current = storage.get_experiment(experiment_id)
        if current is not None and current.status is ExperimentStatus.HOLDOUT_RUNNING:
            return _transition_experiment(
                storage,
                current,
                to_status=ExperimentStatus.FAILED,
                kind=ExperimentEventKind.TRIAL_FAILED,
                details={"stage": "final_holdout_evaluation", "reason": str(exc)[:500]},
            )
        raise
    plan_artifact = _commit_snapshot(
        storage,
        artifacts,
        holdout.plan_bytes,
        "application/json",
        artifact_id=record.holdout_plan_artifact_id,
    )
    if record.holdout_plan_hash is not None and record.holdout_plan_hash != holdout.plan_hash:
        raise ExperimentError("frozen holdout evaluation plan changed while resuming")
    if record.holdout_plan_artifact_id is not None and record.holdout_plan_artifact_id != plan_artifact:
        raise ExperimentError("frozen holdout plan artifact changed while resuming")
    if record.holdout_plan_hash is None:
        record = _transition_experiment(
            storage,
            record,
            to_status=ExperimentStatus.HOLDOUT_RUNNING,
            kind=ExperimentEventKind.HOLDOUT_STARTED,
            details={
                "holdout_plan_hash": holdout.plan_hash,
                "holdout_plan_artifact_id": plan_artifact,
                "case_count": len(holdout.cases),
                "selection": "all protected holdout cases",
            },
            holdout_plan_hash=holdout.plan_hash,
            holdout_plan_artifact_id=plan_artifact,
        )

    roles: list[tuple[Literal["baseline", "selected"], ExperimentTrial, str]] = [
        ("baseline", baseline, baseline_holdout_run_id),
    ]
    if selected.trial_id != baseline.trial_id:
        roles.append(("selected", selected, selected_holdout_run_id))

    for role, trial, run_id in roles:
        current_run = storage.get_run(run_id)
        if current_run is not None and current_run.status == "completed":
            continue
        variant = _variant(holdout, trial.parameters)
        context = {
            **_run_context(record, trial, stage="final_holdout_evaluation"),
            "evaluation_plan_hash": holdout.plan_hash,
            "holdout_role": role,
            "selected_trial_id": selected.trial_id,
        }
        try:
            create_run(
                variant,
                storage=storage,
                artifacts=artifacts,
                granted_by=f"experiment:{experiment_id}:protected-holdout",
                run_id=run_id,
                run_seed=record.definition.budget.seed,
                experiment_context=context,
            )
            current_run = storage.get_run(run_id)
            if current_run is None:
                raise RunError(f"protected holdout run {run_id!r} was not committed")
            if current_run.status in {
                "created", "running", "pausing", "paused", "cancelling", "interrupting", "interrupted"
            }:
                await execute_run(
                    run_id,
                    storage=storage,
                    artifacts=artifacts,
                    environ=environ,
                )
            current_run = storage.get_run(run_id)
            if current_run is None:
                raise RunError(f"protected holdout run {run_id!r} disappeared")
            if current_run.status in {
                "created", "running", "pausing", "paused", "cancelling", "interrupting", "interrupted"
            }:
                return storage.get_experiment(experiment_id) or record
            if current_run.status != "completed":
                raise ExperimentError(
                    f"protected holdout run ended {current_run.status}; final comparison is unavailable"
                )
        except (AibenchError, OSError, ValueError) as exc:
            latest = storage.get_experiment(experiment_id)
            if latest is not None and latest.status is ExperimentStatus.HOLDOUT_RUNNING:
                return _transition_experiment(
                    storage,
                    latest,
                    to_status=ExperimentStatus.FAILED,
                    kind=ExperimentEventKind.TRIAL_FAILED,
                    details={
                        "stage": "final_holdout_evaluation",
                        "holdout_role": role,
                        "run_id": run_id,
                        "reason": str(exc)[:500],
                    },
                )
            raise

    latest = storage.get_experiment(experiment_id) or record
    return _transition_experiment(
        storage,
        latest,
        to_status=ExperimentStatus.COMPLETED,
        kind=ExperimentEventKind.HOLDOUT_COMPLETED,
        details={
            "selected_trial_id": selected.trial_id,
            "baseline_holdout_run_id": record.holdout_baseline_run_id,
            "selected_holdout_run_id": record.holdout_run_id,
            "dataset_hash": record.holdout_dataset_hash,
            "selection_split": "development",
            "final_evaluation_split": "protected_holdout",
        },
    )


def _holdout_comparison(
    storage: Storage, artifacts: ArtifactStore, record: ExperimentRecord
) -> dict[str, Any] | None:
    if (
        record.status is not ExperimentStatus.COMPLETED
        or not record.holdout_baseline_run_id
        or not record.holdout_run_id
        or record.holdout_baseline_run_id == record.holdout_run_id
    ):
        return None
    return compare_runs(
        storage,
        artifacts,
        record.holdout_baseline_run_id,
        record.holdout_run_id,
        mode="strict",
        min_paired_coverage=record.definition.budget.min_paired_coverage,
        bootstrap_seed=record.definition.budget.seed + 1_000_003,
        bootstrap_replicates=record.definition.budget.bootstrap_replicates,
    )


def experiment_report(
    experiment_id: str, *, storage: Storage, artifacts: ArtifactStore
) -> dict[str, Any]:
    """Return a split-labeled report built only from the frozen contract and stored facts."""
    record = storage.get_experiment(experiment_id)
    if record is None:
        raise ExperimentError(f"experiment {experiment_id!r} does not exist")
    plan_ref = storage.get_artifact(record.plan_artifact_id)
    if plan_ref is None:
        raise ExperimentError("frozen development plan artifact is missing")
    plan_bytes = artifacts.read_bytes(plan_ref)
    if bytes_hash(plan_bytes) != record.plan_hash:
        raise ExperimentError("frozen development plan artifact failed identity verification")
    plan_snapshot = json.loads(plan_bytes)
    trials = storage.list_experiment_trials(experiment_id)
    holdout_runs: dict[str, Any] = {}
    for role, run_id in (
        ("baseline", record.holdout_baseline_run_id),
        ("selected", record.holdout_run_id),
    ):
        if not run_id:
            continue
        run = storage.get_run(run_id)
        if run is None:
            continue
        work_items = storage.list_work_items(run_id)
        selected_count = sum(item.kind == "execution" for item in work_items)
        metrics = _metric_summaries(
            storage,
            run_id,
            {record.definition.objective.binding_index}
            | {item.binding_index for item in record.definition.constraints},
            selected_count,
        ) if selected_count else {}
        holdout_runs[role] = {
            "run_id": run_id,
            "status": run.status,
            "dataset_hash": run.manifest.dataset_hash,
            "plan_hash": run.manifest.plan_hash,
            "selected_execution_count": selected_count,
            "metrics": metrics,
        }
    comparison = _holdout_comparison(storage, artifacts, record)
    return {
        "schema": "aibench.controlled_experiment_report.v1",
        "experiment_id": record.experiment_id,
        "status": record.status.value,
        "intended_change": record.definition.intended_change,
        "objective": {
            "metric_id": record.objective_metric_id,
            "direction": record.objective_direction,
            "binding_hash": record.objective_binding_hash,
        },
        "parameter_space": [
            {"name": item.name, "values": list(item.values)}
            for item in record.definition.parameters
        ],
        "constraints": [item.model_dump(mode="json") for item in record.definition.constraints],
        "run_contract": {
            "plan_hash": record.plan_hash,
            "repetitions": plan_snapshot["repetitions"],
            "per_run_budgets": plan_snapshot["budgets"],
            "metric_binding_count": len(plan_snapshot["metrics"]),
            "cache": plan_snapshot["cache"],
            "evaluator_contract_hash": record.evaluator_contract_hash,
        },
        "lineage": {
            "definition_hash": record.definition_hash,
            "plan_hash": record.plan_hash,
            "evaluator_contract_hash": record.evaluator_contract_hash,
            "application_hash": record.application_hash,
            "application_code_hash": record.application_code_hash,
            "policy_hash": record.policy_hash,
        },
        "budget": {
            "initial_trial_budget": record.definition.budget.max_trials,
            "current_trial_limit": record.trial_limit,
            "parameter_combinations": len(trials),
            "seed": record.definition.budget.seed,
            "bootstrap_replicates": record.definition.budget.bootstrap_replicates,
            "min_metric_coverage": record.definition.budget.min_metric_coverage,
            "min_paired_coverage": record.definition.budget.min_paired_coverage,
        },
        "development_selection": {
            "dataset_hash": record.development_dataset_hash,
            "status": record.status.value,
            "selected_trial_id": record.selected_trial_id,
            "selection_locked_at": (
                record.selection_locked_at.isoformat() if record.selection_locked_at else None
            ),
            "trials": [
                {
                    "trial_id": trial.trial_id,
                    "ordinal": trial.ordinal,
                    "run_id": trial.run_id,
                    "parameters": deep_unfreeze(trial.parameters),
                    "parameter_hash": trial.parameter_hash,
                    "status": trial.status.value,
                    "objective_value": trial.objective_value,
                    "metrics": deep_unfreeze(trial.metrics),
                    "constraints_passed": trial.constraints_passed,
                    "comparison_to_baseline": deep_unfreeze(trial.comparison_to_baseline),
                    "failure": trial.failure,
                }
                for trial in trials
            ],
            "selection_rule": "constraints and coverage pass; a non-baseline candidate must have a paired 95% uncertainty interval excluding zero in the objective direction",
        },
        "protected_holdout_evaluation": {
            "dataset_hash": record.holdout_dataset_hash,
            "status": (
                "not_started"
                if record.status in {ExperimentStatus.READY, ExperimentStatus.RUNNING, ExperimentStatus.BUDGET_EXHAUSTED}
                else record.status.value
            ),
            "selection_locked": record.selection_locked_at is not None,
            "evaluation_plan_hash": record.holdout_plan_hash,
            "runs": holdout_runs,
            "comparison": comparison,
        },
    }


def propose_adoption(
    experiment_id: str, *, storage: Storage, artifacts: ArtifactStore, actor: str = "cli"
) -> dict[str, Any]:
    """Explain the measured tradeoff and propose, but never apply, a config change."""
    record = storage.get_experiment(experiment_id)
    if record is None:
        raise ExperimentError(f"experiment {experiment_id!r} does not exist")
    if record.status is not ExperimentStatus.COMPLETED:
        raise ExperimentError("adoption proposals require a completed protected holdout evaluation")
    report = experiment_report(experiment_id, storage=storage, artifacts=artifacts)
    selected_id = record.selected_trial_id
    trials = storage.list_experiment_trials(experiment_id)
    selected = next((item for item in trials if item.trial_id == selected_id), None)
    baseline = next((item for item in trials if item.ordinal == 0), None)
    if selected is None or baseline is None:
        raise ExperimentError("selected trial or baseline lineage is missing")
    if selected.trial_id == baseline.trial_id:
        recommendation = "retain_baseline"
        explanation = "Development data did not establish a statistically supported improvement over the baseline."
    else:
        comparison = report["protected_holdout_evaluation"].get("comparison")
        metric = next(
            (
                item for item in (comparison or {}).get("metrics", [])
                if item.get("binding_hash") == record.objective_binding_hash
            ),
            None,
        )
        stats = metric.get("comparison") if isinstance(metric, dict) else None
        interval = stats.get("uncertainty", {}) if isinstance(stats, dict) else {}
        lower, upper = interval.get("lower"), interval.get("upper")
        holdout_improves = bool(
            comparison
            and comparison.get("claim_qualified") is True
            and isinstance(lower, (int, float))
            and isinstance(upper, (int, float))
            and (
                lower > 0
                if record.objective_direction == MetricDirection.HIGHER.value
                else upper < 0
            )
        )
        holdout_harms = bool(
            comparison
            and comparison.get("claim_qualified") is True
            and isinstance(lower, (int, float))
            and isinstance(upper, (int, float))
            and (
                upper < 0
                if record.objective_direction == MetricDirection.HIGHER.value
                else lower > 0
            )
        )
        if holdout_improves:
            recommendation = "review_candidate_for_explicit_adoption"
            explanation = "The selected configuration improved on development data and the one-time protected comparison supports the same direction."
        elif holdout_harms:
            recommendation = "retain_baseline"
            explanation = "The one-time protected comparison supports worse objective results for the selected configuration."
        else:
            recommendation = "inconclusive_retain_baseline_pending_new_holdout"
            explanation = "The protected comparison is inconclusive or unqualified; do not adopt from this experiment alone."
    proposal = {
        "experiment_id": experiment_id,
        "recommendation": recommendation,
        "explanation": explanation,
        "intended_change": record.definition.intended_change,
        "selected_trial_id": selected.trial_id,
        "selected_parameters": deep_unfreeze(selected.parameters),
        "baseline_trial_id": baseline.trial_id,
        "objective": report["objective"],
        "development_comparison": deep_unfreeze(selected.comparison_to_baseline),
        "protected_holdout_comparison": report["protected_holdout_evaluation"].get("comparison"),
        "selection_split": "development",
        "final_evaluation_split": "protected_holdout",
        "applied": False,
        "authorization": "Changing source files, deployment targets, or production configuration needs separate explicit authorization.",
    }
    storage.append_experiment_event(
        _event(
            experiment_id,
            ExperimentEventKind.ADOPTION_PROPOSED,
            actor,
            {
                "recommendation": recommendation,
                "selected_trial_id": selected.trial_id,
                "proposal_hash": content_hash(proposal),
                "applied": False,
            },
        )
    )
    return proposal
