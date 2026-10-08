"""Run lifecycle services (06-T4), shared by commands now and the conversational layer later.

- `create_run` freezes everything a run depends on — plan (as an immutable artifact), the
  selected Goldens, application spec, effective policy — into the run manifest, records the
  approval, and materializes the work graph as durable work items.
- `execute_run` runs (or resumes) a run from that frozen state only. It first takes the
  run's lease: one live session per run, so a second `resume` of a running run is refused
  (a lease whose session died — stale heartbeat, or a dead process on this host — is
  taken over, and the lost session's wall time is recorded). It verifies the frozen
  identities (plan and application artifacts, evaluator bindings, approval) and recovers
  work that was in flight when a previous session ended:
    * an execution with a committed attempt is settled from that record (a retryable
      failure is retried only while attempts remain);
    * an execution without one counts as a spent application call (it may have been
      dispatched), and is re-dispatched only if the application declares no effects;
      otherwise it becomes `unknown_effect` — never repeated automatically (§15);
    * an evaluation with a committed final result is settled; otherwise it runs again.
  Evaluator calls cannot affect the application, so re-running an evaluation is safe (its
  judge spend is recorded again as a new attempt).
  Budgets carry across sessions by replaying every committed attempt.
- `run_status` reads committed state only; it needs no model provider.
- `evaluate_run` rescores a run's saved executions with a plan's metrics (never invokes the
  application).
- `run_exit_code` maps a finished session of a run to the §13 exit codes, the same for
  headless commands and the conversation.

Each run freezes its metric profiles (evaluator manifests, parameters and rules) in the
manifest, and each rescoring pass records its own in a `scoring_pass` event, so reports are
rebuilt from storage without loading any evaluator (11-G1).
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import random
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from aibench.core.errors import AibenchError
from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    ApplicationSpec,
    Approval,
    EffectLevel,
    ExecutionResult,
    ExecutionStatus,
    RedactionClass,
    ResetPolicy,
    RunManifest,
    WorkItem,
    WorkItemState,
)
from aibench.core.plans import ExecutablePlan
from aibench.engine.budget import BudgetLedger
from aibench.engine.cache import application_code_identity
from aibench.engine.compile import CompiledRun, PlanInvalid, PolicyDenied, load_plan
from aibench.engine.engine import (
    RunController,
    RunEngine,
    RunOutcome,
    RunState,
    evaluation_key,
    execution_key,
    parse_work_item_key,
    was_dispatched,
    work_counts,
)
from aibench.engine.retry import classify_execution
from aibench.registry import (
    BindingValidationError,
    EvaluatorRegistry,
    RegistryError,
)
from aibench.runners import LoadedApplication, create_runner, reset_hook
from aibench.security.policy import ExecutionPolicy, evaluator_denials, plan_denials
from aibench.services.scoring import (
    ScoringReport,
    declared_dependency_identity,
    metric_profiles,
    score_recorded_run,
)
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import RunLease, Storage, WorkItemSettlement

LEASE_TTL_SECONDS = 60.0  # a lease not heartbeated for this long belongs to a dead session
# A run in any of these states can be continued by a new session. A run left
# `cancelling` by a session that died is continued only to finish its cancellation.
RESUMABLE_STATES = {
    "created",
    "running",
    "pausing",
    "paused",
    "cancelling",
    "interrupting",
    "interrupted",
}
APPROVED_ACTIONS = ("invoke_application", "run_evaluators")
EXIT_OK, EXIT_GATES_FAILED, EXIT_INVALID, EXIT_INCOMPLETE, EXIT_DENIED, EXIT_INTERRUPTED = (
    0,
    1,
    2,
    3,
    4,
    130,
)


class RunError(AibenchError):
    """A run cannot be started or resumed as asked."""


def _approval_scope(run_id: str, manifest: RunManifest, policy_hash: str) -> str:
    return content_hash(
        {
            "run_id": run_id,
            "plan_hash": manifest.plan_hash,
            "application_hash": manifest.application_hash,
            "dataset_hash": manifest.dataset_hash,
            "policy_hash": policy_hash,
        }
    )


def create_run(
    compiled: CompiledRun,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    granted_by: str,
    run_id: str | None = None,
    run_seed: int | None = None,
    experiment_context: dict[str, Any] | None = None,
) -> str:
    spec = compiled.application.spec
    run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
    if not run_id or len(run_id) > 120:
        raise RunError("run_id must be a non-empty value of at most 120 characters")
    if run_seed is not None and (
        isinstance(run_seed, bool) or not isinstance(run_seed, int) or not 0 <= run_seed < 2**31
    ):
        raise RunError("run_seed must be an integer between 0 and 2^31-1")
    existing_run = storage.get_run(run_id)
    if existing_run is not None:
        expected_app_hash = content_hash(spec.model_dump(mode="json"))
        parameters = existing_run.manifest.parameters
        if (
            existing_run.manifest.dataset_hash != compiled.dataset.content_hash
            or existing_run.manifest.application_hash != expected_app_hash
            or existing_run.manifest.plan_hash != compiled.plan_hash
            or (run_seed is not None and existing_run.manifest.seed != run_seed)
            or parameters.get("experiment_context") != experiment_context
        ):
            raise RunError(f"run id {run_id!r} already belongs to different frozen content")
        _ensure_run_work_items(compiled, storage, run_id)
        approval_id = f"{run_id}:approval"
        if storage.get_approval(approval_id) is None:
            storage.commit_approval(
                Approval(
                    approval_id=approval_id,
                    scope_hash=_approval_scope(run_id, existing_run.manifest, compiled.policy_hash),
                    allowed_actions=APPROVED_ACTIONS,
                    granted_by=granted_by,
                )
            )
        if not any(
            event["event_type"] == "run_created" for event in storage.list_run_events(run_id)
        ):
            storage.append_run_event(
                run_id,
                "run_created",
                {
                    "plan_id": compiled.plan.plan_id,
                    "cases": len(compiled.cases),
                    "repetitions": compiled.plan.repetitions,
                    "metrics": len(compiled.metrics),
                },
            )
        return run_id

    if storage.get_dataset(compiled.dataset.content_hash) is None:
        storage.commit_dataset(compiled.dataset)
    storage.commit_cases(compiled.dataset.content_hash, compiled.cases)
    # The applications table is a catalog keyed by application_id (first version seen);
    # the run's own spec is frozen as a content-addressed artifact, so editing an app
    # config never conflicts with, or silently changes, any run.
    if storage.get_application(spec.application_id) is None:
        storage.commit_application(spec)
    spec_ref = artifacts.write_bytes(
        spec.model_dump_json().encode("utf-8"),
        mime_type="application/json",
        redaction=RedactionClass.NONE,
    )
    commit_verified_artifact(artifacts, storage, spec_ref)
    plan_ref = artifacts.write_bytes(
        compiled.plan_bytes, mime_type="application/json", redaction=RedactionClass.NONE
    )
    commit_verified_artifact(artifacts, storage, plan_ref)
    world = compiled.world
    world_params: dict[str, Any] | None = None
    if world is not None:
        seed_ref = artifacts.write_bytes(
            world.seed_bytes, mime_type="application/json", redaction=RedactionClass.NONE
        )
        commit_verified_artifact(artifacts, storage, seed_ref)
        world_params = {
            "world_id": world.world_id,
            "seed_hash": world.seed_hash,
            "seed_artifact_id": seed_ref.artifact_id,
        }

    dependency_lock_hash = declared_dependency_identity(compiled.metrics)
    profiles = metric_profiles(
        list(compiled.metrics),
        application=spec,
        dependency_lock_hash=dependency_lock_hash,
    )
    model_identifiers = {
        metric.manifest.evaluator_id: profile["compatibility"]["judge"]["digest"]
        for metric, profile in zip(compiled.metrics, profiles.values(), strict=True)
        if profile["compatibility"]["judge"]["digest"] is not None
    }
    manifest = RunManifest(
        run_id=run_id,
        dataset_hash=compiled.dataset.content_hash,
        application_hash=content_hash(spec.model_dump(mode="json")),
        plan_hash=compiled.plan_hash,
        application_id=spec.application_id,
        dependency_lock_hash=dependency_lock_hash,
        plugin_hashes={
            m.manifest.evaluator_id: f"{m.manifest.plugin_id}=={m.manifest.plugin_version}"
            for m in compiled.metrics
        },
        model_identifiers=model_identifiers,
        parameters={
            "mode": "manual_plan",
            "plan_artifact_id": plan_ref.artifact_id,
            "application_artifact_id": spec_ref.artifact_id,
            "plan_dir": str(compiled.plan_dir),
            "application_base_dir": str(compiled.application.base_dir),
            "policy": compiled.policy.model_dump(mode="json"),
            "policy_hash": compiled.policy_hash,
            "scoring_id": f"engine-{run_id}",
            "binding_hashes": [m.binding_hash for m in compiled.metrics],
            # State between cases (§7): how and when the application is reset.
            "reset": {
                "policy": spec.reset_policy.value,
                "hook": reset_hook(spec),
                "mode": reset_mode(spec, world is not None),
            },
            "test_world": world_params,
            "metric_profiles": profiles,
            **(
                {"experiment_context": experiment_context} if experiment_context is not None else {}
            ),
        },
        seed=(run_seed if run_seed is not None else random.SystemRandom().randrange(2**31)),
        environment={"python": platform.python_version(), "platform": sys.platform},
    )
    storage.commit_run(manifest, status="created")
    storage.commit_approval(
        Approval(
            approval_id=f"{run_id}:approval",
            scope_hash=_approval_scope(run_id, manifest, compiled.policy_hash),
            allowed_actions=APPROVED_ACTIONS,
            granted_by=granted_by,
        )
    )
    _ensure_run_work_items(compiled, storage, run_id)
    storage.append_run_event(
        run_id,
        "run_created",
        {
            "plan_id": compiled.plan.plan_id,
            "cases": len(compiled.cases),
            "repetitions": compiled.plan.repetitions,
            "metrics": len(compiled.metrics),
        },
    )
    return run_id


def _ensure_run_work_items(compiled: CompiledRun, storage: Storage, run_id: str) -> None:
    """Idempotently materialize a plan's work graph, including after interrupted creation."""
    for case in compiled.cases:
        for repetition in range(compiled.plan.repetitions):
            exec_key = execution_key(case.case_id, repetition)
            storage.commit_work_item(
                WorkItem(
                    work_item_id=f"{run_id}:{exec_key}",
                    run_id=run_id,
                    task_key=exec_key,
                    kind="execution",
                )
            )
            for metric in compiled.metrics:
                eval_key = evaluation_key(case.case_id, repetition, metric.binding_hash)
                storage.commit_work_item(
                    WorkItem(
                        work_item_id=f"{run_id}:{eval_key}",
                        run_id=run_id,
                        task_key=eval_key,
                        kind="evaluation",
                        dependency_keys=(exec_key,),
                    )
                )


