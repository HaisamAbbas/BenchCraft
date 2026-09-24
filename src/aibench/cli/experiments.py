"""Controlled optimization experiments (Prompt 19)."""

# Typer intentionally builds CLI parameters from `typer.Option` annotations.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.console import Console
from rich.markup import escape

from aibench.core.errors import AibenchError
from aibench.engine.compile import load_policy
from aibench.experiments.service import (
    create_experiment,
    evaluate_protected_holdout,
    execute_experiment,
    experiment_report,
    extend_trial_budget,
    prepare_experiment,
    propose_adoption,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Run controlled parameter experiments against development data.")
console = Console()
err_console = Console(stderr=True)


def _fail(message: str, code: int = 2) -> typer.Exit:
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=code)


def _open(workspace_root: Path | None) -> tuple[Database, Storage, ArtifactStore]:
    workspace = Workspace.at(workspace_root or Path.cwd())
    database = Database.open_workspace(workspace)
    return database, Storage(database), ArtifactStore(workspace.artifacts_dir)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@app.command("create")
def create(
    definition: Path = typer.Argument(..., help="YAML/JSON experiment definition."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),
    policy: Path | None = typer.Option(None, "--policy", help="Execution policy JSON/YAML."),
    trust_local_app: bool = typer.Option(False, "--trust-local-app", help="Trust a local Python callable."),
) -> None:
    """Validate and persist the finite grid; this does not execute any trial."""
    database = None
    try:
        prepared = prepare_experiment(
            definition,
            policy=load_policy(policy),
            trusted_local=trust_local_app,
        )
        database, storage, artifacts = _open(workspace)
        record = create_experiment(prepared, storage=storage, artifacts=artifacts)
        console.print_json(
            data={
                "experiment_id": record.experiment_id,
                "status": record.status.value,
                "definition_hash": record.definition_hash,
                "plan_hash": record.plan_hash,
                "development_dataset_hash": record.development_dataset_hash,
                "protected_holdout_hash": record.holdout_dataset_hash,
                "trial_count": len(storage.list_experiment_trials(record.experiment_id)),
                "trial_limit": record.trial_limit,
            }
        )
    except (AibenchError, OSError, PydanticValidationError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if database is not None:
            database.close()


@app.command("run")
def run(
    experiment_id: str = typer.Argument(...),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),
) -> None:
    """Run or resume the currently authorized development trial budget."""
    database = None
    try:
        database, storage, artifacts = _open(workspace)
        record = _run(execute_experiment(experiment_id, storage=storage, artifacts=artifacts))
        console.print_json(data=experiment_report(experiment_id, storage=storage, artifacts=artifacts))
        if record.status.value == "failed":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (AibenchError, OSError, PydanticValidationError, ValueError, RuntimeError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if database is not None:
            database.close()


@app.command("resume")
def resume(
    experiment_id: str = typer.Argument(...),
    add_trials: int = typer.Option(0, "--add-trials", min=0, help="Additional combinations after budget exhaustion."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),
) -> None:
    """Resume an interrupted run or explicitly extend an exhausted trial budget."""
    database = None
    try:
        database, storage, artifacts = _open(workspace)
        if add_trials:
            extend_trial_budget(
                experiment_id,
                additional_trials=add_trials,
                storage=storage,
                actor="cli",
            )
        record = _run(execute_experiment(experiment_id, storage=storage, artifacts=artifacts))
        console.print_json(data=experiment_report(experiment_id, storage=storage, artifacts=artifacts))
        if record.status.value == "failed":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (AibenchError, OSError, PydanticValidationError, ValueError, RuntimeError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if database is not None:
            database.close()


@app.command("status")
def status(
    experiment_id: str = typer.Argument(...),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),
) -> None:
    """Show split labels, trial lineage and whether selection is locked."""
    database = None
    try:
        database, storage, artifacts = _open(workspace)
        console.print_json(data=experiment_report(experiment_id, storage=storage, artifacts=artifacts))
    except (AibenchError, OSError, PydanticValidationError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if database is not None:
            database.close()


@app.command("report")
def report(
    experiment_id: str = typer.Argument(...),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),
) -> None:
    """Build the current development and protected-holdout report from stored facts."""
    database = None
    try:
        database, storage, artifacts = _open(workspace)
        console.print_json(data=experiment_report(experiment_id, storage=storage, artifacts=artifacts))
    except (AibenchError, OSError, PydanticValidationError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if database is not None:
            database.close()


@app.command("evaluate-holdout")
def evaluate_holdout(
    experiment_id: str = typer.Argument(...),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),
) -> None:
    """Lock development selection and perform its one-time protected evaluation."""
    database = None
    try:
        database, storage, artifacts = _open(workspace)
        record = _run(
            evaluate_protected_holdout(
                experiment_id,
                storage=storage,
                artifacts=artifacts,
            )
        )
        console.print_json(data=experiment_report(experiment_id, storage=storage, artifacts=artifacts))
        if record.status.value == "failed":
            raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (AibenchError, OSError, PydanticValidationError, ValueError, RuntimeError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if database is not None:
            database.close()


@app.command("propose-adoption")
def adoption(
    experiment_id: str = typer.Argument(...),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),
) -> None:
    """Explain the evidence and propose a review; this command never changes app files."""
    database = None
    try:
        database, storage, artifacts = _open(workspace)
        proposal = propose_adoption(experiment_id, storage=storage, artifacts=artifacts)
        console.print_json(data=proposal)
    except (AibenchError, OSError, PydanticValidationError, ValueError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        if database is not None:
            database.close()
