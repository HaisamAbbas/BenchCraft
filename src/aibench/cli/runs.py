"""`aibench runs list` / `aibench runs show RUN_ID` (02-T3)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

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
    parameters = manifest["parameters"]
    identity_basis = parameters.get("application_identity_basis")
    return {
        "run_id": manifest["run_id"],
        "status": record.status,
        "dataset_hash": manifest["dataset_hash"],
        "application_hash": manifest["application_hash"],
        "application_identity": (
            {
                "basis": identity_basis,
                "source": parameters.get("application_code_identity"),
                "environment": parameters.get("application_environment_identity"),
            }
            if identity_basis is not None
            else None
        ),
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
    identity = record.manifest.parameters.get("application_identity_basis")
    python_environment = record.manifest.parameters.get("application_environment_identity")
    if identity:
        console.print(f"  application_identity: {escape(str(identity.get('kind', 'unknown')))}")
        if identity.get("revision"):
            console.print(f"    owner revision: {escape(str(identity['revision']))}")
        if identity.get("environment_digest"):
            console.print(
                f"    owner environment digest: {escape(str(identity['environment_digest']))}"
            )
        if identity.get("image"):
            console.print(f"    pinned image: {escape(str(identity['image']))}")
        if identity.get("local_source_digest") or identity.get("digest"):
            digest = identity.get("local_source_digest") or identity["digest"]
            console.print(f"    local source digest: {escape(str(digest))}")
        if identity.get("resume_requirement"):
            console.print(
                f"    resume requirement: {escape(str(identity['resume_requirement']))}"
            )
    if python_environment:
        console.print(
            "  application_environment: "
            f"kind={escape(str(python_environment.get('kind')))} "
            f"executable={escape(str(python_environment.get('executable')))} "
            f"binary={escape(str(python_environment.get('binary')))} "
            f"runtime={escape(str(python_environment.get('runtime')))} "
            f"dependencies={escape(str(python_environment.get('dependencies')))}"
        )
    console.print(f"  plan_hash: {record.manifest.plan_hash}")
    console.print(f"  created_at: {record.created_at}")
    console.print(f"  updated_at: {record.updated_at}")