# --------------------------------------------------------------------------- execute / resume


def _frozen_plan(
    storage: Storage, artifacts: ArtifactStore, manifest: RunManifest
) -> ExecutablePlan:
    params = manifest.parameters
    ref = storage.get_artifact(params["plan_artifact_id"])
    if ref is None:
        raise RunError("the run's frozen plan artifact is missing")
    try:
        raw = artifacts.read_bytes(ref)  # verified: path, size and digest
    except AibenchError as exc:
        raise RunError(f"the run's frozen plan artifact failed verification: {exc}") from exc
    if bytes_hash(raw) != manifest.plan_hash:
        raise RunError("the frozen plan does not match the run manifest's plan hash")
    try:
        return ExecutablePlan.model_validate_json(raw)
    except ValueError as exc:
        raise RunError(f"the run's frozen plan no longer validates: {exc}") from exc


def reset_mode(spec: ApplicationSpec, world_selected: bool) -> str:
    """When the engine resets the application: before every case, before each episode, or
    never. A shared application, or one without a reset hook and without a selected world,
    is never reset (a fresh process per case isolates only in-process state)."""
    if spec.reset_policy is ResetPolicy.SHARED:
        return "none"
    if spec.reset_policy is ResetPolicy.PER_EPISODE:
        return "per_episode"
    return "per_case" if reset_hook(spec) is not None or world_selected else "none"


