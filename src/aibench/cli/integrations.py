"""`aibench integrations list` — external integrations and whether they can run (17-T4)."""

from __future__ import annotations

from pathlib import Path

import typer

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.engine.compile import load_policy
from aibench.services.integrations import integrations
from aibench.tui import render

app = typer.Typer(help="External integrations: modes, data destinations and availability.")
console = Console()
err_console = Console(stderr=True)


@app.command("list")
def list_command(
    policy: Path | None = typer.Option(  # noqa: B008
        None, "--policy", help="Policy to judge availability against (none: nothing approved)."
    ),
    langfuse_host: str | None = typer.Option(
        None, "--langfuse-host", help="Langfuse base URL (default: LANGFUSE_HOST)."
    ),
    openai_base_url: str = typer.Option(
        "https://api.openai.com/v1", "--openai-base-url", help="Evals API base URL to judge."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """What each integration supports, where it sends data, and why it can or cannot run.
    Starts no plugin code and contacts no service."""
    try:
        loaded = load_policy(policy) if policy else None
    except AibenchError as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    found = integrations(loaded, langfuse_host=langfuse_host, openai_base_url=openai_base_url)
    if json_output:
        console.print_json(data={"integrations": found})
        return
    render.integrations(console, {"integrations": found})
