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
import signal
from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.core.errors import AibenchError
from aibench.engine.compile import (
    PlanInvalid,
    PolicyDenied,
    compile_plan,
    load_plan,
    load_policy,
)
from aibench.engine.engine import RunController, RunOutcome, RunState
from aibench.services.reports import build_report
from aibench.services.runs import (
    EXIT_DENIED,
    EXIT_INTERRUPTED,
    EXIT_INVALID,
    RunError,
    create_run,
    evaluate_run,
    execute_run,
    outcome_json,
    run_exit_code,
    run_status,
)
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
    run_id: str, outcome: RunOutcome, storage: Storage, artifacts: ArtifactStore, json_output: bool
) -> int:
    """Print the outcome with its gate verdicts (from the stored report) and return the
    §13 exit code."""
    report = build_report(storage, artifacts, run_id, include_content=False)
    code = run_exit_code(outcome.state, report)
    if json_output:
        console.print_json(
            data={
                "run_id": run_id,
                **outcome_json(outcome),
                "gates": report["gates"],
                "outcome": report["outcome"],
                "exit_code": code,
            }
        )
        return code
    _print_outcome(run_id, outcome)
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
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Execute a plan: validate, freeze, run, evaluate."""
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
    try:
        compiled = compile_plan(plan, policy=load_policy(policy), trusted_local=trust_local_app)
    except AibenchError as exc:
        raise _report_problems(exc, json_output=json_output) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(None, json_output=json_output) from exc
    storage, artifacts = _open(workspace, create=True, json_output=json_output)
    run_id: str | None = None
    try:
        granted = "cli:--trust-local-app" if trust_local_app else "policy"
        run_id = create_run(compiled, storage=storage, artifacts=artifacts, granted_by=granted)
        if not json_output:
            console.print(
                f"run [bold]{run_id}[/bold] created from plan {escape(compiled.plan.plan_id)}"
            )
        outcome = asyncio.run(_execute(run_id, storage, artifacts))
        code = _finish(run_id, outcome, storage, artifacts, json_output)
    except AibenchError as exc:
        raise _report_problems(exc, json_output=json_output) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(run_id, json_output=json_output) from exc
    finally:
        storage.db.close()
    raise typer.Exit(code=code)


def resume(
    run_id: str = typer.Argument(..., help="Run to continue."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Continue eligible unfinished work under the run's frozen plan and identities."""
    storage, artifacts = _open(workspace, json_output=json_output)
    try:
        outcome = asyncio.run(_execute(run_id, storage, artifacts))
        code = _finish(run_id, outcome, storage, artifacts, json_output)
    except AibenchError as exc:  # includes RunError, PolicyDenied and LeaseHeld
        raise _report_problems(exc, json_output=json_output) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(run_id, json_output=json_output) from exc
    finally:
        storage.db.close()
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