def _frozen_world_seed(
    storage: Storage, artifacts: ArtifactStore, manifest: RunManifest
) -> tuple[str | None, Any]:
    """The selected test world and its frozen seed, verified against the manifest."""
    world = manifest.parameters.get("test_world")
    if not world:
        return None, None
    ref = storage.get_artifact(world["seed_artifact_id"])
    if ref is None:
        raise RunError("the run's frozen test world seed is missing")
    try:
        raw = artifacts.read_bytes(ref)
    except AibenchError as exc:
        raise RunError(f"the run's frozen test world seed failed verification: {exc}") from exc
    if bytes_hash(raw) != world["seed_hash"]:
        raise RunError("the frozen test world seed does not match the run manifest")
    return str(world["world_id"]), json.loads(raw)


def _frozen_application(
    storage: Storage, artifacts: ArtifactStore, manifest: RunManifest
) -> ApplicationSpec:
    ref = storage.get_artifact(manifest.parameters.get("application_artifact_id", ""))
    if ref is None:
        raise RunError("the run's frozen application spec is missing")
    try:
        spec = ApplicationSpec.model_validate_json(artifacts.read_bytes(ref))
    except (AibenchError, ValueError) as exc:
        raise RunError(f"the run's frozen application spec failed verification: {exc}") from exc
    if content_hash(spec.model_dump(mode="json")) != manifest.application_hash:
        raise RunError("the frozen application spec does not match the run manifest")
    return spec


