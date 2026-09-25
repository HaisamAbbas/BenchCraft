"""Evidence-aware objective-to-metric recommendations (Prompt 26-T1).

Eligibility is delegated to the existing evaluator catalog, which already checks policy,
application claims, and dataset field coverage. This module presents that decision with the
same deterministic concept mapping used by the template planner; it does not execute metrics
or read dataset values into the report.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from aibench.core.models import FrozenModel, ObservationState, deep_unfreeze
from aibench.planning.catalog import ENGINE_RECORDED, MetricOption, missing_params
from aibench.planning.drafts import QuestionProposal
from aibench.planning.planner import PlanningInputs
from aibench.planning.template import template_proposal


class RequirementEvidence(FrozenModel):
    path: str
    state: Literal["available", "missing", "unknown"]
    detail: str
    evidence_refs: tuple[str, ...] = ()


class MetricOpportunity(FrozenModel):
    metric: str
    evaluator_id: str
    description: str
    state: Literal["available", "requires_input", "unavailable"]
    recommended: bool
    requirements: tuple[RequirementEvidence, ...]
    usable_cases: int | None = None
    reasons: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()


class ConceptOpportunity(FrozenModel):
    concept: str
    state: Literal["available", "requires_input", "unavailable"]
    recommended_metric: str | None = None
    metrics: tuple[MetricOpportunity, ...] = ()
    gap: str | None = None
    note: str | None = None


class ObjectiveOpportunity(FrozenModel):
    objective_id: str
    objective: str
    state: Literal["available", "partial", "requires_input", "unavailable", "unknown"]
    concepts: tuple[ConceptOpportunity, ...] = ()
    questions: tuple[str, ...] = ()


ObjectiveState = Literal["available", "partial", "requires_input", "unavailable", "unknown"]
ConceptState = Literal["available", "requires_input", "unavailable"]


class OpportunityReport(FrozenModel):
    schema_version: str = "1.0.0"
    dataset_hash: str
    dataset_cases: int
    objectives: tuple[ObjectiveOpportunity, ...] = ()
    questions: tuple[str, ...] = ()
    limitations: tuple[str, ...] = (
        "objective concepts use the deterministic planner vocabulary and may need user clarification",
        "eligibility uses declared or observed application evidence; source-code inferences do not unlock metrics",
        "dataset references remain judge-only; the report includes field counts, never case values",
        "eligible means the metric's declared inputs and policy are available, not that a run has succeeded",
    )


def _requirement_evidence(path: str, inputs: PlanningInputs) -> RequirementEvidence:
    if path == "execution.output":
        return RequirementEvidence(
            path=path,
            state="available",
            detail="the configured runner captures its output for evaluation",
        )
    if path.startswith("execution."):
        capability = path.removeprefix("execution.")
        claim = inputs.profile.claim(capability)
        if claim is not None and claim.state in (
            ObservationState.DECLARED,
            ObservationState.OBSERVED,
        ):
            return RequirementEvidence(
                path=path,
                state="available",
                detail=f"application evidence is {claim.state.value}",
                evidence_refs=claim.evidence_refs,
            )
        state = claim.state.value if claim is not None else "unknown"
        return RequirementEvidence(
            path=path,
            state="missing",
            detail=f"the application does not declare or expose this field (profile state: {state})",
            evidence_refs=claim.evidence_refs if claim is not None else (),
        )
    if path.startswith("case."):
        coverage = next((item for item in inputs.dataset.fields if item.path == path), None)
        usable = inputs.dataset.usable(path)
        total = inputs.dataset.case_count
        if usable:
            return RequirementEvidence(
                path=path,
                state="available",
                detail=f"usable in {usable} of {total} dataset case(s)",
                evidence_refs=(f"dataset-field:{path}",),
            )
        detail = (
            "no cases"
            if coverage is None or total == 0
            else f"usable in 0 of {total} dataset case(s)"
        )
        return RequirementEvidence(
            path=path,
            state="missing",
            detail=detail,
            evidence_refs=(f"dataset-field:{path}",),
        )
    return RequirementEvidence(
        path=path,
        state="unknown",
        detail="the evaluator declares a requirement the opportunity mapper does not classify",
    )


def _metric_opportunity(
    option: MetricOption,
    *,
    inputs: PlanningInputs,
    selected_metrics: set[str],
    questions: tuple[QuestionProposal, ...],
) -> MetricOpportunity:
    params = deep_unfreeze(inputs.context.user_params.get(option.evaluator_id, {})) or {}
    absent_params = missing_params(option.required_params, params)
    needs_user_input = any(
        field.startswith(f"params.{option.evaluator_id}.")
        or field == f"rule.{option.evaluator_id}.threshold"
        for question in questions
        for field in question.required_fields
    )
    reasons = list(option.reasons)
    if absent_params:
        reasons.append("user input required: " + ", ".join(absent_params))
    elif needs_user_input:
        reasons.append("required pass/fail rule input is missing")
    if not option.eligible:
        state: Literal["available", "requires_input", "unavailable"] = "unavailable"
    elif absent_params or needs_user_input:
        state = "requires_input"
    else:
        state = "available"
    return MetricOpportunity(
        metric=option.metric,
        evaluator_id=option.evaluator_id,
        description=option.description,
        state=state,
        recommended=option.metric in selected_metrics,
        requirements=tuple(_requirement_evidence(path, inputs) for path in option.requires),
        usable_cases=option.usable_cases,
        reasons=tuple(reasons),
        limitations=option.limitations,
    )


def discover_opportunities(
    inputs: PlanningInputs,
    *,
    objective_concepts: Mapping[str, tuple[str, ...]] | None = None,
) -> OpportunityReport:
    """Map stated objectives using the same eligibility and concept rules as plan drafting."""
    params = deep_unfreeze(inputs.context.user_params) or {}
    rules = dict(inputs.context.user_rules)
    proposal = template_proposal(
        inputs.objectives,
        inputs.catalog,
        params=params,
        rules=rules,
        concepts=dict(objective_concepts or {}),
    )
    selected_by_objective: dict[str, set[str]] = {}
    for metric in proposal.metrics:
        for objective_id in metric.objective_ids:
            selected_by_objective.setdefault(objective_id, set()).add(metric.metric)

    opportunity_by_objective: list[ObjectiveOpportunity] = []
    for objective in proposal.objectives:
        selected = selected_by_objective.get(objective.objective_id, set())
        objective_metrics = {
            item.metric for item in proposal.metrics if objective.objective_id in item.objective_ids
        }
        related_questions = tuple(
            question
            for question in proposal.questions
            if question.blocking_scope == f"objective:{objective.objective_id}"
            or question.blocking_scope in {f"metric:{metric}" for metric in objective_metrics}
        )
        concepts: list[ConceptOpportunity] = []
        for concept in objective.concepts:
            if concept in ENGINE_RECORDED:
                concepts.append(
                    ConceptOpportunity(
                        concept=concept,
                        state="available",
                        note="recorded by the harness for every execution",
                    )
                )
                continue
            options = tuple(option for option in inputs.catalog if concept in option.concepts)
            metrics = tuple(
                _metric_opportunity(
                    option,
                    inputs=inputs,
                    selected_metrics=selected,
                    questions=proposal.questions,
                )
                for option in options
            )
            selected_metric = next((item.metric for item in metrics if item.recommended), None)
            if not options or not any(option.eligible for option in options):
                concept_state: ConceptState = "unavailable"
            elif any(item.state == "available" for item in metrics):
                concept_state = "available"
            elif any(item.state == "requires_input" for item in metrics):
                concept_state = "requires_input"
            else:
                concept_state = "unavailable"
            if concept_state == "unavailable" and not options:
                gap = f"no evaluator in the permitted catalog measures {concept}"
            elif concept_state == "unavailable":
                unavailable_reasons = [
                    f"{option.metric}: {', '.join(option.reasons)}"
                    for option in options
                    if not option.eligible
                ]
                gap = f"{concept} cannot be measured: " + "; ".join(unavailable_reasons)
            elif concept_state == "requires_input":
                input_reasons = [
                    f"{metric.metric}: {', '.join(metric.reasons)}"
                    for metric in metrics
                    if metric.state == "requires_input"
                ]
                gap = f"{concept} needs user input: " + "; ".join(input_reasons)
            else:
                gap = None
            concepts.append(
                ConceptOpportunity(
                    concept=concept,
                    state=concept_state,
                    recommended_metric=selected_metric,
                    metrics=metrics,
                    gap=gap,
                )
            )

        states = {item.state for item in concepts}
        if not concepts:
            objective_state: ObjectiveState = "unknown"
        elif states == {"available"}:
            objective_state = "available"
        elif "available" in states:
            objective_state = "partial"
        elif "requires_input" in states:
            objective_state = "requires_input"
        else:
            objective_state = "unavailable"
        opportunity_by_objective.append(
            ObjectiveOpportunity(
                objective_id=objective.objective_id,
                objective=objective.text,
                state=objective_state,
                concepts=tuple(concepts),
                questions=tuple(question.prompt for question in related_questions),
            )
        )
    return OpportunityReport(
        dataset_hash=inputs.dataset.content_hash,
        dataset_cases=inputs.dataset.case_count,
        objectives=tuple(opportunity_by_objective),
        questions=tuple(question.prompt for question in proposal.questions),
    )
