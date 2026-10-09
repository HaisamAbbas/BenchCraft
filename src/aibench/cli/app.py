"""`aibench app describe` / `aibench app smoke` (03-T4 developer smoke path).

`smoke` invokes selected cases once each, sequentially, and records every attempt. It is
explicitly not the benchmark scheduler (Prompt 06): no plan, retries, budgets, resume or
evaluation. Executing a local CLI application requires `--trust-local-app` (§16).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

import typer
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.core.models import BenchmarkCase, ExecutionResult, ExecutionStatus
from aibench.datasets.ingest import ingest_dataset
from aibench.engine.compile import load_policy
from aibench.runners import create_runner, load_application
from aibench.services.applications import describe_application
from aibench.services.execution import SmokeReport, run_developer_smoke
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

app = typer.Typer(help="Describe and smoke-test a configured application.")
console = Console()
err_console = Console(stderr=True)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_PREVIEW_CHARS = 120


def _safe(value: Any) -> str:
    """Application output is untrusted: strip terminal control characters and Rich
    markup before it reaches the terminal."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = _CONTROL_CHARS.sub("", text).replace("\n", " ")
    if len(text) > _PREVIEW_CHARS:
        text = text[: _PREVIEW_CHARS - 3] + "..."
    return escape(text)


def _fail(message: str, code: int = 2) -> typer.Exit:
    return error_exit(
        message, exit_code=code, json_output=False, console=console, err_console=err_console
    )


