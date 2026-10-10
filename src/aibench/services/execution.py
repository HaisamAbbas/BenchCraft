"""Invoke an application and persist the attempt (03-T4), shared by commands, the future
engine (Prompt 06) and the conversational layer.

Commit order follows the artifact protocol (§14): raw captures are written and verified as
artifacts first, then the `ExecutionResult` that references them is committed, so a
committed result never points at a missing artifact.

`run_developer_smoke` is a developer check, not the scheduler: it invokes the selected cases
once each, sequentially, with no retries, budgets, resume or evaluation.
"""

from __future__ import annotations

import asyncio
import platform
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from aibench.core.hashes import content_hash
from aibench.core.models import (
    ApplicationSpec,
    ArtifactRef,
    BenchmarkCase,
    DatasetManifest,
    ExecutionResult,
    RedactionClass,
    RunManifest,
)
from aibench.runners.base import BaseRunner, InvocationContext, InvocationOutcome
from aibench.runners.bindings import AppInputEnvelope
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import Storage

SMOKE_RUN_PREFIX = "smoke-"


async def invoke_and_record(
    runner: BaseRunner,
    case: BenchmarkCase,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    run_id: str,
    repetition_id: int = 0,
    attempt_id: int = 0,
    warmup: bool = False,
    cancel: asyncio.Event | None = None,
) -> ExecutionResult:
    """One attempt: exactly one application invocation, never retried here."""
    ctx = InvocationContext(
        run_id=run_id,
        case_id=case.case_id,
        repetition_id=repetition_id,
        attempt_id=attempt_id,
        cancel=cancel,
    )
    outcome = await runner.invoke(AppInputEnvelope.from_case(case), ctx)
    # The call was dispatched and answered: it is recorded even if the task is cancelled
    # while its captures are written off the loop, as it was when nothing awaited between
    # the answer and the commit. The write is bounded, so the cancel is only delayed.
    record = asyncio.ensure_future(
        _record(outcome, ctx, storage=storage, artifacts=artifacts, warmup=warmup)
    )
    try:
        return await asyncio.shield(record)
    except asyncio.CancelledError:
        return await record


async def _record(
    outcome: InvocationOutcome,
    ctx: InvocationContext,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    warmup: bool = False,
) -> ExecutionResult:
    trace_refs = await _commit_captures(outcome, ctx, storage=storage, artifacts=artifacts)
    result = _to_result(outcome, ctx, trace_refs, warmup=warmup)
    storage.commit_execution_attempt(result)
    return result


async def _commit_captures(
    outcome: InvocationOutcome,
    ctx: InvocationContext,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
) -> tuple[str, ...]:
    """Durably write and verify each capture, then commit its reference. The file work
    (write, fsync, read back, hash) runs in a worker thread, so the event loop, and a chat
    or controls sharing it, stays responsive under load (16-T4); the database commit stays
    on the loop's single writer connection (§14)."""
    refs: list[str] = []
    for capture in outcome.captures:
        # Raw application traffic may contain personal data: restricted, never inlined into
        # sanitized reports (§16 "Store raw traces separately from sanitized reports").
        ref = await asyncio.to_thread(_write_verified, artifacts, capture, ctx)
        storage.commit_artifact_unverified(ref)
        refs.append(ref.artifact_id)
    return tuple(refs)


def _write_verified(artifacts: ArtifactStore, capture: Any, ctx: InvocationContext) -> ArtifactRef:
    ref = artifacts.write_bytes(
        capture.data,
        mime_type=capture.mime_type,
        run_id=ctx.run_id,
        redaction=RedactionClass.RESTRICTED,
        artifact_id=f"{ctx.execution_id}:{capture.name}",
    )
    artifacts.verify_ref(ref)  # what commit_verified_artifact checks, off the loop
    return ref


def _to_result(
    outcome: InvocationOutcome,
    ctx: InvocationContext,
    trace_refs: tuple[str, ...],
    *,
    warmup: bool = False,
) -> ExecutionResult:
    obs = outcome.observations
    completeness = dict(outcome.completeness)
    for capture in outcome.captures:
        if capture.truncated:
            completeness.setdefault("captures", {})[capture.name] = "truncated"
    return ExecutionResult(
        execution_id=ctx.execution_id,
        run_id=ctx.run_id,
        case_id=ctx.case_id,
        repetition_id=ctx.repetition_id,
        attempt_id=ctx.attempt_id,
        warmup=warmup,
        status=outcome.status,
        output=outcome.output,
        retrieved_context=obs.retrieved_context,
        tool_events=obs.tool_events,
        world_state=obs.world_state,
        trace_refs=trace_refs,
        timing=outcome.timing,
        usage=obs.usage,
        cost=obs.cost,
        error=outcome.error,
        observation_completeness=completeness,
        error_kind=outcome.error_kind,
        effect_state=outcome.effect_state,
        correlation_id=outcome.correlation_id,
    )


# --------------------------------------------------------------------------- smoke path


@dataclass
class SmokeReport:
    run_id: str
    results: list[ExecutionResult] = field(default_factory=list)
    status: str = "running"


async def run_developer_smoke(
    runner: BaseRunner,
    spec: ApplicationSpec,
    dataset: DatasetManifest,
    cases: Sequence[BenchmarkCase],
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    on_result: Callable[[ExecutionResult], None] | None = None,
) -> SmokeReport:
    """Invoke each case once, in order, recording every attempt. The run is labelled
    `developer_smoke` in its manifest so it can never be mistaken for a planned benchmark."""
    # A re-ingested dataset has a fresh `created_at`; the stored manifest for the same
    # content hash is the identity, so reuse it rather than conflict with it.
    if storage.get_dataset(dataset.content_hash) is None:
        storage.commit_dataset(dataset)
    storage.commit_cases(dataset.content_hash, cases)
    # Keep the catalog's first-seen entry stable, like plan-based run creation, and freeze
    # this smoke's actual application spec on the run so later edits remain scoreable.
    if storage.get_application(spec.application_id) is None:
        storage.commit_application(spec)
    spec_ref = artifacts.write_bytes(
        spec.model_dump_json().encode("utf-8"),
        mime_type="application/json",
        redaction=RedactionClass.NONE,
    )
    commit_verified_artifact(artifacts, storage, spec_ref)
    report = SmokeReport(run_id=f"{SMOKE_RUN_PREFIX}{uuid.uuid4().hex[:12]}")
    storage.commit_run(
        RunManifest(
            run_id=report.run_id,
            dataset_hash=dataset.content_hash,
            application_hash=content_hash(spec.model_dump(mode="json")),
            plan_hash=content_hash(
                {"kind": "developer_smoke", "case_ids": [c.case_id for c in cases]}
            ),
            parameters={
                "mode": "developer_smoke",
                "application_artifact_id": spec_ref.artifact_id,
                "scheduler": "none",
                "retries": 0,
                "evaluation": "none",
            },
            environment={"python": platform.python_version(), "platform": sys.platform},
            application_id=spec.application_id,
        ),
        status="running",
    )
    try:
        for case in cases:
            result = await invoke_and_record(
                runner, case, storage=storage, artifacts=artifacts, run_id=report.run_id
            )
            report.results.append(result)
            if on_result is not None:
                on_result(result)
    except BaseException:
        report.status = "interrupted"
        storage.update_run_status(report.run_id, report.status)
        raise
    report.status = "completed"
    storage.update_run_status(report.run_id, report.status)
    return report
