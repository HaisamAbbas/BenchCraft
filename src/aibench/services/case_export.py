"""Complete, content-controlled case/repetition exports from committed run facts."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from aibench.core.models import EvaluationResult, ExecutionResult, deep_unfreeze
from aibench.engine.engine import parse_work_item_key
from aibench.reporting.aggregation import reason_code
from aibench.services.reports import ReportError, build_report
from aibench.services.scoring import select_final_executions
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import Storage

CASE_EXPORT_SCHEMA = "aibench.case-results/1"
CASE_ROW_SCHEMA = "aibench.case-result/1"
CASE_EXPORT_FORMATS = {"jsonl", "csv"}
CASE_EXPORT_CSV_COLUMNS = (
    "schema",
    "run_id",
    "run_status",
    "scoring_id",
    "case_id",
    "repetition",
    "work_state",
    "content",
    "execution_id",
    "attempt",
    "execution_status",
    "error_kind",
    "wall_ms",
    "timing",
    "cost_usd",
    "usage",
    "case",
    "output",
    "error",
    "retrieved_context",
    "tool_events",
    "world_state",
    "trace_refs",
    "observation_completeness",
    "metrics",
)


def _case_key(result: EvaluationResult) -> tuple[str, int]:
    return result.case_id, result.repetition_id


def _metric_binding(result: EvaluationResult) -> str:
    return result.binding_hash or f"{result.metric_id}@{result.metric_version}"


def _execution_row(execution: ExecutionResult | None, *, include_content: bool) -> dict[str, Any] | None:
    if execution is None:
        return None
    timing = deep_unfreeze(execution.timing) or {}
    return {
        "execution_id": execution.execution_id,
        "attempt": execution.attempt_id,
        "status": execution.status.value,
        "error_kind": execution.error_kind.value if execution.error_kind else None,
        "timing": timing,
        "cost_usd": execution.cost,
        "usage": deep_unfreeze(execution.usage) if include_content else None,
        "output": deep_unfreeze(execution.output) if include_content else None,
        "error": execution.error if include_content else None,
        "retrieved_context": (
            list(execution.retrieved_context)
            if include_content and execution.retrieved_context is not None
            else None
        ),
        "tool_events": (
            deep_unfreeze(execution.tool_events) if include_content else None
        ),
        "world_state": deep_unfreeze(execution.world_state) if include_content else None,
        "trace_refs": list(execution.trace_refs),
        "observation_completeness": deep_unfreeze(execution.observation_completeness),
    }


def _metric_row(
    result: EvaluationResult,
    *,
    label: str,
    include_content: bool,
) -> dict[str, Any]:
    return {
        "result_id": result.result_id,
        "metric_id": result.metric_id,
        "metric_version": result.metric_version,
        "label": label,
        "binding_hash": result.binding_hash,
        "status": result.status.value,
        "decision": result.decision.value,
        "value": (
            deep_unfreeze(result.value.value)
            if include_content and result.value is not None
            else None
        ),
        "reason": result.reason if include_content else reason_code(result.reason),
        "direction": result.direction.value if result.direction else None,
        "scope": result.scope.value,
        "uncertainty": deep_unfreeze(result.uncertainty) if include_content else None,
        "resources": deep_unfreeze(result.resources) if include_content else None,
        "evidence_refs": list(result.evidence_refs),
        "raw_artifact_ref": result.raw_artifact_ref,
        "execution_id": result.execution_id,
        "attempt": result.attempt_number,
    }


def build_case_export(
    storage: Storage,
    artifacts: ArtifactStore,
    run_id: str,
    *,
    scoring_id: str | None = None,
    include_content: bool = True,
    report_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one row for every selected (case, repetition), without invoking plugins."""

    report = report_document
    if report is None:
        report = build_report(storage, artifacts, run_id, include_content=include_content)
    elif report.get("run", {}).get("run_id") != run_id:
        raise ReportError("case export report does not match the requested run")
    run = report["run"]
    scoring_passes = report["scoring_passes"]
    selected_scoring_id = (
        scoring_id if scoring_id is not None else report["evidence"]["scoring_id"]
    )
    selected_pass = next(
        (item for item in scoring_passes if item["scoring_id"] == selected_scoring_id), None
    )
    if selected_scoring_id is not None and selected_pass is None:
        raise ReportError(f"no scoring pass recorded with scoring_id={selected_scoring_id!r}")

    record = storage.get_run(run_id)
    if record is None:  # build_report already checked; protects concurrent deletion.
        raise ReportError(f"no run committed with run_id={run_id!r}")
    work_items = storage.list_work_items(run_id)
    selected: dict[tuple[str, int], str] = {}
    if work_items:
        for item in work_items:
            if item.kind != "execution":
                continue
            try:
                case_id, repetition, _ = parse_work_item_key(item.task_key, item.kind)
            except ValueError as exc:
                raise ReportError("stored execution selection is malformed") from exc
            key = (case_id, repetition)
            if key in selected:
                raise ReportError("stored execution selection contains a duplicate case/repetition")
            selected[key] = item.state.value
        selected_basis = "planned execution work items"
    else:
        selected_basis = "recorded final executions (legacy run without a work graph)"

    finals = select_final_executions(storage.list_execution_attempts(run_id))
    executions = {(item.case_id, item.repetition_id): item for item in finals}
    for key in executions:
        selected.setdefault(key, "recorded")
    cases = {
        item.case_id: item
        for item in storage.list_cases(record.manifest.dataset_hash)
    }
    if selected_scoring_id is None:
        metric_results = [
            result for result in storage.list_metric_results(run_id) if result.scoring_id is None
        ]
    else:
        metric_results = storage.list_metric_results(run_id, scoring_id=selected_scoring_id)
    results_by_item: dict[tuple[str, int], list[EvaluationResult]] = defaultdict(list)
    for result in metric_results:
        results_by_item[_case_key(result)].append(result)
        selected.setdefault(_case_key(result), "recorded")
    metric_definitions = list(selected_pass["metrics"]) if selected_pass else []
    rows: list[dict[str, Any]] = []
    for case_id, repetition in sorted(selected):
        key = (case_id, repetition)
        case = cases.get(case_id)
        actual = list(results_by_item.get(key, []))
        used: set[str] = set()
        metrics: list[dict[str, Any]] = []
        for definition in metric_definitions:
            binding = definition.get("binding_hash")
            profile = definition.get("profile") or {}
            metric_id = profile.get("evaluator_id")
            version = profile.get("version")
            binding_key = binding or (
                f"{metric_id}@{version}" if metric_id and version else ""
            )
            matching = [result for result in actual if _metric_binding(result) == binding_key]
            if matching:
                for result in matching:
                    used.add(result.result_id)
                    metrics.append(
                        _metric_row(
                            result,
                            label=str(definition.get("label") or result.metric_id),
                            include_content=include_content,
                        )
                    )
            else:
                metrics.append(
                    {
                        "result_id": None,
                        "metric_id": metric_id,
                        "metric_version": version,
                        "label": definition.get("label"),
                        "binding_hash": binding,
                        "status": "pending" if run["provisional"] else "not_recorded",
                        "decision": "not_evaluated",
                        "value": None,
                        "reason": "result_missing",
                        "direction": profile.get("direction"),
                        "scope": profile.get("scope"),
                        "uncertainty": None,
                        "resources": None,
                        "evidence_refs": [],
                        "raw_artifact_ref": None,
                        "execution_id": None,
                        "attempt": None,
                    }
                )
        for result in actual:
            if result.result_id not in used:
                metrics.append(
                    _metric_row(
                        result,
                        label=f"{result.metric_id}@{result.metric_version}",
                        include_content=include_content,
                    )
                )
        rows.append(
            {
                "schema": CASE_ROW_SCHEMA,
                "run_id": run_id,
                "run_status": run["status"],
                "scoring_id": selected_scoring_id,
                "case_id": case_id,
                "repetition": repetition,
                "work_state": selected[key],
                "content": "included" if include_content else "withheld",
                "case": case.model_dump(mode="json") if include_content and case else None,
                "execution": _execution_row(
                    executions.get(key), include_content=include_content
                ),
                "metrics": metrics,
            }
        )
    return {
        "schema": CASE_EXPORT_SCHEMA,
        "run_id": run_id,
        "run_status": run["status"],
        "scoring_id": selected_scoring_id,
        "selected_item_basis": selected_basis,
        "row_count": len(rows),
        "content": "included" if include_content else "withheld",
        "rows": rows,
    }


