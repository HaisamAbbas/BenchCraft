"""Commands for immutable named dataset suite versions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.datasets.suites import (
    list_dataset_suites,
    register_dataset_suite,
    show_dataset_suite,
)
from aibench.tui.render import safe as safe_terminal_text

app = typer.Typer(help="Register and inspect immutable named JSONL dataset versions.")
console = Console()
err_console = Console(stderr=True)
_WORKSPACE = typer.Option(
    None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
)
_JSON = typer.Option(False, "--json", help="Print a machine-readable result.")
_NAME = typer.Option(None, "--name", help="Limit results to one suite name.")


def _fail(message: str, *, json_output: bool) -> typer.Exit:
    return error_exit(
        message,
        exit_code=2,
        json_output=json_output,
        console=console,
        err_console=err_console,
    )


def _payload(record: Any) -> dict[str, object]:
    return {
        "name": record.suite_name,
        "version": record.suite_version,
        "reference": f"{record.suite_name}@{record.suite_version}",
        "content_hash": record.dataset_content_hash,
        "case_count": record.case_count,
        "description": record.description,
        "created_at": record.created_at,
    }


@app.command("register")
def register(
    name: str = typer.Argument(..., help="Stable lowercase suite name, e.g. support."),
    version: str = typer.Argument(..., help="Immutable version label, e.g. 1.0.0."),
    source: Path = typer.Argument(  # noqa: B008
        ..., help="JSONL dataset to snapshot into the workspace."
    ),
    description: str = typer.Option("", "--description", help="Short suite description."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Validate and snapshot a dataset as an immutable NAME@VERSION."""
    try:
        result = register_dataset_suite(
            workspace or Path.cwd(), name, version, source, description=description
        )
    except (AibenchError, OSError) as exc:
        raise _fail(str(exc), json_output=json_output) from exc

    if json_output:
        console.print_json(data=result)
        return
    action = "registered" if result["created"] else "already registered"
    suite = result["suite"]
    assert isinstance(suite, dict)
    console.print(
        f"{action} {safe_terminal_text(suite['reference'])}: "
        f"{safe_terminal_text(suite['case_count'])} cases, "
        f"{safe_terminal_text(suite['content_hash'])}"
    )
    console.print(f"  snapshot: {safe_terminal_text(result['snapshot'])}")
    if result["temporary_cleanup_warning"]:
        console.print(
            f"  warning: temporary staging directory could not be removed: "
            f"{safe_terminal_text(result['temporary_directory'])}",
            markup=False,
        )


@app.command("list")
def list_suites(
    name: str | None = _NAME,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """List registered suite names and immutable versions."""
    try:
        records = list_dataset_suites(workspace or Path.cwd(), name=name)
    except (AibenchError, OSError) as exc:
        raise _fail(str(exc), json_output=json_output) from exc

    suites = [_payload(record) for record in records]
    if json_output:
        console.print_json(data={"suites": suites, "count": len(suites)})
    elif not suites:
        console.print("No dataset suites are registered.")
    else:
        table = Table("Suite", "Cases", "Content hash", "Description")
        for suite in suites:
            table.add_row(
                safe_terminal_text(suite["reference"]),
                safe_terminal_text(suite["case_count"]),
                safe_terminal_text(suite["content_hash"]),
                safe_terminal_text(suite["description"]),
            )
        console.print(table)


@app.command("show")
def show(
    reference: str = typer.Argument(..., help="Pinned suite reference NAME@VERSION."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Show and verify the snapshot for one pinned suite version."""
    try:
        result = show_dataset_suite(workspace or Path.cwd(), reference)
    except (AibenchError, OSError) as exc:
        raise _fail(str(exc), json_output=json_output) from exc

    if json_output:
        console.print_json(data=result)
    else:
        suite = result["suite"]
        assert isinstance(suite, dict)
        console.print(
            f"{safe_terminal_text(suite['reference'])}: "
            f"{safe_terminal_text(suite['case_count'])} case(s)"
        )
        console.print(f"  content hash: {safe_terminal_text(suite['content_hash'])}")
        console.print(f"  snapshot: {safe_terminal_text(result['snapshot'])}")
        if suite["description"]:
            console.print(f"  description: {safe_terminal_text(suite['description'])}")
