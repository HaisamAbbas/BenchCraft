"""Predeclared, storage-only regression tolerances for qualified comparisons."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aibench.core.hashes import content_hash

REGRESSION_POLICY_SCHEMA = "aibench.regression-policy/1"
REGRESSION_GATE_SCHEMA = "aibench.regression-gate/1"


class MetricRegressionRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    metric_id: str = Field(min_length=1, max_length=200)
    max_degradation: float = Field(ge=0, allow_inf_nan=False, strict=True)

    @field_validator("metric_id")
    @classmethod
    def _require_non_whitespace_metric_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("metric_id must not be blank")
        return normalized

class RegressionPolicy(BaseModel):
    """Limits are declared before comparison and use metric-native units."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    policy_schema: Literal["aibench.regression-policy/1"] = Field(alias="schema")
    metric_rules: tuple[MetricRegressionRule, ...] = ()
    max_latency_p95_increase_ms: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, strict=True
    )
    max_application_cost_increase_usd: float | None = Field(
        default=None, ge=0, allow_inf_nan=False, strict=True
    )

    @model_validator(mode="after")
    def _has_rules_and_unique_metric_ids(self) -> RegressionPolicy:
        if not (
            self.metric_rules
            or self.max_latency_p95_increase_ms is not None
            or self.max_application_cost_increase_usd is not None
        ):
            raise ValueError("at least one metric or performance tolerance is required")
        metric_ids = [rule.metric_id for rule in self.metric_rules]
        if len(metric_ids) != len(set(metric_ids)):
            raise ValueError("metric_rules must not repeat a metric_id")
        return self


class RegressionPolicyError(ValueError):
    """A regression-policy document is invalid or cannot be applied safely."""


def parse_regression_policy(document: Mapping[str, Any]) -> RegressionPolicy:
    try:
        return RegressionPolicy.model_validate(document)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"])
        prefix = f"{location}: " if location else ""
        raise RegressionPolicyError(f"invalid regression policy: {prefix}{first['msg']}") from exc


def _rule_result(
    rule: str,
    *,
    status: Literal["pass", "fail", "undetermined"],
    observed: float | None,
    maximum: float,
    reason: str | None = None,
    **fields: Any,
) -> dict[str, Any]:
    return {
        "rule": rule,
        "status": status,
        "passed": status == "pass",
        "observed": observed,
        "maximum": maximum,
        "reason": reason,
        **fields,
    }


def _performance_rule(
    report: Mapping[str, Any],
    *,
    signal: str,
    field: str,
    maximum: float,
    comparison_qualified: bool,
) -> dict[str, Any]:
    if not comparison_qualified:
        return _rule_result(
            signal,
            status="undetermined",
            observed=None,
            maximum=maximum,
            reason="qualified_complete_comparison_required",
        )
    performance = report.get("application_performance")
    if not isinstance(performance, Mapping):
        return _rule_result(
            signal, status="undetermined", observed=None, maximum=maximum,
            reason="performance_facts_unavailable",
        )
    baseline = performance.get("baseline")
    current = performance.get("current")
    if not isinstance(baseline, Mapping) or not isinstance(current, Mapping):
        return _rule_result(
            signal, status="undetermined", observed=None, maximum=maximum,
            reason="performance_facts_unavailable",
        )
    if field == "latency_p95_ms":
        for side_name, side in (("baseline", baseline), ("current", current)):
            expected = side.get("latency_uncached_successful_requests")
            missing = side.get("latency_missing_measurements")
            if (
                isinstance(expected, bool)
                or not isinstance(expected, int)
                or expected <= 0
                or isinstance(missing, bool)
                or not isinstance(missing, int)
                or missing != 0
            ):
                return _rule_result(
                    signal,
                    status="undetermined",
                    observed=None,
                    maximum=maximum,
                    reason="complete_successful_latency_measurements_required",
                    side=side_name,
                    measurements=side.get("latency_successful_requests"),
                    expected=expected,
                    missing=missing,
                )
    baseline_value = baseline.get(field)
    current_value = current.get(field)
    if (
        isinstance(baseline_value, bool)
        or not isinstance(baseline_value, (int, float))
        or not math.isfinite(float(baseline_value))
        or baseline_value < 0
        or isinstance(current_value, bool)
        or not isinstance(current_value, (int, float))
        or not math.isfinite(float(current_value))
        or current_value < 0
    ):
        return _rule_result(
            signal,
            status="undetermined",
            observed=None,
            maximum=maximum,
            reason="complete_baseline_and_current_measurements_required",
            baseline=baseline_value,
            current=current_value,
        )
    increase = float(current_value) - float(baseline_value)
    if field == "total_cost_usd":
        # Comparison reports cost totals to micro-dollar precision.
        increase = round(increase, 6)
    return _rule_result(
        signal,
        status="fail" if _exceeds(increase, maximum) else "pass",
        observed=increase,
        maximum=maximum,
        baseline=float(baseline_value),
        current=float(current_value),
        difference_definition="current - baseline",
    )