def _json_cell(value: Any) -> str:
    if value is None:
        return ""
    return json.dumps(
        _json_safe(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            return _json_safe(dump(mode="json"))
        except TypeError:
            return _json_safe(dump())
    return str(value)


def export_path_component(run_id: str) -> str:
    """Make a deterministic workspace-local directory name from an untrusted run ID."""

    slug = re.sub(r"[^A-Za-z0-9_-]", "_", run_id).strip("_")[:64] or "run"
    suffix = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{suffix}"


def _spreadsheet_safe(value: Any) -> Any:
    """Prefix formula-like CSV text so opening the export cannot execute a cell formula."""

    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def render_case_export(document: Mapping[str, Any], fmt: str) -> str:
    """Render one selected item per JSONL line or stable, flattened CSV row."""

    rows = document.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        raise ReportError("case export rows are malformed")
    if fmt == "jsonl":
        return "".join(
            json.dumps(
                _json_safe(row),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
            for row in rows
        )
    if fmt != "csv":
        raise ReportError("case export format must be jsonl or csv")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output, fieldnames=CASE_EXPORT_CSV_COLUMNS, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for row in rows:
        execution = row.get("execution") or {}
        timing = execution.get("timing") or {}
        cells = {
            "schema": row.get("schema"),
            "run_id": row.get("run_id"),
            "run_status": row.get("run_status"),
            "scoring_id": row.get("scoring_id"),
            "case_id": row.get("case_id"),
            "repetition": row.get("repetition"),
            "work_state": row.get("work_state"),
            "content": row.get("content"),
            "execution_id": execution.get("execution_id"),
            "attempt": execution.get("attempt"),
            "execution_status": execution.get("status"),
            "error_kind": execution.get("error_kind"),
            "wall_ms": timing.get("wall_ms"),
            "timing": _json_cell(timing),
            "cost_usd": execution.get("cost_usd"),
            "usage": _json_cell(execution.get("usage")),
            "case": _json_cell(row.get("case")),
            "output": _json_cell(execution.get("output")),
            "error": _json_cell(execution.get("error")),
            "retrieved_context": _json_cell(execution.get("retrieved_context")),
            "tool_events": _json_cell(execution.get("tool_events")),
            "world_state": _json_cell(execution.get("world_state")),
            "trace_refs": _json_cell(execution.get("trace_refs")),
            "observation_completeness": _json_cell(
                execution.get("observation_completeness")
            ),
            "metrics": _json_cell(row.get("metrics")),
        }
        writer.writerow(
            {key: _spreadsheet_safe(_json_safe(value)) for key, value in cells.items()}
        )
    return output.getvalue()


__all__ = [
    "CASE_EXPORT_CSV_COLUMNS",
    "CASE_EXPORT_FORMATS",
    "CASE_EXPORT_SCHEMA",
    "CASE_ROW_SCHEMA",
    "build_case_export",
    "export_path_component",
    "render_case_export",
]
