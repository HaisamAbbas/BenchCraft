"""Consistent terminal and machine-readable CLI errors."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from aibench.cli.global_options import json_output_enabled

ERROR_CODE_SCHEMA = "aibench.cli-error/1"


def error_document(
    message: str, exit_code: int, *, details: list[str] | None = None
) -> dict[str, Any]:
    """The stable JSON shape used for expected CLI input and operation errors."""
    result: dict[str, Any] = {
        "schema": ERROR_CODE_SCHEMA,
        "status": "error",
        "message": message,
        "exit_code": exit_code,
    }
    if details:
        result["details"] = details
    return result


def error_exit(
    message: str,
    *,
    exit_code: int,
    json_output: bool,
    console: Console,
    err_console: Console,
    details: list[str] | None = None,
) -> typer.Exit:
    """Print one error in the requested format and return its CLI exit."""
    json_output = json_output or json_output_enabled()
    if json_output:
        console.print_json(data=error_document(message, exit_code, details=details))
    else:
        err_console.print(f"[red]{escape(message)}[/red]")
        for detail in details or []:
            err_console.print(f"  {escape(detail)}")
    return typer.Exit(code=exit_code)
