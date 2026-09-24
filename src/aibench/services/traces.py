"""Importing OpenTelemetry traces into a run (16-T2), shared by `aibench traces` and
reports. The raw export is kept as a restricted artifact; each trace becomes one normalized
observation, attached to the execution whose correlation ID it carries.

A run keeps one observation per trace ID. When a later file carries more of a trace (an
exporter that appends, or a trace split across batches), the spans of every file that
carried it are merged, exact duplicates once, and the trace is normalized again, so its
usage is never added up twice and its completeness reflects all of its spans."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from aibench.core.hashes import bytes_hash
from aibench.core.models import RedactionClass
from aibench.observations.otel import Trace, normalize, parse_otlp
from aibench.services.runs import RunError
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import Storage


def import_traces(
    storage: Storage, artifacts: ArtifactStore, run_id: str, path: Path
) -> dict[str, Any]:
    """Import one OTLP/JSON file into `run_id`. Importing the same file again adds nothing.
    Raises `TraceFormatError` for a file that is not a trace export."""
    if storage.get_run(run_id) is None:
        raise RunError(f"no run committed with run_id={run_id!r}")
    data = path.read_bytes()
    traces = parse_otlp(data)
    import_id = "traces-" + bytes_hash(run_id.encode() + b"\0" + data)[7:23]
    ref = artifacts.write_bytes(
        data,
        mime_type="application/json",
        run_id=run_id,
        redaction=RedactionClass.RESTRICTED,
        artifact_id=f"{import_id}:raw",
    )
    commit_verified_artifact(artifacts, storage, ref)
    existing = {o["trace_id"]: o for o in storage.list_trace_observations(run_id)}
    parsed: dict[str, list[Trace]] = {ref.artifact_id: traces}

    def spans_from(artifact_id: str) -> list[Trace]:
        if artifact_id not in parsed:
            prior = storage.get_artifact(artifact_id)
            parsed[artifact_id] = parse_otlp(artifacts.read_bytes(prior)) if prior else []
        return parsed[artifact_id]

    by_correlation = {
        e.correlation_id: e.execution_id
        for e in storage.list_execution_attempts(run_id)
        if e.correlation_id
    }
    rows = []
    reasons: Counter[str] = Counter()
    matched = merged = 0
    for trace in traces:
        prior = existing.get(trace.trace_id)
        raw_ids = _raw_ids(prior) if prior else []
        if ref.artifact_id not in raw_ids:
            raw_ids.append(ref.artifact_id)
        if len(raw_ids) > 1:
            trace = _merge(trace.trace_id, [t for a in raw_ids for t in spans_from(a)])
        observation = normalize(trace)
        execution_id = by_correlation.get(observation["correlation_id"] or "")
        matched += execution_id is not None
        for reason in observation["partial_reasons"]:
            reasons[reason.split(":", 1)[0]] += 1
        observation["raw_artifact_ids"] = raw_ids
        if prior is not None:
            if _unchanged(prior, observation, execution_id):
                continue  # this file adds nothing to the trace
            merged += 1
        rows.append(
            (
                trace.trace_id,
                execution_id,
                not observation["partial_reasons"],
                json.dumps(observation),
            )
        )
    added = storage.commit_trace_observations(import_id, run_id, rows) if rows else 0
    summary = {
        "import_id": import_id,
        "file": path.name,
        "traces": len(traces),
        "matched": matched,
        "unmatched": len(traces) - matched,
        "partial": sum(1 for _, _, complete, _ in rows if not complete),
        "partial_reasons": dict(sorted(reasons.items())),
        "added": added,
        "merged_with_earlier_imports": merged,
        "raw_artifact_id": ref.artifact_id,
    }
    if added:
        storage.append_run_event(run_id, "traces_imported", summary)
    return summary


def _raw_ids(observation: dict[str, Any]) -> list[str]:
    ids = observation.get("raw_artifact_ids")
    return list(ids) if ids else [observation["raw_artifact_id"]]


def _merge(trace_id: str, parts: list[Trace]) -> Trace:
    """One trace from every file's part of it: each span once."""
    merged = Trace(trace_id)
    for part in parts:
        if part.trace_id != trace_id:
            continue
        for span in part.spans:
            merged.add(span)
        merged.conflicting += part.conflicting
    return merged


def _unchanged(prior: dict[str, Any], observation: dict[str, Any], execution_id: Any) -> bool:
    bookkeeping = {"import_id", "trace_id", "execution_id", "complete", "raw_artifact_ids",
                   "raw_artifact_id", "duplicate_spans_ignored"}  # fmt: skip
    before = {k: v for k, v in prior.items() if k not in bookkeeping}
    after = {k: v for k, v in observation.items() if k not in bookkeeping}
    return before == after and prior["execution_id"] == execution_id


def traces_summary(storage: Storage, run_id: str) -> dict[str, Any] | None:
    """What a run's imported traces add, for reports: counts, completeness and usage.
    Usage from partial traces is a lower bound, and says so."""
    observations = storage.list_trace_observations(run_id)
    if not observations:
        return None
    reasons: Counter[str] = Counter()
    for o in observations:
        for reason in o["partial_reasons"]:
            reasons[reason.split(":", 1)[0]] += 1
    # One trace per execution: the same call imported from two sources (an OTLP export and
    # Langfuse) is counted once, preferring a complete trace.
    with_usage, seen, duplicates = [], set(), 0
    for o in sorted((o for o in observations if o.get("usage")), key=lambda o: not o["complete"]):
        if o["execution_id"] and o["execution_id"] in seen:
            duplicates += 1
            continue
        seen.add(o["execution_id"])
        with_usage.append(o)
    usage = {
        key: sum(o["usage"][key] for o in with_usage)
        for key in ("input_tokens", "output_tokens", "total_tokens")
    }
    tools = [t for o in observations for t in o.get("tools", [])]
    return {
        "imports": len({a for o in observations for a in _raw_ids(o)}),
        "traces": len(observations),
        "matched_to_executions": sum(1 for o in observations if o["execution_id"]),
        "complete": sum(1 for o in observations if o["complete"]),
        "partial": sum(1 for o in observations if not o["complete"]),
        "partial_reasons": dict(sorted(reasons.items())),
        "usage": {
            **usage,
            "traces_with_usage": len(with_usage),
            "duplicate_traces_excluded": duplicates,
            "aggregate_spans_excluded": sum(
                len(o["usage"]["aggregate_spans_excluded"]) for o in with_usage
            ),
            "bound": ("lower_bound" if any(not o["complete"] for o in with_usage) else "complete"),
            "source": "imported traces (OpenTelemetry normalization otel-gen-ai/1, or "
            "Langfuse observations); separate from usage the application reported in its "
            "responses",
        },
        "tool_spans": len(tools),
        "tool_errors": sum(1 for t in tools if t["status"] == "error"),
    }
