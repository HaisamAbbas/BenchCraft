"""`aibench langfuse ...` — the Langfuse connector (17-T3): dataset import, trace import and
score export. Data movement only; metrics are computed by the harness's evaluators.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.connectors.langfuse import (
    CONTRACT,
    ConnectorRefused,
    LangfuseConfig,
    export_scores,
    import_dataset,
    import_traces,
)
from aibench.core.errors import AibenchError
from aibench.engine.compile import load_policy
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Langfuse: import a dataset or traces, export recorded results as scores.")
console = Console()
err_console = Console(stderr=True)

_HOST = typer.Option(..., "--host", help="Langfuse base URL, e.g. https://cloud.langfuse.com.")
_POLICY = typer.Option(..., "--policy", help="Policy approving the host and the keys.")
_PUBLIC = typer.Option("env:LANGFUSE_PUBLIC_KEY", "--public-key", help="Secret reference.")
_SECRET = typer.Option("env:LANGFUSE_SECRET_KEY", "--secret-key", help="Secret reference.")
_WORKSPACE = typer.Option(None, "--workspace", help="Project root containing .aibench/.")
_JSON = typer.Option(False, "--json", help="Machine-readable output.")


def _call(action: Callable[[], dict[str, Any]], json_output: bool) -> None:
    try:
        summary = action()
    except ConnectorRefused as exc:
        raise error_exit(
            "Langfuse operation refused by policy",
            exit_code=4,
            json_output=json_output,
            console=console,
            err_console=err_console,
            details=list(exc.denials),
        ) from exc
    except AibenchError as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    if json_output:
        console.print_json(data=summary)
        return
    for key, value in summary.items():
        console.print(f"{key}: {escape(str(value))}")


def _workspace(workspace: Path | None) -> tuple[Storage, ArtifactStore]:
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise error_exit(
            f"no aibench workspace at {ws.root}",
            exit_code=2,
            json_output=False,
            console=console,
            err_console=err_console,
        )
    return Storage(Database.open_workspace(ws)), ArtifactStore(ws.artifacts_dir)


@app.command("import-dataset")
def import_dataset_command(
    dataset: str = typer.Argument(..., help="Langfuse dataset name."),
    out: Path = typer.Option(..., "--out", help="aibench JSONL dataset to write."),  # noqa: B008
    host: str = _HOST,
    policy: Path = _POLICY,
    public_key: str = _PUBLIC,
    secret_key: str = _SECRET,
    json_output: bool = _JSON,
) -> None:
    """Write the dataset's active items as cases that keep each item's identity."""
    config = LangfuseConfig(host, public_key, secret_key)
    _call(
        lambda: import_dataset(
            config, dataset, out, policy=load_policy(policy), environ=dict(os.environ)
        ),
        json_output,
    )


@app.command("import-traces")
def import_traces_command(
    run_id: str = typer.Argument(..., help="Run whose executions' traces to import."),
    host: str = _HOST,
    policy: Path = _POLICY,
    public_key: str = _PUBLIC,
    secret_key: str = _SECRET,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Attach each execution's Langfuse trace (trace ID = its correlation ID)."""
    config = LangfuseConfig(host, public_key, secret_key)
    storage, artifacts = _workspace(workspace)
    try:
        _call(
            lambda: import_traces(
                storage, artifacts, run_id, config,
                policy=load_policy(policy), environ=dict(os.environ),
            ),
            json_output,
        )  # fmt: skip
    finally:
        storage.db.close()


@app.command("export-scores")
def export_scores_command(
    run_id: str = typer.Argument(..., help="Run whose recorded results to export."),
    host: str = _HOST,
    policy: Path = _POLICY,
    public_key: str = _PUBLIC,
    secret_key: str = _SECRET,
    include_reasons: bool = typer.Option(
        False, "--include-reasons", help="Also send evaluator reasons as score comments."
    ),
    scoring_id: str | None = typer.Option(
        None, "--scoring-id", help="Export this scoring pass (default: the latest per metric)."
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Export results as scores on the matching Langfuse traces, checked by read-back."""
    config = LangfuseConfig(host, public_key, secret_key)
    storage, _ = _workspace(workspace)
    try:
        _call(
            lambda: export_scores(
                storage, run_id, config, policy=load_policy(policy),
                environ=dict(os.environ), include_reasons=include_reasons,
                scoring_id=scoring_id,
            ),
            json_output,
        )  # fmt: skip
    finally:
        storage.db.close()


@app.command("status")
def status_command(json_output: bool = _JSON) -> None:
    """What this connector supports and how far it has been verified (no network)."""
    summary = {
        "connector": "langfuse",
        "modes": ["import-dataset", "import-traces", "export-scores"],
        "contract": CONTRACT,
        "sends": {
            "import-dataset": "credentials only",
            "import-traces": "credentials and the run's trace IDs",
            "export-scores": "metric values, metric identity, case and dataset item IDs "
            "(and reasons with --include-reasons)",
        },
        "live_verification": "not verified against a live Langfuse deployment: tested "
        "against a local stand-in only (no credentials were available)",
    }
    if json_output:
        console.print_json(data=summary)
        return
    for key, value in summary.items():
        console.print(f"{key}: {escape(str(value))}")
