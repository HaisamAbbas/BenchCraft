"""`aibench run`, `resume`, `evaluate`, `runs status` (06-T4, 11-T3).

`aibench run --plan FILE` runs that plan. Without `--plan`, the plan comes from the project
config (`plan_path` in `aibench.json`/`config.yaml`, see `aibench init`): `aibench run DIR`
uses DIR's config, and `aibench run DATASET` uses the current directory's config and checks
that its plan is bound to that dataset (a dataset alone cannot identify an application).

Exit codes (§13, shared with the conversation via `services.runs.run_exit_code`):
0 complete with every release gate satisfied; 1 complete with a failed gate; 2 invalid
input or plan; 3 incomplete (failures, unknown effects, blocked or cancelled work,
evaluation errors), which wins over gate failures; 4 authorization required (policy
denied); 130 interrupted (resumable with `aibench resume RUN_ID`).

Ctrl+C during a run: the first press stops new dispatch, records in-flight outcomes and
leaves the rest resumable; a second press also aborts in-flight work (still resumable).
Ctrl+C before dispatch starts exits 130 too; nothing was dispatched.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import typer
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.event_stream import (
    EventLogLock,
    RunEventStream,
    last_logged_sequence,
    latest_stored_sequence,
)
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    EffectState,
    ExecutionResult,
    ExecutionStatus,
    WorkItem,
    WorkItemState,
)
from aibench.engine.compile import (
    CompiledRun,
    PlanInvalid,
    PolicyDenied,
    RunPlanOverrides,
    compile_plan,
    load_plan,
    load_policy,
)
from aibench.engine.engine import RunController, RunOutcome, RunState, parse_work_item_key
from aibench.services.reports import build_report
from aibench.services.runs import (
    EXIT_DENIED,
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    RESUMABLE_STATES,
    RunError,
    compile_retry_run,
    create_run,
    evaluate_run,
    execute_run,
    lease_state,
    outcome_json,
    request_run_control,
    run_exit_code,
    run_status,
)
from aibench.services.scoring import select_final_executions
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.render import safe

console = Console()
err_console = Console(stderr=True)

_WORKSPACE = typer.Option(
    None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
)
_POLICY = typer.Option(
    None, "--policy", help="Execution policy file. Default: the conservative built-in policy."
)
_JSON = typer.Option(False, "--json", help="Machine-readable output on stdout.")


def _fail(
    message: str,
    code: int,
    *,
    json_output: bool = False,
    details: list[str] | None = None,
) -> typer.Exit:
    return error_exit(
        message,
        exit_code=code,
        json_output=json_output,
        console=console,
        err_console=err_console,
        details=details,
    )


def _open(
    workspace: Path | None, *, create: bool = False, json_output: bool = False
) -> tuple[Storage, ArtifactStore]:
    """Open the workspace; only `run` creates one — reading an absent one is an error."""
    ws = Workspace.at(workspace or Path.cwd())
    if not create and not ws.db_path.is_file():
        raise _fail(f"no aibench workspace at {ws.root}", EXIT_INVALID, json_output=json_output)
    ws.ensure_directories()
    return Storage(Database.open_workspace(ws)), ArtifactStore(ws.artifacts_dir)


def _start_event_stream(
    workspace: Path | None,
    run_id: str,
    *,
    log_file: Path | None,
    verbose: bool,
    log_lock: EventLogLock | None = None,
    wait_for_log_lock: bool = False,
) -> RunEventStream:
    try:
        stream = RunEventStream(
            workspace or Path.cwd(),
            run_id,
            log_file=log_file,
            verbose=verbose,
            log_lock=log_lock,
            wait_for_log_lock=wait_for_log_lock,
        )
        stream.start()
        return stream
    except (OSError, RuntimeError, ValueError) as exc:
        raise _fail(f"could not start run event logging: {exc}", EXIT_INVALID) from exc


def _preflight_event_log(
    workspace: Path | None, log_file: Path | None, *, run_id: str | None = None
) -> EventLogLock | None:
    """Validate and test the selected destination before a run/control change is committed."""
    if log_file is None:
        return None
    target = log_file.expanduser().resolve()
    ws = Workspace.at(workspace or Path.cwd())
    if target == ws.db_path.resolve():
        raise _fail("--log-file cannot target the workspace database", EXIT_INVALID)
    log_lock = EventLogLock(target)
    try:
        log_lock.acquire()
        target.parent.mkdir(parents=True, exist_ok=True)
        # Empty run_id validates format before a new run ID has been created.
        cursor = last_logged_sequence(target, run_id or "")
        if run_id is not None and cursor > latest_stored_sequence(ws, run_id):
            raise ValueError("event log sequence is newer than the workspace run history")
        with target.open("a", encoding="utf-8"):
            pass
    except (OSError, ValueError) as exc:
        log_lock.release()
        raise _fail(f"could not open event log {target}: {exc}", EXIT_INVALID) from exc
    return log_lock


def _report_problems(exc: AibenchError, *, json_output: bool = False) -> typer.Exit:
    if isinstance(exc, PolicyDenied):
        if not json_output:
            for denial in exc.denials:
                err_console.print(f"[red]denied:[/red] {escape(denial)}")
        return _fail(
            "nothing was dispatched: the policy denies this plan",
            EXIT_DENIED,
            json_output=json_output,
            details=list(exc.denials),
        )
    if isinstance(exc, PlanInvalid):
        if not json_output:
            for problem in exc.problems:
                err_console.print(f"[red]invalid:[/red] {escape(problem)}")
        return _fail(
            "nothing was dispatched: the plan is invalid",
            EXIT_INVALID,
            json_output=json_output,
            details=list(exc.problems),
        )
    return _fail(str(exc), EXIT_INVALID, json_output=json_output)


def _interrupted_before_dispatch(run_id: str | None, *, json_output: bool = False) -> typer.Exit:
    hint = f"; resume with: aibench resume {run_id}" if run_id else ""
    return _fail(
        f"interrupted before dispatch; nothing was dispatched{hint}",
        EXIT_INTERRUPTED,
        json_output=json_output,
    )


async def _execute(run_id: str, storage: Storage, artifacts: ArtifactStore) -> RunOutcome:
    controller = RunController()
    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda *_: controller.request("interrupt"))
    try:
        return await execute_run(
            run_id, storage=storage, artifacts=artifacts, controller=controller
        )
    finally:
        signal.signal(signal.SIGINT, previous)


def _finish(
    run_id: str,
    outcome: RunOutcome,
    storage: Storage,
    artifacts: ArtifactStore,
    json_output: bool,
    *,
    retry_scope: dict[str, object] | None = None,
    quiet: bool = False,
) -> int:
    """Print the outcome with its gate verdicts (from the stored report) and return the
    §13 exit code."""
    report = build_report(storage, artifacts, run_id, include_content=False)
    code = run_exit_code(outcome.state, report)
    run_record = storage.get_run(run_id)
    run_seed = run_record.manifest.seed if run_record is not None else None
    if json_output:
        console.print_json(
            data={
                "run_id": run_id,
                "parent_run_id": run_record.manifest.parent_run_id if run_record else None,
                "run_seed": run_seed,
                **outcome_json(outcome),
                "gates": report["gates"],
                "outcome": report["outcome"],
                "exit_code": code,
                **({"retry_scope": retry_scope} if retry_scope is not None else {}),
            }
        )
        return code
    if quiet:
        return code
    _print_outcome(run_id, outcome)
    if run_seed is not None:
        console.print(f"  run_seed: {run_seed}")
    for gate in report["gates"]:
        colour = {"pass": "green", "fail": "red"}.get(gate["status"], "yellow")
        reason = f": {gate['reason']}" if gate.get("reason") else ""
        console.print(
            f"  gate {safe(gate['gate_id'])}: [{colour}]{gate['status']}[/{colour}]{safe(reason)}"
        )
    console.print(f"  report: aibench report {run_id}")
    return code


def _print_outcome(run_id: str, outcome: RunOutcome) -> None:
    console.print(f"run [bold]{escape(run_id)}[/bold]: {outcome.state.value}")
    for kind, states in sorted(outcome.counts.items()):
        console.print(f"  {kind}: " + ", ".join(f"{s}={n}" for s, n in sorted(states.items())))
    budget = outcome.budget
    unknown = sum(budget[role]["calls_with_unknown_cost"] for role in ("application", "evaluator"))
    cost = f"known cost ${budget['application']['known_cost_usd'] + budget['evaluator']['known_cost_usd']:g}"
    if unknown:
        cost += f" + {unknown} call(s) of unknown cost"
    console.print(
        f"  calls: application={budget['application']['calls']} "
        f"evaluator={budget['evaluator']['calls']} planner={budget['planner']['calls']}; {cost}"
    )
    for note in budget.get("unenforced", []):
        console.print(f"  [yellow]not enforced:[/yellow] {escape(note)}")
    if outcome.stop_reason:
        console.print(f"  [yellow]stopped dispatching:[/yellow] {escape(outcome.stop_reason)}")
    for warning in outcome.warnings:
        console.print(f"  [yellow]warning:[/yellow] {escape(warning)}")
    if outcome.state is RunState.INTERRUPTED:
        console.print(f"  resume with: aibench resume {run_id}")


def _run_preview(compiled: CompiledRun, *, run_seed: int | None) -> dict[str, object]:
    """Describe the exact compiled inputs and selected work without persisting a run."""
    plan = compiled.plan
    measurements = len(compiled.cases) * plan.repetitions
    warmups = len(compiled.cases) * plan.warmup_repetitions
    executions = measurements + warmups
    return {
        "schema": "aibench.run-preview/1",
        "status": "dry_run",
        "will_dispatch": False,
        "reproducibility": {
            "run_seed": run_seed,
            "seed_will_be_random": run_seed is None,
        },
        "frozen": {
            "plan_id": plan.plan_id,
            "plan_hash": compiled.plan_hash,
            "effective_plan": plan.model_dump(mode="json"),
            "dataset": {
                "dataset_id": compiled.dataset.dataset_id,
                "dataset_hash": compiled.dataset.content_hash,
                "dataset_case_count": compiled.dataset.case_count,
            },
            "application": {
                "application_id": compiled.application.spec.application_id,
                "application_hash": content_hash(
                    compiled.application.spec.model_dump(mode="json")
                ),
                "runner": compiled.application.spec.runner.value,
            },
            "policy_hash": compiled.policy_hash,
        },
        "scope": {
            "case_ids": [case.case_id for case in compiled.cases],
            "case_count": len(compiled.cases),
            "repetitions": plan.repetitions,
            "warmup_repetitions": plan.warmup_repetitions,
            "execution_items": executions,
            "measurement_execution_items": measurements,
            "warmup_execution_items": warmups,
            "evaluation_items": measurements * len(compiled.metrics),
            "metrics": [
                {
                    "binding_hash": metric.binding_hash,
                    "evaluator_id": metric.manifest.evaluator_id,
                    "version": metric.manifest.version,
                    "plugin_id": metric.manifest.plugin_id,
                    "plugin_version": metric.manifest.plugin_version,
                }
                for metric in compiled.metrics
            ],
        },
    }


def _launch_detached(
    run_id: str,
    project_root: Path,
    storage: Storage,
    *,
    log_file: Path | None = None,
    verbose: bool = False,
    quiet: bool = False,
) -> dict[str, object]:
    """Start a separately supervised CLI worker and retain its output in the workspace."""
    record = storage.get_run(run_id)
    if record is None:
        raise RunError(f"no run committed with run_id={run_id!r}")
    if record.status not in RESUMABLE_STATES:
        raise RunError(f"run {run_id} is {record.status}; only unfinished runs can be detached")
    if lease_state(storage, run_id) == "live":
        raise RunError(f"run {run_id} already has a live worker")
    digest = content_hash(run_id).split(":", 1)[-1][:24]
    workspace = Workspace.at(project_root)
    log_path = workspace.root / "logs" / f"run-{digest}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    event_log: Path | None = None
    if log_file is not None:
        event_log = log_file.expanduser().resolve()
        if event_log == workspace.db_path.resolve() or event_log == log_path.resolve():
            raise RunError("--log-file must be separate from the workspace database and worker log")
        try:
            event_log.parent.mkdir(parents=True, exist_ok=True)
            logged_sequence = last_logged_sequence(event_log, run_id)
            if logged_sequence > latest_stored_sequence(workspace, run_id):
                raise ValueError("event log sequence is newer than the workspace run history")
            with event_log.open("a", encoding="utf-8"):
                pass
        except (OSError, ValueError) as exc:
            raise RunError(f"could not open event log {event_log}: {exc}") from exc
    relative_log = log_path.relative_to(workspace.root.parent).as_posix()
    storage.append_run_event(
        run_id,
        "detached_worker_launching",
        {"log_path": relative_log, "requested_by": "cli"},
    )
    command = [
        sys.executable,
        "-m",
        "aibench.cli.main",
        "--non-interactive",
        "--json",
    ]
    if verbose:
        command.append("--verbose")
    if quiet:
        command.append("--quiet")
    command.extend(
        [
            "resume",
            run_id,
            "--workspace",
            str(project_root.resolve()),
            "--worker",
        ]
    )
    if log_file is not None:
        command.extend(["--log-file", str(event_log)])
    creationflags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    try:
        with log_path.open("ab") as worker_log:
            worker = subprocess.Popen(
                command,
                cwd=project_root,
                stdin=subprocess.DEVNULL,
                stdout=worker_log,
                stderr=subprocess.STDOUT,
                close_fds=True,
                creationflags=creationflags,
                start_new_session=start_new_session,
            )
    except OSError as exc:
        storage.append_run_event(
            run_id,
            "detached_worker_launch_failed",
            {"log_path": relative_log, "reason": str(exc)},
        )
        raise RunError(f"could not start detached worker: {exc}") from exc
    payload: dict[str, object] = {
        "pid": worker.pid,
        "host": socket.gethostname(),
        "log_path": relative_log,
    }
    if event_log is not None:
        payload["event_log_path"] = str(event_log)
    storage.append_run_event(run_id, "detached_worker_started", payload)
    return {"run_id": run_id, "worker": {"state": "started", **payload}}


def _print_run_preview(preview: dict[str, object], *, json_output: bool) -> None:
    if json_output:
        console.print_json(data=preview)
    else:
        typer.echo(json.dumps(preview, indent=2, ensure_ascii=False))


def _retry_failure_case_ids(
    executions: list[ExecutionResult], work_items: list[WorkItem] | None = None
) -> tuple[set[str], set[str], set[str]]:
    """Return failed, automatically safe, and effect-risk case IDs.

    A child repeats work at case granularity. A failed repetition is automatically safe
    only when every final repetition for that case has a known non-effectful outcome and
    no execution work item records an ambiguous/in-flight dispatch or an uncommitted failure.
    """
    final_executions = select_final_executions(executions)
    failed = {
        execution.case_id
        for execution in final_executions
        if not execution.warmup and execution.status is ExecutionStatus.ERROR
    }
    effect_risk: set[str] = set()
    safe_states = (EffectState.NONE_DECLARED, EffectState.NOT_DISPATCHED)
    recorded_attempts = {
        (execution.case_id, execution.repetition_id, execution.attempt_id)
        for execution in executions
    }
    for execution in final_executions:
        if execution.effect_state not in safe_states and execution.status not in (
            ExecutionStatus.SKIPPED,
            ExecutionStatus.NOT_APPLICABLE,
        ):
            effect_risk.add(execution.case_id)
    for item in work_items or []:
        if item.kind != "execution" or item.state not in (
            WorkItemState.RUNNING,
            WorkItemState.UNKNOWN_EFFECT,
            WorkItemState.FAILED,
        ):
            continue
        try:
            case_id, repetition, _ = parse_work_item_key(item.task_key, "execution")
        except ValueError:
            # If an in-flight item cannot be associated with its case, fail closed for
            # every failed case rather than risk repeating an unknown dispatch.
            effect_risk.update(failed)
        else:
            if item.state is not WorkItemState.FAILED or (
                case_id,
                repetition,
                item.attempt,
            ) not in recorded_attempts:
                effect_risk.add(case_id)
    unsafe_failed = failed & effect_risk
    return failed, failed - unsafe_failed, effect_risk


def _resolve_plan(
    target: Path | None, policy: Path | None, *, json_output: bool = False
) -> tuple[Path, Path | None]:
    """The plan and policy for `aibench run [TARGET]` without `--plan`."""
    from aibench.cli.chat import project_settings

    if target is not None and not target.exists():
        raise _fail(f"{target} does not exist", EXIT_INVALID, json_output=json_output)
    dataset = target if target is not None and target.is_file() else None
    root = target if target is not None and target.is_dir() else Path.cwd()
    try:
        settings = project_settings(root.resolve(), None, None, policy)
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    plan = settings["plan"]
    if plan is None:
        where = settings["config"] or root.resolve()
        raise _fail(
            f"no plan to run: pass --plan FILE, or set plan_path in the project config "
            f"({where}); `aibench init` creates one. A dataset alone cannot identify an "
            "application.",
            EXIT_INVALID,
            json_output=json_output,
        )
    if dataset is not None:
        try:
            bound = (plan.parent / load_plan(plan).dataset).resolve()
        except PlanInvalid as exc:
            raise _report_problems(exc, json_output=json_output) from exc
        if bound != dataset.resolve():
            raise _fail(
                f"the configured plan {plan} is bound to {bound}, not {dataset.resolve()}; "
                "run a plan whose dataset is this file (aibench plan --dataset ... --out ...)",
                EXIT_INVALID,
                json_output=json_output,
            )
    return plan, settings["policy"]


def run_plan(
    target: Path | None = typer.Argument(  # noqa: B008
        None, help="Project directory, or a dataset its configured plan uses (default: cwd)."
    ),
    plan: Path | None = typer.Option(  # noqa: B008
        None, "--plan", help="Executable plan file (JSON/YAML); default: the project config's."
    ),
    policy: Path | None = _POLICY,
    trust_local_app: bool = typer.Option(
        False, "--trust-local-app", help="Grant trusted-local mode for a CLI application."
    ),
    limit: int | None = typer.Option(
        None, "--limit", min=1, help="Use the first N cases after the plan's selectors."
    ),
    sample_size: int | None = typer.Option(
        None, "--sample-size", min=1, help="Use a stable seeded sample after the plan's selectors."
    ),
    selection_seed: int | None = typer.Option(
        None, "--selection-seed", min=0, help="Seed for --sample-size (or a plan sample)."
    ),
    run_seed: int | None = typer.Option(
        None,
        "--run-seed",
        min=0,
        max=2**31 - 1,
        help="Set the engine RNG seed; defaults to a random seed recorded with the run.",
    ),
    repetitions: int | None = typer.Option(
        None, "--repetitions", min=1, max=100, help="Override the plan's repetition count."
    ),
    warmup_repetitions: int | None = typer.Option(
        None,
        "--warmup-repetitions",
        min=0,
        max=100,
        help="Override the plan's warmup application-call count per case (excluded from metrics).",
    ),
    application_concurrency: int | None = typer.Option(
        None, "--application-concurrency", min=1, max=64,
        help="Override concurrent application calls.",
    ),
    evaluation_concurrency: int | None = typer.Option(
        None, "--evaluation-concurrency", min=1, max=64,
        help="Override concurrent evaluator calls.",
    ),
    max_attempts: int | None = typer.Option(
        None, "--max-attempts", min=1, max=10,
        help="Override total attempts per work item, including the first.",
    ),
    evaluation_timeout_seconds: float | None = typer.Option(
        None, "--evaluation-timeout-seconds", min=0.000001, max=3600,
        help="Override the per-case non-model evaluator timeout.",
    ),
    model_evaluation_timeout_seconds: float | None = typer.Option(
        None, "--model-evaluation-timeout-seconds", min=0.000001, max=3600,
        help="Override the per-case model-backed evaluator timeout.",
    ),
    max_application_calls: int | None = typer.Option(
        None, "--max-app-calls", min=1, help="Override the maximum application calls."
    ),
    max_evaluator_calls: int | None = typer.Option(
        None, "--max-evaluator-calls", min=1, help="Override the maximum evaluator calls."
    ),
    max_judge_tokens: int | None = typer.Option(
        None, "--max-judge-tokens", min=1, help="Override the maximum judge tokens."
    ),
    max_wall_seconds: float | None = typer.Option(
        None, "--max-wall-seconds", min=0.000001, help="Override the run wall-time budget."
    ),
    max_cost_usd: float | None = typer.Option(
        None, "--max-cost-usd", min=0, help="Override the soft estimated-cost budget."
    ),
    estimated_cost_per_application_call_usd: float | None = typer.Option(
        None, "--estimated-cost-per-app-call-usd", min=0,
        help="Estimated spend per application call, required to enforce --max-cost-usd.",
    ),
    estimated_cost_per_evaluation_usd: float | None = typer.Option(
        None, "--estimated-cost-per-evaluation-usd", min=0,
        help="Estimated spend per evaluator call for model-backed metrics.",
    ),
    cache_executions: bool | None = typer.Option(
        None, "--cache-executions/--no-cache-executions",
        help="Override execution-cache behavior from the plan.",
    ),
    cache_evaluations: bool | None = typer.Option(
        None, "--cache-evaluations/--no-cache-evaluations",
        help="Override evaluation-cache behavior from the plan.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and print the exact frozen scope without creating a run."
    ),
    detach: bool = typer.Option(
        False, "--detach", help="Run in a separate supervised process and write output to its log."
    ),
    log_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--log-file",
        help="Append this run's durable events as JSONL to a file.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress human-readable run progress; errors and JSON remain visible.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Write each durable run event to stderr while it occurs."
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Execute a plan: validate, freeze, run, evaluate."""
    if quiet and verbose:
        raise _fail(
            "--quiet cannot be combined with --verbose", EXIT_INVALID, json_output=json_output
        )
    if dry_run and log_file is not None:
        raise _fail(
            "--log-file requires a run; it cannot be used with --dry-run",
            EXIT_INVALID,
            json_output=json_output,
        )
    if plan is None:
        plan, policy = _resolve_plan(target, policy, json_output=json_output)
        if workspace is None and target is not None and target.is_dir():
            workspace = target  # the project's own .aibench/, where its chat and report look
    elif target is not None:
        raise _fail(
            "pass either --plan FILE or a project directory/dataset, not both",
            EXIT_INVALID,
            json_output=json_output,
        )
    if dry_run and detach:
        raise _fail(
            "--detach cannot be combined with --dry-run", EXIT_INVALID, json_output=json_output
        )
    try:
        compiled = compile_plan(
            plan,
            policy=load_policy(policy),
            trusted_local=trust_local_app,
            overrides=RunPlanOverrides(
                limit=limit,
                sample_size=sample_size,
                selection_seed=selection_seed,
                repetitions=repetitions,
                warmup_repetitions=warmup_repetitions,
                application_concurrency=application_concurrency,
                evaluation_concurrency=evaluation_concurrency,
                max_attempts=max_attempts,
                evaluation_timeout_seconds=evaluation_timeout_seconds,
                model_evaluation_timeout_seconds=model_evaluation_timeout_seconds,
                max_application_calls=max_application_calls,
                max_evaluator_calls=max_evaluator_calls,
                max_judge_tokens=max_judge_tokens,
                max_wall_seconds=max_wall_seconds,
                max_cost_usd=max_cost_usd,
                estimated_cost_per_application_call_usd=(
                    estimated_cost_per_application_call_usd
                ),
                estimated_cost_per_evaluation_usd=estimated_cost_per_evaluation_usd,
                cache_executions=cache_executions,
                cache_evaluations=cache_evaluations,
            ),
        )
    except AibenchError as exc:
        raise _report_problems(exc, json_output=json_output) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(None, json_output=json_output) from exc
    if dry_run:
        _print_run_preview(_run_preview(compiled, run_seed=run_seed), json_output=json_output)
        return
    event_log_lock = _preflight_event_log(workspace, log_file)
    try:
        storage, artifacts = _open(workspace, create=True, json_output=json_output)
    except BaseException:
        if event_log_lock is not None:
            event_log_lock.release()
        raise
    run_id: str | None = None
    try:
        granted = "cli:--trust-local-app" if trust_local_app else "policy"
        run_id = create_run(
            compiled,
            storage=storage,
            artifacts=artifacts,
            granted_by=granted,
            run_seed=run_seed,
        )
        if not json_output and not quiet:
            console.print(
                f"run [bold]{run_id}[/bold] created from plan {escape(compiled.plan.plan_id)}"
            )
        if detach:
            project_root = (workspace or Path.cwd()).resolve()
            launch = _launch_detached(
                run_id,
                project_root,
                storage,
                log_file=log_file,
                verbose=verbose,
                quiet=quiet,
            )
            code = 0
            if json_output:
                console.print_json(data={**launch, "exit_code": code})
            elif not quiet:
                worker = launch["worker"]
                assert isinstance(worker, dict)
                console.print(
                    f"detached worker started for run [bold]{run_id}[/bold] "
                    f"(pid {worker['pid']}); log: {worker['log_path']}"
                )
                if log_file is not None:
                    console.print(f"  event log: {log_file.expanduser().resolve()}")
                console.print(f"control it with: aibench runs control {run_id} pause|resume|cancel")
        else:
            stream = _start_event_stream(
                workspace,
                run_id,
                log_file=log_file,
                verbose=verbose,
                log_lock=event_log_lock,
            )
            try:
                outcome = asyncio.run(_execute(run_id, storage, artifacts))
            finally:
                stream.close()
            code = _finish(run_id, outcome, storage, artifacts, json_output, quiet=quiet)
    except AibenchError as exc:
        raise _report_problems(exc, json_output=json_output) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(run_id, json_output=json_output) from exc
    finally:
        storage.db.close()
        if event_log_lock is not None:
            event_log_lock.release()
    raise typer.Exit(code=code)