def _frozen_registry(
    plan: ExecutablePlan,
    manifest: RunManifest,
    policy: ExecutionPolicy,
    application: ApplicationSpec,
) -> tuple[EvaluatorRegistry, list[Any]]:
    plan_dir = Path(manifest.parameters["plan_dir"])
    denials = plan_denials(policy, plan, plan_dir)
    if denials:
        raise PolicyDenied(denials)
    registry = EvaluatorRegistry.with_native()
    for env in plan.plugin_environments:
        python = Path(env.python) if Path(env.python).is_absolute() else plan_dir / env.python
        try:
            registry.load_plugin_environment(
                python,
                secret_env=dict(env.secret_env),
                extra_paths=[Path(p) if Path(p).is_absolute() else plan_dir / p for p in env.paths],
                startup_timeout_seconds=env.startup_timeout_seconds,
            )
        except RegistryError as exc:
            raise RunError(str(exc)) from exc
    try:
        metrics = registry.validate(plan.metrics, application=application)
    except BindingValidationError as exc:
        raise RunError(f"the plan's metrics no longer validate: {exc}") from exc
    if [m.binding_hash for m in metrics] != list(manifest.parameters["binding_hashes"]):
        raise RunError(
            "evaluator identities changed since the run started (version drift); "
            "start a new run or rescore with `aibench evaluate`"
        )
    denials = evaluator_denials(policy, [m.manifest for m in metrics])
    if denials:
        raise PolicyDenied(denials)
    return registry, metrics


def _recover_in_flight(
    storage: Storage, run_id: str, spec: ApplicationSpec, plan: ExecutablePlan, scoring_id: str
) -> int:
    """Settle work items left `running` by a session that ended abruptly. The transitions
    and the `recovered` event that records them commit together, so a crash during
    recovery can never settle an item while losing the record that it may have been
    dispatched. Returns the number of executions that may have been dispatched without a
    committed attempt."""
    settlements: list[WorkItemSettlement] = []
    notes: list[str] = []
    uncommitted = 0
    finals = {
        (r.case_id, r.repetition_id, r.binding_hash)
        for r in storage.list_metric_results(run_id, scoring_id=scoring_id)
    }
    running = frozenset({WorkItemState.RUNNING})

    def settle(item: WorkItem, state: WorkItemState, note: str, error: str | None) -> None:
        settlements.append(WorkItemSettlement(item.task_key, running, state, error))
        notes.append(f"{item.task_key}: {note}")

    for item in storage.list_work_items(run_id):
        if item.state is not WorkItemState.RUNNING:
            continue
        case_id, repetition, binding_key = parse_work_item_key(item.task_key, item.kind)
        if item.kind == "execution":
            attempt = storage.get_execution_attempt(
                ExecutionResult.build_id(run_id, case_id, repetition, item.attempt)
            )
            if attempt is not None:
                verdict = classify_execution(attempt)
                retry = verdict.retry and item.attempt < plan.retry.max_attempts
                state = WorkItemState.PENDING if retry else verdict.final_state
                reason = verdict.reason
                if verdict.retry and not retry:
                    reason = f"{reason} (retries exhausted after {item.attempt} attempts)"
                settle(
                    item,
                    state,
                    f"settled from its committed attempt ({state.value})",
                    None if state is WorkItemState.SUCCEEDED else reason,
                )
                continue
            uncommitted += 1
            if spec.effects is EffectLevel.NONE:
                settle(
                    item,
                    WorkItemState.PENDING,
                    "re-dispatch (no declared effects)",
                    "interrupted in flight; safe to repeat (no effects)",
                )
            else:
                settle(
                    item,
                    WorkItemState.UNKNOWN_EFFECT,
                    "unknown_effect (effectful, not repeated)",
                    "interrupted while possibly dispatched to an effectful application; "
                    "reconcile the application state before repeating",
                )
        else:
            done = any(
                f[0] == case_id and f[1] == repetition and f[2] and f[2][7:23] == binding_key
                for f in finals
            )
            settle(
                item,
                WorkItemState.SUCCEEDED if done else WorkItemState.PENDING,
                "settled from its final result" if done else "re-evaluate",
                None,
            )
    if settlements:
        storage.settle_work_items(
            run_id,
            settlements,
            event_type="recovered",
            payload={"items": notes, "uncommitted_dispatches": uncommitted},
        )
    return uncommitted


