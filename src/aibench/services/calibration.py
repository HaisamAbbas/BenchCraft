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
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from aibench.core.errors import AibenchError
from aibench.core.hashes import content_hash
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
MAX_PAIRED_VARIANTS = 8
MAX_PAIRED_BINDING_COMPARISONS = 512
MAX_PAIR_ID_LENGTH = 256


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
    pair_id: str | None = None

    def binding_key(self) -> str:
        return f"{self.metric} {json.dumps(self.params, sort_keys=True)}"


def load_calibration_set(
    root: Path,
    *,
    metadata_path: Path | None = None,
    cases_path: Path | None = None,
) -> tuple[dict[str, Any], list[CalibrationCase]]:
    metadata_file = metadata_path or root / "set.json"
    cases_file = cases_path or root / "cases.jsonl"
    try:
        meta = json.loads(metadata_file.read_text(encoding="utf-8"))
        lines = cases_file.read_text(encoding="utf-8").splitlines()
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
            pair_id = raw.get("pair_id")
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
            if "pair_id" in raw and (
                not isinstance(pair_id, str)
                or not pair_id.strip()
                or len(pair_id) > MAX_PAIR_ID_LENGTH
            ):
                raise TypeError(
                    f"pair_id must be non-empty text of at most {MAX_PAIR_ID_LENGTH} characters"
                )
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
                    pair_id=pair_id,
                )
            )
            _evaluation_view(cases[-1])
        except KeyError as exc:
            raise CalibrationError(f"{root}/cases.jsonl:{number}: missing {exc}") from exc
        except (TypeError, ValidationError) as exc:
            raise CalibrationError(f"{root}/cases.jsonl:{number}: {exc}") from exc
    _validate_pair_groups(cases, root=root)
    return meta, cases


def _pair_identity(case: CalibrationCase) -> str:
    judge_neutral_params = {key: value for key, value in case.params.items() if key != "judge"}
    return content_hash(
        {
            "category": case.category,
            "metric": case.metric,
            "params": judge_neutral_params,
            "input": case.input,
            "has_input": case.has_input,
            "reference": case.reference.model_dump(mode="json") if case.reference else None,
            "output": case.output,
            "retrieved_context": case.retrieved_context,
            "tool_events": case.tool_events,
            "metadata": case.metadata,
            "expectations": case.expectations,
            "label": case.label,
        }
    )


def _vision_judge_identity(judge: dict[str, Any]) -> tuple[str, str]:
    endpoint = judge["base_url"]
    model = judge["model"]
    try:
        normalized = httpx.URL(endpoint.rstrip("/"))
    except (httpx.InvalidURL, UnicodeError, ValueError):
        normalized_endpoint = endpoint.rstrip("/")
    else:
        if normalized.port == (443 if normalized.scheme == "https" else 80):
            normalized = normalized.copy_with(port=None)
        normalized_endpoint = str(normalized)
    return normalized_endpoint, model.strip()


