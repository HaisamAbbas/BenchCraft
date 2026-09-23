"""aibench CLI entry point. Bare `aibench` opening a chat session is scheduled for
Prompt 09; until then this exposes only the implemented scriptable commands (no
placeholder commands claiming unbuilt functionality, per the engineering contract)."""

from __future__ import annotations

import sys

import typer

from aibench import __version__
from aibench.cli import app as app_cli
from aibench.cli import dataset as dataset_cli
from aibench.cli import runs as runs_cli

app = typer.Typer(
    name="aibench",
    help="Conversational CLI for AI application benchmarking (working name: BenchCraft).",
    no_args_is_help=False,
)
app.add_typer(dataset_cli.app, name="dataset")
app.add_typer(runs_cli.app, name="runs")
app.add_typer(app_cli.app, name="app")


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the aibench version and exit."),
) -> None:
    if version:
        typer.echo(f"aibench {__version__}")
        raise typer.Exit(code=0)
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            typer.echo(
                "Interactive conversation is not implemented yet (scheduled for Prompt 09). "
                "Run `aibench --help` for available commands."
            )
        else:
            typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
