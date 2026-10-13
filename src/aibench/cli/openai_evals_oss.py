"""`aibench openai-evals-oss run` — the openai/evals live completion-function bridge (17-T1).

Recorded replay needs no command of its own: bind `openai_evals_oss.<type>` metrics in a
plan or `aibench evaluate` with the plugin environment, like any other evaluator.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.engine.compile import load_policy
from aibench.runners import load_application
from aibench.services.delegated import (
    DelegatedSuiteError,
    PolicyRefused,
    default_scratch,
    run_oss_suite,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(
    help="openai/evals (open-source framework): run an allowlisted eval live against your "
    "application through a completion-function bridge."
)
console = Console()
err_console = Console(stderr=True)


@app.command("run")
def run_command(
    app_file: Path = typer.Argument(..., help="Application config file (JSON/YAML)."),  # noqa: B008
    eval_type: str = typer.Option(
        ..., "--eval", help="Allowlisted eval type: match, includes, fuzzy_match, json_match."
    ),
    samples: Path = typer.Option(..., "--samples", help="openai/evals samples JSONL."),  # noqa: B008
    plugin_env: Path = typer.Option(  # noqa: B008
        ..., "--plugin-env", help="Python interpreter of the aibench-openai-evals-oss plugin."
    ),
    policy: Path = typer.Option(  # noqa: B008
        ..., "--policy", help="Policy approving the application, plugin env and evaluator."
    ),
    params: str = typer.Option("{}", "--params", help="Eval parameters as a JSON object."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Delegated execution: the eval asks, the harness invokes the application once per
    sample and records it, then the recorded outputs are scored by replay."""
    try:
        parsed = json.loads(params)
        if not isinstance(parsed, dict):
            raise TypeError("--params must be a JSON object")
        loaded = load_application(app_file)
        loaded_policy = load_policy(policy)
    except (AibenchError, ValueError, TypeError) as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    ws = Workspace.at(workspace or Path.cwd())
    ws.ensure_directories()
    storage = Storage(Database.open_workspace(ws))
    artifacts = ArtifactStore(ws.artifacts_dir)
    try:
        report = asyncio.run(
            run_oss_suite(
                loaded=loaded,
                samples_path=samples,
                eval_type=eval_type,
                params=parsed,
                plugin_python=plugin_env,
                policy=loaded_policy,
                storage=storage,
                artifacts=artifacts,
                scratch_dir=default_scratch(ws.root),
            )
        )
    except PolicyRefused as exc:
        raise error_exit(
            "delegated evaluation refused by policy",
            exit_code=4,
            json_output=json_output,
            console=console,
            err_console=err_console,
            details=list(exc.denials),
        ) from exc
    except (DelegatedSuiteError, AibenchError) as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    finally:
        storage.db.close()
    data = report.as_dict()
    if json_output:
        console.print_json(data=data)
        return
    console.print(
        f"[bold]delegated suite[/bold] {escape(report.run_id)}: openai/evals {eval_type}, "
        f"{report.samples} sample(s), {report.executed} application call(s)"
    )
    for reason, count in data["refused"].items():
        console.print(f"  refused {count}: {escape(reason)}")
    for sample_id, why in report.unprepared.items():
        console.print(f"  not prepared {escape(sample_id)}: {escape(why)}")
    passed = sum(1 for d in report.decisions.values() if d == "pass")
    console.print(f"  replay-scored: {passed}/{len(report.decisions)} pass")
    if report.mismatches:
        console.print(f"[red]  live and replay verdicts disagree: {report.mismatches}[/red]")
