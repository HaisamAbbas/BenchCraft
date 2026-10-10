"""Analyze and compile a plan into a validated, policy-approved run (06-T1/T2, 07-T2).

Everything here happens before anything is dispatched: a plan that is invalid, or asks for
anything the policy denies, raises and no application or judge call is made (06-G2).

`analyze_plan` collects every finding instead of stopping at the first, and classifies it:

- `missing_permission` — the policy denies something (trust, targets, effects, secrets,
  evaluators, data egress, plugin environments, data roots, budget ceilings);
- `missing_information` — the plan asks for something the evidence cannot supply: an
  observation the application does not expose, a Golden field no selected case has;
- `invalid` — the plan itself is wrong: unknown evaluator IDs, bad parameters or rules,
  undefined selector fields, empty selections, broken references.

Warnings (`blocking=False`) are reported but do not stop a run: partial per-case coverage,
budgets that cannot cover every planned call. `compile_plan` is the execution gate: any
blocking denial raises `PolicyDenied`, any other blocking finding raises `PlanInvalid`.

Order matters: plan-level policy (plugin environments and paths, secrets, data roots,
budget ceilings) and application policy are checked *before* any plugin environment is
loaded, so a denied plan never runs plugin code.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from aibench.config.resolve import load_mapping_file
from aibench.core.errors import AibenchError
from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    BenchmarkCase,
    DatasetManifest,
    EffectLevel,
    ResetPolicy,
    deep_unfreeze,
)
from aibench.core.plans import CasePredicate, ExecutablePlan
from aibench.datasets.ingest import ingest_dataset
from aibench.engine.cache import code_identity_problem
from aibench.evaluators.protocol import EvaluationView, case_field
from aibench.registry import (
    BindingValidationError,
    EvaluatorRegistry,
    RegistryError,
    ResolvedMetric,
    applicability_problems,
)
from aibench.runners import LoadedApplication, load_application, reset_hook
from aibench.security.policy import (
    ExecutionPolicy,
    application_denials,
    evaluator_denials,
    plan_denials,
    plugin_denials,
    policy_matches,
)

FindingKind = Literal["invalid", "missing_information", "missing_permission"]

# Aggregation operations the reporting layer implements, and the value kind each needs.
_AGGREGATION_VALUE_KINDS = {"rate": "boolean", "mean": "scalar", "category_counts": "category"}


class PlanInvalid(AibenchError):
    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("; ".join(problems))


class PolicyDenied(AibenchError):
    def __init__(self, denials: list[str]) -> None:
        self.denials = denials
        super().__init__("; ".join(denials))


@dataclass(frozen=True)
class PlanFinding:
    kind: FindingKind
    message: str
    subject: str = "plan"  # "application", "dataset", "selection", "metric:<ref>", "budget"...
    blocking: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "subject": self.subject,
            "blocking": self.blocking,
            "message": self.message,
        }


@dataclass(frozen=True)
class MetricCoverage:
    """How many selected cases can supply each Golden field a metric reads."""

    binding_hash: str
    metric: str
    eligible_cases: int
    selected_cases: int
    fields: dict[str, int]  # case.* path -> selected cases where it is usable


@dataclass(frozen=True)
class SelectedWorld:
    """The test world a plan selected, with its seed as loaded at compile time."""

    world_id: str
    seed: object
    seed_bytes: bytes  # canonical JSON, frozen with the run
    seed_hash: str


@dataclass(frozen=True)
class RunPlanOverrides:
    """Validated command-line adjustments applied to a plan before it is frozen."""

    limit: int | None = None
    sample_size: int | None = None
    selection_seed: int | None = None
    repetitions: int | None = None
    warmup_repetitions: int | None = None
    application_concurrency: int | None = None
    evaluation_concurrency: int | None = None
    max_attempts: int | None = None
    evaluation_timeout_seconds: float | None = None
    model_evaluation_timeout_seconds: float | None = None
    max_application_calls: int | None = None
    max_evaluator_calls: int | None = None
    max_judge_tokens: int | None = None
    max_wall_seconds: float | None = None
    max_cost_usd: float | None = None
    estimated_cost_per_application_call_usd: float | None = None
    estimated_cost_per_evaluation_usd: float | None = None
    cache_executions: bool | None = None
    cache_evaluations: bool | None = None


@dataclass
class PlanAnalysis:
    plan: ExecutablePlan
    plan_dir: Path
    policy: ExecutionPolicy  # effective policy, including explicit grants
    application: LoadedApplication | None = None
    dataset: DatasetManifest | None = None
    dataset_case_count: int = 0
    cases: list[BenchmarkCase] = field(default_factory=list)  # selected, in dataset order
    registry: EvaluatorRegistry | None = None
    metrics: list[ResolvedMetric] = field(default_factory=list)
    coverage: list[MetricCoverage] = field(default_factory=list)
    findings: list[PlanFinding] = field(default_factory=list)
    world: SelectedWorld | None = None

    def add(
        self, kind: FindingKind, message: str, subject: str = "plan", *, blocking: bool = True
    ) -> None:
        self.findings.append(PlanFinding(kind, message, subject, blocking))

    def blocking(self, kind: FindingKind | None = None) -> list[PlanFinding]:
        return [f for f in self.findings if f.blocking and (kind is None or f.kind == kind)]

    @property
    def executable(self) -> bool:
        return not self.blocking()


@dataclass
class CompiledRun:
    plan: ExecutablePlan
    plan_bytes: bytes  # canonical JSON of the frozen plan
    plan_hash: str
    plan_dir: Path
    application: LoadedApplication
    dataset: DatasetManifest
    cases: list[BenchmarkCase]  # selected, in dataset order
    registry: EvaluatorRegistry
    metrics: list[ResolvedMetric]
    policy: ExecutionPolicy  # effective policy, including explicit grants
    policy_hash: str
    world: SelectedWorld | None = None


def load_policy(path: Path | None) -> ExecutionPolicy:
    """The execution policy file, or the conservative default when none is given."""
    if path is None:
        return ExecutionPolicy()
    try:
        policy = ExecutionPolicy.model_validate(load_mapping_file(path))
    except (PydanticValidationError, AibenchError, OSError) as exc:
        raise PlanInvalid([f"invalid policy {path}: {exc}"]) from exc
    return policy.resolved_against(path.resolve().parent)


def load_plan(path: Path) -> ExecutablePlan:
    if not path.is_file():
        raise PlanInvalid([f"plan not found: {path}"])
    try:
        return ExecutablePlan.model_validate(load_mapping_file(path))
    except (PydanticValidationError, AibenchError) as exc:
        raise PlanInvalid([f"invalid plan {path}: {exc}"]) from exc


def freeze_plan(plan: ExecutablePlan) -> tuple[bytes, str]:
    # Preserve the canonical bytes of plans written before warmup support. The new field's
    # zero default has no semantic effect, so it must not invalidate an existing run ID.
    frozen = plan.model_dump(mode="json")
    if frozen.get("warmup_repetitions") == 0:
        frozen.pop("warmup_repetitions")
    data = json.dumps(frozen, sort_keys=True, separators=(",", ":"))
    raw = data.encode("utf-8")
    return raw, bytes_hash(raw)


# --------------------------------------------------------------------------- selection


def _matches(case: BenchmarkCase, predicate: CasePredicate) -> bool:
    if predicate.op == "exists":
        return EvaluationView.case_state(case, predicate.path) == "present"
    value = case_field(case, predicate.path)
    if predicate.op == "equals":
        return bool(value == deep_unfreeze(predicate.value))
    return any(value == deep_unfreeze(v) for v in predicate.values)


def sample_cases(cases: list[BenchmarkCase], size: int, seed: int) -> list[BenchmarkCase]:
    """A seeded sample kept in dataset order. Ranks cases by SHA-256 of the seed and case
    identity, so the same seed selects the same cases on every platform and Python version
    (unlike `random.sample`, whose algorithm is not guaranteed stable across versions)."""

    def rank(item: tuple[int, BenchmarkCase]) -> str:
        index, case = item
        key = json.dumps([seed, case.case_id, case.source_line, index])
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    picked = sorted(sorted(enumerate(cases), key=rank)[:size], key=lambda item: item[0])
    return [case for _, case in picked]


def _select(analysis: PlanAnalysis, cases: list[BenchmarkCase]) -> list[BenchmarkCase]:
    selection = analysis.plan.selection
    by_id: dict[str, list[BenchmarkCase]] = {}
    for case in cases:
        by_id.setdefault(case.case_id, []).append(case)
    if selection.case_ids:
        missing = [cid for cid in selection.case_ids if cid not in by_id]
        if missing:
            analysis.add(
                "invalid",
                f"selection.case_ids not in the dataset: {', '.join(missing)}",
                "selection",
            )
        chosen = [c for c in cases if c.case_id in set(selection.case_ids)]
    else:
        chosen = list(cases)
    for predicate in selection.where:
        problem = EvaluationView.path_problem(predicate.path)
        if problem:
            analysis.add("invalid", f"selection.where: {problem}", "selection")
            continue
        chosen = [c for c in chosen if _matches(c, predicate)]
    if selection.sample_size is not None and selection.seed is not None:
        if selection.sample_size > len(chosen):
            analysis.add(
                "invalid",
                f"selection.sample_size={selection.sample_size} exceeds the "
                f"{len(chosen)} case(s) available to sample",
                "selection",
            )
        else:
            chosen = sample_cases(chosen, selection.sample_size, selection.seed)
    if selection.limit is not None:
        chosen = chosen[: selection.limit]
    duplicated = sorted({c.case_id for c in chosen if len(by_id[c.case_id]) > 1})
    if duplicated:
        analysis.add(
            "invalid",
            "selected cases have duplicate case_ids (their Goldens would be ambiguous): "
            + ", ".join(duplicated),
            "selection",
        )
    if not chosen and not analysis.blocking("invalid"):
        analysis.add("invalid", "the selection contains no cases", "selection")
    return chosen


# --------------------------------------------------------------------------- metrics


def _load_registry(analysis: PlanAnalysis) -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    plan, plan_dir = analysis.plan, analysis.plan_dir
    for env in plan.plugin_environments:
        python = Path(env.python) if Path(env.python).is_absolute() else plan_dir / env.python
        try:
            for load in registry.load_plugin_environment(
                python,
                secret_env=dict(env.secret_env),
                extra_paths=[Path(p) if Path(p).is_absolute() else plan_dir / p for p in env.paths],
                startup_timeout_seconds=env.startup_timeout_seconds,
            ):
                if load.error:
                    analysis.add("invalid", f"plugin {load.plugin.name}: {load.error}", "plugins")
        except RegistryError as exc:
            analysis.add("invalid", str(exc), "plugins")
    return registry


def _resolve_metrics(analysis: PlanAnalysis, *, plugins_not_loaded: str | None) -> None:
    """`plugins_not_loaded` says why plugin evaluators could not be resolved, if they
    could not; an unknown non-native ID is then a permission problem, not a typo."""
    registry = analysis.registry
    assert registry is not None
    seen: set[str] = set()
    for binding in analysis.plan.metrics:
        subject = f"metric:{binding.metric}"
        try:
            metric = registry.resolve_binding(binding)
        except BindingValidationError as exc:
            unknown = any(p.problem.startswith("unknown evaluator") for p in exc.problems)
            native = binding.metric.startswith("native.")
            if unknown and plugins_not_loaded and not native:
                analysis.add(
                    "missing_permission",
                    f"{binding.metric}: not validated, because {plugins_not_loaded}",
                    subject,
                )
            else:
                for problem in exc.problems:
                    analysis.add("invalid", str(problem), subject)
            continue
        if metric.manifest.consumes == "remote_job":
            analysis.add(
                "invalid",
                f"{binding.metric}: its results come from a remote job, not a per-case "
                "evaluation; submit it with its plugin's remote-job commands",
                subject,
            )
            continue
        if metric.manifest.consumes == "paired_runs":
            analysis.add(
                "invalid",
                f"{binding.metric}: it judges two runs' answers against each other, not one "
                'run; use /compare BASELINE CURRENT --judge "CRITERIA"',
                subject,
            )
            continue
        if metric.binding_hash in seen:
            analysis.add("invalid", f"{binding.metric}: duplicate binding", subject)
            continue
        seen.add(metric.binding_hash)
        analysis.metrics.append(metric)
        if analysis.application is not None:
            for issue in applicability_problems(metric.requirements, analysis.application.spec):
                analysis.add("missing_information", f"{binding.metric}: {issue}", subject)
        _check_aggregation(analysis, metric, subject)


def _check_state(analysis: PlanAnalysis) -> None:
    """State between cases (§7 "State and observability"). A selected test world must be
    declared, approved and loadable through a reset hook. Episodes need a reset hook and
    cannot retry a turn. An application with a reset hook keeps state its cases share, so
    they cannot run concurrently: resetting for one case would disturb another."""
    app, plan = analysis.application, analysis.plan
    if app is None:
        return
    spec, hook = app.spec, reset_hook(app.spec)
    if plan.test_world is not None:
        world = spec.test_worlds.get(plan.test_world)
        declared = ", ".join(sorted(spec.test_worlds)) or "none"
        if world is None:
            analysis.add(
                "invalid",
                f"test world {plan.test_world!r} is not declared by application "
                f"{spec.application_id!r} (declared: {declared})",
                "test_world",
            )
        else:
            qualified = f"{spec.application_id}:{plan.test_world}"
            if not policy_matches(qualified, analysis.policy.allowed_test_worlds):
                analysis.add(
                    "missing_permission",
                    f"test world {qualified} is not approved (allowed_test_worlds in the policy)",
                    "test_world",
                )
            if hook is None:
                analysis.add(
                    "invalid",
                    "a test world is loaded through a reset hook (reset_url, reset_argv or "
                    f"reset_callable); application {spec.application_id!r} has none",
                    "test_world",
                )
            if spec.reset_policy is ResetPolicy.SHARED:
                analysis.add(
                    "invalid",
                    f"application {spec.application_id!r} declares shared state and is never "
                    "reset, so a test world could not be loaded",
                    "test_world",
                )
            try:
                seed = world.seed
                if world.seed_file is not None:
                    base = app.base_dir.resolve()
                    path = Path(world.seed_file)
                    path = (path if path.is_absolute() else base / path).resolve()
                    if not path.is_relative_to(base):
                        raise ValueError(
                            f"seed_file {world.seed_file!r} is outside the application's directory"
                        )
                    seed = json.loads(path.read_text(encoding="utf-8"))
                seed = deep_unfreeze(seed)
                if not isinstance(seed, dict | list):
                    raise TypeError("a seed must be a JSON object or list")
                seed_bytes = json.dumps(seed, sort_keys=True).encode("utf-8")
                analysis.world = SelectedWorld(
                    plan.test_world, seed, seed_bytes, bytes_hash(seed_bytes)
                )
            except (OSError, ValueError, TypeError) as exc:
                analysis.add("invalid", f"test world {qualified}: seed unusable: {exc}")
    if spec.reset_policy is ResetPolicy.PER_EPISODE:
        if hook is None:
            analysis.add(
                "invalid",
                "reset_policy per_episode needs a reset hook (reset_url, reset_argv or "
                f"reset_callable) to reset between episodes; application "
                f"{spec.application_id!r} has none",
                "application",
            )
        if plan.retry.max_attempts > 1:
            analysis.add(
                "invalid",
                "an episode turn cannot be retried: the earlier attempt changed the "
                "application's state. Set retry.max_attempts to 1",
                "plan",
            )
    shared_state = hook is not None and spec.reset_policy is not ResetPolicy.SHARED
    if shared_state and plan.concurrency.application > 1:
        analysis.add(
            "invalid",
            f"application {spec.application_id!r} keeps state its cases share (it has a "
            f"{hook}); resetting it for one case would disturb another. Set "
            "concurrency.application to 1",
            "plan",
        )


def _check_cache(analysis: PlanAnalysis) -> None:
    """Execution caching is refused where the output depends on state the key cannot
    capture (§14): declared effects without a snapshotted test world, episodes, and shared
    state."""
    app, plan = analysis.application, analysis.plan
    if app is None or not plan.cache.executions:
        return
    spec = app.spec
    problems = []
    if spec.effects is not EffectLevel.NONE and plan.test_world is None:
        problems.append(
            f"it declares {spec.effects.value} effects and no test world snapshots its state"
        )
    if spec.reset_policy is ResetPolicy.PER_EPISODE:
        problems.append("its episode turns depend on the state earlier turns built")
    if spec.reset_policy is ResetPolicy.SHARED:
        problems.append("it declares shared state that changes between cases")
    code_problem = code_identity_problem(spec, app.base_dir)
    if code_problem is not None:
        problems.append(code_problem)
    for problem in problems:
        analysis.add(
            "invalid",
            f"execution caching is not allowed for application {spec.application_id!r}: {problem}",
            "cache",
        )


def _check_episodes(analysis: PlanAnalysis, all_cases: list[BenchmarkCase]) -> None:
    """A selection must keep episodes whole from their first turn: a later turn run without
    the turns before it would start from the seed instead of the state they build."""
    app = analysis.application
    if app is None or app.spec.reset_policy is not ResetPolicy.PER_EPISODE:
        return
    selected = {c.case_id for c in analysis.cases}
    turns: dict[str, list[str]] = {}
    for case in all_cases:
        turns.setdefault(case.group_id or case.case_id, []).append(case.case_id)
    for episode, ids in turns.items():
        kept = [i for i in ids if i in selected]
        if kept and kept != ids[: len(kept)]:
            missing = [i for i in ids[: ids.index(kept[-1])] if i not in selected]
            analysis.add(
                "invalid",
                f"the selection splits episode {episode!r}: {', '.join(missing)} would be "
                "skipped before later turns that depend on its state",
                "selection",
            )


def _check_gates(analysis: PlanAnalysis) -> None:
    """A pass-rate gate needs a decision rule on its binding: without one every decision is
    indeterminate and the gate could only ever fail (§12: gates are predeclared)."""
    for gate in analysis.plan.gates:
        binding = analysis.plan.metrics[gate.binding]
        metric = next((m for m in analysis.metrics if m.binding == binding), None)
        if metric is None or gate.min_pass_rate is None:
            continue
        if (binding.rule or metric.manifest.default_rule) is None:
            analysis.add(
                "invalid",
                f"gate {gate.gate_id!r}: {binding.metric} has no pass/fail rule, so a pass rate "
                "cannot be measured; add a rule to the binding or use min_completed_coverage",
                f"gate:{gate.gate_id}",
            )


def _check_aggregation(analysis: PlanAnalysis, metric: ResolvedMetric, subject: str) -> None:
    manifest = metric.manifest
    if manifest.aggregation == "none":
        analysis.add(
            "missing_information",
            f"{metric.binding.metric}: declares no aggregation; only per-case values will be "
            "reported",
            subject,
            blocking=False,
        )
        return
    needed = _AGGREGATION_VALUE_KINDS[manifest.aggregation]
    if manifest.value_kind != needed:
        analysis.add(
            "invalid",
            f"{metric.binding.metric}: aggregation {manifest.aggregation!r} needs "
            f"{needed} values, but the metric produces {manifest.value_kind} values",
            subject,
        )


def _check_coverage(analysis: PlanAnalysis) -> None:
    """Per-case field requirements: a Golden field no selected case can supply is an
    unavailable input, rejected here rather than scored as not_applicable everywhere."""
    selected = len(analysis.cases)
    for metric in analysis.metrics:
        subject = f"metric:{metric.binding.metric}"
        usable_by_field: dict[str, int] = {}
        eligible = 0
        for case in analysis.cases:
            ok = True
            for requirement in metric.requirements:
                if not requirement.path.startswith("case."):
                    continue
                state = EvaluationView.case_state(case, requirement.path)
                usable = state == "present" or (state == "empty" and not requirement.non_empty)
                usable_by_field[requirement.path] = usable_by_field.get(requirement.path, 0) + (
                    1 if usable else 0
                )
                ok = ok and usable
            eligible += 1 if ok else 0
        analysis.coverage.append(
            MetricCoverage(
                metric.binding_hash, metric.binding.metric, eligible, selected, usable_by_field
            )
        )
        for path, count in sorted(usable_by_field.items()):
            if count == 0 and selected:
                analysis.add(
                    "missing_information",
                    f"{metric.binding.metric}: requires {path}, but none of the {selected} "
                    "selected case(s) has it",
                    subject,
                )
            elif count < selected:
                analysis.add(
                    "missing_information",
                    f"{metric.binding.metric}: requires {path}, present in {count} of "
                    f"{selected} selected case(s); the rest will be not_applicable",
                    subject,
                    blocking=False,
                )


def work_graph(
    case_ids: list[str], repetitions: int, binding_hashes: list[str], warmup_repetitions: int = 0
) -> dict[str, tuple[str, ...]]:
    """The plan's work DAG: task key -> dependency keys. Executions depend on nothing; each
    evaluation depends on exactly its execution. (Matches `engine.execution_key` and
    `engine.evaluation_key`.)"""
    graph: dict[str, tuple[str, ...]] = {}
    for case_id in case_ids:
        for repetition in range(repetitions, repetitions + warmup_repetitions):
            graph[f"exec:{case_id}:r{repetition}"] = ()
        for repetition in range(repetitions):
            exec_key = f"exec:{case_id}:r{repetition}"
            graph[exec_key] = ()
            for binding_hash in binding_hashes:
                graph[f"eval:{case_id}:r{repetition}:{binding_hash[7:23]}"] = (exec_key,)
    return graph


def dag_problems(graph: dict[str, tuple[str, ...]]) -> list[str]:
    """Undefined dependencies and cycles, found by a depth-first walk."""
    problems = [
        f"{node} depends on undefined {dep}"
        for node, deps in graph.items()
        for dep in deps
        if dep not in graph
    ]
    state: dict[str, int] = {}  # 1 = visiting, 2 = done
    for root in graph:
        if root in state:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            node, index = stack[-1]
            deps = [d for d in graph[node] if d in graph]
            if index < len(deps):
                stack[-1] = (node, index + 1)
                dep = deps[index]
                if state.get(dep) == 1:
                    problems.append(f"dependency cycle through {dep}")
                elif dep not in state:
                    state[dep] = 1
                    stack.append((dep, 0))
            else:
                state[node] = 2
                stack.pop()
    return problems


def _check_budgets(analysis: PlanAnalysis) -> None:
    plan, budgets = analysis.plan, analysis.plan.budgets
    executions = len(analysis.cases) * (plan.repetitions + plan.warmup_repetitions)
    evaluations = len(analysis.cases) * plan.repetitions * len(analysis.metrics)
    if budgets.max_application_calls is not None and budgets.max_application_calls < executions:
        analysis.add(
            "missing_information",
            f"max_application_calls={budgets.max_application_calls} covers at most "
            f"{budgets.max_application_calls} of {executions} planned executions; the rest "
            "will be blocked (lost coverage)",
            "budget",
            blocking=False,
        )
    if budgets.max_evaluator_calls is not None and budgets.max_evaluator_calls < evaluations:
        analysis.add(
            "missing_information",
            f"max_evaluator_calls={budgets.max_evaluator_calls} covers at most "
            f"{budgets.max_evaluator_calls} of {evaluations} planned evaluations",
            "budget",
            blocking=False,
        )
    if (
        budgets.max_cost_usd is not None
        and budgets.estimated_cost_per_evaluation_usd is None
        and any(m.manifest.uses_models for m in analysis.metrics)
    ):
        analysis.add(
            "invalid",
            "max_cost_usd with model-backed evaluators needs estimated_cost_per_evaluation_usd "
            "(unknown judge costs are never counted as zero)",
            "budget",
        )


# --------------------------------------------------------------------------- analysis


def analyze_plan(
    plan: ExecutablePlan,
    plan_dir: Path,
    *,
    policy: ExecutionPolicy,
    trusted_local: bool = False,
    registry: EvaluatorRegistry | None = None,
) -> PlanAnalysis:
    """Collect every finding for `plan` (paths relative to `plan_dir`). Never dispatches;
    loads plugin environments only when the policy permits the plan. A caller that already
    loaded the plan's plugin environments may pass that `registry` to avoid reloading them
    (it is still used only when the policy permits the plan)."""
    effective = policy.with_trusted_local(trusted_local)
    analysis = PlanAnalysis(plan=plan, plan_dir=plan_dir.resolve(), policy=effective)
    for denial in plan_denials(effective, plan, analysis.plan_dir):
        analysis.add("missing_permission", denial)

    try:
        analysis.application = load_application(analysis.plan_dir / plan.application)
        for denial in application_denials(effective, analysis.application.spec):
            analysis.add("missing_permission", denial, "application")
    except AibenchError as exc:
        analysis.add("invalid", str(exc), "application")
    _check_state(analysis)
    _check_cache(analysis)

    try:
        report = ingest_dataset(analysis.plan_dir / plan.dataset)
        for error in report.errors:
            analysis.add("invalid", f"dataset: {error}", "dataset")
        if report.is_valid and report.manifest is not None:
            analysis.dataset = report.manifest
            analysis.dataset_case_count = len(report.cases)
            analysis.cases = _select(analysis, report.cases)
            _check_episodes(analysis, report.cases)
    except AibenchError as exc:
        analysis.add("invalid", f"dataset: {exc}", "dataset")

    permitted = not analysis.blocking("missing_permission")
    if not permitted:
        analysis.registry = EvaluatorRegistry.with_native()
    else:
        analysis.registry = registry if registry is not None else _load_registry(analysis)
    if plugin_denials(effective, plan.plugin_environments, analysis.plan_dir):
        not_loaded = "its plugin environment is not permitted by the policy"
    elif not permitted and plan.plugin_environments:
        not_loaded = "plugin environments are not started while other permissions are missing"
    else:
        not_loaded = None
    _resolve_metrics(analysis, plugins_not_loaded=not_loaded)
    _check_gates(analysis)
    for denial in evaluator_denials(effective, [m.manifest for m in analysis.metrics]):
        analysis.add("missing_permission", denial, "evaluators")
    _check_coverage(analysis)
    _check_budgets(analysis)
    graph = work_graph(
        [c.case_id for c in analysis.cases],
        plan.repetitions,
        [m.binding_hash for m in analysis.metrics],
        plan.warmup_repetitions,
    )
    for problem in dag_problems(graph):
        analysis.add("invalid", f"work graph: {problem}", "plan")
    return analysis


def compile_plan(
    plan_path: Path,
    *,
    policy: ExecutionPolicy,
    trusted_local: bool = False,
    overrides: RunPlanOverrides | None = None,
) -> CompiledRun:
    """The execution gate: analyze, then refuse on any blocking finding."""
    plan = load_plan(plan_path)
    if overrides is not None:
        plan = apply_run_plan_overrides(plan, overrides)
    analysis = analyze_plan(
        plan, plan_path.resolve().parent, policy=policy, trusted_local=trusted_local
    )
    return compiled_from(analysis)


def apply_run_plan_overrides(
    plan: ExecutablePlan, overrides: RunPlanOverrides
) -> ExecutablePlan:
    """Apply direct run controls to a plan and validate the complete effective plan."""
    if overrides.limit is not None and overrides.sample_size is not None:
        raise PlanInvalid(["--limit and --sample-size cannot be used together"])

    data = plan.model_dump(mode="json")
    selection = data["selection"]
    if overrides.limit is not None:
        selection.update({"limit": overrides.limit, "sample_size": None, "seed": None})
    if overrides.sample_size is not None:
        selection["limit"] = None
        selection["sample_size"] = overrides.sample_size
    if overrides.selection_seed is not None:
        if overrides.sample_size is None and selection["sample_size"] is None:
            raise PlanInvalid(
                ["--selection-seed requires --sample-size or a plan with sample_size"]
            )
        selection["seed"] = overrides.selection_seed

    if overrides.repetitions is not None:
        data["repetitions"] = overrides.repetitions
    if overrides.warmup_repetitions is not None:
        data["warmup_repetitions"] = overrides.warmup_repetitions
    concurrency = data["concurrency"]
    if overrides.application_concurrency is not None:
        concurrency["application"] = overrides.application_concurrency
    if overrides.evaluation_concurrency is not None:
        concurrency["evaluation"] = overrides.evaluation_concurrency
    if overrides.max_attempts is not None:
        data["retry"]["max_attempts"] = overrides.max_attempts
    for field_name in (
        "evaluation_timeout_seconds",
        "model_evaluation_timeout_seconds",
    ):
        value = getattr(overrides, field_name)
        if value is not None:
            data[field_name] = value
    budgets = data["budgets"]
    for field_name in (
        "max_application_calls",
        "max_evaluator_calls",
        "max_judge_tokens",
        "max_wall_seconds",
        "max_cost_usd",
        "estimated_cost_per_application_call_usd",
        "estimated_cost_per_evaluation_usd",
    ):
        value = getattr(overrides, field_name)
        if value is not None:
            budgets[field_name] = value
    cache = data["cache"]
    for field_name in ("cache_executions", "cache_evaluations"):
        value = getattr(overrides, field_name)
        if value is not None:
            cache[field_name.removeprefix("cache_")] = value

    try:
        return ExecutablePlan.model_validate(data)
    except PydanticValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise PlanInvalid(
            [f"invalid direct run override: {problem}" for problem in problems]
        ) from exc


def compiled_from(analysis: PlanAnalysis) -> CompiledRun:
    denials = analysis.blocking("missing_permission")
    if denials:
        raise PolicyDenied(sorted({f.message for f in denials}))
    problems = analysis.blocking()
    if problems:
        raise PlanInvalid([f.message for f in problems])
    assert analysis.application is not None and analysis.dataset is not None
    assert analysis.registry is not None
    plan_bytes, plan_hash = freeze_plan(analysis.plan)
    return CompiledRun(
        plan=analysis.plan,
        plan_bytes=plan_bytes,
        plan_hash=plan_hash,
        plan_dir=analysis.plan_dir,
        application=analysis.application,
        dataset=analysis.dataset,
        cases=analysis.cases,
        registry=analysis.registry,
        metrics=analysis.metrics,
        policy=analysis.policy,
        policy_hash=content_hash(analysis.policy.model_dump(mode="json")),
        world=analysis.world,
    )


def finding_counts(findings: list[PlanFinding]) -> dict[str, int]:
    return dict(Counter(f.kind for f in findings if f.blocking))
