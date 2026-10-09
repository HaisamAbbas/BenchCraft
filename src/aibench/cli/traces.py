"""`aibench traces import RUN_ID FILE` and `aibench traces show RUN_ID` (16-T2)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.core.errors import AibenchError
from aibench.observations.otel import TraceFormatError
from aibench.services.traces import import_traces, traces_summary
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Import OpenTelemetry traces into a run and inspect them.")
console = Console()
err_console = Console(stderr=True)

_WORKSPACE = typer.Option(None, "--workspace", help="Project root containing .aibench/.")
_JSON = typer.Option(False, "--json", help="Machine-readable output.")


def _open(workspace: Path | None, *, json_output: bool = False) -> tuple[Storage, ArtifactStore]:
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise error_exit(
            f"no aibench workspace at {ws.root}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    return Storage(Database.open_workspace(ws)), ArtifactStore(ws.artifacts_dir)


@app.command("import")
def import_command(
    run_id: str = typer.Argument(..., help="Run whose executions the traces belong to."),
    file: Path = typer.Argument(..., help="OTLP/JSON export (one document or JSON Lines)."),  # noqa: B008
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Attach traces to the run's executions by correlation ID. Nothing is re-executed."""
    storage, artifacts = _open(workspace, json_output=json_output)
    try:
        summary = import_traces(storage, artifacts, run_id, file)
    except (AibenchError, TraceFormatError, OSError) as exc:
        raise error_exit(
            str(exc),
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data=summary)
        return
    console.print(
        f"imported {summary['traces']} trace(s) from {escape(summary['file'])}: "
        f"{summary['matched']} matched to executions, {summary['unmatched']} unmatched, "
        f"{summary['partial']} partial"
        + (f" ({', '.join(f'{k} {v}' for k, v in summary['partial_reasons'].items())})"
           if summary["partial_reasons"] else "")
    )  # fmt: skip
    if not summary["added"]:
        console.print("  already imported; nothing added")


@app.command("show")
def show_command(
    run_id: str = typer.Argument(..., help="Run to summarize."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """What the run's imported traces add: completeness, usage (a lower bound when any
    trace is partial), tool spans."""
    storage, _ = _open(workspace, json_output=json_output)
    try:
        summary = traces_summary(storage, run_id)
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data=summary)
        return
    if summary is None:
        console.print(f"run {escape(run_id)} has no imported traces")
        return
    usage = summary["usage"]
    console.print(
        f"{summary['traces']} trace(s) from {summary['imports']} import(s): "
        f"{summary['matched_to_executions']} matched, {summary['complete']} complete, "
        f"{summary['partial']} partial"
    )
    console.print(
        f"  usage from traces: {usage['total_tokens']} tokens ({usage['bound'].replace('_', ' ')};"
        f" {usage['aggregate_spans_excluded']} aggregate span(s) excluded)"
    )
    console.print(f"  tool spans: {summary['tool_spans']} ({summary['tool_errors']} failed)")