def _validate_pair_groups(cases: list[CalibrationCase], *, root: Path) -> None:
    groups: dict[str, list[CalibrationCase]] = defaultdict(list)
    comparison_keys: set[tuple[str, str]] = set()
    for case in cases:
        if case.pair_id is not None:
            groups[case.pair_id].append(case)
    for pair_id, group in groups.items():
        if any(case.metric != "deepeval.vision_g_eval" for case in group):
            raise CalibrationError(
                f"{root}/cases.jsonl: pair_id {pair_id!r} is only supported for "
                "deepeval.vision_g_eval"
            )
        variants: dict[str, CalibrationCase] = {}
        provider_identities: set[tuple[str, str]] = set()
        for case in group:
            judge = case.params.get("judge")
            if not isinstance(judge, dict):
                raise CalibrationError(
                    f"{root}/cases.jsonl: pair_id {pair_id!r} requires a vision judge config"
                )
            endpoint, model = judge.get("base_url"), judge.get("model")
            if (
                not isinstance(endpoint, str)
                or not endpoint
                or not isinstance(model, str)
                or not model
            ):
                raise CalibrationError(
                    f"{root}/cases.jsonl: pair_id {pair_id!r} requires a judge endpoint and model"
                )
            key = case.binding_key()
            if key in variants:
                raise CalibrationError(
                    f"{root}/cases.jsonl: pair_id {pair_id!r} repeats the same vision binding"
                )
            variants[key] = case
            provider_identities.add(_vision_judge_identity(judge))
        if len(variants) > MAX_PAIRED_VARIANTS:
            raise CalibrationError(
                f"{root}/cases.jsonl: pair_id {pair_id!r} exceeds the "
                f"{MAX_PAIRED_VARIANTS}-binding comparison limit"
            )
        if len(provider_identities) != len(variants):
            raise CalibrationError(
                f"{root}/cases.jsonl: pair_id {pair_id!r} repeats a judge endpoint/model identity"
            )
        bindings = sorted(variants)
        for left_index, binding_a in enumerate(bindings):
            for binding_b in bindings[left_index + 1 :]:
                comparison_keys.add((binding_a, binding_b))
                if len(comparison_keys) > MAX_PAIRED_BINDING_COMPARISONS:
                    raise CalibrationError(
                        f"{root}/cases.jsonl: paired comparisons exceed the "
                        f"{MAX_PAIRED_BINDING_COMPARISONS}-binding-pair report limit"
                    )
        identities = {_pair_identity(case) for case in group}
        if len(identities) != 1:
            raise CalibrationError(
                f"{root}/cases.jsonl: pair_id {pair_id!r} variants must use the same image, "
                "prompt, reference, output, label, and evaluator parameters"
            )


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
                "score": None,
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
            "score": None,
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
            "score": None,
            "usage": _usage_rows(ctx.usage),
            "evaluated": evaluated,
        }
    try:
        decision = decide(outcome.status, outcome.value, rule_for(binding, resolved.manifest))
    except (AttributeError, TypeError, ValueError):
        decision = Decision.NOT_EVALUATED
        error_type = "InvalidOutcome"
    raw_score = outcome.value.value if outcome.value is not None else None
    score = None
    if (
        outcome.value is not None
        and outcome.value.kind == "scalar"
        and isinstance(raw_score, (int, float))
        and not isinstance(raw_score, bool)
    ):
        try:
            finite_score = float(raw_score)
        except (OverflowError, ValueError):
            pass
        else:
            if math.isfinite(finite_score):
                score = finite_score
    return {
        "decision": decision.value,
        "reason": outcome.reason,
        "error_type": error_type,
        "score": score,
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


def _paired_comparisons(rows: list[dict[str, Any]]) -> dict[str, Any]:
    groups: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        pair_id = row.get("pair_id")
        if pair_id is not None:
            groups[pair_id][row["binding"]] = row

    pair_stats: dict[tuple[str, str], dict[str, Any]] = {}
    unpaired = []
    for pair_id, variants in sorted(groups.items()):
        bindings = sorted(variants)
        if len(bindings) < 2:
            unpaired.append(pair_id)
            continue
        for left_index, binding_a in enumerate(bindings):
            for binding_b in bindings[left_index + 1 :]:
                stat = pair_stats.setdefault(
                    (binding_a, binding_b),
                    {
                        "paired_cases": 0,
                        "paired_observations": 0,
                        "decided_observations": 0,
                        "matching_decisions": 0,
                        "score_observations": 0,
                        "score_a_total": 0.0,
                        "score_b_total": 0.0,
                        "score_delta_total": 0.0,
                        "score_delta_absolute_total": 0.0,
                        "disagreements": [],
                        "disagreements_omitted": 0,
                    },
                )
                row_a = variants[binding_a]
                row_b = variants[binding_b]
                stat["paired_cases"] += 1
                for repeat_a, repeat_b in zip(
                    row_a["repetitions"], row_b["repetitions"], strict=True
                ):
                    stat["paired_observations"] += 1
                    decision_a, decision_b = repeat_a["decision"], repeat_b["decision"]
                    decided = {Decision.PASS.value, Decision.FAIL.value}
                    if decision_a in decided and decision_b in decided:
                        stat["decided_observations"] += 1
                        if decision_a == decision_b:
                            stat["matching_decisions"] += 1
                        else:
                            disagreement = {
                                "pair_id": pair_id,
                                "repetition": repeat_a["index"],
                                "label": row_a["label"],
                                "decision_a": decision_a,
                                "decision_b": decision_b,
                            }
                            if len(stat["disagreements"]) < 100:
                                stat["disagreements"].append(disagreement)
                            else:
                                stat["disagreements_omitted"] += 1
                    score_a, score_b = repeat_a.get("score"), repeat_b.get("score")
                    if score_a is not None and score_b is not None:
                        delta = score_b - score_a
                        stat["score_observations"] += 1
                        stat["score_a_total"] += score_a
                        stat["score_b_total"] += score_b
                        stat["score_delta_total"] += delta
                        stat["score_delta_absolute_total"] += abs(delta)

    comparisons = []
    for (binding_a, binding_b), stat in sorted(pair_stats.items()):
        score_count = stat["score_observations"]
        decision_count = stat["decided_observations"]
        comparisons.append(
            {
                "binding_a": binding_a,
                "binding_b": binding_b,
                "paired_cases": stat["paired_cases"],
                "paired_observations": stat["paired_observations"],
                "decision_agreement": {
                    "value": round(stat["matching_decisions"] / decision_count, 4)
                    if decision_count
                    else None,
                    "numerator": stat["matching_decisions"],
                    "denominator": decision_count,
                },
                "undecided_observations": stat["paired_observations"] - decision_count,
                "scores": {
                    "observations": score_count,
                    "mean_a": round(stat["score_a_total"] / score_count, 4)
                    if score_count
                    else None,
                    "mean_b": round(stat["score_b_total"] / score_count, 4)
                    if score_count
                    else None,
                    "mean_delta_b_minus_a": round(stat["score_delta_total"] / score_count, 4)
                    if score_count
                    else None,
                    "mean_absolute_delta": round(
                        stat["score_delta_absolute_total"] / score_count, 4
                    )
                    if score_count
                    else None,
                },
                "disagreements": stat["disagreements"],
                "disagreements_omitted": stat["disagreements_omitted"],
            }
        )
    return {
        "group_count": len(groups),
        "paired_group_count": len(groups) - len(unpaired),
        "unpaired_group_count": len(unpaired),
        "unpaired_pair_ids": unpaired[:100],
        "unpaired_pair_ids_omitted": max(0, len(unpaired) - 100),
        "pairs": comparisons,
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
    calibration_data: tuple[dict[str, Any], list[CalibrationCase]] | None = None,
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
    # Live callers can pass the exact evidence snapshot they preflighted against policy.
    # Re-reading after plugin discovery would permit worker startup code to replace the set
    # between authorization and evaluation.
    meta, cases = calibration_data if calibration_data is not None else load_calibration_set(root)
    if not cases:
        raise CalibrationError(f"{root}/cases.jsonl: calibration set has no cases")
    _validate_pair_groups(cases, root=root)
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
        review.get("status") != "reviewed"
        or not isinstance(review.get("reviewer"), str)
        or not review["reviewer"].strip()
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
                    "pair_id": case.pair_id,
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
    measured_case_ids = {
        row["id"]
        for row in rows
        if row["uses_models"] and any(repeat["evaluated"] for repeat in row["repetitions"])
    }
    measured_model_evaluators = {
        (metric.manifest.evaluator_id, metric.manifest.version)
        for case, metric in zip(cases, resolved, strict=True)
        if metric.manifest.uses_models and case.case_id in measured_case_ids
    }
    remaining_not_measured = [
        item
        for item in meta.get("not_measured", [])
        if not _measured_evaluator_reference(item.get("evaluator"), measured_model_evaluators)
    ]
    return {
        "schema": "aibench.judge-calibration-report/3",
        "set": meta.get("version"),
        "mode": "live" if allow_models and model_evaluations else "offline",
        "label_review": reviewed,
        "reviewer": review.get("reviewer"),
        "label_note": review.get("notes", ""),
        "repetitions": repetitions,
        "bindings": bindings,
        "not_measured": remaining_not_measured,
        "cases": rows,
        "paired_comparisons": _paired_comparisons(rows),
        "usage": _usage_summary(rows),
    }
