"""`aibench benchmark APP --dataset DATA` (§3 "One-command workflow", 11-T3).

In an interactive terminal it opens the benchmark conversation with the application and
dataset already selected (a new session): inspection, drafting, review, running and
follow-up discussion all happen there, under the same authorization rules as `aibench`.

With `--non-interactive` (or without a terminal) it composes the headless workflow without
asking anything: inspect the declared app and dataset, draft a plan (template planner,
written to `--out` with its draft document), validate it, and then either
- stop with a machine-readable blocked result, when the draft needs information (exit 2)
  or a permission (exit 4). It never invents an endpoint, output schema or threshold;
- stop with `authorization_required` (exit 4) when the draft is executable but `--auto`
  was not given: running is an action the user has to authorize; or
- with `--auto --policy FILE`, run it within that existing policy only. `--auto` grants
  nothing: not trusted-local mode (set `allow_trusted_local` in the policy), not new
  destinations, installs or effects. The run's report is exported (HTML and JSON) and the
  exit code follows §13, exactly as for `aibench run`.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from aibench.cli.chat import chat, interactive_terminal
from aibench.cli.output import Console, json_result
from aibench.cli.plan import _exit_for, _print_findings, _summary
from aibench.cli.run import _execute, _interrupted_before_dispatch, _report_problems
from aibench.core.errors import AibenchError
from aibench.engine.compile import compile_plan, load_policy
from aibench.planning.planner import plan_with_template
from aibench.planning.service import gather_inputs, write_draft
from aibench.services.reports import build_report, export_report, report_dir
from aibench.services.runs import EXIT_DENIED, EXIT_INVALID, create_run, run_exit_code
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.render import safe

console = Console(highlight=False, emoji=False)
err_console = Console(stderr=True, highlight=False, emoji=False)


def _emit(data: dict[str, Any], json_output: bool, message: str, code: int) -> typer.Exit:
    if json_output:
        print(json.dumps(json_result({**data, "exit_code": code}, exit_code=code), default=str))
    else:
        (console if code == 0 else err_console).print(safe(message))
    return typer.Exit(code=code)


def benchmark(
    app: Path = typer.Argument(..., help="Application config file."),  # noqa: B008
    dataset: Path = typer.Option(..., "--dataset", help="JSONL dataset."),  # noqa: B008
    objectives: list[str] = typer.Option(  # noqa: B008
        [], "--objective", help="What the benchmark should check (repeatable)."
    ),
    out: Path = typer.Option(  # noqa: B008
        Path("benchmark.plan.json"), "--out", help="Plan file to write."
    ),
    policy: Path | None = typer.Option(  # noqa: B008
        None, "--policy", help="Execution policy (required with --auto)."
    ),
    auto: bool = typer.Option(
        False, "--auto", help="Run the drafted plan within --policy, without asking."
    ),
    non_interactive: bool = typer.Option(
        False, "--non-interactive", help="Headless workflow; never prompts."
    ),
    trust_local_app: bool = typer.Option(
        False,
        "--trust-local-app",
        help="Interactive/draft only: grant trusted-local mode (not with --auto).",
    ),
    provider_config: Path | None = typer.Option(  # noqa: B008
        None, "--provider-config", help="Assistant model config for the conversation."
    ),
    revise: bool = typer.Option(False, "--revise", help="Replace a different existing plan."),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output on stdout."),
) -> None:
    """Guided inspect -> plan -> validate -> run composition."""
    if not (non_interactive or auto) and interactive_terminal():
        chat(
            project=workspace,
            resume=None,
            new=True,
            app=app,
            dataset=dataset,
            policy=policy,
            trust_local_app=trust_local_app,
            provider_config=provider_config,
            send=None,
            json_output=False,
            objectives=list(objectives),
        )
        return
    if auto and policy is None:
        raise _emit(
            {"status": "invalid", "problem": "--auto needs --policy"},
            json_output,
            "--auto runs only within an existing policy: pass --policy FILE",
            EXIT_INVALID,
        )
    if auto and trust_local_app:
        raise _emit(
            {"status": "invalid", "problem": "--auto does not grant trusted-local mode"},
            json_output,
            "--auto grants no new permission: set allow_trusted_local in the policy instead "
            "of passing --trust-local-app",
            EXIT_INVALID,
        )
    try:
        loaded_policy = load_policy(policy)
        gathered = gather_inputs(
            application=app,
            dataset=dataset,
            objectives=list(objectives),
            out=out,
            policy=loaded_policy,
            trusted_local=trust_local_app,
            source_root=Path.cwd(),
        )
        outcome = plan_with_template(gathered.inputs)
        document = write_draft(outcome, gathered.inputs, out, revise=revise)
    except AibenchError as exc:
        raise _emit(
            {"status": "invalid", "stage": "plan", "problem": str(exc)},
            json_output,
            str(exc),
            EXIT_INVALID,
        ) from exc
    findings = outcome.validation.findings
    draft = {
        "plan": str(out),
        "revision": document.revision,
        "executable": document.executable,
        "gaps": [g.model_dump(mode="json") for g in document.gaps],
        "questions": [q.model_dump(mode="json") for q in document.pending_questions],
        "findings": [f.as_dict() for f in findings],
    }
    if not json_output:
        _summary(document)
        _print_findings(findings)
    code = _exit_for(findings)
    if code != 0 or not document.executable:
        code = code or EXIT_INVALID
        raise _emit(
            {"status": "blocked", "stage": "plan", "draft": draft},
            json_output,
            f"not run: the draft in {out} needs "
            + ("a permission the policy does not grant" if code == EXIT_DENIED else "information")
            + "; nothing was dispatched",
            code,
        )
    if not auto:
        next_command = f"aibench run --plan {out}" + (f" --policy {policy}" if policy else "")
        raise _emit(
            {"status": "authorization_required", "draft": draft, "next": next_command},
            json_output,
            f"the draft is executable but was not run: running needs your authorization. "
            f"Run it with `{next_command}`, or pass --auto --policy FILE to run within a policy.",
            EXIT_DENIED,
        )

    ws = Workspace.at(workspace or Path.cwd())
    ws.ensure_directories()
    storage = Storage(Database.open_workspace(ws))
    artifacts = ArtifactStore(ws.artifacts_dir)
    run_id: str | None = None
    try:
        compiled = compile_plan(out, policy=loaded_policy)
        run_id = create_run(
            compiled,
            storage=storage,
            artifacts=artifacts,
            granted_by=f"benchmark --auto within policy {Path(str(policy)).resolve()}",
        )
        if not json_output:
            console.print(f"run {run_id} started within the policy")
        run_outcome = asyncio.run(_execute(run_id, storage, artifacts))
        report = build_report(storage, artifacts, run_id)
        paths = export_report(report, ["html", "json"], report_dir(ws.root, run_id))
    except AibenchError as exc:
        raise _report_problems(exc) from exc
    except KeyboardInterrupt as exc:
        raise _interrupted_before_dispatch(run_id) from exc
    finally:
        storage.db.close()
    code = run_exit_code(run_outcome.state, report)
    data = {
        "status": "ran",
        "draft": draft,
        "run_id": run_id,
        "state": run_outcome.state.value,
        "outcome": report["outcome"],
        "gates": report["gates"],
        "reports": paths,
    }
    gates = ", ".join(f"{g['gate_id']} {g['status']}" for g in report["gates"]) or "none declared"
    raise _emit(
        data,
        json_output,
        f"run {run_id}: {run_outcome.state.value}; gates: {gates}; report: {paths['html']}",
        code,
    )
