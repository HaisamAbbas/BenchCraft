"""`aibench cache list` and `aibench cache clear` (16-T3): the explicit cross-run cache.

Clearing invalidates entries only; the records they pointed at stay part of their runs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import typer
from rich.console import Console
from rich.markup import escape

from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Inspect or invalidate the opt-in execution and evaluation caches.")
console = Console()

_WORKSPACE = typer.Option(None, "--workspace", help="Project root containing .aibench/.")
_KIND = typer.Option(None, "--kind", help="execution or evaluation (default: both).")


def _storage(workspace: Path | None) -> Storage:
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        console.print(f"[red]no aibench workspace at {escape(str(ws.root))}[/red]")
        raise typer.Exit(code=2)
    return Storage(Database.open_workspace(ws))


@app.command("list")
def list_command(
    kind: Literal["execution", "evaluation"] | None = _KIND,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    storage = _storage(workspace)
    try:
        entries = storage.list_cache_entries(kind)
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data={"entries": entries})
        return
    console.print(f"{len(entries)} cache entr{'y' if len(entries) == 1 else 'ies'}")
    for e in entries:
        console.print(f"  {e['kind']} {e['cache_key'][:19]} -> run {e['run_id']}")


@app.command("clear")
def clear_command(
    kind: Literal["execution", "evaluation"] | None = _KIND,
    workspace: Path | None = _WORKSPACE,
) -> None:
    storage = _storage(workspace)
    try:
        removed = storage.clear_cache(kind)
    finally:
        storage.db.close()
    console.print(f"invalidated {removed} cache entr{'y' if removed == 1 else 'ies'}")