_SESSION_EVENTS = ("run_session_ended", "run_session_aborted", "run_session_lost")


def _replay_prior_spend(
    storage: Storage, run_id: str, ledger: BudgetLedger, scoring_id: str | None = None
) -> None:
    """Replay every committed attempt of earlier sessions into the ledger, so hard limits,
    tokens and known costs carry across sessions of the same run. Only the run's own
    scoring pass counts: a later rescore (`aibench evaluate`) is not spend of this run."""
    for attempt in storage.list_execution_attempts(run_id):
        if was_dispatched(attempt):
            ledger.record_prior_application(attempt.cost)
    for result in storage.list_evaluation_attempts(run_id):
        if scoring_id is None or result.scoring_id == scoring_id:
            ledger.record_prior_evaluation(dict(result.resources))
    for event in storage.list_run_events(run_id):
        payload = event["payload"]
        if event["event_type"] == "recovered":
            for _ in range(int(payload.get("uncommitted_dispatches", 0))):
                ledger.record_prior_application(None)  # may have run; cost unknown
        elif event["event_type"] in _SESSION_EVENTS:
            ledger.record_prior_elapsed(float(payload.get("session_elapsed_seconds") or 0))


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _lease_is_stale(lease: RunLease) -> bool:
    if time.time() - lease.heartbeat_at > LEASE_TTL_SECONDS:
        return True
    return lease.host == socket.gethostname() and not _pid_alive(lease.pid)


