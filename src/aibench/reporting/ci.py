"""JUnit XML and SARIF adapters for stored BenchCraft run results."""

from __future__ import annotations

import hashlib
import json
import math
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any

from aibench import __version__
from aibench.reporting.aggregation import reason_code
from aibench.security.redaction import sanitize, sanitize_value

SARIF_SCHEMA = (
    "https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/"
    "sarif-schema-2.1.0.json"
)


def _display(value: Any, *, content: bool = True, limit: int = 500) -> str:
    if value is None:
        return ""
    if not content:
        code = reason_code(str(value))
        return code or "details_withheld"
    text = sanitize(value if isinstance(value, str) else str(value))
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _xml_text(value: Any) -> str:
    text = _display(value)
    return "".join(
        char
        for char in text
        if (
            ord(char) in (0x9, 0xA, 0xD)
            or 0x20 <= ord(char) <= 0xD7FF
            or 0xE000 <= ord(char) <= 0xFFFD
            or 0x10000 <= ord(char) <= 0x10FFFF
        )
    )


def _seconds(wall_ms: Any) -> float:
    if isinstance(wall_ms, (int, float)) and not isinstance(wall_ms, bool):
        seconds = float(wall_ms) / 1000
        if math.isfinite(seconds) and seconds >= 0:
            return seconds
    return 0.0


def _case_name(row: Mapping[str, Any]) -> str:
    return f"{row.get('case_id', 'unknown')} repetition {row.get('repetition', '?')}"


def render_junit(report: Mapping[str, Any], case_export: Mapping[str, Any]) -> str:
    """Render one JUnit testcase per application item, metric result, and release gate."""

    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", {"name": "aibench"})
    content_included = case_export.get("content") == "included"
    properties = ET.SubElement(suite, "properties")
    for name, value in (
        ("run_id", report.get("run", {}).get("run_id")),
        ("run_status", report.get("run", {}).get("status")),
        ("scoring_id", case_export.get("scoring_id")),
        ("content", case_export.get("content")),
    ):
        if value is not None:
            ET.SubElement(properties, "property", {"name": name, "value": _xml_text(value)})

    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    total_time = 0.0

    def testcase(
        classname: str,
        name: str,
        *,
        wall_ms: Any = None,
        outcome: str | None = None,
        message: str = "",
        detail: str = "",
        kind: str = "",
    ) -> None:
        nonlocal total_time
        elapsed = _seconds(wall_ms)
        total_time += elapsed
        attrs = {"classname": _xml_text(classname), "name": _xml_text(name)}
        attrs["time"] = f"{elapsed:.6f}"
        element = ET.SubElement(suite, "testcase", attrs)
        counts["tests"] += 1
        if outcome is None:
            return
        counts[outcome] += 1
        node_attrs = {"message": _xml_text(message)}
        if kind:
            node_attrs["type"] = _xml_text(kind)
        node = ET.SubElement(element, outcome[:-1] if outcome in ("failures", "errors") else "skipped", node_attrs)
        if detail:
            node.text = _xml_text(detail)

    rows = case_export.get("rows", [])
    if not isinstance(rows, list):
        rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        name = _case_name(row)
        execution = row.get("execution")
        execution = execution if isinstance(execution, Mapping) else None
        timing = execution.get("timing") if execution else None
        wall_ms = timing.get("wall_ms") if isinstance(timing, Mapping) else None
        work_state = str(row.get("work_state", "unknown"))
        execution_status = str(execution.get("status", "missing")) if execution else "missing"
        if execution_status == "ok" and work_state in ("succeeded", "recorded"):
            testcase("aibench.execution", name + " application", wall_ms=wall_ms)
        elif execution_status == "not_applicable":
            testcase(
                "aibench.execution",
                name + " application",
                wall_ms=wall_ms,
                outcome="skipped",
                message="application execution is not applicable",
                kind="execution_not_applicable",
            )
        else:
            detail = execution.get("error") if execution and content_included else None
            reason = execution_status if execution else work_state
            testcase(
                "aibench.execution",
                name + " application",
                wall_ms=wall_ms,
                outcome="errors",
                message=f"application execution {reason}",
                detail=_display(detail) if detail else "Application output is unavailable.",
                kind="application_execution",
            )

        metrics = row.get("metrics", [])
        if not isinstance(metrics, list):
            continue
        for metric in metrics:
            if not isinstance(metric, Mapping):
                continue
            label = str(metric.get("label") or metric.get("metric_id") or "metric")
            status = str(metric.get("status", "not_recorded"))
            decision = str(metric.get("decision", "not_evaluated"))
            outcome: str | None
            kind: str
            if status == "not_applicable":
                outcome, kind = "skipped", "metric_not_applicable"
            elif status == "ok" and decision == "pass":
                outcome, kind = None, ""
            elif status == "ok" and decision == "fail":
                outcome, kind = "failures", "metric_failed"
            elif status == "ok" and decision == "indeterminate":
                outcome, kind = "skipped", "metric_without_decision_rule"
            else:
                outcome, kind = "errors", "metric_incomplete"
            metric_reason = (
                metric.get("reason")
                if content_included
                else reason_code(metric.get("reason"))
            )
            message = f"{label}: {decision if status == 'ok' else status}"
            testcase(
                "aibench.metric",
                name + " " + label,
                outcome=outcome,
                message=message,
                detail=(
                    _display(metric_reason, content=content_included)
                    if metric_reason
                    else ""
                ),
                kind=kind,
            )

    for gate in report.get("gates", []):
        if not isinstance(gate, Mapping):
            continue
        status = str(gate.get("status", "undecided"))
        outcome = {"fail": "failures", "undecided": "errors"}.get(status)
        testcase(
            "aibench.release_gate",
            str(gate.get("gate_id", "release gate")),
            outcome=outcome,
            message=f"release gate {status}",
            detail=_display(gate.get("reason"), content=content_included),
            kind="release_gate",
        )

    run = report.get("run", {})
    if report.get("outcome", {}).get("complete") is not True:
        unhealthy = report.get("outcome", {}).get("unhealthy_work", {})
        testcase(
            "aibench.run",
            "run completion",
            outcome="errors",
            message=f"run is incomplete ({run.get('status', 'unknown')})",
            detail=json.dumps(unhealthy, sort_keys=True),
            kind="run_incomplete",
        )
    suite.set("tests", str(counts["tests"]))
    suite.set("failures", str(counts["failures"]))
    suite.set("errors", str(counts["errors"]))
    suite.set("skipped", str(counts["skipped"]))
    suite.set("time", f"{total_time:.6f}")
    root.set("tests", str(counts["tests"]))
    root.set("failures", str(counts["failures"]))
    root.set("errors", str(counts["errors"]))
    root.set("skipped", str(counts["skipped"]))
    root.set("time", f"{total_time:.6f}")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True).decode("utf-8") + "\n"