def _exceeds(observed: float, maximum: float) -> bool:
    return observed > maximum


def evaluate_regression_policy(
    report: Mapping[str, Any], policy: RegressionPolicy
) -> dict[str, Any]:
    """Evaluate tolerances against qualified, complete comparison facts only.

    Metric tolerances use the point estimate in native metric units. The comparison's
    cluster-bootstrap interval remains in the result as context; this policy's fail rule
    is deliberately the predeclared point-estimate threshold.
    """

    results: list[dict[str, Any]] = []
    entries = report.get("metrics")
    if not isinstance(entries, list):
        entries = []
    qualified = report.get("status") == "qualified" and report.get("qualified") is True
    coverage = report.get("overall_coverage_gate")
    coverage_passed = isinstance(coverage, Mapping) and coverage.get("passed") is True

    for rule in policy.metric_rules:
        matches = [
            item
            for item in entries
            if isinstance(item, Mapping) and item.get("metric_id") == rule.metric_id
        ]
        result_name = f"metric:{rule.metric_id}"
        if len(matches) != 1:
            results.append(
                _rule_result(
                    result_name,
                    status="undetermined",
                    observed=None,
                    maximum=rule.max_degradation,
                    reason="metric_missing_or_ambiguous",
                )
            )
            continue
        entry = matches[0]
        comparison = entry.get("comparison")
        gate = entry.get("coverage_gate")
        if (
            not qualified
            or not coverage_passed
            or entry.get("qualified") is not True
            or not isinstance(comparison, Mapping)
            or not isinstance(gate, Mapping)
            or gate.get("passed") is not True
        ):
            results.append(
                _rule_result(
                    result_name,
                    status="undetermined",
                    observed=None,
                    maximum=rule.max_degradation,
                    reason="qualified_complete_comparison_required",
                )
            )
            continue
        metric = comparison.get("metric")
        case_macro = comparison.get("case_macro")
        if not isinstance(metric, Mapping) or not isinstance(case_macro, Mapping):
            delta = None
            direction = None
        else:
            delta = case_macro.get("mean_current_minus_baseline")
            direction = metric.get("direction")
        if isinstance(delta, bool) or not isinstance(delta, (int, float)):
            degradation = None
        elif direction == "higher":
            degradation = -float(delta)
        elif direction == "lower":
            degradation = float(delta)
        else:
            degradation = None
        if degradation is None:
            results.append(
                _rule_result(
                    result_name,
                    status="undetermined",
                    observed=None,
                    maximum=rule.max_degradation,
                    reason="metric_direction_or_numeric_delta_unavailable",
                    direction=direction,
                )
            )
            continue
        uncertainty = comparison.get("uncertainty")
        uncertainty_bounds = None
        if isinstance(uncertainty, Mapping):
            lower = uncertainty.get("lower")
            upper = uncertainty.get("upper")
            if isinstance(lower, (int, float)) and isinstance(upper, (int, float)):
                uncertainty_bounds = (
                    {"lower": -float(upper), "upper": -float(lower)}
                    if direction == "higher"
                    else {"lower": float(lower), "upper": float(upper)}
                )
        results.append(
            _rule_result(
                result_name,
                status="fail" if _exceeds(degradation, rule.max_degradation) else "pass",
                observed=degradation,
                maximum=rule.max_degradation,
                reason=None,
                metric_id=rule.metric_id,
                direction=direction,
                uncertainty=uncertainty_bounds,
            )
        )

    if policy.max_latency_p95_increase_ms is not None:
        results.append(
            _performance_rule(
                report,
                signal="application.latency_p95_ms",
                field="latency_p95_ms",
                maximum=policy.max_latency_p95_increase_ms,
                comparison_qualified=qualified and coverage_passed,
            )
        )
    if policy.max_application_cost_increase_usd is not None:
        results.append(
            _performance_rule(
                report,
                signal="application.total_cost_usd",
                field="total_cost_usd",
                maximum=policy.max_application_cost_increase_usd,
                comparison_qualified=qualified and coverage_passed,
            )
        )

    statuses = {item["status"] for item in results}
    status: Literal["pass", "fail", "undetermined"] = (
        "fail" if "fail" in statuses else "undetermined" if "undetermined" in statuses else "pass"
    )
    return {
        "schema": REGRESSION_GATE_SCHEMA,
        "policy_schema": policy.policy_schema,
        "policy_hash": content_hash(policy.model_dump(mode="json", by_alias=True)),
        "method": "point_estimate_tolerance",
        "status": status,
        "passed": status == "pass",
        "rules": results,
    }


__all__ = [
    "REGRESSION_GATE_SCHEMA",
    "REGRESSION_POLICY_SCHEMA",
    "MetricRegressionRule",
    "RegressionPolicy",
    "RegressionPolicyError",
    "evaluate_regression_policy",
    "parse_regression_policy",
]
