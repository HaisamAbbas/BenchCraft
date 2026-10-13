"""Opt-in report summaries by a selected dataset group or metadata field."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from aibench.core.models import (
    BenchmarkCase,
    EvaluationResult,
    EvaluatorManifest,
    ExecutionResult,
    ExecutionStatus,
    WorkItem,
    deep_unfreeze,
)
from aibench.engine.engine import parse_work_item_key
from aibench.reporting.aggregation import reason_code, summarize
from aibench.security.redaction import sanitize
from aibench.services.performance import (
    stream_performance_summary,
    successful_latency_values,
    summarize_latency,
)

MAX_GROUP_BY_FIELDS = 8
MAX_SEGMENT_GROUPS = 100
_MISSING = object()


def validate_group_by(fields: Sequence[str]) -> tuple[str, ...]:
    """Accept explicit dataset group IDs or dotted paths below case metadata."""
    if len(fields) > MAX_GROUP_BY_FIELDS:
        raise ValueError(f"at most {MAX_GROUP_BY_FIELDS} --group-by fields are allowed")
    result: list[str] = []
    for field in fields:
        if not isinstance(field, str) or len(field) > 256 or any(ord(char) < 32 for char in field):
            raise ValueError(
                "--group-by values must be non-empty field paths of at most 256 characters"
            )
        if field != "group_id" and not field.startswith("metadata."):
            raise ValueError("--group-by must be group_id or metadata.<field.path>")
        parts = field.split(".")
        if any(not part.strip() for part in parts):
            raise ValueError("--group-by field paths cannot contain empty components")
        if field not in result:
            result.append(field)
    return tuple(result)


def _field_value(case: BenchmarkCase, field: str) -> Any:
    if field == "group_id":
        return case.group_id if case.group_id is not None else _MISSING
    value: Any = deep_unfreeze(case.metadata)
    for component in field.removeprefix("metadata.").split("."):
        if not isinstance(value, Mapping) or component not in value:
            return _MISSING
        value = value[component]
    return value


def _identity(value: Any) -> tuple[str, dict[str, Any]]:
    if value is _MISSING:
        record = {"state": "missing"}
    elif value is None:
        record = {"state": "null"}
    else:
        record = {"state": "value", "value": value}
    return json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")), record


def _segment_value_record(
    field: str,
    identity_key: str,
    identity: dict[str, Any],
    *,
    digest_salt: bytes | None,
    include_content: bool,
) -> dict[str, Any]:
    digest_input = f"{field}\0{identity_key}".encode()
    digest = (
        hmac.new(digest_salt, digest_input, hashlib.sha256).hexdigest()
        if digest_salt is not None
        else hashlib.sha256(digest_input).hexdigest()
    )[:16]
    state = identity["state"]
    result: dict[str, Any] = {
        "key": digest,
        "value_type": state,
        "value_redacted": not include_content,
    }
    if state == "missing":
        result.update(label="(missing)", value=None, value_redacted=False)
    elif state == "ambiguous":
        result.update(label="(ambiguous case metadata)", value=None, value_redacted=False)
    elif not include_content:
        result.update(label=f"[redacted:{digest}]", value=None)
    else:
        value = identity.get("value")
        if isinstance(value, str):
            label = sanitize(value)
            truncated = len(label) > 160
            result["label"] = label[:159] + "…" if truncated else label
            result["value"] = result["label"] if truncated else value
            result["value_truncated"] = truncated
        elif isinstance(value, (dict, list)):
            preview = sanitize(json.dumps(value, ensure_ascii=False, sort_keys=True))
            truncated = len(preview) > 160
            result["label"] = preview[:159] + "…" if truncated else preview
            result["value"] = result["label"]
            result["value_type"] = "structured"
            result["value_truncated"] = truncated
        else:
            result["label"] = json.dumps(value, ensure_ascii=False)
            result["value"] = value
    return result


def _case_id_from_item(item: WorkItem) -> str | None:
    try:
        return parse_work_item_key(item.task_key, item.kind)[0]
    except ValueError:
        return None


def build_segment_analysis(
    fields: Sequence[str],
    *,
    cases: Sequence[BenchmarkCase],
    work_items: Sequence[WorkItem],
    executions: Sequence[ExecutionResult],
    scoring_passes: Sequence[dict[str, Any]],
    include_content: bool,
) -> dict[str, Any]:
    """Summarize selected work and metric outcomes by each requested field independently.

    Planned work items define denominators where available. Legacy records without a work
    graph summarize only stored observations, preserving their smaller known denominator.
    """
    selected_case_ids = {
        case_id
        for item in work_items
        if not item.warmup and (case_id := _case_id_from_item(item)) is not None
    }
    final_executions = list(executions)
    selected_case_ids.update(e.case_id for e in final_executions if not e.warmup)
    for scoring_pass in scoring_passes:
        for results in scoring_pass["results"].values():
            selected_case_ids.update(result.case_id for result in results)

    case_records: dict[str, list[BenchmarkCase]] = defaultdict(list)
    for case in cases:
        case_records[case.case_id].append(case)

    # Content-withheld reports need an unpredictable per-document key so low-entropy values
    # cannot be guessed from their label. Full reports retain stable IDs across regeneration.
    digest_salt = None if include_content else secrets.token_bytes(32)
    segment_fields: list[dict[str, Any]] = []
    for field in fields:
        identity_by_key: dict[str, dict[str, Any]] = {}
        cases_by_identity: dict[str, set[str]] = defaultdict(set)
        for case_id in selected_case_ids:
            records = case_records.get(case_id, [])
            identities = {_identity(_field_value(case, field))[0] for case in records}
            if not records:
                identity = {"state": "missing"}
            elif len(identities) > 1:
                identity = {"state": "ambiguous"}
            else:
                identity = _identity(_field_value(records[0], field))[1]
            canonical = json.dumps(
                identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            identity_by_key[canonical] = identity
            cases_by_identity[canonical].add(case_id)

        ordered = sorted(
            cases_by_identity,
            key=lambda key: (-len(cases_by_identity[key]), key),
        )
        omitted = ordered[MAX_SEGMENT_GROUPS - 1 :] if len(ordered) > MAX_SEGMENT_GROUPS else []
        visible = ordered[: MAX_SEGMENT_GROUPS - 1] if omitted else ordered
        group_cases: dict[str, set[str]] = {}
        group_values: dict[str, dict[str, Any]] = {}
        case_group: dict[str, str] = {}
        for identity_key in visible:
            group = _segment_value_record(
                field,
                identity_key,
                identity_by_key[identity_key],
                digest_salt=digest_salt,
                include_content=include_content,
            )
            key = group["key"]
            group_cases[key] = cases_by_identity[identity_key]
            group_values[key] = group
            for case_id in group_cases[key]:
                case_group[case_id] = key
        if omitted:
            omitted_cases = set().union(*(cases_by_identity[key] for key in omitted))
            key = "other"
            group_cases[key] = omitted_cases
            group_values[key] = {
                "key": key,
                "label": f"other ({len(omitted)} omitted values)",
                "value": None,
                "value_type": "omitted",
                "value_redacted": not include_content,
                "value_count": len(omitted),
            }
            for case_id in omitted_cases:
                case_group[case_id] = key

        execution_plan: Counter[str] = Counter()
        warmup_plan: Counter[str] = Counter()
        evaluation_plan: dict[str, Counter[str]] = defaultdict(Counter)
        for item in work_items:
            case_id = _case_id_from_item(item)
            if case_id is None or case_id not in case_group:
                continue
            if item.kind == "execution":
                (warmup_plan if item.warmup else execution_plan)[case_group[case_id]] += 1
            elif item.kind == "evaluation" and not item.warmup:
                try:
                    _, _, binding_key = parse_work_item_key(item.task_key, item.kind)
                except ValueError:
                    continue
                if binding_key:
                    evaluation_plan[binding_key][case_group[case_id]] += 1

        measured_by_group: dict[str, list[ExecutionResult]] = defaultdict(list)
        warmup_by_group: dict[str, list[ExecutionResult]] = defaultdict(list)
        for execution in final_executions:
            group_key = case_group.get(execution.case_id)
            if group_key is None:
                continue
            (warmup_by_group if execution.warmup else measured_by_group)[group_key].append(
                execution
            )

        scoring_results_by_group: list[dict[str, dict[str, list[EvaluationResult]]]] = []
        for scoring_pass in scoring_passes:
            pass_results: dict[str, dict[str, list[EvaluationResult]]] = {}
            for binding_hash, results in scoring_pass["results"].items():
                grouped: dict[str, list[EvaluationResult]] = defaultdict(list)
                for result in results:
                    group_key = case_group.get(result.case_id)
                    if group_key is not None:
                        grouped[group_key].append(result)
                pass_results[binding_hash] = grouped
            scoring_results_by_group.append(pass_results)

        field_groups = []
        for group_key, members in group_cases.items():
            value_record = group_values[group_key]
            measured_executions = measured_by_group.get(group_key, [])
            warmup_executions = warmup_by_group.get(group_key, [])
            app_status = Counter(execution.status.value for execution in measured_executions)
            app_eligible = sum(
                execution.status is ExecutionStatus.OK and not execution.cache
                for execution in measured_executions
            )
            app_latency = successful_latency_values(measured_executions, warmup=False)
            app_planned = execution_plan[group_key] if work_items else None
            warmup_planned = warmup_plan[group_key] if work_items else None
            app_data: dict[str, Any] = {
                "planned_requests": app_planned,
                "recorded_requests": len(measured_executions),
                "missing_requests": (
                    max(0, app_planned - len(measured_executions))
                    if app_planned is not None
                    else None
                ),
                "completed_requests": app_status.get(ExecutionStatus.OK.value, 0),
                "failed_requests": len(measured_executions)
                - app_status.get(ExecutionStatus.OK.value, 0),
                "by_status": dict(sorted(app_status.items())),
                "latency_ms": {
                    **summarize_latency(app_latency),
                    "eligible_successful_requests": app_eligible,
                    "missing_measurements": app_eligible - len(app_latency),
                },
                "warmup": {
                    "planned_requests": warmup_planned,
                    "recorded_requests": len(warmup_executions),
                    "completed_requests": sum(
                        execution.status is ExecutionStatus.OK for execution in warmup_executions
                    ),
                    "latency_ms": summarize_latency(
                        successful_latency_values(warmup_executions, warmup=True)
                    ),
                },
                "streaming": stream_performance_summary(measured_executions, warmup=False),
                "warmup_streaming": stream_performance_summary(warmup_executions, warmup=True),
            }

            segment_pass_summaries = []
            for pass_index, scoring_pass in enumerate(scoring_passes):
                metric_summaries = []
                results_by_binding = scoring_results_by_group[pass_index]
                for binding_hash, profile in sorted(scoring_pass["profiles"].items()):
                    manifest = EvaluatorManifest.model_validate(profile["manifest"])
                    group_results = results_by_binding.get(binding_hash, {}).get(group_key, [])
                    if not work_items:
                        planned = None
                    elif scoring_pass["kind"] == "engine":
                        suffix = binding_hash[7:23]
                        planned = (
                            evaluation_plan[suffix][group_key]
                            if suffix in evaluation_plan
                            else None
                        )
                    else:
                        planned = execution_plan[group_key]
                    summary = summarize(
                        group_results,
                        manifest=manifest,
                        binding_hash=binding_hash,
                        params=profile.get("params") or {},
                        planned=planned,
                        missing="pending" if scoring_pass["kind"] == "engine" else "unavailable",
                    ).as_dict()
                    failures = Counter(
                        reason_code(result.reason) or "unclassified"
                        for result in group_results
                        if result.status is ExecutionStatus.ERROR
                    )
                    metric_summaries.append(
                        {
                            "metric": f"{manifest.evaluator_id}@{manifest.version}",
                            "binding_hash": binding_hash,
                            "summary": summary,
                            "unstable_results": sum(
                                result.status is ExecutionStatus.OK
                                and (result.reason or "").startswith("unstable:")
                                for result in group_results
                            ),
                            "evaluator_failures": dict(sorted(failures.items())),
                        }
                    )
                segment_pass_summaries.append(
                    {
                        "scoring_id": scoring_pass["scoring_id"],
                        "kind": scoring_pass["kind"],
                        "metrics": metric_summaries,
                    }
                )

            field_groups.append(
                {
                    **value_record,
                    "case_count": len(members),
                    "application": app_data,
                    "scoring_passes": segment_pass_summaries,
                }
            )

        for index, group in enumerate(field_groups, start=1):
            suffix = f" [segment:{index:03d}]"
            group["label"] = group["label"][: 160 - len(suffix)] + suffix
            group.pop("key", None)

        field_segments: dict[str, Any] = {
            "field": field,
            "selected_cases": len(selected_case_ids),
            "category_count": len(cases_by_identity),
            "omitted_categories": len(omitted),
            "omitted_cases": sum(len(cases_by_identity[key]) for key in omitted),
            "max_groups": MAX_SEGMENT_GROUPS,
            "groups": field_groups,
            "basis": (
                "planned work-item denominators where available; otherwise recorded executions "
                "and metric results only. Warmup requests are reported separately and do not "
                "enter measured metric or latency summaries."
            ),
        }
        segment_fields.append(field_segments)
    return {"fields": segment_fields}
