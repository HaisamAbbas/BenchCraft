"""CI adapter mappings from stored benchmark facts to JUnit and SARIF."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Any

from aibench.reporting.ci import render_ci_report


def _report(*, complete: bool = False) -> dict[str, Any]:
    return {
        "run": {"run_id": "run-17", "status": "completed" if complete else "running"},
        "outcome": {
            "complete": complete,
            "unhealthy_work": {} if complete else {"pending": 1},
            "gates_failed": ["release"],
            "gates_undecided": [],
        },
        "gates": [{"gate_id": "release", "status": "fail", "reason": "pass rate below 1"}],
    }


def _case_export(*, content: str = "withheld") -> dict[str, Any]:
    return {
        "scoring_id": "score-2",
        "content": content,
        "row_count": 5,
        "rows": [
            {
                "case_id": "pass-case",
                "repetition": 0,
                "work_state": "succeeded",
                "execution": {"status": "ok", "timing": {"wall_ms": 125}},
                "metrics": [
                    {
                        "label": "native.score",
                        "metric_id": "native.score",
                        "metric_version": "1",
                        "binding_hash": "sha256:pass",
                        "status": "ok",
                        "decision": "pass",
                        "reason": None,
                    }
                ],
            },
            {
                "case_id": "bad-case",
                "repetition": 1,
                "work_state": "succeeded",
                "execution": {"status": "ok", "timing": {"wall_ms": 250}},
                "metrics": [
                    {
                        "label": "native.score",
                        "metric_id": "native.score",
                        "metric_version": "1",
                        "binding_hash": "sha256:pass",
                        "result_id": "result-fail",
                        "status": "ok",
                        "decision": "fail",
                        "reason": "threshold_not_met: private answer text",
                    }
                ],
            },
            {
                "case_id": "judge-error",
                "repetition": 0,
                "work_state": "succeeded",
                "execution": {"status": "ok"},
                "metrics": [
                    {
                        "label": "llm.judge",
                        "metric_id": "llm.judge",
                        "metric_version": "2",
                        "binding_hash": "sha256:error",
                        "status": "error",
                        "decision": "not_evaluated",
                        "reason": "provider_error: private prompt text",
                    }
                ],
            },
            {
                "case_id": "not-applicable",
                "repetition": 0,
                "work_state": "succeeded",
                "execution": {"status": "ok"},
                "metrics": [
                    {
                        "label": "native.reference",
                        "metric_id": "native.reference",
                        "metric_version": "1",
                        "status": "not_applicable",
                        "decision": "not_evaluated",
                        "reason": "reference_missing",
                    }
                ],
            },
            {
                "case_id": "pending-case",
                "repetition": 0,
                "work_state": "pending",
                "execution": None,
                "metrics": [
                    {
                        "label": "native.score",
                        "metric_id": "native.score",
                        "metric_version": "1",
                        "status": "pending",
                        "decision": "not_evaluated",
                        "reason": "result_missing",
                    }
                ],
            },
        ],
    }


def test_junit_reports_metric_failures_incompleteness_gates_and_skips() -> None:
    rendered = render_ci_report(_report(), _case_export(), "junit")
    root = ET.fromstring(rendered)
    suite = root.find("testsuite")

    assert suite is not None
    assert suite.attrib["tests"] == root.attrib["tests"]
    assert int(suite.attrib["failures"]) == 2  # metric and release gate
    assert int(suite.attrib["errors"]) == 4  # evaluator, pending work/metric, incomplete run
    assert int(suite.attrib["skipped"]) == 1  # not-applicable metric
    assert suite.attrib["time"] == "0.375000"
    messages = [node.attrib.get("message", "") for node in suite.iter()]
    assert any("native.score: fail" in message for message in messages)
    assert any("release gate fail" in message for message in messages)
    assert "private answer text" not in rendered
    assert "private prompt text" not in rendered


def test_sarif_is_versioned_and_emits_only_actionable_or_unresolved_results() -> None:
    rendered = render_ci_report(_report(), _case_export(), "sarif")
    document = json.loads(rendered)
    run = document["runs"][0]

    assert document["version"] == "2.1.0"
    assert document["$schema"].endswith("sarif-schema-2.1.0.json")
    assert run["tool"]["driver"]["name"] == "BenchCraft"
    assert run["invocations"][0]["executionSuccessful"] is True
    assert run["properties"]["selectedCaseRepetitions"] == 5
    assert {result["properties"].get("caseId") for result in run["results"]} >= {
        "bad-case",
        "judge-error",
        "pending-case",
    }
    assert any(result["properties"].get("gateId") == "release" for result in run["results"])
    assert all(result["ruleId"] for result in run["results"])
    assert {rule["id"] for rule in run["tool"]["driver"]["rules"]} == {
        result["ruleId"] for result in run["results"]
    }
    assert "private answer text" not in rendered
    assert "private prompt text" not in rendered


def test_successful_junit_and_sarif_preserve_all_passes_without_findings() -> None:
    report = _report(complete=True)
    report["gates"] = [{"gate_id": "release", "status": "pass", "reason": None}]
    export = _case_export()
    export["rows"] = export["rows"][:1]
    export["row_count"] = 1

    junit = ET.fromstring(render_ci_report(report, export, "junit"))
    suite = junit.find("testsuite")
    sarif = json.loads(render_ci_report(report, export, "sarif"))

    assert suite is not None and suite.attrib["failures"] == "0" and suite.attrib["errors"] == "0"
    assert sarif["runs"][0]["results"] == []


def test_junit_preserves_valid_supplementary_unicode() -> None:
    report = _report(complete=True)
    report["gates"] = []
    case_export = {
        "scoring_id": "score-unicode",
        "content": "included",
        "rows": [
            {
                "case_id": "customer-😀",
                "repetition": 0,
                "work_state": "succeeded",
                "execution": {"status": "ok"},
                "metrics": [
                    {
                        "label": "quality-🧪",
                        "metric_id": "quality",
                        "metric_version": "1",
                        "status": "ok",
                        "decision": "fail",
                        "reason": "score_failed: 🚫",
                    }
                ],
            }
        ],
    }

    root = ET.fromstring(render_ci_report(report, case_export, "junit"))
    names = [case.attrib["name"] for case in root.iter("testcase")]
    failure = root.find(".//failure")

    assert "customer-😀 repetition 0 application" in names
    assert "customer-😀 repetition 0 quality-🧪" in names
    assert failure is not None and "🚫" in (failure.text or "")

