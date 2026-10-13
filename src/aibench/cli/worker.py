"""Connect an application-only worker to a run coordinator."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import typer

from aibench.core.errors import AibenchError
from aibench.core.hashes import content_hash
from aibench.runners import create_runner, load_application
from aibench.services.worker_client import DistributedWorker, WorkerClientError
from aibench.tui.render import safe

app = typer.Typer(
    help="Connect application workers to a coordinator API; workers need no workspace."
)


@app.command("connect")
def connect(
    coordinator: str = typer.Option(
        ..., "--coordinator", help="Coordinator URL, for example https://bench.example:8765."
    ),
    app_config: Path = typer.Option(  # noqa: B008
        ..., "--app-config", help="Application config available on this worker."
    ),
    token_env: str = typer.Option(
        "AIBENCH_WORKER_TOKEN", "--token-env", help="Environment variable holding the API token."
    ),
    capacity: int = typer.Option(
        1, "--capacity", min=1, max=64, help="Concurrent application calls this worker can accept."
    ),
    report_runtime: bool = typer.Option(
        False,
        "--report-runtime",
        help="Report OS, architecture, Python version, and CPU count during registration.",
    ),
    ca_bundle: Path | None = typer.Option(  # noqa: B008
        None, "--ca-bundle", help="CA bundle for a private coordinator TLS certificate."
    ),
    trust_local_app: bool = typer.Option(
        False, "--trust-local-app", help="Allow this worker to run a CLI or Python application."
    ),
) -> None:
    """Poll a coordinator and execute leased app calls without opening a workspace database."""
    if not token_env or any(not (c.isascii() and (c.isalnum() or c == "_")) for c in token_env):
        raise typer.BadParameter("--token-env must be an environment variable name")
    token = os.environ.get(token_env, "")
    if not token:
        raise typer.BadParameter(f"environment variable {token_env!r} is not set")
    if (
        not token.isascii()
        or len(token) < 32
        or len(token) > 4096
        or any(ord(character) < 33 or ord(character) > 126 for character in token)
    ):
        raise typer.BadParameter("worker token must be 32 to 4096 visible ASCII characters")
    if ca_bundle is not None and not ca_bundle.expanduser().is_file():
        raise typer.BadParameter("--ca-bundle must name a readable CA certificate file")
    try:
        loaded = load_application(app_config.expanduser().resolve())
        runner = create_runner(loaded, trusted_local=trust_local_app, environ=dict(os.environ))

        def connected(worker_id: str) -> None:
            typer.echo(
                f"connected worker {worker_id[:12]} to coordinator; waiting for application calls",
                err=True,
            )

        async def run() -> None:
            try:
                worker = DistributedWorker(
                    runner,
                    coordinator_url=coordinator,
                    token=token,
                    application_hash=content_hash(loaded.spec.model_dump(mode="json")),
                    capacity=capacity,
                    ca_bundle=str(ca_bundle.expanduser().resolve()) if ca_bundle else None,
                    report_runtime=report_runtime,
                    on_connected=connected,
                )
                await runner.prepare()
                await worker.run()
            finally:
                await runner.close()

        asyncio.run(run())
    except (AibenchError, WorkerClientError, OSError) as exc:
        typer.echo(f"error: {safe(str(exc))}", err=True)
        raise typer.Exit(code=2) from exc
    except KeyboardInterrupt:
        typer.echo("worker disconnected", err=True)
        raise typer.Exit(code=130) from None
