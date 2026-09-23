"""aibench CLI entry point. Bare `aibench` in an interactive terminal opens the project's
benchmark conversation (`aibench chat`); without a terminal it prints command guidance
instead of starting an unusable chat (§13)."""

from __future__ import annotations

import typer

from aibench import __version__
from aibench.cli import app as app_cli
from aibench.cli import benchmark as benchmark_cli
from aibench.cli import chat as chat_cli
from aibench.cli import dataset as dataset_cli
from aibench.cli import inspect as inspect_cli
from aibench.cli import plan as plan_cli
from aibench.cli import project as project_cli
from aibench.cli import report as report_cli
from aibench.cli import run as run_cli
from aibench.cli import runs as runs_cli
from aibench.cli import score as score_cli
from aibench.cli import sessions as sessions_cli

app = typer.Typer(
    name="aibench",
    help="Conversational CLI for AI application benchmarking (working name: BenchCraft).",
    no_args_is_help=False,
)
app.add_typer(dataset_cli.app, name="dataset")
app.add_typer(runs_cli.app, name="runs")
app.add_typer(sessions_cli.app, name="sessions")
app.add_typer(app_cli.app, name="app")
app.add_typer(score_cli.evaluators_app, name="evaluators")
app.command("score")(score_cli.score)
app.add_typer(plan_cli.plan_app, name="plan")
app.command("inspect")(inspect_cli.inspect)
app.command("run")(run_cli.run_plan)
app.command("resume")(run_cli.resume)
app.command("evaluate")(run_cli.evaluate)
app.command("chat")(chat_cli.chat)
app.command("init")(project_cli.init)
app.command("doctor")(project_cli.doctor)
app.command("benchmark")(benchmark_cli.benchmark)
app.command("report")(report_cli.report)
app.command("compare")(project_cli.compare)
app.add_typer(project_cli.plugins_app, name="plugins")
runs_cli.app.command("status")(run_cli.status)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the aibench version and exit."),
) -> None:
    if version:
        typer.echo(f"aibench {__version__}")
        raise typer.Exit(code=0)
    if ctx.invoked_subcommand is None:
        if chat_cli.interactive_terminal():
            # `chat` is also registered as a Typer command. Invoking its undecorated
            # callback through Click would pass Typer's OptionInfo objects as defaults;
            # supply the actual no-argument values for the bare entry point.
            ctx.invoke(
                chat_cli.chat,
                project=None,
                resume=None,
                new=False,
                app=None,
                dataset=None,
                policy=None,
                trust_local_app=False,
                provider_config=None,
                send=None,
                json_output=False,
                objectives=[],
            )
            return
        typer.echo(ctx.get_help())
        typer.echo(
            "\nNo interactive terminal: `aibench` opens a conversation only in a terminal. "
            "For scripts, use the commands above or `aibench chat --send TEXT --json`."
        )
        raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
