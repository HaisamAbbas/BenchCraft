"""`aibench report RUN_ID` (§3, §13, 11-T1): render a run's report from stored facts.

The application, evaluators and judges are never invoked: the report is rebuilt from the
committed run records, so it can be regenerated at any time, including for a run that is
still active (labelled a partial snapshot).

By default the file is written to `.aibench/reports/RUN_ID/report.<ext>`; `--out PATH`
chooses another file and `--out -` prints to stdout. `--no-content` withholds output,
reason excerpts and per-case metric values (IDs, counts, aggregate summaries and references
remain). Exit codes: 0 the report was written;
2 the run or format is unknown.
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.reporting.render import render
from aibench.services.reports import FORMATS, build_report, report_dir, write_text_atomic
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.render import safe

console = Console(highlight=False, emoji=False)
err_console = Console(stderr=True, highlight=False, emoji=False)
EXIT_OK, EXIT_INVALID = 0, 2


def report(
    run_id: str = typer.Argument(..., help="Run to report on."),
    fmt: str = typer.Option("html", "--format", help="html | markdown | json"),
    out: str | None = typer.Option(
        None, "--out", help="File to write, or - for stdout (default: .aibench/reports/RUN_ID/)."
    ),
    no_content: bool = typer.Option(
        False, "--no-content", help="Withhold output, reason excerpts and per-case values."
    ),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print {path, run status, outcome} as JSON after writing."
    ),
) -> None:
    """Render a run's report (JSON, Markdown or HTML) without rerunning anything."""
    if fmt not in FORMATS:
        raise error_exit(
            f"unknown format {fmt}; use html, markdown or json",
            exit_code=EXIT_INVALID,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise error_exit(
            f"no aibench workspace at {ws.root}",
            exit_code=EXIT_INVALID,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    storage = Storage(Database.open_workspace(ws))
    try:
        document = build_report(
            storage, ArtifactStore(ws.artifacts_dir), run_id, include_content=not no_content
        )
    except AibenchError as exc:
        raise error_exit(
            str(exc), exit_code=EXIT_INVALID, json_output=json_output, console=console, err_console=err_console
        ) from exc
    finally:
        storage.db.close()
    text = render(document, fmt)
    if out == "-":
        sys.stdout.write(text)
        raise typer.Exit(code=EXIT_OK)
    path = Path(out) if out else report_dir(ws.root, run_id) / f"report.{FORMATS[fmt]}"
    write_text_atomic(path, text)
    summary = {
        "path": str(path),
        "run_id": run_id,
        "status": document["run"]["status"],
        "provisional": document["run"]["provisional"],
        "partial": document["run"]["partial"],
        "outcome": document["outcome"],
    }
    if json_output:
        console.print_json(data=summary)
        return
    partial = (
        " (partial snapshot: the run is not finished)"
        if summary["provisional"]
        else " (partial results)"
        if summary["partial"]
        else ""
    )
    console.print(f"wrote {safe(str(path))}{safe(partial)}")
