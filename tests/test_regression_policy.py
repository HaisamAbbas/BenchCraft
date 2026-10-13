"""Validation and fail-closed behavior for comparison regression policies."""

from __future__ import annotations

import pytest

from aibench.services.comparison import comparison_exit_code
from aibench.services.regression_policy import (
    RegressionPolicyError,
    evaluate_regression_policy,
    parse_regression_policy,
)


def test_policy_rejects_empty_duplicate_and_boolean_limits() -> None:
    with pytest.raises(RegressionPolicyError, match="at least one"):
        parse_regression_policy({"schema": "aibench.regression-policy/1"})

    with pytest.raises(RegressionPolicyError, match="must not repeat"):
        parse_regression_policy(
            {
                "schema": "aibench.regression-policy/1",
                "metric_rules": [
                    {"metric_id": "score", "max_degradation": 0},
                    {"metric_id": "score", "max_degradation": 1},
                ],
            }
        )

    with pytest.raises(RegressionPolicyError):
        parse_regression_policy(
            {
                "schema": "aibench.regression-policy/1",
                "max_latency_p95_increase_ms": True,
            }
        )


def test_lower_is_better_metric_uses_current_minus_baseline() -> None:
    policy = parse_regression_policy(
        {
            "schema": "aibench.regression-policy/1",
            "metric_rules": [{"metric_id": "latency.metric", "max_degradation": 10}],
        }
    )
    report = {
        "status": "qualified",
        "qualified": True,
        "overall_coverage_gate": {"passed": True},
        "metrics": [
            {
                "metric_id": "latency.metric",
                "qualified": True,
                "coverage_gate": {"passed": True},
                "comparison": {
                    "metric": {"direction": "lower"},
                    "case_macro": {"mean_current_minus_baseline": 11},
                    "uncertainty": {"lower": 8, "upper": 14},
                },
            }
        ],
    }

    gate = evaluate_regression_policy(report, policy)

    assert gate["status"] == "fail"
    assert gate["rules"][0]["observed"] == 11
    assert gate["rules"][0]["uncertainty"] == {"lower": 8, "upper": 14}
    assert comparison_exit_code({**report, "regression_gate": gate}) == 1


def test_zero_tolerance_rejects_a_small_positive_degradation() -> None:
    policy = parse_regression_policy(
        {
            "schema": "aibench.regression-policy/1",
            "metric_rules": [{"metric_id": "latency.metric", "max_degradation": 0}],
        }
    )
    report = {
        "status": "qualified",
        "qualified": True,
        "overall_coverage_gate": {"passed": True},
        "metrics": [
            {
                "metric_id": "latency.metric",
                "qualified": True,
                "coverage_gate": {"passed": True},
                "comparison": {
                    "metric": {"direction": "lower"},
                    "case_macro": {"mean_current_minus_baseline": 1e-12},
                },
            }
        ],
    }

    gate = evaluate_regression_policy(report, policy)

    assert gate["status"] == "fail"
    assert gate["rules"][0]["observed"] == 1e-12


def test_performance_rule_cannot_pass_a_blocked_or_undercovered_comparison() -> None:
    policy = parse_regression_policy(
        {
            "schema": "aibench.regression-policy/1",
            "max_latency_p95_increase_ms": 10,
        }
    )
    report = {
        "mode": "strict",
        "status": "blocked",
        "qualified": False,
        "overall_coverage_gate": {"passed": True},
        "application_performance": {
            "baseline": {"latency_p95_ms": 100},
            "current": {"latency_p95_ms": 1_000},
        },
    }

    gate = evaluate_regression_policy(report, policy)

    assert gate["status"] == "undetermined"
    assert gate["rules"][0]["reason"] == "qualified_complete_comparison_required"
    assert comparison_exit_code({**report, "regression_gate": gate}) == 2


def test_cost_tolerance_boundary_uses_report_precision() -> None:
    policy = parse_regression_policy(
        {
            "schema": "aibench.regression-policy/1",
            "max_application_cost_increase_usd": 0.25,
        }
    )
    report = {
        "status": "qualified",
        "qualified": True,
        "overall_coverage_gate": {"passed": True},
        "application_performance": {
            "baseline": {"total_cost_usd": 0.3},
            "current": {"total_cost_usd": 0.55},
        },
    }

    gate = evaluate_regression_policy(report, policy)

    assert gate["status"] == "pass"
    assert gate["rules"][0]["observed"] == 0.25


def test_latency_rule_requires_every_successful_uncached_measurement() -> None:
    policy = parse_regression_policy(
        {
            "schema": "aibench.regression-policy/1",
            "max_latency_p95_increase_ms": 10,
        }
    )
    report = {
        "status": "qualified",
        "qualified": True,
        "overall_coverage_gate": {"passed": True},
        "application_performance": {
            "baseline": {
                "latency_p95_ms": 100,
                "latency_uncached_successful_requests": 2,
                "latency_missing_measurements": 0,
            },
            "current": {
                "latency_p95_ms": 105,
                "latency_uncached_successful_requests": 2,
                "latency_missing_measurements": 1,
            },
        },
    }

    gate = evaluate_regression_policy(report, policy)

    assert gate["status"] == "undetermined"
    assert gate["rules"][0]["reason"] == (
        "complete_successful_latency_measurements_required"
    )


def test_coverage_failure_keeps_its_existing_exit_code_with_a_policy() -> None:
    report = {
        "mode": "strict",
        "status": "qualified",
        "qualified": True,
        "overall_coverage_gate": {"passed": False},
        "regression_gate": {"status": "undetermined"},
    }

    assert comparison_exit_code(report) == 1
