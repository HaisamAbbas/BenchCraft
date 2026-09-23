"""`aibench inspect APP` (07-T1, 07-T4): an evidence-backed profile of what the harness can
observe, from the declared config and — with `--run` — recorded executions. Nothing is
invoked; no source code is read."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from aibench.core.errors import AibenchError
from aibench.core.hashes import content_hash
from aibench.inspection.dataset_summary import summarize_dataset
from aibench.inspection.profile import inspect_application
from aibench.runners import load_application
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

console = Console()
err_console = Console(stderr=True)

_STYLE = {"observed": "green", "declared": "yellow", "inferred": "cyan", "unknown": "dim"}


def _fail(message: str, code: int = 2) -> typer.Exit:
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=code)


def _recorded(
    app_file: Path, run_ids: list[str], workspace: Path | None
) -> tuple[list[Any], list[str], list[str]]:
    """Execution attempts of the named runs that used this exact app config, the IDs of
    those runs, and notes about runs that did not."""
    if not run_ids:
        return [], [], []
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise _fail(f"no aibench workspace at {ws.root}")
    spec_hash = content_hash(load_application(app_file).spec.model_dump(mode="json"))
    storage = Storage(Database.open_workspace(ws))
    executions: list[Any] = []
    used: list[str] = []
    notes: list[str] = []
    try:
        for run_id in run_ids:
            record = storage.get_run(run_id)
            if record is None:
                raise _fail(f"no run committed with run_id={run_id!r}")
            if record.manifest.application_hash != spec_hash:
                notes.append(
                    f"run {run_id} used a different application config; its executions are "
                    "not evidence for this one"
                )
                continue
            executions.extend(storage.list_execution_attempts(run_id))
            used.append(run_id)
    finally:
        storage.db.close()
    return executions, used, notes


def inspect(
    app_file: Path = typer.Argument(..., help="Application config file (JSON/YAML)."),  # noqa: B008
    dataset: Path | None = typer.Option(None, "--dataset", help="Also summarize a dataset."),  # noqa: B008
    run_ids: list[str] = typer.Option(  # noqa: B008
        [], "--run", help="Recorded run whose executions count as evidence (repeatable)."
    ),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the profile JSON here."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Build an evidence-backed application profile (declared config and recorded runs only;
    no source code or architecture discovery)."""
    try:
        executions, used, notes = _recorded(app_file, list(run_ids), workspace)
        profile = inspect_application(app_file, executions=executions, run_ids=used)
        summary = summarize_dataset(dataset) if dataset is not None else None
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    data = {
        "profile": json.loads(profile.model_dump_json()),
        "dataset": json.loads(summary.model_dump_json()) if summary else None,
        "notes": notes,
    }
    if out is not None:
        out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if json_output:
        console.print_json(data=data)
        return
    console.print(
        f"[bold]{escape(profile.application_id)}[/bold] ({profile.runner}, {escape(profile.endpoint)})"
    )
    console.print(f"  scope: {escape(profile.scope)}")
    for claim in profile.claims:
        style = _STYLE.get(claim.state.value, "dim")
        detail = f" — {claim.scope}" if claim.scope else ""
        limit = f" ({claim.limitations})" if claim.limitations else ""
        console.print(
            f"  [{style}]{claim.capability}: {claim.state.value}[/{style}]{escape(detail + limit)}"
        )
    for gap in profile.gaps:
        console.print(f"  [yellow]gap:[/yellow] {escape(gap)}")
    if summary is not None:
        console.print(f"  dataset {escape(summary.dataset_id)}: {summary.case_count} case(s)")
        for field in summary.fields:
            console.print(
                f"    {escape(field.path)}: present={field.present} empty={field.empty} "
                f"missing={field.missing}"
            )
        for claim in summary.inferred:
            console.print(
                f"  [cyan]inferred {claim.capability}[/cyan]: {escape(claim.limitations or '')}"
            )
    for note in notes:
        err_console.print(f"[yellow]note:[/yellow] {escape(note)}")
    if out is not None:
        console.print(f"wrote {escape(str(out))}")
