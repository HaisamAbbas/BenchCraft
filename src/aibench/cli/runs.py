"""`aibench runs list` / `aibench runs show RUN_ID` (02-T3)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import RunRecord, Storage

app = typer.Typer(help="Inspect committed runs.")
console = Console()
err_console = Console(stderr=True)


def _open_storage(workspace: Path | None) -> Storage:
    ws = Workspace.at(workspace or Path.cwd())
    db = Database.open_workspace(ws)
    return Storage(db)


def _run_to_dict(record: RunRecord) -> dict[str, object]:
    manifest = record.manifest.model_dump(mode="json")
    return {
        "run_id": manifest["run_id"],
        "status": record.status,
        "dataset_hash": manifest["dataset_hash"],
        "application_hash": manifest["application_hash"],
        "plan_hash": manifest["plan_hash"],
        "seed": manifest["seed"],
        "created_at": record.created_at,
        "committed_at": record.committed_at,
        "updated_at": record.updated_at,
    }


@app.command("list")
def list_runs(
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    status: str | None = typer.Option(None, "--status", help="Filter by run status."),
    limit: int = typer.Option(100, "--limit", help="Maximum runs to show."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    storage = _open_storage(workspace)
    try:
        records = storage.list_runs(status=status, limit=limit)
    finally:
        storage.db.close()

    if json_output:
        console.print_json(data=[_run_to_dict(r) for r in records])
        return

    if not records:
        console.print("[dim]No runs committed in this workspace.[/dim]")
        return
    for record in records:
        console.print(
            f"[bold]{record.manifest.run_id}[/bold]  status={record.status}  "
            f"created_at={record.created_at}"
        )


@app.command("show")
def show_run(
    run_id: str = typer.Argument(..., help="Run ID to show."),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    storage = _open_storage(workspace)
    try:
        record = storage.get_run(run_id)
    finally:
        storage.db.close()

    if record is None:
        err_console.print(f"[red]no run committed with run_id={run_id!r}[/red]")
        raise typer.Exit(code=2)

    if json_output:
        console.print_json(data=_run_to_dict(record))
        return

    console.print(f"[bold]{record.manifest.run_id}[/bold]")
    console.print(f"  status: {record.status}")
    console.print(f"  dataset_hash: {record.manifest.dataset_hash}")
    console.print(f"  application_hash: {record.manifest.application_hash}")
    console.print(f"  plan_hash: {record.manifest.plan_hash}")
    console.print(f"  created_at: {record.created_at}")
    console.print(f"  updated_at: {record.updated_at}")
