"""`aibench openai-evals-api ...` — hosted OpenAI Evals API remote jobs (17-T2).

Grades a run's recorded outputs remotely. Every command needs a policy approving the
plugin environment, the API destination (`allowed_egress_origins`) and the API key's
secret reference; nothing is sent otherwise.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.engine.compile import load_policy
from aibench.security.policy import ExecutionPolicy
from aibench.services.remote_jobs import (
    DEFAULT_API_KEY,
    DEFAULT_BASE_URL,
    RemoteConfig,
    RemoteJobRefused,
    cancel_job,
    fetch_job,
    poll_job,
    resume_job,
    submit_job,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(
    help="Hosted OpenAI Evals API: grade a run's recorded outputs as a remote job "
    "(submit, status, resume, cancel, fetch)."
)
console = Console()
err_console = Console(stderr=True)

_POLICY = typer.Option(..., "--policy", help="Policy approving plugin, destination and key.")
_WORKSPACE = typer.Option(None, "--workspace", help="Project root containing .aibench/.")
_JSON = typer.Option(False, "--json", help="Machine-readable output.")


def _open(workspace: Path | None) -> tuple[Storage, ArtifactStore]:
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise error_exit(
            f"no aibench workspace at {ws.root}", exit_code=2, json_output=False, console=console, err_console=err_console
        )
    return Storage(Database.open_workspace(ws)), ArtifactStore(ws.artifacts_dir)


def _policy(path: Path) -> ExecutionPolicy:
    try:
        return load_policy(path)
    except AibenchError as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=False, console=console, err_console=err_console
        ) from exc


def _run(
    workspace: Path | None,
    action: Callable[[Storage, ArtifactStore], Coroutine[Any, Any, dict[str, Any]]],
    json_output: bool,
) -> None:
    storage, artifacts = _open(workspace)
    try:
        job = asyncio.run(action(storage, artifacts))
    except RemoteJobRefused as exc:
        raise error_exit(
            "remote job refused by policy",
            exit_code=4,
            json_output=json_output,
            console=console,
            err_console=err_console,
            details=list(exc.denials),
        ) from exc
    except (AibenchError, KeyError, ValueError) as exc:
        # A malformed worker or service reply is reported, not shown as a traceback.
        raise error_exit(
            type(exc).__name__ + ": " + str(exc),
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data=job)
        return
    remote = job.get("remote", {})
    console.print(
        f"job {escape(job['job_id'])}: [bold]{escape(job['state'])}[/bold]"
        + (f", eval {escape(remote['eval_id'])}" if remote.get("eval_id") else "")
        + (f", run {escape(remote['run_id'])} ({remote.get('run_status')})" if remote.get("run_id") else "")
    )  # fmt: skip
    if job.get("mapping"):
        console.print(f"  imported: {json.dumps(job['mapping'])}")
    last = (job.get("history") or [{}])[-1]
    if last.get("event") in ("ambiguous", "reconcile_not_found"):
        console.print(
            "  the service may have received the request: `resume` reconciles it; "
            "`resume --resend` sends it again (possible duplicate)"
        )


@app.command("submit")
def submit_command(
    run_id: str = typer.Argument(..., help="Run whose recorded outputs are graded."),
    criteria: Path = typer.Option(  # noqa: B008
        ..., "--criteria", help="JSON list of testing criteria (string_check, ...)."
    ),
    plugin_env: Path = typer.Option(  # noqa: B008
        ..., "--plugin-env", help="Python of the aibench-openai-evals-api plugin."
    ),
    policy: Path = _POLICY,
    base_url: str = typer.Option(DEFAULT_BASE_URL, "--base-url", help="Evals API base URL."),
    api_key: str = typer.Option(DEFAULT_API_KEY, "--api-key", help="Secret reference."),
    allow_duplicate: bool = typer.Option(
        False, "--allow-duplicate", help="Send even if an identical job was submitted."
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Upload recorded outputs and create the eval and run (state stored before sending)."""
    try:
        parsed = json.loads(criteria.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise error_exit(
            f"cannot read criteria: {exc}", exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    if not isinstance(parsed, list):
        raise error_exit(
            "--criteria must be a JSON list", exit_code=2, json_output=json_output, console=console, err_console=err_console
        )
    loaded = _policy(policy)
    config = RemoteConfig(plugin_env.absolute(), base_url, api_key)
    _run(
        workspace,
        lambda s, a: submit_job(
            s, a, run_id, parsed, config=config, policy=loaded, allow_duplicate=allow_duplicate
        ),
        json_output,
    )


@app.command("resume")
def resume_command(
    job_id: str = typer.Argument(...),
    policy: Path = _POLICY,
    resend: bool = typer.Option(
        False, "--resend", help="Send an unknown submission again (accepts a duplicate)."
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Reconcile an ambiguous submission, or send the job's next request."""
    loaded = _policy(policy)
    _run(
        workspace,
        lambda s, a: resume_job(s, a, job_id, policy=loaded, resend=resend),
        json_output,
    )


@app.command("status")
def status_command(
    job_id: str = typer.Argument(...),
    policy: Path = _POLICY,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Poll the remote run."""
    loaded = _policy(policy)
    _run(workspace, lambda s, a: poll_job(s, job_id, policy=loaded), json_output)


@app.command("cancel")
def cancel_command(
    job_id: str = typer.Argument(...),
    policy: Path = _POLICY,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Ask the service to cancel (best effort; graded items stay graded)."""
    loaded = _policy(policy)
    _run(workspace, lambda s, a: cancel_job(s, job_id, policy=loaded), json_output)


@app.command("fetch")
def fetch_command(
    job_id: str = typer.Argument(...),
    policy: Path = _POLICY,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Import a finished job's results into its run, once."""
    loaded = _policy(policy)
    _run(workspace, lambda s, a: fetch_job(s, a, job_id, policy=loaded), json_output)


@app.command("jobs")
def jobs_command(
    run_id: str | None = typer.Argument(None, help="Only this run's jobs."),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """List stored remote jobs (no network)."""
    storage, _ = _open(workspace)
    try:
        jobs = storage.list_remote_jobs(run_id)
    finally:
        storage.db.close()
    if json_output:
        console.print_json(data={"jobs": jobs})
        return
    for job in jobs:
        console.print(f"{escape(job['job_id'])}  {escape(job['run_id'])}  {escape(job['state'])}")