def lease_state(storage: Storage, run_id: str) -> str | None:
    """Whether a session is executing the run right now: "live" (a lease held by a live
    session), "stale" (its session ended without releasing it: killed, crashed or
    disconnected) or None (no session holds it). The stored run status alone cannot tell a
    running run from one whose process died mid-run (10-T1)."""
    row = storage.conn.execute(
        "SELECT owner, host, pid, acquired_at, heartbeat_at FROM run_leases WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    lease = RunLease(run_id, row[0], row[1], int(row[2]), row[3], row[4])
    return "stale" if _lease_is_stale(lease) else "live"


async def execute_run(
    run_id: str,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    controller: RunController | None = None,
    environ: dict[str, str] | None = None,
) -> RunOutcome:
    record = storage.get_run(run_id)
    if record is None:
        raise RunError(f"no run committed with run_id={run_id!r}")
    if record.status not in RESUMABLE_STATES:
        raise RunError(
            f"run {run_id} is {record.status}; only interrupted or unfinished runs resume"
        )
    mode = record.manifest.parameters.get("mode")
    if mode != "manual_plan":
        raise RunError(f"run {run_id} was not created from a plan (mode={mode!r})")

    if record.status == "cancelling":
        # The previous session was cancelling when it ended: finish that, never un-cancel.
        controller = controller or RunController()
        controller.request("cancel")
    owner = uuid.uuid4().hex
    started = time.monotonic()
    previous = storage.acquire_run_lease(
        run_id,
        owner=owner,
        host=socket.gethostname(),
        pid=os.getpid(),
        now=time.time(),
        is_stale=_lease_is_stale,
    )  # raises LeaseHeld while another session is live
    if previous is not None:
        storage.append_run_event(
            run_id,
            "run_session_lost",
            {
                "host": previous.host,
                "pid": previous.pid,
                "session_elapsed_seconds": round(
                    max(0.0, previous.heartbeat_at - previous.acquired_at), 3
                ),
            },
        )
    ended = False
    try:
        outcome = await _execute_leased(
            run_id, record.manifest, storage, artifacts, controller, environ, owner
        )
        ended = True
        return outcome
    finally:
        if ended:
            storage.release_run_lease(run_id, owner)
        else:
            # Ended by an exception: keep the session's wall time and free the lease, best
            # effort — never mask the original error. (If the process dies instead, the
            # next session takes over the stale lease and records the time from it.)
            with contextlib.suppress(Exception):
                storage.append_run_event(
                    run_id,
                    "run_session_aborted",
                    {"session_elapsed_seconds": round(time.monotonic() - started, 3)},
                )
            with contextlib.suppress(Exception):
                storage.release_run_lease(run_id, owner)


async def _execute_leased(
    run_id: str,
    manifest: RunManifest,
    storage: Storage,
    artifacts: ArtifactStore,
    controller: RunController | None,
    environ: dict[str, str] | None,
    owner: str,
) -> RunOutcome:
    current = storage.get_run(run_id)
    if current is None or current.status not in RESUMABLE_STATES:
        # Another session finished it between our first check and taking the lease.
        raise RunError(f"run {run_id} is {current.status if current else 'missing'}; not resumable")
    params = manifest.parameters
    plan = _frozen_plan(storage, artifacts, manifest)
    spec = _frozen_application(storage, artifacts, manifest)
    try:
        policy = ExecutionPolicy.model_validate(params["policy"])
    except ValueError as exc:
        raise RunError(f"the run's frozen policy no longer validates: {exc}") from exc
    approval = storage.get_approval(f"{run_id}:approval")
    if approval is None or approval.scope_hash != _approval_scope(
        run_id, manifest, params["policy_hash"]
    ):
        raise PolicyDenied(["no approval is recorded for this run's frozen scope"])
    _, metrics = _frozen_registry(plan, manifest, policy, spec)

    cases = {c.case_id: c for c in storage.list_cases(manifest.dataset_hash)}
    needed = {parse_work_item_key(w.task_key, w.kind)[0] for w in storage.list_work_items(run_id)}
    if not needed <= set(cases):
        raise RunError("stored Goldens are missing for some of this run's cases")

    ledger = BudgetLedger(plan.budgets)
    _replay_prior_spend(storage, run_id, ledger, params["scoring_id"])  # before recovery
    uncommitted = _recover_in_flight(storage, run_id, spec, plan, params["scoring_id"])
    for _ in range(uncommitted):
        ledger.record_prior_application(None)

    pending_exec = any(
        w.kind == "execution" and w.state is WorkItemState.PENDING
        for w in storage.list_work_items(run_id)
    )
    runner = None
    if pending_exec:
        runner = create_runner(
            LoadedApplication(spec=spec, base_dir=Path(params["application_base_dir"])),
            trusted_local=policy.allow_trusted_local,
            environ=environ if environ is not None else dict(os.environ),
        )
    world_id, seed = _frozen_world_seed(storage, artifacts, manifest)
    reset = params.get("reset") or {"mode": "none"}
    world = params.get("test_world") or {}
    execution_cache = (
        {
            "application_hash": manifest.application_hash,
            "code_identity": application_code_identity(
                spec,
                Path(params["application_base_dir"]),
                environ if environ is not None else os.environ,
            ),
            "world_seed_hash": world.get("seed_hash"),
            "policy_hash": params["policy_hash"],
        }
        if plan.cache.executions
        else None
    )
    engine = RunEngine(
        storage=storage,
        artifacts=artifacts,
        run_id=run_id,
        plan=plan,
        application=spec,
        runner=runner,
        reset_mode=reset["mode"],
        world_id=world_id,
        world_seed=seed,
        execution_cache=execution_cache,
        evaluation_cache_policy=params["policy_hash"] if plan.cache.evaluations else None,
        cases={cid: case for cid, case in cases.items() if cid in needed},  # dataset order
        metrics=metrics,
        dependency_lock_hash=manifest.dependency_lock_hash,
        scoring_id=params["scoring_id"],
        ledger=ledger,
        controller=controller or RunController(),
        rng=random.Random(manifest.seed),
        heartbeat=lambda: storage.heartbeat_run_lease(run_id, owner, time.time()),
    )
    return await engine.execute()


# --------------------------------------------------------------------------- status / rescore


def _evaluation_identity(task_key: str) -> tuple[str, int, str | None]:
    return parse_work_item_key(task_key, "evaluation")


class _EvaluationStates:
    """Where each of a run's evaluations stands after every scoring pass so far: its latest
    stored result, an error being not finished. Changing the judge's model or timeout gives an
    evaluation a new binding, so a failure the run left under the old one is replaced, not
    repeated, when the newest pass finished the same metric on the same case."""

    def __init__(self, storage: Storage, run_id: str) -> None:
        results = storage.list_metric_results(run_id)  # in the order they were committed
        self._by_binding: dict[tuple[str, int, str | None], tuple[bool, str | None]] = {}
        self._metric_of: dict[str | None, str] = {}
        newest = results[-1].scoring_id if results else None
        self._newest_bindings: set[str | None] = set()
        self._newest: dict[tuple[str, int, str], tuple[bool, str | None]] = {}
        for result in results:
            binding = result.binding_hash[7:23] if result.binding_hash else None
            # An evaluation the run never got to (a limit was reached) is as unfinished as one
            # that failed: it needs attention and a rescore picks it up. One skipped because
            # its execution failed is not: that failure is already listed on its own.
            failed = result.status is ExecutionStatus.ERROR or (
                result.status is ExecutionStatus.SKIPPED
                and str(result.reason or "").startswith("not_evaluated:")
            )
            state = (not failed, result.reason if failed else None)
            self._by_binding[(result.case_id, result.repetition_id, binding)] = state
            self._metric_of[binding] = result.metric_id
            if result.scoring_id == newest:
                self._newest_bindings.add(binding)
                self._newest[(result.case_id, result.repetition_id, result.metric_id)] = state

    def of(self, identity: tuple[str, int, str | None]) -> tuple[bool, str | None] | None:
        """(finished, reason) of the evaluation `identity` names, or None if never scored."""
        case_id, repetition, binding = identity
        if binding not in self._newest_bindings and binding in self._metric_of:
            replaced = self._newest.get((case_id, repetition, self._metric_of[binding]))
            if replaced is not None:
                return replaced
        return self._by_binding.get(identity)


def run_status(storage: Storage, run_id: str) -> dict[str, Any]:
    record = storage.get_run(run_id)
    if record is None:
        raise RunError(f"no run committed with run_id={run_id!r}")
    events = storage.list_run_events(run_id)
    last_session = next(
        (e for e in reversed(events) if e["event_type"] == "run_session_ended"), None
    )
    items = storage.list_work_items(run_id)
    counts = work_counts(storage, run_id)
    blocked = [
        {"task_key": w.task_key, "state": w.state.value, "reason": w.last_error}
        for w in items
        if w.state in (WorkItemState.FAILED, WorkItemState.BLOCKED, WorkItemState.UNKNOWN_EFFECT)
    ]
    if record.status in _FINISHED:
        # A rescore settles evaluations the run itself left failed, but it never touches the
        # run's work records: "needs attention 11" stayed after every result was scored.
        # A failed evaluation is not waiting for attention once a later pass finished it.
        states = _EvaluationStates(storage, run_id)
        settled = set()
        newly_failed: list[tuple[str, str | None]] = []
        for w in items:
            if w.kind != "evaluation":
                continue
            state = states.of(_evaluation_identity(w.task_key))
            if state is None:
                continue
            if w.state is WorkItemState.FAILED and state[0]:
                settled.add(w.task_key)
            # ... and the other way: an evaluation the run finished that the latest pass could
            # not (a rescore timed out): it needs attention though the run's record is clean.
            elif w.state is WorkItemState.SUCCEEDED and not state[0]:
                newly_failed.append((w.task_key, state[1]))
        if settled or newly_failed:
            blocked = [b for b in blocked if b["task_key"] not in settled]
            blocked += [
                {"task_key": key, "state": WorkItemState.FAILED.value, "reason": reason}
                for key, reason in newly_failed
            ]
            evaluation = dict(counts.get("evaluation", {}))
            evaluation["failed"] = evaluation.get("failed", 0) - len(settled) + len(newly_failed)
            evaluation["succeeded"] = (
                evaluation.get("succeeded", 0) + len(settled) - len(newly_failed)
            )
            counts["evaluation"] = {k: v for k, v in evaluation.items() if v}
    return {
        "run_id": run_id,
        "status": record.status,
        "counts": counts,
        "needs_attention": blocked,
        "budget": last_session["payload"].get("budget") if last_session else None,
        # Why the last session stopped early, if it said (storage failure, lease lost).
        "warnings": list(last_session["payload"].get("warnings") or []) if last_session else [],
        "last_event_sequence": events[-1]["sequence"] if events else 0,
    }


async def evaluate_run(
    run_id: str,
    plan_path: Path,
    *,
    storage: Storage,
    artifacts: ArtifactStore,
    policy: ExecutionPolicy,
    carry_forward: bool = False,
) -> ScoringReport:
    """Rescore saved executions with the metrics of `plan_path` (never invokes the app).
    With `carry_forward`, results already finished in earlier passes are reused and only the
    missing or failed ones are evaluated (see `score_recorded_run`)."""
    plan = load_plan(plan_path)
    plan_dir = plan_path.resolve().parent
    record = storage.get_run(run_id)
    if record is None:
        raise RunError(f"no run committed with run_id={run_id!r}")
    denials = plan_denials(policy, plan, plan_dir)
    if denials:
        raise PolicyDenied(denials)
    application: ApplicationSpec | None
    if "application_artifact_id" in record.manifest.parameters:
        application = _frozen_application(storage, artifacts, record.manifest)
    else:  # a run recorded outside the engine (e.g. `aibench app smoke`)
        application = storage.get_application(record.manifest.application_id or "")
    registry = EvaluatorRegistry.with_native()
    for env in plan.plugin_environments:
        python = Path(env.python) if Path(env.python).is_absolute() else plan_dir / env.python
        try:
            registry.load_plugin_environment(
                python,
                secret_env=dict(env.secret_env),
                extra_paths=[Path(p) if Path(p).is_absolute() else plan_dir / p for p in env.paths],
                startup_timeout_seconds=env.startup_timeout_seconds,
            )
        except RegistryError as exc:
            raise PlanInvalid([str(exc)]) from exc
    try:
        metrics = registry.validate(plan.metrics, application=application)
    except BindingValidationError as exc:
        raise PlanInvalid([str(p) for p in exc.problems]) from exc
    denials = evaluator_denials(policy, [m.manifest for m in metrics])
    if denials:
        raise PolicyDenied(denials)
    return await score_recorded_run(
        storage=storage,
        artifacts=artifacts,
        registry=registry,
        run_id=run_id,
        bindings=plan.metrics,
        timeout_seconds=plan.evaluation_timeout_seconds,
        model_timeout_seconds=plan.model_evaluation_timeout_seconds,
        application=application,
        carry_forward=carry_forward,
    )


def outcome_json(outcome: RunOutcome) -> dict[str, Any]:
    return json.loads(
        json.dumps(
            {
                "state": outcome.state.value,
                "counts": outcome.counts,
                "budget": outcome.budget,
                "stop_reason": outcome.stop_reason,
                "warnings": outcome.warnings,
            }
        )
    )


def run_budget(storage: Storage, artifacts: ArtifactStore, run_id: str) -> dict[str, Any]:
    """The run's frozen budget ceilings and its committed spend, replayed from stored
    attempts the same way a resume does. Work in flight and the current session's wall
    time are not included until committed."""
    record = storage.get_run(run_id)
    if record is None:
        raise RunError(f"no run committed with run_id={run_id!r}")
    plan = _frozen_plan(storage, artifacts, record.manifest)
    ledger = BudgetLedger(plan.budgets)
    _replay_prior_spend(storage, run_id, ledger, record.manifest.parameters.get("scoring_id"))
    return {
        "run_id": run_id,
        "status": record.status,
        "basis": "committed attempts; in-flight work and the live session's time not included",
        **ledger.summary(),
    }


def run_report(storage: Storage, artifacts: ArtifactStore, run_id: str) -> dict[str, Any]:
    """The run's report document, built from stored facts only (services.reports)."""
    from aibench.services.reports import build_report  # reports builds on this module

    return build_report(storage, artifacts, run_id)


def run_exit_code(state: RunState, report: dict[str, Any]) -> int:
    """§13 exit codes for a session of a run that has ended in `state`: 130 interrupted
    (resumable); 3 incomplete (unfinished, or failed, blocked, cancelled or unknown-effect
    work), which wins over gate failures (both are in the report); 1 finished with a failed
    release gate; 0 finished with every gate satisfied."""
    if state is RunState.INTERRUPTED:
        return EXIT_INTERRUPTED
    outcome = report["outcome"]
    if state is not RunState.COMPLETED or not outcome["complete"]:
        return EXIT_INCOMPLETE
    return EXIT_GATES_FAILED if outcome["gates_failed"] else EXIT_OK


_FINISHED = frozenset({"completed", "cancelled", "budget_exhausted"})
