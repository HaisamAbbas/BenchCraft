"""BenchCraft CLI entry point (`benchcraft`, also installed as `aibench`). Bare
`benchcraft` in an interactive terminal opens the project's benchmark conversation
(`chat`); without a terminal it prints command guidance instead of starting an unusable
chat (§13)."""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from aibench import __version__
from aibench.cli import app as app_cli
from aibench.cli import benchmark as benchmark_cli
from aibench.cli import cache as cache_cli
from aibench.cli import chat as chat_cli
from aibench.cli import connect as connect_cli
from aibench.cli import dataset as dataset_cli
from aibench.cli import experiments as experiments_cli
from aibench.cli import inspect as inspect_cli
from aibench.cli import integrations as integrations_cli
from aibench.cli import langfuse as langfuse_cli
from aibench.cli import openai_evals_api as openai_evals_api_cli
from aibench.cli import openai_evals_oss as openai_evals_oss_cli
from aibench.cli import plan as plan_cli
from aibench.cli import project as project_cli
from aibench.cli import report as report_cli
from aibench.cli import run as run_cli
from aibench.cli import runs as runs_cli
from aibench.cli import score as score_cli
from aibench.cli import sessions as sessions_cli
from aibench.cli import traces as traces_cli
from aibench.core.errors import WorkspaceTooNew

app = typer.Typer(
    name="aibench",
    help="BenchCraft: evaluate AI applications in conversation. Run `benchcraft` in a project.",
    no_args_is_help=False,
)
app.add_typer(dataset_cli.app, name="dataset")
app.add_typer(experiments_cli.app, name="experiments")
app.add_typer(runs_cli.app, name="runs")
app.add_typer(sessions_cli.app, name="sessions")
app.add_typer(traces_cli.app, name="traces")
app.add_typer(connect_cli.app, name="connect")
app.add_typer(cache_cli.app, name="cache")
app.add_typer(openai_evals_oss_cli.app, name="openai-evals-oss")
app.add_typer(openai_evals_api_cli.app, name="openai-evals-api")
app.add_typer(langfuse_cli.app, name="langfuse")
app.add_typer(integrations_cli.app, name="integrations")
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


@app.command("setup")
def setup() -> None:
    """Choose the assistant's model (saved for your user account; the key is not)."""
    from aibench import userconfig

    userconfig.run_setup(typer.echo)


def _program() -> str:
    """The name the user typed: `benchcraft` or `aibench`."""
    name = Path(sys.argv[0]).stem.lower()
    return name if name in ("benchcraft", "aibench") else "benchcraft"


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", help="Show the aibench version and exit."),
) -> None:
    if version:
        typer.echo(f"{_program()} {__version__}")
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


def run() -> None:
    """The `aibench` console entry point: the Typer app, with a workspace written by a
    newer aibench reported as a plain error (exit 2) wherever it is opened."""
    try:
        app()
    except WorkspaceTooNew as exc:
        typer.echo(f"error: {exc}", err=True)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    run()
