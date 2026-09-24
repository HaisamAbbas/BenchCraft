"""Judge calibration against labelled outputs (§23 "Judge and outcome validation", 12-T3).

A calibration set is a JSONL file of labelled cases: an evaluator binding, a reference,
an output, and the label a careful person would give (`acceptable` / `unacceptable`),
grouped by category (correct, subtly wrong, incomplete, verbose, adversarial,
unanswerable, paraphrase, formatting...). Running it measures, per binding:

- agreement: decisions that match the label / decided cases;
- false acceptance: unacceptable outputs the evaluator passed / unacceptable cases;
- false rejection: acceptable outputs the evaluator failed / acceptable cases;
- repeat stability: identical decisions across two runs / cases;

per category too. A case the evaluator did not decide (error, not applicable,
indeterminate) is counted as undecided, never as agreement.

Only evaluators that run in this process are calibrated here (the deterministic native
checks). A model judge's calibration needs live calls; it is reported as not measured
unless such a run is explicitly authorized. Labels are only as good as their reviewer:
the set records its review status and reports never present unreviewed labels as
human-verified.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.models import (
    BenchmarkCase,
    Decision,
    ExecutionResult,
    ExecutionStatus,
    MetricBinding,
    ReferenceAnswer,
)
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext, rule_for
from aibench.registry import EvaluatorRegistry
from aibench.services.scoring import decide

CALIBRATION_SCHEMA = "aibench.judge-calibration/1"


class CalibrationError(AibenchError):
    """A calibration set cannot be loaded or run."""


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    category: str
    metric: str
    params: dict[str, Any]
    reference: str | None
    output: Any
    label: str  # "acceptable" | "unacceptable"

    def binding_key(self) -> str:
        return f"{self.metric} {json.dumps(self.params, sort_keys=True)}"


def load_calibration_set(root: Path) -> tuple[dict[str, Any], list[CalibrationCase]]:
    try:
        meta = json.loads((root / "set.json").read_text(encoding="utf-8"))
        lines = (root / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    except (OSError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"cannot read calibration set {root}: {exc}") from exc
    if not isinstance(meta, dict):
        raise CalibrationError(f"{root}/set.json: metadata must be an object")
    if meta.get("schema") != CALIBRATION_SCHEMA:
        raise CalibrationError(f"{root}: expected schema {CALIBRATION_SCHEMA!r}")
    if not isinstance(meta.get("version"), str) or not meta["version"]:
        raise CalibrationError(f"{root}/set.json: version must be a non-empty string")
    review = meta.get("review", {})
    if not isinstance(review, dict) or not isinstance(review.get("status", "unreviewed"), str):
        raise CalibrationError(f"{root}/set.json: review must be an object with a string status")
    not_measured = meta.get("not_measured", [])
    if not isinstance(not_measured, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("reason"), str)
        for item in not_measured
    ):
        raise CalibrationError(f"{root}/set.json: not_measured must be a list of reason objects")
    cases = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CalibrationError(f"{root}/cases.jsonl:{number}: invalid JSON: {exc}") from exc
        if not isinstance(raw, dict) or raw.get("label") not in ("acceptable", "unacceptable"):
            raise CalibrationError(
                f"{root}/cases.jsonl:{number}: label must be acceptable/unacceptable"
            )
        try:
            case_id = raw["id"]
            category = raw["category"]
            metric = raw["metric"]
            params = raw.get("params", {})
            reference = raw.get("reference")
            if not all(isinstance(value, str) and value for value in (case_id, category, metric)):
                raise TypeError("id, category and metric must be non-empty strings")
            if not isinstance(params, dict):
                raise TypeError("params must be an object")
            if reference is not None and not isinstance(reference, str):
                raise TypeError("reference must be a string or null")
            cases.append(
                CalibrationCase(
                    case_id=case_id,
                    category=category,
                    metric=metric,
                    params=params,
                    reference=reference,
                    output=raw.get("output"),
                    label=raw["label"],
                )
            )
        except KeyError as exc:
            raise CalibrationError(f"{root}/cases.jsonl:{number}: missing {exc}") from exc
        except TypeError as exc:
            raise CalibrationError(f"{root}/cases.jsonl:{number}: {exc}") from exc
    return meta, cases


async def _decide(registry: EvaluatorRegistry, case: CalibrationCase) -> str:
    binding = MetricBinding(metric=case.metric, params=case.params)
    resolved = registry.resolve_binding(binding)
    evaluator = resolved.factory()
    await evaluator.prepare(dict(case.params))
    view = EvaluationView(
        case=BenchmarkCase(
            case_id=case.case_id,
            input="calibration question",
            reference=None if case.reference is None else ReferenceAnswer(answer=case.reference),
        ),
        execution=ExecutionResult(
            execution_id=f"calibration:{case.case_id}",
            run_id="calibration",
            case_id=case.case_id,
            status=ExecutionStatus.OK,
            output=case.output,
        ),
    )
    ctx = EvaluatorContext(
        run_id="calibration",
        scoring_id="calibration",
        write_artifact=lambda data, mime: "calibration-artifact-not-stored",
    )
    try:
        outcome = await evaluator.evaluate(view, ctx)
    finally:
        await evaluator.close()
    decision = decide(outcome.status, outcome.value, rule_for(binding, resolved.manifest))
    return decision.value


def _measure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    decided = [r for r in rows if r["decision"] in (Decision.PASS.value, Decision.FAIL.value)]
    agree = sum(1 for r in decided if (r["decision"] == "pass") == (r["label"] == "acceptable"))
    bad = [r for r in decided if r["label"] == "unacceptable"]
    good = [r for r in decided if r["label"] == "acceptable"]

    def ratio(n: int, d: int) -> dict[str, Any]:
        return {"value": round(n / d, 4) if d else None, "numerator": n, "denominator": d}

    return {
        "cases": len(rows),
        "undecided": len(rows) - len(decided),
        "agreement": ratio(agree, len(decided)),
        "false_acceptance": ratio(sum(1 for r in bad if r["decision"] == "pass"), len(bad)),
        "false_rejection": ratio(sum(1 for r in good if r["decision"] == "fail"), len(good)),
        # trivially 1.0 for deterministic evaluators; informative only for model judges
        "repeat_stability": ratio(sum(1 for r in rows if r["stable"]), len(rows)),
    }


def run_calibration(root: Path, registry: EvaluatorRegistry | None = None) -> dict[str, Any]:
    """Run every case twice and report agreement with the labels, per binding and
    category. Evaluators are native (in-process); nothing leaves the machine."""
    meta, cases = load_calibration_set(root)
    registry = registry or EvaluatorRegistry.with_native()

    async def run_all() -> list[tuple[str, str]]:
        return [(await _decide(registry, c), await _decide(registry, c)) for c in cases]

    decisions = asyncio.run(run_all())
    rows: list[dict[str, Any]] = [
        {
            "id": c.case_id,
            "binding": c.binding_key(),
            "category": c.category,
            "label": c.label,
            "decision": first,
            "stable": first == second,
        }
        for c, (first, second) in zip(cases, decisions, strict=True)
    ]
    by_binding: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_binding[row["binding"]].append(row)
    bindings = {}
    for key, bound in sorted(by_binding.items()):
        categories: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in bound:
            categories[row["category"]].append(row)
        bindings[key] = {
            "overall": _measure(bound),
            "by_category": {name: _measure(r) for name, r in sorted(categories.items())},
            "disagreements": [
                {k: r[k] for k in ("id", "category", "label", "decision")}
                for r in bound
                if r["decision"] in ("pass", "fail")
                and (r["decision"] == "pass") != (r["label"] == "acceptable")
            ],
        }
    review = meta.get("review", {})
    reviewed = review.get("status", "unreviewed")
    return {
        "schema": "aibench.judge-calibration-report/1",
        "set": meta.get("version"),
        "label_review": reviewed,
        "label_note": review.get("notes", ""),
        "bindings": bindings,
        "not_measured": meta.get("not_measured", []),
        "cases": rows,
    }
