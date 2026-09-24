"""`aibench inspect APP` (07-T1, 07-T4, 16-T1): an evidence-backed profile of what the
harness can observe, from the declared config, recorded executions (`--run`), an approved
source tree (`--source`, manifests and imports only, all findings inferred) and, when asked,
policy-checked probes (`--probe N`, the ordinary runner on N dataset cases). Without
`--probe` nothing is invoked."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from aibench.core.errors import AibenchError
from aibench.core.hashes import content_hash
from aibench.engine.compile import load_policy
from aibench.inspection.dataset_summary import summarize_dataset
from aibench.inspection.probe import ProbeRefused, probe_application, probe_denials
from aibench.inspection.profile import inspect_application
from aibench.inspection.source import inspect_source
from aibench.runners import load_application
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

console = Console()
err_console = Console(stderr=True)

_STYLE = {"observed": "green", "declared": "yellow", "inferred": "cyan", "unknown": "dim"}


def _fail(message: str, code: int = 2) -> typer.Exit:
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=code)


def _recorded(
    app_file: Path, run_ids: list[str], workspace: Path | None
) -> tuple[list[Any], list[str], list[str]]:
    """Execution attempts of the named runs that used this exact app config, the IDs of
    those runs, and notes about runs that did not."""
    if not run_ids:
        return [], [], []
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        raise _fail(f"no aibench workspace at {ws.root}")
    spec_hash = content_hash(load_application(app_file).spec.model_dump(mode="json"))
    storage = Storage(Database.open_workspace(ws))
    executions: list[Any] = []
    used: list[str] = []
    notes: list[str] = []
    try:
        for run_id in run_ids:
            record = storage.get_run(run_id)
            if record is None:
                raise _fail(f"no run committed with run_id={run_id!r}")
            if record.manifest.application_hash != spec_hash:
                notes.append(
                    f"run {run_id} used a different application config; its executions are "
                    "not evidence for this one"
                )
                continue
            executions.extend(storage.list_execution_attempts(run_id))
            used.append(run_id)
    finally:
        storage.db.close()
    return executions, used, notes


def _probe(app_file: Path, dataset: Path, policy: Any, workspace: Path | None, limit: int) -> Any:
    loaded = load_application(app_file)
    denials = probe_denials(loaded, policy)
    if denials:
        raise ProbeRefused(denials)  # before the workspace is touched
    ws = Workspace.at(workspace or Path.cwd())
    ws.ensure_directories()
    storage = Storage(Database.open_workspace(ws))
    try:
        return asyncio.run(
            probe_application(
                loaded,
                dataset,
                policy=policy,
                storage=storage,
                artifacts=ArtifactStore(ws.artifacts_dir),
                limit=limit,
            )
        )
    finally:
        storage.db.close()


def inspect(
    app_file: Path = typer.Argument(..., help="Application config file (JSON/YAML)."),  # noqa: B008
    dataset: Path | None = typer.Option(None, "--dataset", help="Also summarize a dataset."),  # noqa: B008
    run_ids: list[str] = typer.Option(  # noqa: B008
        [], "--run", help="Recorded run whose executions count as evidence (repeatable)."
    ),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    source: Path | None = typer.Option(  # noqa: B008
        None, "--source", help="Approved source tree: read manifests and imports (inferred)."
    ),
    policy: Path | None = typer.Option(  # noqa: B008
        None, "--policy", help="Policy: inspection_roots for --source; app approval for --probe."
    ),
    probe: int = typer.Option(
        0, "--probe", min=0, help="Invoke the first N --dataset cases through the runner."
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the profile JSON here."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Build an evidence-backed application profile: declared config, recorded runs, an
    approved source tree (inferred findings only) and optional policy-checked probes."""
    try:
        loaded_policy = load_policy(policy)
        executions, used, notes = _recorded(app_file, list(run_ids), workspace)
        if probe:
            if dataset is None:
                raise _fail("--probe needs --dataset: probes invoke dataset cases")
            probed = _probe(app_file, dataset, loaded_policy, workspace, probe)
            executions = [*executions, *probed.results]
            used = [*used, probed.run_id]
            notes.append(
                f"probed {len(probed.results)} case(s) in run {probed.run_id} (developer "
                "smoke: no retries, no evaluation)"
            )
        tree = inspect_source(source, policy=loaded_policy) if source is not None else None
        profile = inspect_application(
            app_file, executions=executions, run_ids=used, source_tree=tree
        )
        summary = summarize_dataset(dataset) if dataset is not None else None
    except ProbeRefused as exc:
        for denial in exc.denials:
            err_console.print(f"[red]denied:[/red] {escape(denial)}")
        raise typer.Exit(code=4) from exc
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    data = {
        "profile": json.loads(profile.model_dump_json()),
        "dataset": json.loads(summary.model_dump_json()) if summary else None,
        "notes": notes,
    }
    if out is not None:
        out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    if json_output:
        console.print_json(data=data)
        return
    console.print(
        f"[bold]{escape(profile.application_id)}[/bold] ({profile.runner}, {escape(profile.endpoint)})"
    )
    console.print(f"  scope: {escape(profile.scope)}")
    for claim in profile.claims:
        style = _STYLE.get(claim.state.value, "dim")
        detail = f" — {claim.scope}" if claim.scope else ""
        limit = f" ({claim.limitations})" if claim.limitations else ""
        console.print(
            f"  [{style}]{claim.capability}: {claim.state.value}[/{style}]{escape(detail + limit)}"
        )
    for finding in profile.source_findings:
        where = ", ".join(
            f"{e.path}:{e.line} ({e.context})" if e.line else f"{e.path} ({e.context})"
            for e in finding.evidence[:3]
        )
        console.print(
            f"  [cyan]inferred {escape(finding.capability)}[/cyan]: {escape(finding.summary)}"
            f" [dim]{escape(where)}[/dim]"
        )
        for caveat in finding.caveats[1:]:
            console.print(f"    [dim]{escape(caveat)}[/dim]")
    for gap in profile.gaps:
        console.print(f"  [yellow]gap:[/yellow] {escape(gap)}")
    if summary is not None:
        console.print(f"  dataset {escape(summary.dataset_id)}: {summary.case_count} case(s)")
        for field in summary.fields:
            console.print(
                f"    {escape(field.path)}: present={field.present} empty={field.empty} "
                f"missing={field.missing}"
            )
        for claim in summary.inferred:
            console.print(
                f"  [cyan]inferred {claim.capability}[/cyan]: {escape(claim.limitations or '')}"
            )
    for note in notes:
        err_console.print(f"[yellow]note:[/yellow] {escape(note)}")
    if out is not None:
        console.print(f"wrote {escape(str(out))}")
