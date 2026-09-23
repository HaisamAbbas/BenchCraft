"""Measure a planner against annotated fixtures (§23 "Benchmark the planner itself").

A fixture states, per concept, what a correct plan does: select a metric for it (the
evidence supports it), or report it as a gap (it cannot be measured honestly), plus
metrics that must never be chosen. Scores follow §23's definitions, over concepts rather
than exact framework names:

- selection precision: appropriate selected concepts / all selected concepts
- selection recall: required available concepts covered / required available concepts
- gap precision/recall: over concepts reported as gaps
- unnecessary evaluator rate: selected metrics serving none of their objectives' concepts
  / all selected metrics (their concepts also count against selection precision)
- unsupported selections: forbidden metrics, or metrics not in the catalog, actually
  selected (target: zero)
- first-pass validity: drafts executable without repair

These are fixture results, not evidence of broad generalization (§23).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aibench.planning.catalog import MetricOption
from aibench.planning.drafts import DraftProposal


@dataclass(frozen=True)
class PlannerFixture:
    name: str
    category: str  # chatbot, rag, agent, blackbox, partial, misleading, policy, unmapped...
    objectives: tuple[str, ...]
    select: frozenset[str]  # concepts a correct plan measures with a metric
    gaps: frozenset[str]  # concepts a correct plan reports as unmeasurable
    forbidden: frozenset[str] = frozenset()  # evaluator ids that must not be selected


@dataclass
class FixtureResult:
    fixture: PlannerFixture
    selected: set[str]
    gapped: set[str]
    forbidden_selected: set[str]
    executable: bool
    repairs: int
    metrics: int = 0
    unnecessary: int = 0


@dataclass
class PlannerScore:
    fixtures: int
    selection_precision: float | None
    selection_recall: float | None
    gap_precision: float | None
    gap_recall: float | None
    unnecessary_evaluator_rate: float | None
    unsupported_selections: int
    first_pass_valid: float
    results: list[FixtureResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "fixtures": self.fixtures,
            "selection_precision": self.selection_precision,
            "selection_recall": self.selection_recall,
            "gap_precision": self.gap_precision,
            "gap_recall": self.gap_recall,
            "unnecessary_evaluator_rate": self.unnecessary_evaluator_rate,
            "unsupported_selections": self.unsupported_selections,
            "first_pass_valid": self.first_pass_valid,
        }


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def assess(
    fixture: PlannerFixture,
    proposal: DraftProposal,
    catalog: list[MetricOption],
    *,
    executable: bool,
    repairs: int = 0,
) -> FixtureResult:
    by_metric = {o.metric: o for o in catalog} | {o.evaluator_id: o for o in catalog}
    objective_concepts = {o.objective_id: set(o.concepts) for o in proposal.objectives}
    selected: set[str] = set()
    chosen_ids: set[str] = set()
    uncatalogued: set[str] = set()
    unnecessary = 0
    for choice in proposal.metrics:
        option = by_metric.get(choice.metric) or by_metric.get(choice.metric.split("@")[0])
        if option is None:
            uncatalogued.add(choice.metric)
            continue
        chosen_ids.add(option.evaluator_id)
        served = set().union(*(objective_concepts.get(i, set()) for i in choice.objective_ids))
        if not set(option.concepts) & served:
            unnecessary += 1
        selected |= set(option.concepts)
    gapped: set[str] = set()
    for gap in proposal.gaps:
        concepts = objective_concepts.get(gap.subject, {gap.subject})
        if not concepts:  # an objective no known concept covers
            gapped.add("unmapped")
            continue
        gapped |= concepts - selected if concepts - selected else concepts
    return FixtureResult(
        fixture=fixture,
        selected=selected,
        gapped=gapped,
        forbidden_selected=(chosen_ids & fixture.forbidden) | uncatalogued,
        executable=executable,
        repairs=repairs,
        metrics=len(proposal.metrics),
        unnecessary=unnecessary,
    )


def score(results: list[FixtureResult]) -> PlannerScore:
    sel_tp = sum(len(r.selected & r.fixture.select) for r in results)
    sel_all = sum(len(r.selected) for r in results)
    sel_req = sum(len(r.fixture.select) for r in results)
    gap_tp = sum(len(r.gapped & r.fixture.gaps) for r in results)
    gap_all = sum(len(r.gapped) for r in results)
    gap_req = sum(len(r.fixture.gaps) for r in results)
    first_pass = sum(1 for r in results if r.executable and r.repairs == 0)
    return PlannerScore(
        fixtures=len(results),
        selection_precision=_ratio(sel_tp, sel_all),
        selection_recall=_ratio(sel_tp, sel_req),
        gap_precision=_ratio(gap_tp, gap_all),
        gap_recall=_ratio(gap_tp, gap_req),
        unnecessary_evaluator_rate=_ratio(
            sum(r.unnecessary for r in results), sum(r.metrics for r in results)
        ),
        unsupported_selections=sum(len(r.forbidden_selected) for r in results),
        first_pass_valid=_ratio(first_pass, len(results)) or 0.0,
        results=results,
    )
