"""`aibench dataset validate PATH` (01-T4)."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from aibench.core.errors import ValidationError
from aibench.datasets.ingest import ingest_dataset

app = typer.Typer(help="Dataset validation and inspection commands.")
console = Console()
err_console = Console(stderr=True)


@app.command("validate")
def validate(
    path: Path = typer.Argument(..., help="Path to a JSONL dataset file."),  # noqa: B008
    json_output: bool = typer.Option(
        False, "--json", help="Print a machine-readable JSON summary."
    ),
) -> None:
    """Validate, normalize, and fingerprint a JSONL dataset without executing any
    application or provider call."""
    try:
        # Only counts/errors/warnings are needed here, not the full parsed dataset, so this
        # stays bounded-memory regardless of dataset size (see `ingest_dataset` docstring).
        report = ingest_dataset(path, retain_cases=False)
    except ValidationError as exc:
        err_console.print(f"[red]invalid dataset:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    case_count = report.manifest.case_count if report.manifest else 0

    if json_output:
        payload = {
            "valid": report.is_valid,
            "case_count": case_count,
            "manifest": report.manifest.model_dump(mode="json") if report.manifest else None,
            "errors": [{"line": e.line, "message": e.message} for e in report.errors],
            "errors_truncated": report.errors_truncated,
            "warnings": report.warnings,
            "warnings_truncated": report.warnings_truncated,
            "duplicate_case_ids": sorted(set(report.duplicate_case_ids)),
            "dedup_disk_backed": report.dedup_disk_backed,
        }
        console.print_json(data=payload)
    else:
        console.print(f"[bold]{path}[/bold]")
        console.print(f"  cases: {case_count}")
        if report.manifest:
            console.print(f"  content hash: {report.manifest.content_hash}")
        if report.dedup_disk_backed:
            console.print("  [dim]duplicate-ID tracking: on-disk (large file)[/dim]")
        if report.duplicate_case_ids:
            console.print(f"  [yellow]duplicate case IDs:[/yellow] {sorted(set(report.duplicate_case_ids))}")
        for warning in report.warnings:
            console.print(f"  [yellow]warning:[/yellow] {warning}")
        if report.warnings_truncated:
            console.print("  [yellow]warning list truncated[/yellow]")
        for error in report.errors:
            err_console.print(f"  [red]error:[/red] {error}")
        if report.errors_truncated:
            err_console.print("  [red]error list truncated[/red]")

    if not report.is_valid:
        raise typer.Exit(code=2)
