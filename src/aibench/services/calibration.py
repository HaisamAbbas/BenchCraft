"""Judge calibration against labelled outputs (§23 "Judge and outcome validation", 12-T3).

A calibration set is a JSONL file of labelled cases: an evaluator binding, the case input,
reference, recorded output and optional retrieved context/tool events, plus the label a
careful person would give (`acceptable` / `unacceptable`), grouped by category. Running it
measures, per binding:

- agreement: decisions that match the label / decided cases;
- false acceptance: unacceptable outputs the evaluator passed / unacceptable cases;
- false rejection: acceptable outputs the evaluator failed / acceptable cases;
- repeat stability: cases with the same decided result across every repeat / cases;

per category too. Agreement and error rates count each decided observation; an error,
not-applicable or indeterminate result is counted as undecided, never as agreement.

The offline CLI uses deterministic native checks. Worker-backed model evaluators require an
explicit service opt-in and reviewed labels with a named reviewer; the CLI additionally
requires execution-policy approval and caps model-evaluator invocations. Labels are only as
good as their reviewer: reports carry the review status.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from aibench.core.errors import AibenchError
from aibench.core.models import (
    BenchmarkCase,
    Decision,
    ExecutionResult,
    ExecutionStatus,
    MetricBinding,
    ReferenceAnswer,
)
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext, UsageReport, rule_for
from aibench.registry import EvaluatorRegistry, ResolvedMetric
from aibench.services.scoring import decide

CALIBRATION_SCHEMA = "aibench.judge-calibration/1"
MAX_CALIBRATION_CASES = 1_000


class CalibrationError(AibenchError):
    """A calibration set cannot be loaded or run."""


@dataclass(frozen=True)
class CalibrationCase:
    case_id: str
    category: str
    metric: str
    params: dict[str, Any]
    input: Any
    has_input: bool
    reference: ReferenceAnswer | None
    output: Any
    retrieved_context: tuple[str, ...] | None
    tool_events: tuple[Any, ...] | None
    metadata: dict[str, Any]
    expectations: dict[str, Any]
    label: str  # "acceptable" | "unacceptable"

    def binding_key(self) -> str:
        return f"{self.metric} {json.dumps(self.params, sort_keys=True)}"


def load_calibration_set(root: Path) -> tuple[dict[str, Any], list[CalibrationCase]]:
    try:
        meta = json.loads((root / "set.json").read_text(encoding="utf-8"))
        lines = (root / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CalibrationError(f"cannot read calibration set {root}: {exc}") from exc
    if not isinstance(meta, dict):
        raise CalibrationError(f"{root}/set.json: metadata must be an object")
    if meta.get("schema") != CALIBRATION_SCHEMA:
        raise CalibrationError(f"{root}: expected schema {CALIBRATION_SCHEMA!r}")
    if not isinstance(meta.get("version"), str) or not meta["version"]:
        raise CalibrationError(f"{root}/set.json: version must be a non-empty string")
    review = meta.get("review", {})
    if not isinstance(review, dict) or review.get("status", "unreviewed") not in (
        "unreviewed",
        "reviewed",
    ):
        raise CalibrationError(f"{root}/set.json: review.status must be 'unreviewed' or 'reviewed'")
    reviewer = review.get("reviewer")
    if reviewer is not None and (not isinstance(reviewer, str) or not reviewer.strip()):
        raise CalibrationError(f"{root}/set.json: review.reviewer must be a non-empty string")
    not_measured = meta.get("not_measured", [])
    if not isinstance(not_measured, list) or any(
        not isinstance(item, dict) or not isinstance(item.get("reason"), str)
        for item in not_measured
    ):
        raise CalibrationError(f"{root}/set.json: not_measured must be a list of reason objects")
    cases: list[CalibrationCase] = []
    seen_ids: set[str] = set()
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
            raw_reference = raw.get("reference")
            if not all(isinstance(value, str) and value for value in (case_id, category, metric)):
                raise TypeError("id, category and metric must be non-empty strings")
            if case_id in seen_ids:
                raise TypeError(f"duplicate case id {case_id!r}")
            if len(cases) >= MAX_CALIBRATION_CASES:
                raise TypeError(f"calibration set exceeds the {MAX_CALIBRATION_CASES}-case limit")
            seen_ids.add(case_id)
            if not isinstance(params, dict):
                raise TypeError("params must be an object")
            if raw_reference is None:
                reference = None
            elif isinstance(raw_reference, str):
                reference = ReferenceAnswer(answer=raw_reference)
            elif isinstance(raw_reference, dict):
                reference = ReferenceAnswer.model_validate(raw_reference)
            else:
                raise TypeError("reference must be a string, object, or null")
            retrieved_context = raw.get("retrieved_context")
            if retrieved_context is not None and (
                not isinstance(retrieved_context, list)
                or any(not isinstance(item, str) for item in retrieved_context)
            ):
                raise TypeError("retrieved_context must be an array of strings or null")
            tool_events = raw.get("tool_events")
            if tool_events is not None and not isinstance(tool_events, list):
                raise TypeError("tool_events must be an array or null")
            metadata = raw.get("metadata", {})
            expectations = raw.get("expectations", {})
            if not isinstance(metadata, dict) or not isinstance(expectations, dict):
                raise TypeError("metadata and expectations must be objects")
            cases.append(
                CalibrationCase(
                    case_id=case_id,
                    category=category,
                    metric=metric,
                    params=params,
                    input=raw.get("input"),
                    has_input="input" in raw,
                    reference=reference,
                    output=raw.get("output"),
                    retrieved_context=(
                        None if retrieved_context is None else tuple(retrieved_context)
                    ),
                    tool_events=None if tool_events is None else tuple(tool_events),
                    metadata=metadata,
                    expectations=expectations,
                    label=raw["label"],
                )
            )
            _evaluation_view(cases[-1])
        except KeyError as exc:
            raise CalibrationError(f"{root}/cases.jsonl:{number}: missing {exc}") from exc
        except (TypeError, ValidationError) as exc:
            raise CalibrationError(f"{root}/cases.jsonl:{number}: {exc}") from exc
    return meta, cases


def _evaluation_view(case: CalibrationCase) -> EvaluationView:
    observed = {}
    if case.retrieved_context is not None:
        observed["retrieved_context"] = {"state": "observed"}
    if case.tool_events is not None:
        observed["tool_events"] = {"state": "observed"}
    return EvaluationView(
        case=BenchmarkCase(
            case_id=case.case_id,
            input=case.input,
            reference=case.reference,
            expectations=case.expectations,
            metadata=case.metadata,
        ),
        execution=ExecutionResult(
            execution_id=f"calibration:{case.case_id}",
            run_id="calibration",
            case_id=case.case_id,
            status=ExecutionStatus.OK,
            output=case.output,
            retrieved_context=case.retrieved_context,
            tool_events=case.tool_events or (),
            observation_completeness=observed,
        ),
    )


def _usage_rows(usage: list[UsageReport]) -> list[dict[str, Any]]:
    return [
        {"provider": item.provider, "calls": item.calls, "tokens": item.tokens, "cost": item.cost}
        for item in usage
    ]


async def _decide(
    case: CalibrationCase,
    resolved: ResolvedMetric,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    binding = MetricBinding(metric=case.metric, params=case.params)
    view = _evaluation_view(case)
    for requirement in resolved.requirements:
        state = (
            "missing"
            if requirement.path == "case.input" and not case.has_input
            else view.state(requirement.path)
        )
        if state == "missing" or (state == "empty" and requirement.non_empty):
            return {
                "decision": Decision.NOT_EVALUATED.value,
                "reason": f"{state}:{requirement.path}",
                "usage": [],
                "evaluated": False,
            }
    try:
        evaluator = resolved.factory()
    except Exception as exc:  # noqa: BLE001 -- one bad factory must not discard other cases
        return {
            "decision": Decision.NOT_EVALUATED.value,
            "reason": type(exc).__name__,
            "error_type": type(exc).__name__,
            "usage": [],
            "evaluated": False,
        }
    outcome = None
    error_type = None
    ctx = EvaluatorContext(
        run_id="calibration",
        scoring_id="calibration",
        write_artifact=lambda data, mime: "calibration-artifact-not-stored",
    )
    evaluated = False
    try:
        await evaluator.prepare(dict(case.params))
        evaluated = True
        outcome = await asyncio.wait_for(evaluator.evaluate(view, ctx), timeout=timeout_seconds)
    except TimeoutError:
        error_type = "TimeoutError"
    except Exception as exc:  # noqa: BLE001 -- retain later observations after judge failures
        error_type = type(exc).__name__
    finally:
        try:
            await evaluator.close()
        except Exception as exc:  # noqa: BLE001 -- close warnings must not abort this calibration
            if error_type is None:
                error_type = f"close:{type(exc).__name__}"
    if outcome is None:
        return {
            "decision": Decision.NOT_EVALUATED.value,
            "reason": error_type or "evaluation failed",
            "error_type": error_type,
            "usage": _usage_rows(ctx.usage),
            "evaluated": evaluated,
        }
    try:
        decision = decide(outcome.status, outcome.value, rule_for(binding, resolved.manifest))
    except (AttributeError, TypeError, ValueError):
        decision = Decision.NOT_EVALUATED
        error_type = "InvalidOutcome"
    return {
        "decision": decision.value,
        "reason": outcome.reason,
        "error_type": error_type,
        "usage": _usage_rows(ctx.usage),
        "evaluated": evaluated,
    }


def _measure(rows: list[dict[str, Any]]) -> dict[str, Any]:
    has_repeats = bool(rows and len(rows[0]["repetitions"]) > 1)
    observations = [
        {"label": row["label"], **repeat} for row in rows for repeat in row["repetitions"]
    ]
    decided = [
        r for r in observations if r["decision"] in (Decision.PASS.value, Decision.FAIL.value)
    ]
    agree = sum(1 for r in decided if (r["decision"] == "pass") == (r["label"] == "acceptable"))
    bad = [r for r in decided if r["label"] == "unacceptable"]
    good = [r for r in decided if r["label"] == "acceptable"]

    def ratio(n: int, d: int) -> dict[str, Any]:
        return {"value": round(n / d, 4) if d else None, "numerator": n, "denominator": d}

    return {
        "cases": len(rows),
        "observations": len(observations),
        "undecided": len(observations) - len(decided),
        "agreement": ratio(agree, len(decided)),
        "false_acceptance": ratio(sum(1 for r in bad if r["decision"] == "pass"), len(bad)),
        "false_rejection": ratio(sum(1 for r in good if r["decision"] == "fail"), len(good)),
        # trivially 1.0 for deterministic evaluators; informative only for model judges
        "repeat_stability": ratio(
            sum(1 for r in rows if r["stable"]) if has_repeats else 0,
            len(rows) if has_repeats else 0,
        ),
    }


def _usage_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    planned_model_repetitions = [
        repeat for row in rows if row["uses_models"] for repeat in row["repetitions"]
    ]
    model_repetitions = [repeat for repeat in planned_model_repetitions if repeat["evaluated"]]
    reports = [
        {
            "id": row["id"],
            "binding": row["binding"],
            "repetition": repetition["index"],
            **usage,
        }
        for row in rows
        if row["uses_models"]
        for repetition in row["repetitions"]
        for usage in repetition["usage"]
    ]
    unknown_cost = sum(
        not repeat["usage"] or any(item["cost"] is None for item in repeat["usage"])
        for repeat in model_repetitions
    )
    costs = [item["cost"] for item in reports]
    calls = [item["calls"] for item in reports]
    calls_known = all(
        repeat["usage"] and all(item["calls"] is not None for item in repeat["usage"])
        for repeat in model_repetitions
    )
    costs_known = all(
        repeat["usage"] and all(item["cost"] is not None for item in repeat["usage"])
        for repeat in model_repetitions
    )
    return {
        "model_evaluations": len(model_repetitions),
        "model_evaluations_planned": len(planned_model_repetitions),
        "unknown_cost_evaluations": unknown_cost,
        "reported_calls": sum(calls) if calls_known else (0 if not model_repetitions else None),
        "reported_cost": sum(costs) if costs_known else (0.0 if not model_repetitions else None),
        "reports": reports,
    }


def _measured_evaluator_reference(reference: Any, measured: set[tuple[str, str]]) -> bool:
    if not isinstance(reference, str):
        return False
    evaluator_id, separator, wanted = reference.partition("@")
    for measured_id, version in measured:
        if measured_id != evaluator_id:
            continue
        if not separator:
            return True
        try:
            prefix = tuple(int(part) for part in wanted.split("."))
        except ValueError:
            continue
        actual = tuple(int(part) for part in version.split("."))
        if prefix and actual[: len(prefix)] == prefix:
            return True
    return False


def run_calibration(
    root: Path,
    registry: EvaluatorRegistry | None = None,
    *,
    repetitions: int = 2,
    timeout_seconds: float = 600.0,
    allow_models: bool = False,
    max_model_evaluations: int = 100,
) -> dict[str, Any]:
    """Measure reviewed labelled cases against native and explicitly authorized evaluators."""
    if not 1 <= repetitions <= 100:
        raise CalibrationError("repetitions must be between 1 and 100")
    if not 0 < timeout_seconds <= 3600:
        raise CalibrationError("timeout_seconds must be greater than 0 and at most 3600")
    if not 1 <= max_model_evaluations <= 10_000:
        raise CalibrationError("max_model_evaluations must be between 1 and 10000")
    meta, cases = load_calibration_set(root)
    if not cases:
        raise CalibrationError(f"{root}/cases.jsonl: calibration set has no cases")
    registry = registry or EvaluatorRegistry.with_native()
    resolved = [
        registry.resolve_binding(MetricBinding(metric=c.metric, params=c.params)) for c in cases
    ]
    unsupported = sorted(
        {
            metric.manifest.evaluator_id
            for metric in resolved
            if metric.manifest.consumes != "recorded_outputs"
        }
    )
    if unsupported:
        raise CalibrationError(
            "calibration only supports evaluators that consume recorded outputs: "
            + ", ".join(unsupported)
        )
    model_evaluations = sum(metric.manifest.uses_models for metric in resolved) * repetitions
    if model_evaluations and not allow_models:
        raise CalibrationError("model evaluators require explicit live authorization")
    review = meta.get("review", {})
    if model_evaluations and (
        review.get("status") != "reviewed" or not review.get("reviewer", "").strip()
    ):
        raise CalibrationError(
            "live judge calibration requires review.status='reviewed' and a named review.reviewer"
        )
    if model_evaluations > max_model_evaluations:
        raise CalibrationError(
            f"calibration requests {model_evaluations} model evaluations, above the "
            f"--max-model-evaluations limit of {max_model_evaluations}"
        )

    async def run_all() -> list[dict[str, Any]]:
        results = []
        for case, metric in zip(cases, resolved, strict=True):
            repeats = []
            for index in range(1, repetitions + 1):
                result = await _decide(case, metric, timeout_seconds=timeout_seconds)
                repeats.append({"index": index, **result})
            decisions = [repeat["decision"] for repeat in repeats]
            stable = (
                all(d in (Decision.PASS.value, Decision.FAIL.value) for d in decisions)
                and len(set(decisions)) == 1
            )
            results.append(
                {
                    "id": case.case_id,
                    "binding": case.binding_key(),
                    "category": case.category,
                    "label": case.label,
                    "decision": decisions[0],
                    "decisions": decisions,
                    "repetitions": repeats,
                    "stable": stable,
                    "uses_models": metric.manifest.uses_models,
                }
            )
        return results

    rows = asyncio.run(run_all())
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
                {k: r[k] for k in ("id", "category", "label", "decisions")}
                for r in bound
                if any(
                    decision in ("pass", "fail")
                    and (decision == "pass") != (r["label"] == "acceptable")
                    for decision in r["decisions"]
                )
            ],
        }
    reviewed = review.get("status", "unreviewed")
    measured_model_evaluators = {
        (metric.manifest.evaluator_id, metric.manifest.version)
        for metric in resolved
        if metric.manifest.uses_models
    }
    remaining_not_measured = [
        item
        for item in meta.get("not_measured", [])
        if not _measured_evaluator_reference(item.get("evaluator"), measured_model_evaluators)
    ]
    return {
        "schema": "aibench.judge-calibration-report/2",
        "set": meta.get("version"),
        "label_review": reviewed,
        "reviewer": review.get("reviewer"),
        "label_note": review.get("notes", ""),
        "repetitions": repetitions,
        "bindings": bindings,
        "not_measured": remaining_not_measured,
        "cases": rows,
        "usage": _usage_summary(rows),
    }
