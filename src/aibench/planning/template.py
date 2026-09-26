"""Deterministic template planner (07-T3): the no-model baseline and the fallback.

Rules, in order, so the same inputs always give the same draft:

1. Objectives: each user objective's text is matched against a fixed keyword table
   (`catalog.concepts_in`, which skips negated mentions) to find its concepts, plus any
   concepts the user chose for it (the answer to "Which concept does ... mean?"). An
   objective with no concept becomes a gap plus a question (the template does not guess
   meaning; a model planner may interpret free text).
2. For each concept: engine-recorded concepts (latency, reliability) need no metric. Else
   the first *eligible* catalog option for that concept is chosen (native evaluators
   first, then by ID). An option needing parameters the user has not supplied (e.g. a
   JSON Schema) is not chosen; a question asks for them — the template never invents a
   schema or threshold (§3). A scalar metric without a default rule asks for a threshold.
3. No eligible option: an explicit gap cites why each candidate is ineligible, e.g.
   "reads execution.retrieved_context, which the application does not expose".
"""

from __future__ import annotations

import re

from aibench.core.models import DecisionRule
from aibench.planning.catalog import (
    CONCEPTS,
    ENGINE_RECORDED,
    MetricOption,
    concepts_in,
    missing_params,
)
from aibench.planning.drafts import DraftProposal, Gap, MetricChoice, Objective, QuestionProposal


def _objective_id(index: int, text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:24].strip("_")
    return f"o{index}_{slug}" if slug else f"o{index}"


def template_proposal(
    objectives: list[str],
    catalog: list[MetricOption],
    *,
    params: dict[str, dict[str, object]] | None = None,
    rules: dict[str, DecisionRule] | None = None,
    concepts: dict[str, tuple[str, ...]] | None = None,
) -> DraftProposal:
    """`params` and `rules` are what the user supplied, by evaluator ID (e.g. a JSON Schema
    or a threshold); `concepts` maps objective text to concepts the user chose for it.
    Nothing else is filled in."""
    supplied = params or {}
    chosen = concepts or {}
    user_rules = rules or {}
    stated: list[Objective] = []
    metrics: dict[str, MetricChoice] = {}
    gaps: list[Gap] = []
    questions: list[QuestionProposal] = []
    measurable = sorted({c for o in catalog if o.eligible for c in o.concepts} | ENGINE_RECORDED)

    if not objectives:
        questions.append(
            QuestionProposal(
                prompt="What should this benchmark check?",
                required_fields=("objectives",),
                choices=tuple(measurable),
            )
        )
    for index, text in enumerate(objectives, start=1):
        matched = concepts_in(text)
        mapped = (*matched, *(c for c in chosen.get(text, ()) if c not in matched))
        objective = Objective(
            objective_id=_objective_id(index, text), text=text, concepts=mapped, source="user"
        )
        stated.append(objective)
        if not mapped:
            gaps.append(
                Gap(
                    subject=objective.objective_id,
                    reason="the template planner could not map this objective to a known "
                    f"concept ({', '.join(sorted(CONCEPTS))}); choose one or use a model planner",
                )
            )
            questions.append(
                QuestionProposal(
                    prompt=f"Which concept does '{text}' mean?",
                    required_fields=(f"objectives.{objective.objective_id}.concepts",),
                    choices=tuple(measurable),
                    blocking_scope=f"objective:{objective.objective_id}",
                )
            )
            continue
        for concept in mapped:
            if concept in ENGINE_RECORDED:
                continue  # every execution records wall time and errors
            _plan_concept(
                objective, concept, catalog, supplied, user_rules, metrics, gaps, questions
            )

    unique = list({(q.prompt, q.required_fields, q.blocking_scope): q for q in questions}.values())
    return DraftProposal(
        objectives=tuple(stated),
        metrics=tuple(metrics.values()),
        gaps=tuple(gaps),
        questions=tuple(unique),
    )


_GAP_REASON_LIMIT = 1000  # Gap.reason's bound


def _unmeasurable(concept: str, candidates: list[MetricOption]) -> str:
    """Why no candidate can measure `concept`: each candidate and its reasons, as many as
    fit the gap's bound (a plugin can add dozens of candidates), then how many more."""
    head = f"{concept} cannot be measured: "
    parts = [f"{o.metric} {', '.join(o.reasons)}" for o in candidates]
    for shown in range(len(parts), 0, -1):
        more = len(parts) - shown
        reason = head + "; ".join(parts[:shown]) + (f"; and {more} more" if more else "")
        if len(reason) <= _GAP_REASON_LIMIT:
            return reason
    reason = head + f"{len(parts)} candidate metrics are not eligible"
    return reason[:_GAP_REASON_LIMIT]


def _plan_concept(
    objective: Objective,
    concept: str,
    catalog: list[MetricOption],
    supplied: dict[str, dict[str, object]],
    user_rules: dict[str, DecisionRule],
    metrics: dict[str, MetricChoice],
    gaps: list[Gap],
    questions: list[QuestionProposal],
) -> None:
    candidates = [o for o in catalog if concept in o.concepts]
    if not candidates:
        gaps.append(
            Gap(
                subject=objective.objective_id,
                reason=f"no evaluator in the permitted catalog measures {concept}",
            )
        )
        return
    eligible = [o for o in candidates if o.eligible]
    if not eligible:
        gaps.append(Gap(subject=objective.objective_id, reason=_unmeasurable(concept, candidates)))
        return
    for option in eligible:
        params = supplied.get(option.evaluator_id, {})
        missing = missing_params(option.required_params, params)
        if missing:
            questions.append(
                QuestionProposal(
                    prompt=f"{option.metric} needs {' and '.join(m.replace('|', ' or ') for m in missing)} "
                    f"to measure {concept}; please provide it",
                    required_fields=tuple(
                        f"params.{option.evaluator_id}.{alt}"
                        for p in missing
                        for alt in p.split("|")
                    ),
                    blocking_scope=f"objective:{objective.objective_id}",
                )
            )
            continue
        rule = user_rules.get(option.evaluator_id)
        if rule is None and option.default_rule is None and option.value_kind == "scalar":
            questions.append(
                QuestionProposal(
                    prompt=f"What score threshold should {option.metric} require to pass?",
                    required_fields=(f"rule.{option.evaluator_id}.threshold",),
                    blocking_scope=f"objective:{objective.objective_id}",
                )
            )
            continue
        existing = metrics.get(option.metric)
        if existing is not None:
            metrics[option.metric] = existing.model_copy(
                update={"objective_ids": (*existing.objective_ids, objective.objective_id)}
            )
            return
        rationale = f"measures {concept}: {CONCEPTS[concept]} ({option.description})"
        if option.usable_cases is not None:
            rationale += f"; usable in up to {option.usable_cases} dataset case(s)"
        if rule is None and option.default_rule is not None and option.value_kind == "scalar":
            questions.append(
                QuestionProposal(
                    prompt=f"{option.metric} will use its documented default rule "
                    f"{option.default_rule}; confirm or give your own threshold",
                    required_fields=(f"rule.{option.evaluator_id}",),
                    blocking_scope=f"metric:{option.metric}",
                )
            )
        metrics[option.metric] = MetricChoice(
            metric=option.metric,
            params=params,
            rule=rule,
            objective_ids=(objective.objective_id,),
            rationale=rationale,
        )
        return
    gaps.append(
        Gap(
            subject=objective.objective_id,
            reason=f"{concept} needs input only you can supply (see pending questions)",
        )
    )