@app.command("describe")
def describe(
    app_file: Path = typer.Argument(..., help="Application config file (JSON/YAML)."),  # noqa: B008
    policy: Path | None = typer.Option(  # noqa: B008
        None, "--policy", help="Policy file, to show which test worlds it approves."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Show what the harness can honestly observe for this application, how its state is
    reset, and which test worlds it declares, without running it (an observability-gap
    report)."""
    try:
        loaded = load_application(app_file)
        data = describe_application(loaded, load_policy(policy) if policy else None)
    except AibenchError as exc:
        raise _fail(str(exc)) from exc

    if json_output:
        console.print_json(data=data)
        return
    console.print(f"[bold]{escape(data['application_id'])}[/bold] ({data['kind']})")
    console.print(f"  target: {escape(data['target'])}")
    console.print(f"  effects: {data['effects']}")
    console.print(f"  isolation: {escape(data['isolation'])}")
    console.print(f"  reset: {escape(data['reset']['summary'])}")
    console.print("  observable:")
    for capability, state in data["observable"].items():
        style = "green" if state == "observed" else ("yellow" if state == "declared" else "dim")
        console.print(f"    [{style}]{capability}: {state}[/{style}]")
    if data["missing_evidence"]:
        console.print("  missing evidence:")
        for gap in data["missing_evidence"]:
            console.print(f"    - {gap['capability']}: {escape(gap['consequence'])}")
    if data["test_worlds"]:
        console.print("  test worlds:")
        for world in data["test_worlds"]:
            approval = "approved" if world["approved"] else "not approved by this policy"
            if policy is None:
                approval = "approval: pass --policy to check"
            console.print(
                f"    - {escape(world['world_id'])}: {escape(world['description'])} ({approval})"
            )
    console.print("  limitations:")
    for note in data["limitations"]:
        console.print(f"    - {escape(note)}")


def _select(cases: list[BenchmarkCase], case_ids: list[str], limit: int) -> list[BenchmarkCase]:
    if case_ids:
        by_id = {c.case_id: c for c in cases}
        missing = [cid for cid in case_ids if cid not in by_id]
        if missing:
            raise _fail(f"case IDs not in dataset: {', '.join(missing)}")
        return [by_id[cid] for cid in case_ids]
    return cases[:limit]


def _result_row(result: ExecutionResult) -> dict[str, Any]:
    return {
        "execution_id": result.execution_id,
        "case_id": result.case_id,
        "status": result.status.value,
        "error_kind": result.error_kind.value if result.error_kind else None,
        "error": result.error,
        "effect_state": result.effect_state.value if result.effect_state else None,
        "wall_ms": result.timing.get("wall_ms"),
        "output": result.model_dump(mode="json")["output"],
        "retrieved_context": result.model_dump(mode="json")["retrieved_context"],
        "trace_refs": list(result.trace_refs),
    }


def _print_result(result: ExecutionResult) -> None:
    ok = result.status is ExecutionStatus.OK
    marker = "[green]ok[/green]" if ok else f"[red]{result.status.value}[/red]"
    effect = result.effect_state.value if result.effect_state else "-"
    line = (
        f"  {escape(result.case_id)}  {marker}  effect={effect}  "
        f"wall_ms={result.timing.get('wall_ms')}"
    )
    if ok:
        line += f"  output={_safe(result.model_dump(mode='json')['output'])}"
    else:
        kind = result.error_kind.value if result.error_kind else "error"
        line += f"  {kind}: {_safe(result.error or '')}"
    console.print(line)


@app.command("smoke")
def smoke(
    app_file: Path = typer.Argument(..., help="Application config file (JSON/YAML)."),  # noqa: B008
    dataset: Path = typer.Option(..., "--dataset", help="JSONL dataset."),  # noqa: B008
    case_ids: list[str] = typer.Option(  # noqa: B008
        [], "--case", help="Case ID to invoke (repeatable). Default: the first --limit cases."
    ),
    limit: int = typer.Option(5, "--limit", min=1, help="Cases to invoke when --case is unset."),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    trust_local_app: bool = typer.Option(
        False,
        "--trust-local-app",
        help="Allow executing a local CLI application. A subprocess is not a sandbox.",
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Developer smoke check: invoke each selected case once and record the attempts.
    Not a benchmark run: no scheduler, retries, budgets or evaluation."""
    try:
        loaded = load_application(app_file)
        runner = create_runner(loaded, trusted_local=trust_local_app)
        report = ingest_dataset(dataset)
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    if not report.is_valid or report.manifest is None:
        raise error_exit(
            "dataset is invalid; run `aibench dataset validate` for details",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
            details=[str(error) for error in report.errors],
        )
    manifest = report.manifest
    selected = _select(report.cases, case_ids, limit)

    ws = Workspace.at(workspace or Path.cwd())
    ws.ensure_directories()
    storage = Storage(Database.open_workspace(ws))
    artifacts = ArtifactStore(ws.artifacts_dir)
    if not json_output:
        console.print(
            f"[bold]developer smoke[/bold]: {len(selected)} case(s), sequential, no retries, "
            "no evaluation"
        )

    async def _run() -> SmokeReport:
        async with runner:
            health = await runner.healthcheck()
            if health.status == "unhealthy":
                raise _fail(f"healthcheck failed: {health.detail}", code=1)
            if not json_output:
                console.print(f"  healthcheck: {health.status} ({escape(health.detail)})")
            return await run_developer_smoke(
                runner,
                loaded.spec,
                manifest,
                selected,
                storage=storage,
                artifacts=artifacts,
                on_result=None if json_output else _print_result,
            )

    try:
        smoke_report = asyncio.run(_run())
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    finally:
        storage.db.close()

    failures = sum(r.status is not ExecutionStatus.OK for r in smoke_report.results)
    if json_output:
        console.print_json(
            data={
                "run_id": smoke_report.run_id,
                "status": smoke_report.status,
                "mode": "developer_smoke",
                "results": [_result_row(r) for r in smoke_report.results],
            },
            cli_exit_code=1 if failures else 0,
        )
    else:
        console.print(
            f"run {smoke_report.run_id}: {len(smoke_report.results) - failures} ok, "
            f"{failures} failed; inspect with `aibench runs show {smoke_report.run_id}`"
        )
    if failures:
        raise typer.Exit(code=1)
