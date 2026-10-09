"""`aibench cache list` and `aibench cache clear` (16-T3): the explicit cross-run cache.

Clearing invalidates entries only; the records they pointed at stay part of their runs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import typer

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Inspect or invalidate the opt-in execution and evaluation caches.")
console = Console()
err_console = Console(stderr=True)

_WORKSPACE = typer.Option(None, "--workspace", help="Project root containing .aibench/.")
_KIND = typer.Option(None, "--kind", help="execution or evaluation (default: both).")


def _storage(workspace: Path | None) -> Storage:
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise error_exit(
            f"no aibench workspace at {ws.root}",
            exit_code=2,
            json_output=False,
            console=console,
            err_console=err_console,
        )
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
