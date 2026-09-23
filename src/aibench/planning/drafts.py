"""Plan drafts: what a planner proposes, how it becomes an executable plan, and how it is
validated (07-T2, 07-T4).

A planner — the deterministic template or a model — produces a `DraftProposal`. It may
choose only objectives' concepts, metric bindings (from installed evaluators), rationale,
repetitions, gaps and questions. Everything else comes from the user and the policy:
dataset and application paths, case selection, budgets, concurrency, retries, plugin
environments, metric parameters and thresholds. So a model cannot widen a run's reach by
writing a plan (contract: "do not expand data egress or external effects based on ... LLM
suggestions"), cannot probe label values through selection predicates, and cannot invent a
schema or success threshold (§3) — parameters and rules it sets that the user did not
supply are blocking findings.

`build_plan` turns a proposal into an `ExecutablePlan` — the same format a person writes by
hand, so equivalent manual and generated plans run identically (07-G2). `validate_draft`
runs the execution gate's analysis (`engine.compile.analyze_plan`) plus planning checks:
the user's stated objectives are all present, verbatim; every concept of every objective
has a metric that measures it, an explicit gap, or is engine-recorded.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from aibench.core.models import (
    DecisionRule,
    FrozenModel,
    FrozenValue,
    MetricBinding,
    deep_unfreeze,
)
from aibench.core.plans import (
    BudgetLimits,
    CaseSelection,
    ConcurrencyLimits,
    ExecutablePlan,
    PluginEnvironmentRef,
    RetryPolicy,
)
from aibench.engine.compile import (
    FindingKind,
    PlanAnalysis,
    PlanFinding,
    analyze_plan,
    freeze_plan,
)
from aibench.planning.catalog import CONCEPTS, ENGINE_RECORDED, concepts_for, concepts_in
from aibench.registry import EvaluatorRegistry
from aibench.security.policy import ExecutionPolicy

DRAFT_SCHEMA_VERSION = "1.0.0"
_OBJECTIVE_ID = r"^[a-z][a-z0-9_]{0,39}$"


class Objective(FrozenModel):
    objective_id: str = Field(pattern=_OBJECTIVE_ID)
    text: str = Field(min_length=1, max_length=500)
    concepts: tuple[str, ...] = ()
    source: Literal["user", "planner"] = "user"


class MetricChoice(FrozenModel):
    metric: str
    params: FrozenValue = Field(default_factory=dict)
    rule: DecisionRule | None = None
    objective_ids: tuple[str, ...] = ()
    rationale: str = Field(min_length=1, max_length=1000)


class Gap(FrozenModel):
    """Something asked for that this plan cannot measure, and why."""

    subject: str  # objective id or concept
    reason: str = Field(min_length=1, max_length=1000)


class QuestionProposal(FrozenModel):
    prompt: str = Field(min_length=1, max_length=500)
    required_fields: tuple[str, ...] = ()
    choices: tuple[str, ...] = ()
    blocking_scope: str = "plan"


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


class DraftProposal(FrozenModel):
    """The only thing a planner writes (the `write_plan_draft` tool's argument schema).
    There is deliberately no selection: predicates over Golden fields would let a model
    learn label values from case counts."""

    objectives: tuple[Objective, ...] = ()
    metrics: tuple[MetricChoice, ...] = ()
    repetitions: int = Field(default=1, ge=1, le=10)
    gaps: tuple[Gap, ...] = ()
    questions: tuple[QuestionProposal, ...] = ()


class SpendEstimate(FrozenModel):
    selected_cases: int
    repetitions: int
    executions: int
    application_calls_upper_bound: int  # with every retry the retry policy allows
    evaluations: int
    model_evaluations: int  # evaluations sent to a model judge (data egress, spend)
    estimated_cost_usd: float | None  # only from the plan's own per-call estimates
    cost_basis: str
    judge_tokens: Literal["unknown"] = "unknown"


class PlannerProvenance(FrozenModel):
    kind: Literal["template", "model"]
    provider: str | None = None
    model: str | None = None
    model_calls: int = 0
    tool_calls: int = 0
    rejected_tool_calls: tuple[str, ...] = ()
    repairs: int = 0
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    calls_without_usage: int = 0  # replies that reported no token usage (cap not enforceable)
    fallback_reason: str | None = None


class PlanDraft(FrozenModel):
    """The draft document `aibench plan` writes next to the executable plan."""

    schema_version: str = DRAFT_SCHEMA_VERSION
    plan_id: str
    revision: int = Field(ge=1)
    supersedes: str | None = None  # plan hash of the previous revision
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    plan_file: str
    plan_hash: str
    executable: bool
    planner: PlannerProvenance
    objectives: tuple[Objective, ...]
    rationale: tuple[MetricChoice, ...]
    gaps: tuple[Gap, ...]
    pending_questions: tuple[PendingQuestion, ...]
    findings: tuple[FrozenValue, ...]
    coverage: tuple[FrozenValue, ...]
    estimate: SpendEstimate | None
    profile_hash: str
    dataset_hash: str
    scope: str = (
        "planned from the declared application config, dataset field counts and installed "
        "evaluator manifests; no source code was read and no hidden labels were used"
    )


@dataclass
class DraftContext:
    """What the user and policy fix; the planner cannot change any of it."""

    plan_id: str
    out_dir: Path  # where the plan file will live; paths are written relative to it
    dataset: Path
    application: Path
    policy: ExecutionPolicy
    trusted_local: bool = False
    budgets: BudgetLimits = field(default_factory=BudgetLimits)
    concurrency: ConcurrencyLimits = field(default_factory=ConcurrencyLimits)
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    plugin_environments: tuple[PluginEnvironmentRef, ...] = ()
    # The user's case selection (e.g. a seeded pilot sample); planners cannot select.
    selection: CaseSelection | None = None
    revision: int = 1
    # The registry the catalog was built from (native + permitted plugin environments), so
    # validation does not restart plugin workers for every draft.
    registry: EvaluatorRegistry | None = None
    # Plugin load problems met while building that registry (the execution gate reports
    # them as invalid too).
    plugin_problems: tuple[str, ...] = ()
    # What the user stated: objectives (verbatim), metric parameters and rules by
    # evaluator ID. Planners may use these; anything else they set is unconfirmed.
    user_objectives: tuple[str, ...] = ()
    user_params: dict[str, dict[str, object]] = field(default_factory=dict)
    user_rules: dict[str, DecisionRule] = field(default_factory=dict)


@dataclass
class DraftValidation:
    plan: ExecutablePlan | None
    analysis: PlanAnalysis | None
    findings: list[PlanFinding]
    estimate: SpendEstimate | None

    @property
    def executable(self) -> bool:
        return self.plan is not None and not any(f.blocking for f in self.findings)

    def blocking_messages(self) -> list[str]:
        return [f"[{f.kind}] {f.message}" for f in self.findings if f.blocking]


def _relative(path: Path, base: Path) -> str:
    try:
        return Path(os.path.relpath(path.resolve(), base.resolve())).as_posix()
    except ValueError:  # another drive on Windows
        return str(path.resolve())


def build_plan(proposal: DraftProposal, ctx: DraftContext) -> ExecutablePlan:
    """Raises pydantic's ValidationError if the proposal cannot form a valid plan."""
    return ExecutablePlan(
        plan_id=ctx.plan_id,
        dataset=_relative(ctx.dataset, ctx.out_dir),
        application=_relative(ctx.application, ctx.out_dir),
        metrics=tuple(
            MetricBinding(metric=m.metric, params=m.params, rule=m.rule) for m in proposal.metrics
        ),
        repetitions=proposal.repetitions,
        selection=ctx.selection or CaseSelection(),
        concurrency=ctx.concurrency,
        retry=ctx.retry,
        budgets=ctx.budgets,
        plugin_environments=ctx.plugin_environments,
    )


def estimate_spend(plan: ExecutablePlan, analysis: PlanAnalysis) -> SpendEstimate:
    selected = len(analysis.cases)
    executions = selected * plan.repetitions
    model_metrics = sum(1 for m in analysis.metrics if m.manifest.uses_models)
    evaluations = executions * len(analysis.metrics)
    budgets = plan.budgets
    app_rate, eval_rate = (
        budgets.estimated_cost_per_application_call_usd,
        budgets.estimated_cost_per_evaluation_usd,
    )
    if app_rate is None and eval_rate is None:
        cost, basis = None, "unknown: the plan declares no per-call cost estimates"
    else:
        cost = round(executions * (app_rate or 0.0) + evaluations * (eval_rate or 0.0), 6)
        missing = [
            name
            for name, rate in (("application", app_rate), ("evaluation", eval_rate))
            if rate is None
        ]
        basis = "plan per-call estimates, one attempt per call"
        if missing:
            basis += f"; {' and '.join(missing)} cost not estimated (excluded)"
    return SpendEstimate(
        selected_cases=selected,
        repetitions=plan.repetitions,
        executions=executions,
        application_calls_upper_bound=executions * plan.retry.max_attempts,
        evaluations=evaluations,
        model_evaluations=executions * model_metrics,
        estimated_cost_usd=cost,
        cost_basis=basis,
    )


def _objective_findings(
    proposal: DraftProposal, ctx: DraftContext, analysis: PlanAnalysis
) -> list[PlanFinding]:
    findings: list[PlanFinding] = []

    def add(kind: FindingKind, message: str, subject: str, blocking: bool = True) -> None:
        findings.append(PlanFinding(kind, message, subject, blocking))

    ids = [o.objective_id for o in proposal.objectives]
    if len(set(ids)) != len(ids):
        add("invalid", "objective ids must be unique", "objectives")
    known = set(ids)
    if not proposal.objectives:
        add(
            "missing_information",
            "no evaluation objective is stated; say what the benchmark should check",
            "objectives",
        )
    stated = {o.text.strip() for o in proposal.objectives if o.source == "user"}
    for text in ctx.user_objectives:
        if text.strip() not in stated:
            add(
                "missing_information",
                f"your objective {text!r} is missing from the draft (a planner may not drop "
                "or reword stated objectives)",
                "objectives",
            )
    for objective in proposal.objectives:
        subject = f"objective:{objective.objective_id}"
        unknown = [c for c in objective.concepts if c not in CONCEPTS]
        if unknown:
            add(
                "invalid",
                f"objective {objective.objective_id}: unknown concepts {unknown}; "
                f"known: {sorted(CONCEPTS)}",
                subject,
            )
        suggested = set(concepts_in(objective.text))
        if objective.source == "user" and suggested and not suggested <= set(objective.concepts):
            add(
                "missing_information",
                f"objective {objective.objective_id} ({objective.text!r}) was mapped to "
                f"{sorted(objective.concepts)}, but its wording suggests {sorted(suggested)}; "
                "check the mapping",
                subject,
                blocking=False,
            )

    by_ref = {m.binding.metric: m for m in analysis.metrics}
    concepts_of = {
        ref: set(concepts_for(m.manifest, tuple(m.requirements))) for ref, m in by_ref.items()
    }
    objective_concepts = {o.objective_id: set(o.concepts) for o in proposal.objectives}
    for choice in proposal.metrics:
        subject = f"metric:{choice.metric}"
        undefined = [i for i in choice.objective_ids if i not in known]
        if undefined:
            add("invalid", f"{choice.metric}: references undefined objectives {undefined}", subject)
        if not choice.objective_ids:
            add(
                "missing_information",
                f"{choice.metric}: serves no stated objective (unjustified evaluator)",
                subject,
                blocking=False,
            )
        resolved = by_ref.get(choice.metric)
        if resolved is None:
            continue  # already reported by the plan analysis
        claimed = set().union(*(objective_concepts.get(i, set()) for i in choice.objective_ids))
        if choice.objective_ids and not concepts_of[choice.metric] & claimed:
            add(
                "missing_information",
                f"{choice.metric}: measures {sorted(concepts_of[choice.metric]) or 'nothing'}, "
                f"none of its objectives' concepts {sorted(claimed)} (unjustified evaluator)",
                subject,
                blocking=False,
            )
        evaluator_id = resolved.manifest.evaluator_id
        params = deep_unfreeze(choice.params) or {}
        if evaluator_id in ctx.user_params and params != ctx.user_params[evaluator_id]:
            add(
                "missing_information",
                f"{choice.metric}: omitted or changed the user-supplied parameters; include "
                f"them exactly with --params {evaluator_id}=JSON",
                subject,
            )
        elif params and params != ctx.user_params.get(evaluator_id):
            add(
                "missing_information",
                f"{choice.metric}: parameters were set by the planner, not supplied by you; "
                f"give them with --params {evaluator_id}=JSON",
                subject,
            )
        if evaluator_id in ctx.user_rules and choice.rule != ctx.user_rules[evaluator_id]:
            add(
                "missing_information",
                f"{choice.metric}: omitted or changed the user-supplied pass/fail rule; include "
                f"it exactly with --rule {evaluator_id}=JSON",
                subject,
            )
        elif choice.rule is not None and choice.rule != ctx.user_rules.get(evaluator_id):
            add(
                "missing_information",
                f"{choice.metric}: the pass/fail rule was set by the planner, not by you; give "
                f"it with --rule {evaluator_id}=JSON or rely on the evaluator's documented default",
                subject,
            )

    gap_subjects = {g.subject for g in proposal.gaps}
    for objective in proposal.objectives:
        subject = f"objective:{objective.objective_id}"
        if objective.objective_id in gap_subjects:
            continue
        measured = set().union(
            *(
                concepts_of.get(m.metric, set())
                for m in proposal.metrics
                if objective.objective_id in m.objective_ids
            )
        )
        # For a user's objective, concepts its wording names must be covered too, so a
        # planner cannot relabel "catch hallucinations" as latency and measure nothing.
        required = set(objective.concepts)
        if objective.source == "user":
            required |= set(concepts_in(objective.text))
        if not required:
            if not measured:
                add(
                    "missing_information",
                    f"objective {objective.objective_id} has no metric and no explicit gap",
                    subject,
                )
            continue
        for concept in sorted(required):
            if concept in ENGINE_RECORDED or concept in measured or concept in gap_subjects:
                continue
            add(
                "missing_information",
                f"objective {objective.objective_id}: nothing measures {concept} and no gap "
                "explains why",
                subject,
            )
    return findings


def validate_draft(proposal: DraftProposal, ctx: DraftContext) -> DraftValidation:
    try:
        plan = build_plan(proposal, ctx)
    except PydanticValidationError as exc:
        messages = [
            f"{'.'.join(str(p) for p in error['loc']) or 'plan'}: {error['msg']}"
            for error in exc.errors()
        ]
        return DraftValidation(None, None, [PlanFinding("invalid", m) for m in messages], None)
    analysis = analyze_plan(
        plan,
        ctx.out_dir,
        policy=ctx.policy,
        trusted_local=ctx.trusted_local,
        registry=ctx.registry,
    )
    findings = [
        *analysis.findings,
        *(PlanFinding("invalid", problem, "plugins") for problem in ctx.plugin_problems),
        *_objective_findings(proposal, ctx, analysis),
    ]
    estimate = estimate_spend(plan, analysis) if analysis.dataset is not None else None
    return DraftValidation(plan, analysis, findings, estimate)


def pending_questions(proposal: DraftProposal, revision: int) -> tuple[PendingQuestion, ...]:
    questions = []
    seen: set[str] = set()
    for q in proposal.questions:
        key = f"{q.prompt}|{'|'.join(q.required_fields)}|{q.blocking_scope}"
        digest = hashlib.sha256(key.encode()).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        questions.append(
            PendingQuestion(
                question_id=f"q-{digest[:12]}",
                prompt=q.prompt,
                required_fields=q.required_fields,
                choices=q.choices,
                blocking_scope=q.blocking_scope,
                draft_revision=revision,
            )
        )
    return tuple(questions)


def draft_document(
    proposal: DraftProposal,
    validation: DraftValidation,
    ctx: DraftContext,
    *,
    planner: PlannerProvenance,
    plan_file: str,
    profile_hash: str,
    dataset_hash: str,
    supersedes: str | None = None,
) -> PlanDraft:
    assert validation.plan is not None
    _, plan_hash = freeze_plan(validation.plan)
    coverage = validation.analysis.coverage if validation.analysis else []
    return PlanDraft(
        plan_id=ctx.plan_id,
        revision=ctx.revision,
        supersedes=supersedes,
        plan_file=plan_file,
        plan_hash=plan_hash,
        executable=validation.executable,
        planner=planner,
        objectives=proposal.objectives,
        rationale=proposal.metrics,
        gaps=proposal.gaps,
        pending_questions=pending_questions(proposal, ctx.revision),
        findings=tuple(f.as_dict() for f in validation.findings),
        coverage=tuple(
            {
                "metric": c.metric,
                "eligible_cases": c.eligible_cases,
                "selected_cases": c.selected_cases,
                "fields": c.fields,
            }
            for c in coverage
        ),
        estimate=validation.estimate,
        profile_hash=profile_hash,
        dataset_hash=dataset_hash,
    )
