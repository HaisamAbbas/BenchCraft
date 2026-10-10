"""Dataset import, validation, and inspection commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import typer

from aibench.cli import candidates as candidates_cli
from aibench.cli import episodes as episodes_cli
from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import ValidationError
from aibench.datasets.diff import (
    DEFAULT_DETAIL_LIMIT,
    MAX_DETAIL_LIMIT,
    DatasetDiffError,
    diff_datasets,
)
from aibench.datasets.importers import DatasetFormat, DatasetImportError, import_dataset
from aibench.datasets.ingest import ingest_dataset

app = typer.Typer(help="Import, validate, and inspect benchmark datasets.")
console = Console()
err_console = Console(stderr=True)
app.add_typer(candidates_cli.app, name="candidates")
app.add_typer(episodes_cli.app, name="episodes")


@app.command("diff")
def diff(
    left: Path = typer.Argument(..., help="Earlier dataset JSONL file."),  # noqa: B008
    right: Path = typer.Argument(..., help="Newer dataset JSONL file."),  # noqa: B008
    limit: str = typer.Option(
        str(DEFAULT_DETAIL_LIMIT),
        "--limit",
        help=f"Maximum case IDs shown per change category (0-{MAX_DETAIL_LIMIT}).",
    ),
    fail_on_change: bool = typer.Option(
        False, "--fail-on-change", help="Exit 1 when cases were added, removed, or changed."
    ),
    json_output: bool = typer.Option(False, "--json", help="Print a machine-readable diff."),
) -> None:
    """Compare normalized cases by unique case_id without retaining them in memory."""
    try:
        detail_limit = int(limit)
    except ValueError as exc:
        raise error_exit(
            "--limit must be an integer",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    try:
        result = diff_datasets(left, right, limit=detail_limit)
    except (DatasetDiffError, ValidationError, OSError) as exc:
        raise error_exit(
            f"dataset diff failed: {exc}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc

    has_changes = bool(result["summary"]["has_changes"])
    exit_code = 1 if fail_on_change and has_changes else 0
    if json_output:
        console.print_json(data=result, cli_exit_code=exit_code)
    else:
        summary = result["summary"]
        console.print(
            f"dataset diff: {summary['added']} added, {summary['removed']} removed, "
            f"{summary['changed']} changed, {summary['unchanged']} unchanged"
        )
        console.print(
            f"  left:  {result['left']['dataset_id']} "
            f"({result['left']['case_count']} cases, {result['left']['content_hash']})"
        )
        console.print(
            f"  right: {result['right']['dataset_id']} "
            f"({result['right']['case_count']} cases, {result['right']['content_hash']})"
        )
        for label, detail_key, omitted_key in (
            ("added", "added_case_ids", "added_omitted"),
            ("removed", "removed_case_ids", "removed_omitted"),
            ("changed", "changed_case_ids", "changed_omitted"),
        ):
            case_ids = result["details"][detail_key]
            if case_ids:
                escaped_ids = ", ".join(
                    json.dumps(case_id, ensure_ascii=True) for case_id in case_ids
                )
                console.print(f"  {label} case IDs: {escaped_ids}", markup=False)
            omitted = result["details"][omitted_key]
            if omitted:
                console.print(f"  {label}: {omitted} additional case ID(s) omitted")
    if exit_code:
        raise typer.Exit(code=exit_code)


@app.command("import")
def import_data(
    source: Path = typer.Argument(..., help="CSV, JSON array, JSONL, or Parquet source."),  # noqa: B008
    output: Path = typer.Argument(..., help="New canonical JSONL dataset path."),  # noqa: B008
    format: str = typer.Option(
        "auto", "--format", help="Source format: auto, jsonl, json, csv, or parquet."
    ),
    dataset_id: str | None = typer.Option(None, "--dataset-id", help="Manifest dataset identity."),
    split: str | None = typer.Option(None, "--split", help="Optional split label."),
    trust_parquet: bool = typer.Option(
        False,
        "--trust-parquet",
        help=(
            "Acknowledge that Parquet pages are decompressed before row-size checks; "
            "use only for trusted files."
        ),
    ),
    field_mappings: list[str] = typer.Option(  # noqa: B008
        [],
        "--map",
        help="Map a canonical case field to a source field, e.g. --map input=prompt (repeatable).",
    ),
    json_output: bool = typer.Option(False, "--json", help="Print a machine-readable summary."),
) -> None:
    """Validate records against the BenchCraft case schema and write canonical JSONL."""
    if format not in {"auto", "jsonl", "json", "csv", "parquet"}:
        raise error_exit(
            f"unsupported dataset format: {format}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    try:
        result = import_dataset(
            source,
            output,
            source_format=cast(DatasetFormat, format),
            dataset_id=dataset_id,
            split=split,
            field_mappings=tuple(field_mappings),
            trust_parquet=trust_parquet,
        )
    except (DatasetImportError, ValidationError, OSError, RecursionError) as exc:
        raise error_exit(
            f"dataset import failed: {exc}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc

    if json_output:
        console.print_json(data=result)
    else:
        console.print(
            f"imported {result['case_count']} case(s) as {result['dataset_id']} "
            f"to {result['output']}"
        )
        console.print(f"  format: {result['format']}")
        console.print(f"  content hash: {result['content_hash']}")
        if result["duplicate_case_ids"]:
            console.print(f"  [yellow]duplicate case IDs:[/yellow] {result['duplicate_case_ids']}")
        for warning in result["warnings"]:
            console.print(f"  [yellow]warning:[/yellow] {warning}")
        if result["warnings_truncated"]:
            console.print("  [yellow]warning list truncated[/yellow]")


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
        raise error_exit(
            f"invalid dataset: {exc}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc

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
        console.print_json(data=payload, cli_exit_code=0 if report.is_valid else 2)
    else:
        console.print(f"[bold]{path}[/bold]")
        console.print(f"  cases: {case_count}")
        if report.manifest:
            console.print(f"  content hash: {report.manifest.content_hash}")
        if report.dedup_disk_backed:
            console.print("  [dim]duplicate-ID tracking: on-disk (large file)[/dim]")
        if report.duplicate_case_ids:
            console.print(
                f"  [yellow]duplicate case IDs:[/yellow] {sorted(set(report.duplicate_case_ids))}"
            )
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
