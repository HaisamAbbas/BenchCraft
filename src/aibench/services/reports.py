"""Evidence reports built only from stored facts (§12, §13 `report`, 11-T1).

`build_report` reads committed records: the frozen run manifest and plan, work items,
execution attempts, evaluation attempts and final metric results. It never loads an
evaluator, starts a plugin environment, or invokes the application, so a report can be
regenerated at any time with identical numbers (11-G1). Rendering (JSON, Markdown, HTML)
lives in `reporting.render`; exporting writes sanitized files under `.aibench/reports/`.

What a report states, and on which basis:
- **Metric profiles**, one per binding and scoring pass, never an average across metrics.
  The profile (value kind, direction, aggregation, rule) comes from the manifests frozen
  with the run (or with a rescoring pass). Runs recorded before profiles were frozen fall
  back to the stored results themselves, and say so.
- **Denominators.** The engine pass counts every planned (case, repetition) item as
  selected; unfinished items are `pending`, so a partial snapshot cannot look better than
  the finished run. A rescoring pass retains that selected work, including unavailable
  items without recorded executions. Legacy runs use final recorded executions.
- **Application failures** (the app did not produce a usable output) are separate from
  **evaluator failures** (a metric could not be computed). Neither is a low score.
- **Latency** is the wall time of successful final attempts, p50/p95 by nearest rank.
  Failed and timed-out requests are counted separately, never mixed into the percentiles.
- **Cost** is observed spend plus how complete the accounting is. A total is given only
  when every call reported its cost; otherwise the known amount is a lower bound.
- **Gates** are the plan's predeclared release gates, decided only for a finished run.
- **Evidence** lists non-passing cases with sanitized, truncated excerpts and references.
  Raw evaluator outputs and traces stay in the artifact store; the report names them by
  ID and digest only.
"""

from __future__ import annotations

import json
import math
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from aibench.core.errors import AibenchError
from aibench.core.models import (
    Decision,
    EvaluationResult,
    EvaluatorManifest,
    ExecutionResult,
    ExecutionStatus,
    WorkItemState,
    deep_unfreeze,
)
from aibench.core.plans import ExecutablePlan, ReleaseGate
from aibench.engine.engine import parse_work_item_key, was_dispatched, work_counts
from aibench.reporting.aggregation import MetricSummary, reason_code, summarize
from aibench.reporting.render import render
from aibench.security.redaction import sanitize
from aibench.services.runs import _FINISHED, RunError, _frozen_application, _frozen_plan
from aibench.services.scoring import rescore_selected_count, select_final_executions
from aibench.services.suspect_answers import looks_like_error
from aibench.services.traces import traces_summary
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import Storage

REPORT_SCHEMA = "aibench.report/1"
EXCERPT_CHARS = 300
FORMATS = {"json": "json", "markdown": "md", "html": "html"}
_UNHEALTHY = ("failed", "blocked", "cancelled", "unknown_effect")


class ReportError(AibenchError):
    """A report cannot be built or written as asked."""


