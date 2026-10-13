"""`aibench export RUN_ID`: machine-readable case/repetition result rows."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.services.case_export import (
    CASE_EXPORT_FORMATS,
    build_case_export,
    export_path_component,
    render_case_export,
)
from aibench.services.reports import write_text_atomic
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.render import safe

console = Console(highlight=False, emoji=False)
err_console = Console(stderr=True, highlight=False, emoji=False)


def export(
    run_id: str = typer.Argument(..., help="Run whose selected case results to export."),
    fmt: str = typer.Option("jsonl", "--format", help="jsonl | csv"),
    out: str | None = typer.Option(
        None,
        "--out",
        help="File to write, or - for stdout (default: .aibench/exports/SAFE_RUN_ID/).",
    ),
    scoring_id: str | None = typer.Option(
        None, "--scoring-id", help="Stored scoring pass (default: the report's primary pass)."
    ),
    no_content: bool = typer.Option(
        False,
        "--no-content",
        help="Withhold case data, outputs, errors, contexts, metric values and free-text reasons.",
    ),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print export metadata as JSON after writing a file."
    ),
) -> None:
    """Export every selected case/repetition from committed run records."""

    if fmt not in CASE_EXPORT_FORMATS:
        raise error_exit(
            f"unknown export format {fmt}; use csv or jsonl",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise error_exit(
            f"no aibench workspace at {ws.root}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    storage = Storage(Database.open_readonly(ws.db_path))
    try:
        document = build_case_export(
            storage,
            ArtifactStore(ws.artifacts_dir, create=False),
            run_id,
            scoring_id=scoring_id,
            include_content=not no_content,
        )
    except AibenchError as exc:
        raise error_exit(
            str(exc),
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    except (OSError, ValueError) as exc:
        raise error_exit(
            "stored case results could not be read",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    finally:
        storage.db.close()

    text = render_case_export(document, fmt)
    if out == "-":
        sys.stdout.write(text)
        raise typer.Exit(code=0)
    path = Path(out) if out else (
        ws.root
        / "exports"
        / export_path_component(run_id)
        / f"case-results.{fmt}"
    )
    try:
        write_text_atomic(path, text)
    except OSError as exc:
        raise error_exit(
            f"could not write case export: {exc}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    summary = {
        "path": str(path),
        "run_id": document["run_id"],
        "run_status": document["run_status"],
        "scoring_id": document["scoring_id"],
        "selected_item_basis": document["selected_item_basis"],
        "rows": document["row_count"],
        "format": fmt,
        "content": document["content"],
    }
    if json_output:
        console.print_json(data=summary)
        return
    console.print(f"wrote {safe(str(path))} ({summary['rows']} case/repetition row(s))")


__all__ = ["export"]