def _rule_id(prefix: str, identity: str) -> str:
    digest = hashlib.sha256(identity.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"aibench/{prefix}/{digest}"


def render_sarif(report: Mapping[str, Any], case_export: Mapping[str, Any]) -> str:
    """Render failed and unresolved benchmark results as SARIF 2.1.0 findings."""

    rules: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    content_included = case_export.get("content") == "included"
    scoring_id = case_export.get("scoring_id")
    run_id = report.get("run", {}).get("run_id")

    def add_result(
        *,
        rule_id: str,
        rule_name: str,
        title: str,
        level: str,
        kind: str,
        message: str,
        properties: dict[str, Any],
        detail: str | None = None,
    ) -> None:
        rules.setdefault(
            rule_id,
            {
                "id": rule_id,
                "name": rule_name,
                "shortDescription": {"text": _display(title)},
                "defaultConfiguration": {"level": level},
            },
        )
        text = message if not detail else f"{message} {detail}"
        results.append(
            {
                "ruleId": rule_id,
                "level": level,
                "kind": kind,
                "message": {"text": _display(text)},
                "properties": properties,
            }
        )

    for row in case_export.get("rows", []):
        if not isinstance(row, Mapping):
            continue
        case_id = str(row.get("case_id", "unknown"))
        repetition = row.get("repetition")
        execution = row.get("execution")
        if (
            not isinstance(execution, Mapping)
            or execution.get("status") not in ("ok", "not_applicable")
        ):
            state = str(execution.get("status")) if isinstance(execution, Mapping) else str(row.get("work_state", "missing"))
            identity = "application_execution"
            rule_id = _rule_id("execution", identity)
            error = execution.get("error") if isinstance(execution, Mapping) else None
            add_result(
                rule_id=rule_id,
                rule_name="Application execution",
                title="Application execution did not complete successfully",
                level="error",
                kind="fail",
                message=f"Application execution {state} for case {case_id}, repetition {repetition}.",
                detail=_display(error) if error and content_included else None,
                properties={
                    "runId": run_id,
                    "caseId": case_id,
                    "repetition": repetition,
                    "executionStatus": state,
                },
            )
        for metric in row.get("metrics", []):
            if not isinstance(metric, Mapping):
                continue
            status = str(metric.get("status", "not_recorded"))
            decision = str(metric.get("decision", "not_evaluated"))
            if status == "not_applicable" or (status == "ok" and decision in ("pass", "indeterminate")):
                continue
            metric_id = str(metric.get("metric_id", "metric"))
            version = str(metric.get("metric_version", "unknown"))
            binding = str(metric.get("binding_hash") or f"{metric_id}@{version}")
            rule_id = _rule_id("metric", binding)
            failed = status == "ok" and decision == "fail"
            state = decision if status == "ok" else status
            reason = metric.get("reason") if content_included else reason_code(metric.get("reason"))
            add_result(
                rule_id=rule_id,
                rule_name=metric_id,
                title=f"Metric {metric.get('label') or metric_id}",
                level="error" if failed or status in ("error", "cancelled", "skipped", "pending", "not_recorded") else "warning",
                kind="fail" if failed else "review",
                message=f"Metric {metric.get('label') or metric_id} is {state} for case {case_id}, repetition {repetition}.",
                detail=_display(reason, content=content_included) if reason else None,
                properties={
                    "runId": run_id,
                    "scoringId": scoring_id,
                    "caseId": case_id,
                    "repetition": repetition,
                    "metricId": metric_id,
                    "metricVersion": version,
                    "bindingHash": binding,
                    "resultId": metric.get("result_id"),
                    "decision": decision,
                    "status": status,
                },
            )

    for gate in report.get("gates", []):
        if not isinstance(gate, Mapping) or gate.get("status") == "pass":
            continue
        gate_id = str(gate.get("gate_id", "unknown"))
        status = str(gate.get("status", "undecided"))
        failed = status == "fail"
        add_result(
            rule_id=_rule_id("gate", gate_id),
            rule_name=gate_id,
            title=f"Release gate {status}",
            level="error" if failed else "warning",
            kind="fail" if failed else "review",
            message=f"Release gate {gate_id} is {status}.",
            detail=_display(gate.get("reason"), content=content_included) if gate.get("reason") else None,
            properties={"runId": run_id, "gateId": gate_id, "status": status},
        )

    outcome = report.get("outcome", {})
    if outcome.get("complete") is not True:
        add_result(
            rule_id=_rule_id("run", "incomplete"),
            rule_name="Incomplete benchmark run",
            title="Benchmark run did not complete",
            level="error",
            kind="review",
            message=f"Run {run_id} is incomplete ({report.get('run', {}).get('status', 'unknown')}).",
            properties={
                "runId": run_id,
                "runStatus": report.get("run", {}).get("status"),
                "unhealthyWork": outcome.get("unhealthy_work", {}),
            },
        )

    document = {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "BenchCraft",
                        "version": __version__,
                        "informationUri": "https://github.com/HaisamAbbas/BenchCraft",
                        "rules": list(rules.values()),
                    }
                },
                "invocations": [
                    {
                        "executionSuccessful": True,
                        "properties": {"runId": run_id, "scoringId": scoring_id},
                    }
                ],
                "results": results,
                "properties": {
                    "runId": run_id,
                    "runStatus": report.get("run", {}).get("status"),
                    "scoringId": scoring_id,
                    "content": case_export.get("content"),
                    "selectedCaseRepetitions": case_export.get("row_count", 0),
                    "outcome": outcome,
                },
            }
        ],
    }
    return json.dumps(sanitize_value(document), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def render_ci_report(
    report: Mapping[str, Any], case_export: Mapping[str, Any], fmt: str
) -> str:
    if fmt == "junit":
        return render_junit(report, case_export)
    if fmt == "sarif":
        return render_sarif(report, case_export)
    raise ValueError(f"unknown CI report format {fmt!r}")


__all__ = ["SARIF_SCHEMA", "render_ci_report", "render_junit", "render_sarif"]
