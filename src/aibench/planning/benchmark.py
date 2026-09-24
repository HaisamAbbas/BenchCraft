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

- invalid-fixture rejection: fixtures whose plan must be refused (a denied application,
  destination or effect) that are in fact not executable (target: 100%)

These are fixture results, not evidence of broad generalization (§23).

A versioned fixture set (`load_fixture_set`, `run_fixture_set`, `aibench plan benchmark`) is a
directory with `fixtures.json`: the catalog (evaluator manifests, used for planning only:
nothing is ever evaluated), named policies, and the annotated fixtures. Each fixture also
records its family, whether that family is held out from planner development, and its
review status; a report never counts an unreviewed annotation as reviewed.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.models import EvaluatorManifest, ExecutionResult, ExecutionStatus
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
    # Concepts a plan may also measure without being wrong (not required for recall).
    acceptable: frozenset[str] = frozenset()
    # False: the plan must be refused (a denied application, destination or effect).
    executable: bool = True
    holdout: bool = False  # a family the planner was not developed against
    review: str = "unreviewed"  # "unreviewed" | "reviewed" | "adjudicated"


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
    fell_back: bool = False  # a model planner whose draft came from the template instead
    refused_for_permission: bool = False  # not executable because of a permission finding


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
        if not option.eligible:
            uncatalogued.add(option.evaluator_id)  # selected although the catalog refused it
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
    sel_tp = sum(len(r.selected & (r.fixture.select | r.fixture.acceptable)) for r in results)
    sel_all = sum(len(r.selected) for r in results)
    sel_req = sum(len(r.fixture.select) for r in results)
    sel_hit = sum(len(r.selected & r.fixture.select) for r in results)
    gap_tp = sum(len(r.gapped & r.fixture.gaps) for r in results)
    gap_all = sum(len(r.gapped) for r in results)
    gap_req = sum(len(r.fixture.gaps) for r in results)
    valid = [r for r in results if r.fixture.executable]
    first_pass = sum(1 for r in valid if r.executable and r.repairs == 0)
    return PlannerScore(
        fixtures=len(results),
        selection_precision=_ratio(sel_tp, sel_all),
        selection_recall=_ratio(sel_hit, sel_req),
        gap_precision=_ratio(gap_tp, gap_all),
        gap_recall=_ratio(gap_tp, gap_req),
        unnecessary_evaluator_rate=_ratio(
            sum(r.unnecessary for r in results), sum(r.metrics for r in results)
        ),
        unsupported_selections=sum(len(r.forbidden_selected) for r in results),
        first_pass_valid=_ratio(first_pass, len(valid)) or 0.0,
        results=results,
    )


# --------------------------------------------------------------------------- fixture sets

FIXTURE_SET_SCHEMA = "aibench.planner-fixtures/1"
# §23 "Suggested initial targets": engineering targets, not evidence of achieved results.
TARGETS: dict[str, tuple[str, float]] = {
    "selection_precision": (">=", 0.90),
    "selection_recall": (">=", 0.85),
    "first_pass_valid": (">=", 0.95),
    "invalid_rejection": (">=", 1.0),
    "unsupported_selections": ("<=", 0.0),
}


class FixtureSetError(AibenchError):
    """A fixture set cannot be loaded."""


@dataclass
class FixtureSet:
    version: str
    root: Path
    catalog: list[EvaluatorManifest]
    policies: dict[str, dict[str, Any]]
    specs: list[dict[str, Any]]
    reviewers: list[str]
    notes: str

    def fixture(self, spec: dict[str, Any]) -> PlannerFixture:
        expect = spec["expect"]
        return PlannerFixture(
            name=spec["id"],
            category=spec["family"],
            objectives=tuple(spec["objectives"]),
            select=frozenset(expect.get("select", ())),
            gaps=frozenset(expect.get("gaps", ())),
            forbidden=frozenset(expect.get("forbidden", ())),
            acceptable=frozenset(expect.get("acceptable", ())),
            executable=bool(expect.get("executable", True)),
            holdout=bool(spec.get("holdout", False)),
            review=spec.get("review", {}).get("status", "unreviewed"),
        )


