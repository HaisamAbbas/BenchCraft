"""`aibench report RUN_ID` (§3, §13, 11-T1): render a run's report from stored facts.

The application, evaluators and judges are never invoked: the report is rebuilt from the
committed run records, so it can be regenerated at any time, including for a run that is
still active (labelled a partial snapshot).

By default the file is written to `.aibench/reports/RUN_ID/report.<ext>`; `--out PATH`
chooses another file and `--out -` prints to stdout. Formats include JSON, Markdown, HTML,
JUnit XML and SARIF 2.1.0. `--no-content` withholds output,
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
from aibench.reporting.ci import render_ci_report
from aibench.reporting.render import render
from aibench.services.case_export import build_case_export
from aibench.services.reports import (
    CI_FORMATS,
    REPORT_FORMATS,
    ReportError,
    build_report,
    report_dir,
    write_text_atomic,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.render import safe

console = Console(highlight=False, emoji=False)
err_console = Console(stderr=True, highlight=False, emoji=False)
EXIT_OK, EXIT_INVALID = 0, 2


def report(
    run_id: str = typer.Argument(..., help="Run to report on."),
    fmt: str = typer.Option("html", "--format", help="html | markdown | json | junit | sarif"),
    out: str | None = typer.Option(
        None, "--out", help="File to write, or - for stdout (default: .aibench/reports/RUN_ID/)."
    ),
    no_content: bool = typer.Option(
        False, "--no-content", help="Withhold output, reason excerpts and per-case values."
    ),
    group_by: list[str] = typer.Option(  # noqa: B008
        [],
        "--group-by",
        help="Summarize by group_id or metadata.<field.path>; repeat; supports html/markdown/json.",
    ),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    json_output: bool = typer.Option(
        False, "--json", help="Print {path, run status, outcome} as JSON after writing."
    ),
) -> None:
    """Render a stored run report or CI artifact without rerunning anything."""
    if fmt not in REPORT_FORMATS:
        raise error_exit(
            f"unknown format {fmt}; use html, markdown, json, junit or sarif",
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
    artifacts = ArtifactStore(ws.artifacts_dir)
    try:
        if group_by and fmt in CI_FORMATS:
            raise ReportError(
                "--group-by is supported with html, markdown and json reports, not junit or sarif"
            )
        document = build_report(
            storage,
            artifacts,
            run_id,
            include_content=not no_content,
            group_by=group_by,
        )
        case_document = None
        if fmt in ("junit", "sarif"):
            case_document = build_case_export(
                storage,
                artifacts,
                run_id,
                scoring_id=document["evidence"]["scoring_id"],
                include_content=not no_content,
                report_document=document,
            )
    except AibenchError as exc:
        raise error_exit(
            str(exc),
            exit_code=EXIT_INVALID,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    finally:
        storage.db.close()
    text = (
        render_ci_report(document, case_document, fmt)
        if case_document is not None
        else render(document, fmt)
    )
    if out == "-":
        sys.stdout.write(text)
        raise typer.Exit(code=EXIT_OK)
    path = Path(out) if out else report_dir(ws.root, run_id) / f"report.{REPORT_FORMATS[fmt]}"
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