def excerpt(value: Any, limit: int = EXCERPT_CHARS) -> str | None:
    """A sanitized, bounded rendering of stored content (credentials and terminal control
    content removed). The full value stays in storage."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(deep_unfreeze(value), default=str)
    text = sanitize(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: the smallest value with at least p% of values at or below
    it. No interpolation, so the result is always an observed value."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


# --------------------------------------------------------------------------- profiles


def _profiles_from_manifest(parameters: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {k: dict(v) for k, v in (parameters.get("metric_profiles") or {}).items()}


def _rescoring_profiles(
    events: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    """scoring_id -> {binding_hash: profile}, and scoring_id -> the event sequence that
    recorded the pass (its order), from `scoring_pass` events."""
    passes: dict[str, dict[str, Any]] = {}
    order: dict[str, int] = {}
    for event in events:
        if event["event_type"] == "scoring_pass":
            payload = event["payload"]
            passes[payload["scoring_id"]] = dict(payload.get("metric_profiles") or {})
            order[payload["scoring_id"]] = int(event["sequence"])
    return passes, order


_DERIVED_AGGREGATION = {"boolean": "rate", "scalar": "mean", "category": "category_counts"}


def _derived_profile(results: list[EvaluationResult]) -> dict[str, Any]:
    """For results recorded before profiles were frozen: what the results themselves say.
    The aggregation follows the value kind (the contract every shipped evaluator uses)."""
    first = results[0]
    kinds = {r.value.kind for r in results if r.value is not None}
    kind = kinds.pop() if len(kinds) == 1 else "structured"
    provenance = deep_unfreeze(first.provenance) or {}
    return {
        "metric": (provenance.get("binding") or {}).get("metric", first.metric_id),
        "manifest": {
            "evaluator_id": first.metric_id,
            "version": first.metric_version,
            "plugin_id": provenance.get("plugin_id", "unknown"),
            "plugin_version": provenance.get("plugin_version", "unknown"),
            "description": "profile derived from stored results (not frozen with the run)",
            "value_kind": kind,
            "direction": (first.direction.value if first.direction else "none"),
            "scope": first.scope.value,
            "aggregation": _DERIVED_AGGREGATION.get(kind, "none"),
        },
        "params": (provenance.get("binding") or {}).get("params") or {},
        "rule": first.rule.model_dump(mode="json") if first.rule else None,
        "compatibility": provenance.get("compatibility"),
        "source": "derived_from_results",
    }


def _profile_manifest(profile: dict[str, Any]) -> EvaluatorManifest:
    return EvaluatorManifest.model_validate(profile["manifest"])


# --------------------------------------------------------------------------- sections


def unstable_count(results: list[EvaluationResult]) -> int:
    """Scored results whose repeated judge calls disagreed (the evaluator says so with an
    `unstable:` reason). The number is recorded, but it should not be trusted alone."""
    return sum(
        1
        for r in results
        if r.status is ExecutionStatus.OK and (r.reason or "").startswith("unstable:")
    )


def _metric_section(
    profile: dict[str, Any],
    binding_hash: str,
    results: list[EvaluationResult],
    planned: int | None,
    *,
    missing: Literal["pending", "unavailable"] = "pending",
) -> dict[str, Any]:
    manifest = _profile_manifest(profile)
    summary: MetricSummary = summarize(
        results,
        manifest=manifest,
        binding_hash=binding_hash,
        params=profile.get("params") or {},
        planned=planned,
        missing=missing,
    )
    return {
        "label": f"{manifest.evaluator_id}@{manifest.version}",
        "binding_hash": binding_hash,
        "profile": {
            "evaluator_id": manifest.evaluator_id,
            "version": manifest.version,
            "plugin": f"{manifest.plugin_id}=={manifest.plugin_version}",
            "description": manifest.description,
            "limitations": list(manifest.limitations),
            "value_kind": manifest.value_kind,
            "direction": manifest.direction.value,
            "scope": manifest.scope.value,
            "aggregation": manifest.aggregation,
            "uses_models": manifest.uses_models,
            "rule": profile.get("rule"),
            "params": profile.get("params") or {},
            "compatibility": profile.get("compatibility"),
            "source": profile.get("source", "frozen_with_run"),
        },
        "summary": summary.as_dict(),
        # Scores whose repeated judge calls disagreed (the evaluator says so with an
        # "unstable:" reason): a number is recorded, but it should not be trusted alone.
        "unstable_results": unstable_count(results),
        # The metric could not be computed (evaluator failures), by reason code (never free
        # text); distinct from cases the application failed, which are `unavailable`.
        "evaluator_failures": dict(
            sorted(
                Counter(
                    reason_code(r.reason) or "unclassified"
                    for r in results
                    if r.status is ExecutionStatus.ERROR
                ).items()
            )
        ),
    }


def _pass_cost(attempts: list[EvaluationResult]) -> dict[str, Any]:
    """Evaluator spend of one scoring pass, from its committed attempts (every attempt,
    including retried ones, is spend)."""
    called = [
        a for a in attempts if (deep_unfreeze(a.resources) or {}).get("latency_ms") is not None
    ]
    known = 0.0
    unknown = 0
    tokens = 0
    unknown_tokens = 0
    for attempt in called:
        resources = deep_unfreeze(attempt.resources) or {}
        cost = resources.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            known += float(cost)
        else:
            unknown += 1
        if resources.get("model_calls") == 0:
            continue
        if isinstance(resources.get("tokens"), dict) and resources["tokens"]:
            tokens += sum(v for v in resources["tokens"].values() if isinstance(v, int))
        else:
            unknown_tokens += 1
    return _cost_block(len(called), known, unknown, tokens=tokens, unknown_tokens=unknown_tokens)


def _cost_block(
    calls: int, known: float, unknown: int, *, tokens: int | None = None, unknown_tokens: int = 0
) -> dict[str, Any]:
    if calls == 0:
        completeness = "no_calls"
    elif unknown == 0:
        completeness = "complete"
    elif unknown == calls:
        completeness = "unknown"
    else:
        completeness = "partial"
    block: dict[str, Any] = {
        "calls": calls,
        "calls_with_known_cost": calls - unknown,
        "calls_with_unknown_cost": unknown,
        "known_cost_usd": round(known, 6),
        # A total exists only when calls were made and every one reported its cost.
        "total_cost_usd": round(known, 6) if calls and unknown == 0 else None,
        "accounting": completeness,
    }
    if tokens is not None:
        block["reported_tokens"] = tokens
        block["calls_with_unknown_tokens"] = unknown_tokens
    return block


def _application_section(
    storage: Storage,
    run_id: str,
    planned_executions: int | None,
    concurrency: int | None,
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[ExecutionResult]]:
    attempts = storage.list_execution_attempts(run_id)
    # Calls that may have reached the application without a committed attempt: in flight
    # when a session died, recorded when the next session recovered them (engine recovery).
    uncommitted = sum(
        int(e["payload"].get("uncommitted_dispatches", 0))
        for e in events
        if e["event_type"] == "recovered"
    )
    finals = select_final_executions(attempts)
    status = Counter(e.status.value for e in finals)
    error_kinds = Counter(e.error_kind.value for e in finals if e.error_kind is not None)
    cache_hits = sum(1 for e in finals if e.cache)
    ok_wall = [
        float(w)
        for e in finals
        if e.status is ExecutionStatus.OK
        and not e.cache  # a cached output is not a fresh latency measurement (§14)
        and isinstance(w := (deep_unfreeze(e.timing) or {}).get("wall_ms"), (int, float))
    ]
    timeouts = sum(
        1 for e in finals if e.error_kind is not None and e.error_kind.value == "timeout"
    )
    failed = sum(1 for e in finals if e.status is not ExecutionStatus.OK)
    error_like = sorted(
        e.case_id for e in finals if e.status is ExecutionStatus.OK and looks_like_error(e.output)
    )
    dispatched = [a for a in attempts if was_dispatched(a)]
    known = sum(float(a.cost) for a in dispatched if a.cost is not None)
    unknown = sum(1 for a in dispatched if a.cost is None) + uncommitted
    section = {
        "planned": planned_executions,
        "recorded": len(finals),
        "not_recorded": None if planned_executions is None else planned_executions - len(finals),
        "completed": status.get("ok", 0),
        "failed": failed,
        "by_status": dict(sorted(status.items())),
        "error_kinds": dict(sorted(error_kinds.items())),
        "attempts": len(attempts),
        "uncommitted_dispatches": uncommitted,
        "retried_items": sum(1 for e in finals if e.attempt_id > 1),
        # Answers that read like the application failing while reporting success (see
        # services.suspect_answers): every metric would score an error message.
        "error_like_answers": {"count": len(error_like), "case_ids": error_like[:10]},
        "cache_hits": cache_hits,
        "latency": {
            "definition": (
                "wall time of the final attempt of each successful request, measured by the "
                "runner around the invocation; p50/p95 by nearest rank. Failed and timed-out "
                "requests are excluded from the percentiles and counted separately. Cache "
                "hits are excluded: a cached output is not a fresh measurement."
            ),
            "successful_requests": len(ok_wall),
            "p50_ms": percentile(ok_wall, 50),
            "p95_ms": percentile(ok_wall, 95),
            "min_ms": min(ok_wall) if ok_wall else None,
            "max_ms": max(ok_wall) if ok_wall else None,
            "excluded_failures": failed - timeouts,
            "excluded_timeouts": timeouts,
            "concurrency": concurrency,
            "cache": (
                f"{cache_hits} cached execution(s) excluded from latency"
                if cache_hits
                else "no cached executions; every latency is a fresh invocation"
            ),
        },
        "cost": _cost_block(len(dispatched) + uncommitted, known, unknown),
    }
    return section, finals


def _cache_section(
    finals: list[ExecutionResult], results: list[EvaluationResult]
) -> dict[str, Any]:
    """Cache hits are labelled (§14): reused observations, not fresh measurements or
    independent repetitions."""
    evaluation_hits = sum(1 for r in results if (deep_unfreeze(r.provenance) or {}).get("cache"))
    return {
        "execution_hits": sum(1 for e in finals if e.cache),
        "executions": len(finals),
        "evaluation_hits": evaluation_hits,
        "evaluations": len(results),
        "note": "cache hits reuse stored observations: they are not fresh latency "
        "measurements or independent repetitions",
    }


def _state_section(params: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """How the application's state was reset between cases (§7), from the frozen manifest
    and the recorded resets: part of what makes the run reproducible."""
    reset = params.get("reset") or {}
    world = params.get("test_world") or None
    resets = Counter(
        str(e["payload"].get("status")) for e in events if e["event_type"] == "app_reset"
    )
    return {
        "reset_policy": reset.get("policy"),
        "reset_hook": reset.get("hook"),
        "reset_mode": reset.get("mode", "none"),
        "test_world": (
            {"world_id": world["world_id"], "seed_hash": world["seed_hash"]} if world else None
        ),
        "resets": dict(sorted(resets.items())),
    }


def _gate_results(
    gates: tuple[ReleaseGate, ...],
    plan: ExecutablePlan,
    metrics_by_binding: dict[int, dict[str, Any] | None],
    finished: bool,
) -> list[dict[str, Any]]:
    out = []
    for gate in gates:
        metric = metrics_by_binding.get(gate.binding)
        entry: dict[str, Any] = {
            "gate_id": gate.gate_id,
            "binding": gate.binding,
            "metric": plan.metrics[gate.binding].metric,
            "min_pass_rate": gate.min_pass_rate,
            "min_completed_coverage": gate.min_completed_coverage,
            "denominator": "selected",
        }
        if metric is None:
            out.append({**entry, "status": "undecided", "reason": "no results for this binding"})
            continue
        summary = metric["summary"]
        selected = summary["selected"]
        passes = summary["decisions"].get(Decision.PASS.value, 0)
        entry.update(
            passes=passes,
            completed=summary["completed"],
            selected=selected,
            pass_rate=None if selected == 0 else round(passes / selected, 6),
            completed_coverage=summary["completed_coverage"],
        )
        if not finished:
            entry.update(status="undecided", reason="the run is not finished (partial snapshot)")
        elif selected == 0:
            entry.update(status="fail", reason="no selected cases")
        else:
            failures = []
            if gate.min_pass_rate is not None and passes / selected < gate.min_pass_rate:
                failures.append(f"pass rate {passes}/{selected} below {gate.min_pass_rate}")
            if (
                gate.min_completed_coverage is not None
                and summary["completed"] / selected < gate.min_completed_coverage
            ):
                failures.append(
                    f"completed coverage {summary['completed']}/{selected} below "
                    f"{gate.min_completed_coverage}"
                )
            entry.update(status="fail" if failures else "pass", reason="; ".join(failures) or None)
        out.append(entry)
    return out


def _evidence(
    finals: list[ExecutionResult],
    results: list[EvaluationResult],
    labels: dict[str, str],
    include_content: bool,
) -> list[dict[str, Any]]:
    """Non-passing (case, repetition) items: application failures, and failed,
    indeterminate or errored metric results."""
    by_item: dict[tuple[str, int], list[EvaluationResult]] = defaultdict(list)
    for r in results:
        if (
            r.decision in (Decision.FAIL, Decision.INDETERMINATE)
            or r.status is ExecutionStatus.ERROR
        ):
            by_item[(r.case_id, r.repetition_id)].append(r)
    executions = {(e.case_id, e.repetition_id): e for e in finals}
    for key, final in executions.items():
        if final.status is not ExecutionStatus.OK:
            by_item.setdefault(key, [])
    items = []
    for case_id, repetition in sorted(by_item):
        execution = executions.get((case_id, repetition))
        entry: dict[str, Any] = {"case_id": case_id, "repetition": repetition}
        if execution is not None:
            entry["execution"] = {
                "execution_id": execution.execution_id,
                "attempt": execution.attempt_id,
                "status": execution.status.value,
                "error_kind": execution.error_kind.value if execution.error_kind else None,
                "wall_ms": (deep_unfreeze(execution.timing) or {}).get("wall_ms"),
                "output_excerpt": excerpt(execution.output) if include_content else None,
                "error_excerpt": excerpt(execution.error) if include_content else None,
                "retrieved_context_items": (
                    None
                    if execution.retrieved_context is None
                    else len(execution.retrieved_context)
                ),
                # What the app retrieved is often the explanation of a wrong answer.
                "retrieved_context_excerpts": (
                    [excerpt(c, 200) for c in execution.retrieved_context[:3]]
                    if include_content and execution.retrieved_context is not None
                    else None
                ),
                "trace_refs": list(execution.trace_refs),
            }
        else:
            entry["execution"] = None
        entry["results"] = [
            {
                "metric": labels.get(r.binding_hash or "", f"{r.metric_id}@{r.metric_version}"),
                "binding_hash": r.binding_hash,
                "status": r.status.value,
                "decision": r.decision.value,
                # Metric values may be arbitrary structured data from a custom evaluator,
                # including case-derived explanations. Keep aggregate summaries available,
                # but omit per-case values whenever case content is withheld.
                "value": deep_unfreeze(r.value.value) if r.value and include_content else None,
                "reason_excerpt": excerpt(r.reason) if include_content else reason_code(r.reason),
                "evidence_refs": list(r.evidence_refs),
                "raw_artifact": r.raw_artifact_ref,
            }
            for r in sorted(
                by_item[(case_id, repetition)], key=lambda r: labels.get(r.binding_hash or "", "")
            )
        ]
        items.append(entry)
    return items


def _raw_artifacts(storage: Storage, results: list[EvaluationResult]) -> dict[str, dict[str, Any]]:
    refs = {}
    for artifact_id in sorted({r.raw_artifact_ref for r in results if r.raw_artifact_ref}):
        ref = storage.get_artifact(artifact_id)
        if ref is not None:
            refs[artifact_id] = {
                "digest": ref.digest,
                "mime_type": ref.mime_type,
                "size_bytes": ref.size_bytes,
                "redaction": ref.redaction.value,
            }
    return refs


# --------------------------------------------------------------------------- report


def build_report(
    storage: Storage, artifacts: ArtifactStore, run_id: str, *, include_content: bool = True
) -> dict[str, Any]:
    """The report document for `run_id`, from stored facts only."""
    record = storage.get_run(run_id)
    if record is None:
        raise RunError(f"no run committed with run_id={run_id!r}")
    manifest = record.manifest
    params = deep_unfreeze(manifest.parameters) or {}
    engine_run = params.get("mode") == "manual_plan"
    plan = _frozen_plan(storage, artifacts, manifest) if engine_run else None
    spec = _frozen_application(storage, artifacts, manifest) if engine_run else None
    if spec is None and manifest.application_id:
        spec = storage.get_application(manifest.application_id)
    events = storage.list_run_events(run_id)
    finished = record.status in _FINISHED
    items = storage.list_work_items(run_id)
    notes: list[str] = []
    if not finished:
        notes.append(
            f"Partial snapshot: the run is {record.status}. Pending work is counted in every "
            "denominator; nothing here is final."
        )
    elif record.status != "completed":
        notes.append(
            f"Partial results: the run ended {record.status}. Work that never ran stays in "
            "every denominator as unavailable, blocked or cancelled."
        )

    planned_exec = sum(1 for w in items if w.kind == "execution") if items else None
    application, finals = _application_section(
        storage, run_id, planned_exec, plan.concurrency.application if plan else None, events
    )
    application["state"] = _state_section(params, events)

    all_results = storage.list_metric_results(run_id)
    all_attempts = storage.list_evaluation_attempts(run_id)
    engine_scoring = params.get("scoring_id")
    rescoring_profiles, pass_order = _rescoring_profiles(events)
    rescore_counts = {
        event["payload"]["scoring_id"]: event["payload"]["selected_count"]
        for event in events
        if event["event_type"] == "scoring_pass" and "selected_count" in event["payload"]
    }
    pass_accounting = {
        event["payload"]["scoring_id"]: event["payload"]
        for event in events
        if event["event_type"] == "scoring_pass_completed"
    }
    frozen = _profiles_from_manifest(params)
    planned_by_key: Counter[str] = Counter(
        str(parse_work_item_key(w.task_key, w.kind)[2]) for w in items if w.kind == "evaluation"
    )
    # The engine pass first, then rescoring passes in the order they were recorded.
    scoring_ids = sorted(
        {r.scoring_id for r in all_results if r.scoring_id} | set(pass_order),
        key=lambda s: (s != engine_scoring, pass_order.get(s, 0), s),
    )
    if engine_scoring and engine_scoring not in scoring_ids and frozen:
        scoring_ids.insert(0, engine_scoring)  # nothing evaluated yet: all pending
    # Evidence and the evaluator cost row describe one pass: the engine's, else the latest.
    primary = (
        engine_scoring
        if engine_scoring in scoring_ids
        else (scoring_ids[-1] if scoring_ids else None)
    )

    passes = []
    labels: dict[str, str] = {}
    metrics_by_binding: dict[int, dict[str, Any] | None] = {}
    derived = False
    for scoring_id in scoring_ids:
        is_engine = scoring_id == engine_scoring
        results = [r for r in all_results if r.scoring_id == scoring_id]
        profiles = frozen if is_engine else rescoring_profiles.get(scoring_id, {})
        by_binding: dict[str, list[EvaluationResult]] = defaultdict(list)
        for r in results:
            by_binding[r.binding_hash or f"{r.metric_id}@{r.metric_version}"].append(r)
        order = list(profiles) + sorted(b for b in by_binding if b not in profiles)
        metrics = []
        for binding_hash in order:
            bound = by_binding.get(binding_hash, [])
            profile = profiles.get(binding_hash)
            if profile is None:
                if not bound:
                    continue
                profile = _derived_profile(bound)
                derived = True
            if is_engine and items:
                section = _metric_section(
                    profile, binding_hash, bound, planned_by_key.get(binding_hash[7:23])
                )
            elif items or scoring_id in rescore_counts:
                # A rescore of an engine run: every planned execution is selected, and one
                # that never produced an execution is unavailable, not silently dropped.
                section = _metric_section(
                    profile,
                    binding_hash,
                    bound,
                    max(
                        rescore_counts.get(scoring_id, rescore_selected_count(items, len(finals))),
                        len(bound),
                    ),
                    missing="unavailable",
                )
            else:
                section = _metric_section(profile, binding_hash, bound, None)
            metrics.append(section)
        repeated = Counter(m["label"] for m in metrics)
        for m in metrics:  # one metric bound twice (e.g. different parameters)
            if repeated[m["label"]] > 1:
                m["label"] = f"{m['label']} #{m['binding_hash'][7:15]}"
        if scoring_id == primary:
            labels = {m["binding_hash"]: m["label"] for m in metrics}
        if is_engine and plan is not None:
            hashes = list(params.get("binding_hashes") or [])
            for index in range(len(plan.metrics)):
                match = hashes[index] if index < len(hashes) else None
                metrics_by_binding[index] = next(
                    (m for m in metrics if m["binding_hash"] == match), None
                )
        if is_engine and items:
            basis = "planned (case, repetition) items of the run"
        elif items:
            basis = (
                "planned (case, repetition) items of the run; items without a recorded "
                "execution are unavailable"
            )
        else:
            basis = "the run's recorded executions (final attempt per case and repetition)"
        passes.append(
            {
                "scoring_id": scoring_id,
                "kind": "engine" if is_engine else "rescore",
                "basis": basis,
                "metrics": metrics,
                "evaluator_cost": _pass_cost(
                    [a for a in all_attempts if a.scoring_id == scoring_id]
                ),
                "budget": pass_accounting.get(scoring_id, {}).get("budget"),
                "quotas": pass_accounting.get(scoring_id, {}).get("quotas"),
                "stop_reason": pass_accounting.get(scoring_id, {}).get("stop_reason"),
            }
        )
    if derived:
        notes.append(
            "Some metric profiles were derived from the stored results, because no profile "
            "was recorded with their scoring pass; value kind and direction come from the "
            "results themselves."
        )
    gates = _gate_results(plan.gates, plan, metrics_by_binding, finished) if plan else []
    primary_results = [r for r in all_results if r.scoring_id == primary] if primary else []
    primary_pass = next((p for p in passes if p["scoring_id"] == primary), None)
    counts = work_counts(storage, run_id)
    approval = storage.get_approval(f"{run_id}:approval")
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(UTC).isoformat(),
        "basis": "stored facts only: no application, evaluator or judge was invoked",
        "as_of_event_sequence": events[-1]["sequence"] if events else 0,
        "run": {
            "run_id": run_id,
            "status": record.status,
            "finished": finished,
            # 09 terms: provisional = may still change (not finished); partial = not every
            # planned item completed normally (also true of a cancelled run).
            "provisional": not finished,
            "partial": record.status != "completed",
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "mode": params.get("mode"),
            "plan_id": plan.plan_id if plan else None,
            "plan_hash": manifest.plan_hash,
            "dataset_hash": manifest.dataset_hash,
            "application_id": manifest.application_id,
            "application_hash": manifest.application_hash,
            "application_revision": spec.revision if spec else None,
            "runner": spec.runner.value if spec else None,
            "effects": spec.effects.value if spec else None,
            "policy_hash": params.get("policy_hash"),
            "plugins": dict(sorted((deep_unfreeze(manifest.plugin_hashes) or {}).items())),
            "seed": manifest.seed,
            "repetitions": plan.repetitions if plan else None,
            "environment": deep_unfreeze(manifest.environment) or {},
            "approved_by": approval.granted_by if approval else None,
        },
        "work": {
            "counts": counts,
            "needs_attention": [
                {"task_key": w.task_key, "state": w.state.value, "reason": excerpt(w.last_error)}
                for w in items
                if w.state
                in (WorkItemState.FAILED, WorkItemState.BLOCKED, WorkItemState.UNKNOWN_EFFECT)
            ],
        },
        "application": application,
        # Imported OpenTelemetry traces (16-T2), kept apart from what responses reported.
        "traces": traces_summary(storage, run_id),
        "cache": _cache_section(finals, all_results),
        "scoring_passes": passes,
        "gates": gates,
        "cost": {
            "application": application["cost"],
            "evaluator": (
                primary_pass["evaluator_cost"] if primary_pass else _cost_block(0, 0.0, 0)
            ),
            "evaluator_scoring_id": primary,
            "planner": {
                "accounting": "not_attributed",
                "note": "planning and conversation usage is recorded per session (/budget), "
                "not per run",
            },
            "note": "observed spend only; estimates are never added to observed totals",
        },
        "evidence": {
            "content": "included" if include_content else "withheld",
            "scoring_id": primary,
            "items": _evidence(finals, primary_results, labels, include_content),
            "raw_artifacts": _raw_artifacts(storage, primary_results),
            "note": "raw evaluator outputs and application traces are stored as separate "
            "artifacts; this report references them by ID and digest only",
        },
        "notes": notes,
    }
    report["outcome"] = outcome_summary(report)
    return report


def outcome_summary(report: dict[str, Any]) -> dict[str, Any]:
    """What the report means for automation: `complete` (finished with no unhealthy work),
    and the gate verdicts. §13 exit codes derive from this."""
    counts = report["work"]["counts"]
    unhealthy = {
        state: sum(counts.get(kind, {}).get(state, 0) for kind in counts) for state in _UNHEALTHY
    }
    unhealthy = {k: v for k, v in unhealthy.items() if v}
    gates = report["gates"]
    return {
        "complete": report["run"]["status"] == "completed" and not unhealthy,
        "unhealthy_work": unhealthy,
        "gates_failed": [g["gate_id"] for g in gates if g["status"] == "fail"],
        "gates_undecided": [g["gate_id"] for g in gates if g["status"] == "undecided"],
    }


# --------------------------------------------------------------------------- export


def report_dir(workspace_root: Path, run_id: str) -> Path:
    return workspace_root / "reports" / run_id


def export_report(report: dict[str, Any], formats: list[str], out_dir: Path) -> dict[str, str]:
    """Write the report in each format to `out_dir` (atomically per file); returns
    format -> path. Only sanitized report content is written."""
    unknown = [f for f in formats if f not in FORMATS]
    if unknown:
        raise ReportError(f"unknown report format(s) {unknown}; use {sorted(FORMATS)}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}
    for fmt in formats:
        path = out_dir / f"report.{FORMATS[fmt]}"
        write_text_atomic(path, render(report, fmt))
        written[fmt] = str(path)
    return written


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def report_facts(report: dict[str, Any], *, evidence_limit: int = 10) -> dict[str, Any]:
    """The aggregates of a report, compact enough for the terminal and the assistant:
    status, gates, one line of counts per metric, application counts, latency and cost,
    plus the first non-passing case IDs. Every number is copied from the report; nothing
    is recomputed."""
    run = report["run"]
    metrics = []
    for scoring in report["scoring_passes"]:
        for m in scoring["metrics"]:
            s = m["summary"]
            metrics.append(
                {
                    "scoring": scoring["kind"],
                    "metric": m["label"],
                    "selected": s["selected"],
                    "completed": s["completed"],
                    "decisions": s["decisions"],
                    "value_summary": s["value_summary"],
                    "evaluator_errors": s["errors"],
                    "unstable_results": m["unstable_results"],
                    "not_applicable": s["not_applicable"],
                    "unavailable": s["unavailable"],
                    "cancelled": s["cancelled"],
                    "pending": s["pending"],
                    "completed_coverage": s["completed_coverage"],
                    "provenance": {
                        "evaluator_id": m["profile"]["evaluator_id"],
                        "version": m["profile"]["version"],
                        "plugin": m["profile"]["plugin"],
                        "source": m["profile"]["source"],
                        "limitations": m["profile"]["limitations"],
                    },
                }
            )
    app = report["application"]
    items = report["evidence"]["items"]
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "provisional": run["provisional"],
        "partial": run["partial"],
        "as_of_event_sequence": report["as_of_event_sequence"],
        "basis": report["basis"],
        "provenance": {
            key: run[key]
            for key in (
                "plan_hash",
                "dataset_hash",
                "application_id",
                "application_hash",
                "runner",
                "effects",
                "policy_hash",
            )
        },
        "gates": [
            {
                k: g.get(k)
                for k in ("gate_id", "status", "reason", "passes", "completed", "selected")
            }
            for g in report["gates"]
        ],
        "metrics": metrics,
        "application": {
            k: app[k]
            for k in (
                "planned",
                "recorded",
                "completed",
                "failed",
                "error_kinds",
                "error_like_answers",
            )
        },
        "latency_ms": {
            k: app["latency"][k]
            for k in ("p50_ms", "p95_ms", "successful_requests", "excluded_failures")
        },
        "cost": {
            role: {
                k: block.get(k) for k in ("calls", "known_cost_usd", "total_cost_usd", "accounting")
            }
            for role, block in report["cost"].items()
            if isinstance(block, dict)
        },
        "non_passing_cases": {
            "total": len(items),
            "first": [f"{i['case_id']} r{i['repetition']}" for i in items[:evidence_limit]],
        },
        "outcome": report["outcome"],
        "notes": report["notes"],
    }
