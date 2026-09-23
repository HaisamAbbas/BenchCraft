"""`aibench run --plan`, `resume`, `evaluate`, `runs status` (06-T4).

Exit codes (§13): 0 complete; 2 invalid input or plan; 3 incomplete (failures, unknown
effects, blocked or cancelled work, evaluation errors); 4 authorization required (policy
denied); 130 interrupted (resumable with `aibench resume RUN_ID`).

Ctrl+C during a run: the first press stops new dispatch, records in-flight outcomes and
leaves the rest resumable; a second press also aborts in-flight work (still resumable).
Ctrl+C before dispatch starts exits 130 too; nothing was dispatched.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from aibench.core.errors import AibenchError
from aibench.engine.compile import PlanInvalid, PolicyDenied, compile_plan, load_policy
from aibench.engine.engine import RunController, RunOutcome, RunState
from aibench.services.runs import (
    RunError,
    create_run,
    evaluate_run,
    execute_run,
    outcome_json,
    run_status,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

console = Console()
err_console = Console(stderr=True)

EXIT_OK, EXIT_INVALID, EXIT_INCOMPLETE, EXIT_DENIED, EXIT_INTERRUPTED = 0, 2, 3, 4, 130

_WORKSPACE = typer.Option(
    None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
)
_POLICY = typer.Option(
    None, "--policy", help="Execution policy file. Default: the conservative built-in policy."
)
_JSON = typer.Option(False, "--json", help="Machine-readable output on stdout.")


def _fail(message: str, code: int) -> typer.Exit:
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=code)


def _open(workspace: Path | None, *, create: bool = False) -> tuple[Storage, ArtifactStore]:
    """Open the workspace; only `run` creates one — reading an absent one is an error."""
    ws = Workspace.at(workspace or Path.cwd())
    if not create and not ws.db_path.is_file():
        raise _fail(f"no aibench workspace at {ws.root}", EXIT_INVALID)
    ws.ensure_directories()
    return Storage(Database.open_workspace(ws)), ArtifactStore(ws.artifacts_dir)


def _report_problems(exc: AibenchError) -> typer.Exit:
    if isinstance(exc, PolicyDenied):
        for denial in exc.denials:
            err_console.print(f"[red]denied:[/red] {escape(denial)}")
        return _fail("nothing was dispatched: the policy denies this plan", EXIT_DENIED)
    if isinstance(exc, PlanInvalid):
        for problem in exc.problems:
            err_console.print(f"[red]invalid:[/red] {escape(problem)}")
        return _fail("nothing was dispatched: the plan is invalid", EXIT_INVALID)
    return _fail(str(exc), EXIT_INVALID)


def _interrupted_before_dispatch(run_id: str | None) -> typer.Exit:
    hint = f"; resume with: aibench resume {run_id}" if run_id else ""
    return _fail(f"interrupted before dispatch; nothing was dispatched{hint}", EXIT_INTERRUPTED)


def _exit_code(outcome: RunOutcome) -> int:
    if outcome.state is RunState.INTERRUPTED:
        return EXIT_INTERRUPTED
    counts = outcome.counts
    unhealthy = {"failed", "blocked", "cancelled", "unknown_effect"}
    if outcome.state is not RunState.COMPLETED or any(
        counts.get(kind, {}).get(state) for kind in counts for state in unhealthy
    ):
        return EXIT_INCOMPLETE
    return EXIT_OK


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


def _print_outcome(run_id: str, outcome: RunOutcome, json_output: bool) -> None:
    if json_output:
        console.print_json(data={"run_id": run_id, **outcome_json(outcome)})
        return
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
    if outcome.state is RunState.INTERRUPTED:
        console.print(f"  resume with: aibench resume {run_id}")


def run_plan(
    plan: Path = typer.Option(..., "--plan", help="Executable plan file (JSON/YAML)."),  # noqa: B008
    policy: Path | None = _POLICY,
    trust_local_app: bool = typer.Option(
        False, "--trust-local-app", help="Grant trusted-local mode for a CLI application."
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Execute a manual plan: validate, freeze, run, evaluate."""
    try:
        compiled = compile_plan(plan, policy=load_policy(policy), trusted_local=trust_local_app)
    except AibenchError as exc:
        raise _report_problems(exc) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(None) from exc
    storage, artifacts = _open(workspace, create=True)
    run_id: str | None = None
    try:
        granted = "cli:--trust-local-app" if trust_local_app else "policy"
        run_id = create_run(compiled, storage=storage, artifacts=artifacts, granted_by=granted)
        if not json_output:
            console.print(
                f"run [bold]{run_id}[/bold] created from plan {escape(compiled.plan.plan_id)}"
            )
        outcome = asyncio.run(_execute(run_id, storage, artifacts))
    except AibenchError as exc:
        raise _report_problems(exc) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(run_id) from exc
    finally:
        storage.db.close()
    _print_outcome(run_id, outcome, json_output)
    raise typer.Exit(code=_exit_code(outcome))


def resume(
    run_id: str = typer.Argument(..., help="Run to continue."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Continue eligible unfinished work under the run's frozen plan and identities."""
    storage, artifacts = _open(workspace)
    try:
        outcome = asyncio.run(_execute(run_id, storage, artifacts))
    except AibenchError as exc:  # includes RunError, PolicyDenied and LeaseHeld
        raise _report_problems(exc) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(run_id) from exc
    finally:
        storage.db.close()
    _print_outcome(run_id, outcome, json_output)
    raise typer.Exit(code=_exit_code(outcome))


def evaluate(
    run_id: str = typer.Argument(..., help="Run whose saved executions to rescore."),
    plan: Path = typer.Option(..., "--plan", help="Plan whose metrics to apply."),  # noqa: B008
    policy: Path | None = _POLICY,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Rescore stored executions without rerunning the application."""
    storage, artifacts = _open(workspace)
    try:
        report = asyncio.run(
            evaluate_run(
                run_id, plan, storage=storage, artifacts=artifacts, policy=load_policy(policy)
            )
        )
    except AibenchError as exc:
        raise _report_problems(exc) from exc
    finally:
        storage.db.close()
    data = {
        "scoring_id": report.scoring_id,
        "run_id": run_id,
        "summaries": [s.as_dict() for s in report.summaries],
        "warnings": report.warnings,
    }
    if json_output:
        console.print_json(data=data)
        return
    console.print(
        f"rescored run {escape(run_id)} as {report.scoring_id} (the application was not invoked)"
    )
    for s in report.summaries:
        console.print(
            f"  {s.metric_id}@{s.metric_version}: completed={s.completed}/{s.selected} "
            f"decisions={s.decisions}"
        )


def status(
    run_id: str = typer.Argument(..., help="Run to inspect."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Committed run state: work item counts, items needing attention, budget."""
    storage, _ = _open(workspace)
    try:
        data = run_status(storage, run_id)
    except RunError as exc:
        raise _fail(str(exc), EXIT_INVALID) from exc
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