def resume(
    run_id: str = typer.Argument(..., help="Run to continue."),
    detach: bool = typer.Option(
        False,
        "--detach",
        help="Resume in a separate supervised process and write output to its log.",
    ),
    worker_process: bool = typer.Option(
        False, "--worker", hidden=True, help="Internal detached worker entry point."
    ),
    log_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--log-file",
        help="Append this run's durable events as JSONL to a file.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress human-readable run progress; errors and JSON remain visible.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Write each durable run event to stderr while it occurs."
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Continue eligible unfinished work under the run's frozen plan and identities."""
    if quiet and verbose:
        raise _fail(
            "--quiet cannot be combined with --verbose", EXIT_INVALID, json_output=json_output
        )
    storage, artifacts = _open(workspace, json_output=json_output)
    event_log_lock: EventLogLock | None = None
    try:
        if worker_process:
            record = storage.get_run(run_id)
            if record is None:
                raise RunError(f"no run committed with run_id={run_id!r}")
            stream = _start_event_stream(
                workspace,
                run_id,
                log_file=log_file,
                verbose=verbose,
                wait_for_log_lock=True,
            )
            try:
                outcome = asyncio.run(_execute(run_id, storage, artifacts))
            finally:
                stream.close()
            code = _finish(run_id, outcome, storage, artifacts, json_output, quiet=quiet)
        else:
            record = storage.get_run(run_id)
            if record is None:
                raise RunError(f"no run committed with run_id={run_id!r}")
            if record.status not in RESUMABLE_STATES:
                raise RunError(
                    f"run {run_id} is {record.status}; only interrupted or unfinished runs resume"
                )
            if record.manifest.parameters.get("mode") != "manual_plan":
                raise RunError(f"run {run_id} was not created from a plan")
            control = storage.get_run_control_state(run_id)
            pending_cancel = record.status == "cancelling" or (
                control is not None and control.desired_state == "cancelled"
            )
            worker_is_live = lease_state(storage, run_id) == "live"
            if worker_is_live and (log_file is not None or verbose):
                raise RunError(
                    "logging options cannot be changed on a live worker; use "
                    f"aibench runs events {run_id} --follow instead"
                )
            event_log_lock = _preflight_event_log(workspace, log_file, run_id=run_id)
            if pending_cancel:
                if control is None or control.desired_state != "cancelled":
                    control = request_run_control(
                        storage, run_id, "cancel", requested_by="aibench resume recovery"
                    )
            else:
                control = request_run_control(
                    storage, run_id, "resume", requested_by="aibench resume"
                )
            if worker_is_live:
                accepted: dict[str, object] = {
                    "run_id": run_id,
                    "action": "cancel" if pending_cancel else "resume",
                    "desired_state": control.desired_state if control else "running",
                    "sequence": control.sequence if control else 0,
                    "accepted": True,
                    "accepted_by": "live worker",
                }
                code = 0
                if json_output:
                    console.print_json(data=accepted)
                elif not quiet:
                    action_text = "pending cancel" if pending_cancel else "resume request"
                    console.print(f"{action_text} accepted for live run [bold]{run_id}[/bold]")
            elif detach:
                launch = _launch_detached(
                    run_id,
                    (workspace or Path.cwd()).resolve(),
                    storage,
                    log_file=log_file,
                    verbose=verbose,
                    quiet=quiet,
                )
                code = 0
                if json_output:
                    console.print_json(
                        data={
                            **launch,
                            "control_sequence": control.sequence if control else 0,
                            "pending_cancel": pending_cancel,
                        }
                    )
                elif not quiet:
                    worker = launch["worker"]
                    assert isinstance(worker, dict)
                    console.print(
                        f"detached worker started for run [bold]{run_id}[/bold] "
                        f"(pid {worker['pid']}); log: {worker['log_path']}"
                    )
                    if log_file is not None:
                        console.print(f"  event log: {log_file.expanduser().resolve()}")
            else:
                stream = _start_event_stream(
                    workspace,
                    run_id,
                    log_file=log_file,
                    verbose=verbose,
                    log_lock=event_log_lock,
                )
                try:
                    outcome = asyncio.run(_execute(run_id, storage, artifacts))
                finally:
                    stream.close()
                code = _finish(run_id, outcome, storage, artifacts, json_output, quiet=quiet)
    except AibenchError as exc:  # includes RunError, PolicyDenied and LeaseHeld
        raise _report_problems(exc, json_output=json_output) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(run_id, json_output=json_output) from exc
    finally:
        storage.db.close()
        if event_log_lock is not None:
            event_log_lock.release()
    raise typer.Exit(code=code)


def control(
    run_id: str = typer.Argument(..., help="Run with a live or resumable worker."),
    action: str = typer.Argument(..., help="Desired state: pause, resume, or cancel."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Send a durable desired-state request to a run's owning worker."""
    if action not in {"pause", "resume", "cancel"}:
        raise _fail(
            "action must be pause, resume, or cancel", EXIT_INVALID, json_output=json_output
        )
    storage, _ = _open(workspace, json_output=json_output)
    try:
        state = request_run_control(storage, run_id, action, requested_by="aibench runs control")
        data = {
            "run_id": run_id,
            "action": action,
            "desired_state": state.desired_state,
            "sequence": state.sequence,
            "accepted": True,
            "worker_lease": lease_state(storage, run_id),
        }
    except RunError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data=data)
    else:
        worker = data["worker_lease"]
        message = (
            "request accepted by live worker"
            if worker == "live"
            else "request saved for the next resume"
        )
        console.print(
            f"{action} request accepted for run [bold]{run_id}[/bold] "
            f"(sequence {state.sequence}; {message})"
        )


def retry(
    parent_run_id: str = typer.Argument(..., help="Finished parent run to retry from."),
    policy: Path | None = _POLICY,
    trust_local_app: bool = typer.Option(
        False, "--trust-local-app", help="Grant trusted-local mode for a CLI application."
    ),
    case_ids: list[str] | None = typer.Option(  # noqa: B008
        None,
        "--case",
        help="Explicit case to rerun; repeat this option for multiple cases. Defaults to safely failed cases.",
    ),
    max_cases: int = typer.Option(
        100, "--max-cases", min=1, max=1000, help="Maximum failed cases selected automatically."
    ),
    repetitions: int = typer.Option(
        1, "--repetitions", min=1, max=100, help="Application repetitions in the child run."
    ),
    run_seed: int | None = typer.Option(
        None,
        "--run-seed",
        min=0,
        max=2**31 - 1,
        help="Set the child engine RNG seed; defaults to a recorded random seed.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Validate and preview the child scope without creating a run."
    ),
    log_file: Path | None = typer.Option(  # noqa: B008
        None,
        "--log-file",
        help="Append this run's durable events as JSONL to a file.",
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        help="Suppress human-readable run progress; errors and JSON remain visible.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Write each durable run event to stderr while it occurs."
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Create a bounded child run for safe application failures or explicit cases."""
    if quiet and verbose:
        raise _fail(
            "--quiet cannot be combined with --verbose", EXIT_INVALID, json_output=json_output
        )
    if dry_run and log_file is not None:
        raise _fail(
            "--log-file requires a run; it cannot be used with --dry-run",
            EXIT_INVALID,
            json_output=json_output,
        )
    if max_cases > 1000:
        raise _fail("--max-cases must be between 1 and 1000", EXIT_INVALID, json_output=json_output)
    if case_ids is not None:
        if len(set(case_ids)) != len(case_ids):
            raise _fail("--case values must be unique", EXIT_INVALID, json_output=json_output)
        if len(case_ids) > max_cases:
            raise _fail(
                f"{len(case_ids)} explicit cases exceed --max-cases={max_cases}",
                EXIT_INVALID,
                json_output=json_output,
            )

    storage, artifacts = _open(workspace, json_output=json_output)
    child_run_id: str | None = None
    event_log_lock: EventLogLock | None = None
    try:
        parent = storage.get_run(parent_run_id)
        if parent is None:
            raise RunError(f"no run committed with run_id={parent_run_id!r}")
        if parent.status in RESUMABLE_STATES:
            raise RunError(
                f"run {parent_run_id!r} is {parent.status}; finish or resume it before retrying"
            )
        from aibench.cli.chat import project_settings

        parent_plan_dir = Path(
            str(parent.manifest.parameters.get("plan_dir", Path.cwd()))
        ).resolve()
        current_settings = project_settings(parent_plan_dir, None, None, policy)

        failed_case_ids, safe_failed_case_ids, effect_risk_case_ids = _retry_failure_case_ids(
            storage.list_execution_attempts(parent_run_id), storage.list_work_items(parent_run_id)
        )
        unsafe_failed_case_ids = failed_case_ids & effect_risk_case_ids

        truncated_case_ids: list[str] = []
        if case_ids is None:
            ordered = sorted(safe_failed_case_ids)
            selected_case_ids = ordered[:max_cases]
            truncated_case_ids = ordered[max_cases:]
            if not selected_case_ids:
                if failed_case_ids:
                    raise RunError(
                        "no safely retryable failed cases; the parent failures may have caused "
                        "external effects. Select intended cases explicitly with --case"
                    )
                raise RunError("the parent run has no failed application cases to retry")
        else:
            selected_case_ids = list(case_ids)
            if not selected_case_ids:
                raise RunError("provide at least one --case or omit --case to select failures")

        selected_effect_risk_case_ids = sorted(set(selected_case_ids) & effect_risk_case_ids)
        compiled = compile_retry_run(
            parent_run_id,
            selected_case_ids,
            repetitions,
            storage=storage,
            artifacts=artifacts,
            current_policy=load_policy(current_settings["policy"]),
            trusted_local=trust_local_app,
        )
        preview = {
            "schema": "aibench.retry-preview/1",
            "status": "dry_run" if dry_run else "ready",
            "will_dispatch": not dry_run,
            "parent_run_id": parent_run_id,
            "selected_case_ids": [case.case_id for case in compiled.cases],
            "case_count": len(compiled.cases),
            "repetitions": compiled.plan.repetitions,
            "warmup_repetitions": compiled.plan.warmup_repetitions,
            "execution_items": len(compiled.cases)
            * (compiled.plan.repetitions + compiled.plan.warmup_repetitions),
            "measurement_execution_items": len(compiled.cases) * compiled.plan.repetitions,
            "warmup_execution_items": len(compiled.cases) * compiled.plan.warmup_repetitions,
            "evaluation_items": len(compiled.cases)
            * compiled.plan.repetitions
            * len(compiled.metrics),
            "failed_case_count": len(failed_case_ids),
            "unsafe_failed_case_count": len(unsafe_failed_case_ids),
            "unsafe_failed_case_ids": sorted(unsafe_failed_case_ids),
            "selected_effect_risk_case_ids": selected_effect_risk_case_ids,
            "truncated_case_ids": truncated_case_ids,
            "run_seed": run_seed,
            "plan_hash": compiled.plan_hash,
            "dataset_hash": compiled.dataset.content_hash,
        }
        if dry_run:
            if json_output:
                console.print_json(data=preview)
            elif not quiet:
                console.print(
                    f"retry preview from {escape(parent_run_id)}: "
                    f"{len(compiled.cases)} case(s), {repetitions} repetition(s), "
                    f"{preview['execution_items']} application execution(s); nothing dispatched"
                )
                console.print(
                    "  cases: " + ", ".join(escape(case_id) for case_id in selected_case_ids)
                )
                if truncated_case_ids:
                    console.print(
                        f"  [yellow]{len(truncated_case_ids)} safe failed case(s) omitted by --max-cases[/yellow]"
                    )
                unsafe_selected = selected_effect_risk_case_ids
                if unsafe_selected:
                    console.print(
                        "  [yellow]selected case has prior execution(s) that may have caused "
                        "external effects; retry can repeat them: "
                        f"{', '.join(escape(case_id) for case_id in unsafe_selected)}[/yellow]"
                    )
            return

        event_log_lock = _preflight_event_log(workspace, log_file)
        child_run_id = create_run(
            compiled,
            storage=storage,
            artifacts=artifacts,
            granted_by="cli:runs retry",
            run_seed=run_seed,
            parent_run_id=parent_run_id,
        )
        if not json_output and not quiet:
            console.print(
                f"child run [bold]{child_run_id}[/bold] created from parent "
                f"[bold]{escape(parent_run_id)}[/bold] with {len(compiled.cases)} case(s)"
            )
            if truncated_case_ids:
                console.print(
                    f"[yellow]selected the first {max_cases} safe failures; "
                    f"{len(truncated_case_ids)} more were omitted by --max-cases[/yellow]"
                )
            explicitly_retried_unsafe = selected_effect_risk_case_ids
            if explicitly_retried_unsafe:
                console.print(
                    "[yellow]selected case has prior execution(s) that may have caused "
                    "external effects; retry can repeat them: "
                    f"{', '.join(escape(case_id) for case_id in explicitly_retried_unsafe)}[/yellow]"
                )
        stream = _start_event_stream(
            workspace,
            child_run_id,
            log_file=log_file,
            verbose=verbose,
            log_lock=event_log_lock,
        )
        try:
            outcome = asyncio.run(_execute(child_run_id, storage, artifacts))
        finally:
            stream.close()
        code = _finish(
            child_run_id,
            outcome,
            storage,
            artifacts,
            json_output,
            quiet=quiet,
            retry_scope={
                "selected_case_ids": [case.case_id for case in compiled.cases],
                "repetitions": compiled.plan.repetitions,
                "truncated_case_ids": truncated_case_ids,
                "unsafe_failed_case_ids": sorted(unsafe_failed_case_ids),
                "selected_effect_risk_case_ids": selected_effect_risk_case_ids,
            },
        )
    except AibenchError as exc:
        raise _report_problems(exc, json_output=json_output) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(child_run_id, json_output=json_output) from exc
    finally:
        storage.db.close()
        if event_log_lock is not None:
            event_log_lock.release()
    raise typer.Exit(code=code)


def evaluate(
    run_id: str = typer.Argument(..., help="Run whose saved executions to rescore."),
    plan: Path = typer.Option(..., "--plan", help="Plan whose metrics to apply."),  # noqa: B008
    policy: Path | None = _POLICY,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
    only_unfinished: bool = typer.Option(
        False,
        "--only-unfinished",
        help="Carry forward results already finished in earlier passes; evaluate only what "
        "failed or is missing.",
    ),
) -> None:
    """Rescore stored executions without rerunning the application."""
    storage, artifacts = _open(workspace, json_output=json_output)
    try:
        report = asyncio.run(
            evaluate_run(
                run_id,
                plan,
                storage=storage,
                artifacts=artifacts,
                policy=load_policy(policy),
                carry_forward=only_unfinished,
            )
        )
    except AibenchError as exc:
        raise _report_problems(exc, json_output=json_output) from exc
    finally:
        storage.db.close()
    data = {
        "scoring_id": report.scoring_id,
        "run_id": run_id,
        "summaries": [s.as_dict() for s in report.summaries],
        "warnings": report.warnings,
        "carried_forward": report.carried,
        "budget": report.budget,
        "quotas": report.quotas,
        "stop_reason": report.stop_reason,
        "outcome": report.outcome,
        "gates": report.gates,
        "exit_code": report.exit_code,
    }
    if json_output:
        console.print_json(data=data)
        raise typer.Exit(code=report.exit_code)
    console.print(
        f"rescored run {escape(run_id)} as {report.scoring_id} (the application was not invoked)"
    )
    console.print(
        f"  outcome: {'complete' if report.outcome.get('complete') else 'incomplete'} "
        f"(exit code {report.exit_code})"
    )
    for gate in report.gates:
        console.print(f"  release gate {escape(str(gate['gate_id']))}: {gate['status']}")
    if report.carried:
        console.print(f"  carried forward {report.carried} finished result(s)")
    if report.stop_reason:
        console.print(f"  stopped: {escape(report.stop_reason)}")
    for s in report.summaries:
        console.print(
            f"  {s.metric_id}@{s.metric_version}: completed={s.completed}/{s.selected} "
            f"decisions={s.decisions}"
        )
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


def status(
    run_id: str = typer.Argument(..., help="Run to inspect."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Committed run state: work item counts, items needing attention, budget."""
    storage, _ = _open(workspace, json_output=json_output)
    try:
        data = run_status(storage, run_id)
    except RunError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data=data)
        return
    console.print(f"run [bold]{escape(run_id)}[/bold]: {data['status']}")
    for kind, states in sorted(data["counts"].items()):
        console.print(f"  {kind}: " + ", ".join(f"{s}={n}" for s, n in sorted(states.items())))
    for item in data["needs_attention"]:
        console.print(
            f"  [yellow]{item['state']}[/yellow] {escape(item['task_key'])}: {escape(str(item['reason']))}"
        )
    for warning in data["warnings"]:
        console.print(f"  [yellow]warning:[/yellow] {escape(warning)}")
    supervision = data["supervision"]
    control_state = supervision["control"]
    console.print(
        f"  worker lease: {supervision['worker_lease'] or 'none'}; "
        f"desired state: {control_state['desired_state']} (sequence {control_state['sequence']})"
    )
    worker = supervision["detached_worker"]
    if worker:
        console.print(
            f"  detached worker: {worker['state']} (pid {worker.get('pid', 'unknown')}); "
            f"log: {worker.get('log_path', 'unknown')}"
        )