def load_fixture_set(root: Path) -> FixtureSet:
    path = root / "fixtures.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureSetError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != FIXTURE_SET_SCHEMA:
        raise FixtureSetError(f"{path}: expected schema {FIXTURE_SET_SCHEMA!r}")
    try:
        version = data["version"]
        catalog_data = data["catalog"]
        policies = data["policies"]
        specs = data["fixtures"]
        if not isinstance(version, str) or not version:
            raise ValueError("version must be a non-empty string")
        if not isinstance(catalog_data, list):
            raise TypeError("catalog must be a list")
        if not isinstance(policies, dict) or not all(
            isinstance(name, str) and isinstance(policy, dict) for name, policy in policies.items()
        ):
            raise TypeError("policies must be an object of named policy objects")
        from aibench.security.policy import ExecutionPolicy

        for name, policy in policies.items():
            try:
                ExecutionPolicy.model_validate(policy)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"policy {name!r} is invalid: {exc}") from exc
        if not isinstance(specs, list):
            raise TypeError("fixtures must be a list")
        reviewers = data.get("reviewers", [])
        if not isinstance(reviewers, list) or not all(isinstance(r, str) for r in reviewers):
            raise TypeError("reviewers must be a list of names")
        notes = data.get("notes", "")
        if not isinstance(notes, str):
            raise TypeError("notes must be a string")

        ids = []
        for number, spec in enumerate(specs, start=1):
            if not isinstance(spec, dict):
                raise TypeError(f"fixture {number} must be an object")
            for key in ("id", "family", "objectives", "app", "dataset", "policy", "expect"):
                if key not in spec:
                    raise FixtureSetError(f"{path}: fixture {number} is missing {key!r}")
            fixture_id = spec["id"]
            if not isinstance(fixture_id, str) or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", fixture_id
            ):
                raise TypeError(f"fixture {number} id must be a safe identifier")
            ids.append(fixture_id)
            if not isinstance(spec["family"], str) or not spec["family"]:
                raise TypeError(f"{fixture_id}: family must be a non-empty string")
            if not isinstance(spec["objectives"], list) or not all(
                isinstance(objective, str) for objective in spec["objectives"]
            ):
                raise TypeError(f"{fixture_id}: objectives must be a list of strings")
            if not isinstance(spec["app"], dict):
                raise TypeError(f"{fixture_id}: app must be an object")
            runner = spec["app"].get("runner", "http")
            if runner not in ("http", "cli"):
                raise ValueError(f"{fixture_id}: app.runner must be http or cli")
            if "cli" in spec["app"] and not isinstance(spec["app"]["cli"], dict):
                raise TypeError(f"{fixture_id}: app.cli must be an object")
            if "output_binding" in spec["app"] and not isinstance(
                spec["app"]["output_binding"], dict
            ):
                raise TypeError(f"{fixture_id}: app.output_binding must be an object")
            if not isinstance(spec["dataset"], list) or not all(
                isinstance(group, dict) for group in spec["dataset"]
            ):
                raise TypeError(f"{fixture_id}: dataset must be a list of row-group objects")
            for group in spec["dataset"]:
                fields = group.get("fields", {})
                count = group.get("count", 1)
                if not isinstance(fields, dict):
                    raise TypeError(f"{fixture_id}: dataset row-group fields must be an object")
                if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                    raise ValueError(
                        f"{fixture_id}: dataset row-group count must be a positive integer"
                    )
            if not isinstance(spec["policy"], str):
                raise TypeError(f"{fixture_id}: policy must name a policy")
            if not isinstance(spec["expect"], dict):
                raise TypeError(f"{fixture_id}: expect must be an object")
            for key in ("select", "gaps", "forbidden", "acceptable"):
                values = spec["expect"].get(key, [])
                if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                    raise TypeError(f"{fixture_id}: expect.{key} must be a list of concepts")
            if "executable" in spec["expect"] and not isinstance(
                spec["expect"]["executable"], bool
            ):
                raise TypeError(f"{fixture_id}: expect.executable must be a boolean")
            if "holdout" in spec and not isinstance(spec["holdout"], bool):
                raise TypeError(f"{fixture_id}: holdout must be a boolean")
            review = spec.get("review", {})
            if not isinstance(review, dict) or not isinstance(
                review.get("status", "unreviewed"), str
            ):
                raise TypeError(f"{fixture_id}: review must be an object with a string status")
            if spec["policy"] not in policies:
                raise FixtureSetError(f"{path}: {fixture_id}: unknown policy {spec['policy']!r}")
        if len(ids) != len(set(ids)):
            raise FixtureSetError(f"{path}: duplicate fixture ids")
        return FixtureSet(
            version=version,
            root=root,
            catalog=[EvaluatorManifest.model_validate(m) for m in catalog_data],
            policies=policies,
            specs=specs,
            reviewers=reviewers,
            notes=notes,
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise FixtureSetError(f"{path}: invalid fixture set: {exc}") from exc


def _rows(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """The fixture's dataset: each group is `count` rows sharing its `fields`."""
    rows: list[dict[str, Any]] = []
    for group in spec["dataset"]:
        for _ in range(int(group.get("count", 1))):
            number = len(rows)
            rows.append(
                {
                    "case_id": f"{group.get('prefix', 'c')}{number}",
                    "input": group.get("input", f"question {number}"),
                    **group.get("fields", {}),
                }
            )
    return rows


def _application(spec: dict[str, Any]) -> dict[str, Any]:
    app = spec["app"]
    runner = app.get("runner", "http")
    url = app.get("url", "http://127.0.0.1:9/answer")
    transport: dict[str, Any] = (
        {"kind": "http", "url": url}
        if runner == "http"
        else {"kind": "cli", "argv": ["python", "app.py"], **app.get("cli", {})}
    )
    return {
        "application_id": app.get("application_id", spec["id"].replace("_", "-")),
        "runner": runner,
        "target": url if runner == "http" else "app.py",
        "effects": app.get("effects", "none"),
        "transport": transport,
        "output_binding": app.get("output_binding", {"output": "/output"}),
    }


def _recorded(spec: dict[str, Any], run_id: str) -> list[ExecutionResult]:
    """Recorded executions for the profile, e.g. a declared retriever that returned
    nothing at runtime."""
    out = []
    for index, record in enumerate(spec.get("recorded_executions", ())):
        out.append(
            ExecutionResult(
                execution_id=f"{run_id}:c{index}:r0:a1",
                run_id=run_id,
                case_id=f"c{index}",
                attempt_id=1,
                status=ExecutionStatus.OK,
                output=record.get("output", "answer"),
                retrieved_context=(
                    tuple(record["retrieved_context"]) if "retrieved_context" in record else None
                ),
                tool_events=tuple(record.get("tool_events", ())),
                # what a runner records for each bound field (runners/bindings.py)
                observation_completeness={
                    name: {"state": "observed", "detail": "present" if record[name] else "empty"}
                    for name in ("retrieved_context", "tool_events")
                    if name in record
                },
            )
        )
    return out


Planner = Callable[[Any], Any]  # PlanningInputs -> PlanningOutcome


def run_fixture_set(
    fixture_set: FixtureSet,
    planner: Planner,
    *,
    planner_name: str,
    workdir: Path | None = None,
) -> dict[str, Any]:
    """Plan every fixture with `planner` and score it. Nothing is executed: evaluators in
    the catalog are manifests only, and applications are never contacted."""
    from aibench.evaluators.worker_client import WorkerSpec
    from aibench.inspection.dataset_summary import summarize_dataset
    from aibench.inspection.profile import inspect_application
    from aibench.planning.catalog import build_catalog
    from aibench.planning.drafts import DraftContext
    from aibench.planning.planner import PlanningInputs
    from aibench.registry import EvaluatorRegistry
    from aibench.security.policy import ExecutionPolicy

    never = WorkerSpec(python=Path("catalog-entry-never-started"), target="catalog:none")
    results: list[FixtureResult] = []
    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        for index, spec in enumerate(fixture_set.specs):
            fixture = fixture_set.fixture(spec)
            # Fixture IDs are user-controlled labels. Never join them to the temporary
            # path: an absolute ID or `..` could otherwise write app/data files outside it.
            root = Path(tmp) / f"fixture-{index:04d}"
            root.mkdir()
            (root / "app.json").write_text(json.dumps(_application(spec)), encoding="utf-8")
            dataset = root / "data.jsonl"
            dataset.write_text(
                "\n".join(json.dumps(r) for r in _rows(spec)) + "\n", encoding="utf-8"
            )
            registry = EvaluatorRegistry.with_native()
            for manifest in fixture_set.catalog:
                registry.register_external(manifest, worker=never)
            policy = ExecutionPolicy.model_validate(fixture_set.policies[spec["policy"]])
            profile = inspect_application(
                root / "app.json", executions=_recorded(spec, f"bench-{spec['id']}")
            )
            summary = summarize_dataset(dataset)
            context = DraftContext(
                plan_id=spec["id"],
                out_dir=root,
                dataset=dataset,
                application=root / "app.json",
                policy=policy,
                registry=registry,
                user_objectives=tuple(spec["objectives"]),
            )
            inputs = PlanningInputs(
                list(spec["objectives"]),
                profile,
                summary,
                build_catalog(registry, profile, summary, policy),
                context,
            )
            outcome = planner(inputs)
            result = assess(
                fixture,
                outcome.proposal,
                inputs.catalog,
                executable=outcome.validation.executable,
                repairs=outcome.provenance.repairs,
            )
            result.fell_back = outcome.provenance.fallback_reason is not None
            result.refused_for_permission = any(
                f.blocking and f.kind == "missing_permission" for f in outcome.validation.findings
            )
            results.append(result)
            rows.append(_fixture_row(result, outcome))
    return benchmark_report(fixture_set, results, rows, planner_name=planner_name)


def _fixture_row(result: FixtureResult, outcome: Any) -> dict[str, Any]:
    f = result.fixture
    return {
        "id": f.name,
        "family": f.category,
        "holdout": f.holdout,
        "review": f.review,
        "expected": {
            "select": sorted(f.select),
            "gaps": sorted(f.gaps),
            "acceptable": sorted(f.acceptable),
            "executable": f.executable,
        },
        "fell_back": result.fell_back,
        "observed": {
            "select": sorted(result.selected),
            "gaps": sorted(result.gapped),
            "executable": result.executable,
            "forbidden_selected": sorted(result.forbidden_selected),
            "metrics": [m.metric for m in outcome.proposal.metrics],
            "repairs": result.repairs,
            "fallback": outcome.provenance.fallback_reason,
        },
        "correct": (
            result.selected <= (f.select | f.acceptable)
            and f.select <= result.selected
            and result.gapped == f.gaps
            and not result.forbidden_selected
            and result.executable == f.executable
        ),
    }


def wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    """95% Wilson score interval for a proportion; None without observations."""
    if total == 0:
        return None
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _counts(results: Sequence[FixtureResult]) -> dict[str, Any]:
    sel_all = sum(len(r.selected) for r in results)
    sel_ok = sum(len(r.selected & (r.fixture.select | r.fixture.acceptable)) for r in results)
    sel_req = sum(len(r.fixture.select) for r in results)
    sel_hit = sum(len(r.selected & r.fixture.select) for r in results)
    gap_all = sum(len(r.gapped) for r in results)
    gap_req = sum(len(r.fixture.gaps) for r in results)
    gap_hit = sum(len(r.gapped & r.fixture.gaps) for r in results)
    valid = [r for r in results if r.fixture.executable]
    first_pass = sum(1 for r in valid if r.executable and r.repairs == 0)
    repaired = sum(1 for r in valid if r.executable and r.repairs > 0)
    invalid = [r for r in results if not r.fixture.executable]
    # a correct refusal names the missing permission; failing for another reason is not one
    rejected = sum(1 for r in invalid if not r.executable and r.refused_for_permission)

    def measure(numerator: int, denominator: int) -> dict[str, Any]:
        return {
            "value": _ratio(numerator, denominator),
            "numerator": numerator,
            "denominator": denominator,
            "wilson95": wilson(numerator, denominator),
        }

    return {
        "fixtures": len(results),
        "selection_precision": measure(sel_ok, sel_all),
        "selection_recall": measure(sel_hit, sel_req),
        "gap_precision": measure(gap_hit, gap_all),
        "gap_recall": measure(gap_hit, gap_req),
        "unnecessary_evaluator_rate": measure(
            sum(r.unnecessary for r in results), sum(r.metrics for r in results)
        ),
        "first_pass_valid": measure(first_pass, len(valid)),
        "repaired_valid": measure(repaired, len(valid)),
        "invalid_rejection": measure(rejected, len(invalid)),
        "unsupported_selections": sum(len(r.forbidden_selected) for r in results),
        # template fallbacks by a model planner; a refused plan may not brief a model at all
        "fallbacks": sum(1 for r in results if r.fell_back),
        "fallbacks_on_executable_fixtures": sum(
            1 for r in results if r.fell_back and r.fixture.executable
        ),
    }


def benchmark_report(
    fixture_set: FixtureSet,
    results: list[FixtureResult],
    rows: list[dict[str, Any]],
    *,
    planner_name: str,
) -> dict[str, Any]:
    overall = _counts(results)
    families = sorted({r.fixture.category for r in results})
    reviewed = sum(1 for r in results if r.fixture.review in ("reviewed", "adjudicated"))
    targets = []
    tainted = overall["fallbacks_on_executable_fixtures"]
    for name, (comparator, target) in TARGETS.items():
        measured = overall[name]
        value = measured if isinstance(measured, int) else measured["value"]
        if tainted:
            status = f"not measured: {tainted} fixture(s) fell back to the template"
        elif value is None:
            status = "not measured"
        else:
            met = value >= target if comparator == ">=" else value <= target
            status = "met" if met else "not met"
        targets.append(
            {
                "measure": name,
                "target": f"{comparator} {target}",
                "observed": value,
                "status": status,
            }
        )
    return {
        "schema": "aibench.planner-benchmark/1",
        "fixture_set": fixture_set.version,
        "planner": planner_name,
        "overall": overall,
        "development_families": _counts([r for r in results if not r.fixture.holdout]),
        "holdout_families": _counts([r for r in results if r.fixture.holdout]),
        "by_family": {
            f: _counts([r for r in results if r.fixture.category == f]) for f in families
        },
        "targets": targets,
        "review": {
            "reviewed_fixtures": reviewed,
            "fixtures": len(results),
            "reviewers": fixture_set.reviewers,
            "status": "unverified: no fixture has been reviewed"
            if reviewed == 0
            else f"{reviewed}/{len(results)} fixtures reviewed",
            "notes": fixture_set.notes,
        },
        "warnings": (
            [
                (
                    f"{overall['fallbacks']} of {len(results)} fixture(s) were planned by the "
                    "template fallback, not by the planner under test"
                )
            ]
            if overall["fallbacks"]
            else []
        ),
        "limitations": [
            (
                "gap and unnecessary-evaluator scoring use the concepts the planner assigns "
                "to its own objectives"
            ),
            "fixture results, not evidence of broad generalization (§23)",
            "targets are engineering targets to revise before testing, not claims of success",
            "catalog evaluators are manifests only; no metric was computed",
        ],
        "fixtures": rows,
    }
