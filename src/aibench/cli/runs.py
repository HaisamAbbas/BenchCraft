"""`aibench runs list` / `aibench runs show RUN_ID` (02-T3)."""

from __future__ import annotations

import json
import time
from pathlib import Path

import typer
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.event_stream import (
    EventLogLock,
    event_document,
    last_logged_sequence,
    latest_stored_sequence,
)
from aibench.cli.output import Console
from aibench.core.errors import AibenchError
from aibench.services.run_catalog import (
    RunCatalogError,
    normalize_baseline_alias,
    normalize_note,
    normalize_tag,
    promote_approved_baseline,
)
from aibench.services.runs import RESUMABLE_STATES, lease_state
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import BaselinePromotion, RunBaseline, RunRecord, Storage

app = typer.Typer(help="Inspect committed runs.")
baseline_app = typer.Typer(help="Inspect and promote approved named baselines.")
app.add_typer(baseline_app, name="baseline")
console = Console()
err_console = Console(stderr=True)
MAX_SQLITE_INTEGER = (1 << 63) - 1


def _open_storage(workspace: Path | None) -> Storage:
    ws = Workspace.at(workspace or Path.cwd())
    db = Database.open_workspace(ws)
    return Storage(db)


def _run_to_dict(
    record: RunRecord, metadata: dict[str, object] | None = None
) -> dict[str, object]:
    manifest = record.manifest.model_dump(mode="json")
    parameters = manifest["parameters"]
    identity_basis = parameters.get("application_identity_basis")
    return {
        "run_id": manifest["run_id"],
        "parent_run_id": manifest["parent_run_id"],
        "status": record.status,
        "dataset_hash": manifest["dataset_hash"],
        "application_hash": manifest["application_hash"],
        "application_identity": (
            {
                "basis": identity_basis,
                "source": parameters.get("application_code_identity"),
                "environment": parameters.get("application_environment_identity"),
                "version_control": parameters.get("application_vcs_identity"),
            }
            if identity_basis is not None
            else None
        ),
        "plan_hash": manifest["plan_hash"],
        "dependency_lock_hash": manifest["dependency_lock_hash"],
        "plugin_hashes": manifest["plugin_hashes"],
        "model_identifiers": manifest["model_identifiers"],
        "benchmark_environment": manifest["environment"],
        "seed": manifest["seed"],
        "created_at": record.created_at,
        "committed_at": record.committed_at,
        "updated_at": record.updated_at,
        "tags": (metadata or {}).get("tags", []),
        "note": (metadata or {}).get("note"),
        "baselines": (metadata or {}).get("baselines", []),
    }


@app.command("list")
def list_runs(
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    status: str | None = typer.Option(None, "--status", help="Filter by run status."),
    limit: int = typer.Option(100, "--limit", help="Maximum runs to show."),
    offset: int = typer.Option(0, "--offset", help="Skip this many matching runs for pagination."),
    query: str | None = typer.Option(None, "--query", help="Search run IDs, identities, tags, notes and baseline aliases."),
    tag: str | None = typer.Option(None, "--tag", help="Filter by an exact run tag."),
    baseline: str | None = typer.Option(None, "--baseline", help="Filter by a promoted baseline alias."),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    if not 1 <= limit <= 1000 or offset < 0:
        raise error_exit(
            "--limit must be 1 to 1000 and --offset must be non-negative",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    try:
        normalized_tag = normalize_tag(tag) if tag is not None else None
        normalized_baseline = (
            normalize_baseline_alias(baseline) if baseline is not None else None
        )
    except RunCatalogError as exc:
        raise error_exit(
            str(exc),
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        ) from exc
    storage = _open_storage(workspace)
    try:
        records = storage.search_runs(
            status=status,
            query=query.strip() if query and query.strip() else None,
            tag=normalized_tag,
            baseline=normalized_baseline,
            limit=limit,
            offset=offset,
        )
        metadata = storage.list_run_metadata(record.manifest.run_id for record in records)
    finally:
        storage.db.close()

    if json_output:
        console.print_json(
            data=[_run_to_dict(record, metadata[record.manifest.run_id]) for record in records]
        )
        return

    if not records:
        console.print("[dim]No runs matched those criteria.[/dim]")
        return
    for record in records:
        details = metadata[record.manifest.run_id]
        console.print(
            f"[bold]{escape(record.manifest.run_id)}[/bold]  status={escape(record.status)}  "
            f"created_at={escape(record.created_at)}"
        )
        if details["tags"]:
            console.print(f"  tags: {escape(', '.join(details['tags']))}")
        if details["baselines"]:
            console.print(f"  baselines: {escape(', '.join(details['baselines']))}")
        if details["note"]:
            console.print(f"  note: {escape(details['note'])}")


@app.command("show")
def show_run(
    run_id: str = typer.Argument(..., help="Run ID to show."),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    storage = _open_storage(workspace)
    try:
        record = storage.get_run(run_id)
        metadata = storage.list_run_metadata([run_id])[run_id] if record else None
    finally:
        storage.db.close()

    if record is None:
        raise error_exit(
            f"no run committed with run_id={run_id!r}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )

    assert metadata is not None

    if json_output:
        console.print_json(data=_run_to_dict(record, metadata))
        return

    console.print(f"[bold]{record.manifest.run_id}[/bold]")
    console.print(f"  status: {record.status}")
    if record.manifest.parent_run_id:
        console.print(f"  parent_run_id: {escape(record.manifest.parent_run_id)}")
    console.print(f"  dataset_hash: {record.manifest.dataset_hash}")
    console.print(f"  application_hash: {record.manifest.application_hash}")
    identity = record.manifest.parameters.get("application_identity_basis")
    python_environment = record.manifest.parameters.get("application_environment_identity")
    if identity:
        console.print(f"  application_identity: {escape(str(identity.get('kind', 'unknown')))}")
        if identity.get("revision"):
            console.print(f"    owner revision: {escape(str(identity['revision']))}")
        if identity.get("environment_digest"):
            console.print(
                f"    owner environment digest: {escape(str(identity['environment_digest']))}"
            )
        if identity.get("image"):
            console.print(f"    pinned image: {escape(str(identity['image']))}")
        if identity.get("local_source_digest") or identity.get("digest"):
            digest = identity.get("local_source_digest") or identity["digest"]
            console.print(f"    local source digest: {escape(str(digest))}")
        if identity.get("resume_requirement"):
            console.print(
                f"    resume requirement: {escape(str(identity['resume_requirement']))}"
            )
    if python_environment:
        console.print(
            "  application_environment: "
            f"kind={escape(str(python_environment.get('kind')))} "
            f"executable={escape(str(python_environment.get('executable')))} "
            f"binary={escape(str(python_environment.get('binary')))} "
            f"runtime={escape(str(python_environment.get('runtime')))} "
            f"dependencies={escape(str(python_environment.get('dependencies')))}"
        )
    vcs_identity = record.manifest.parameters.get("application_vcs_identity")
    if vcs_identity:
        if vcs_identity.get("kind") == "git":
            console.print(f"  application_git_commit: {escape(str(vcs_identity['commit']))}")
            console.print(
                "  application_git_tracked_worktree: "
                f"{escape(str(vcs_identity['tracked_worktree']))}"
            )
            console.print(
                "  application_git_tracked_diff_hash: "
                f"{escape(str(vcs_identity.get('tracked_diff_hash')))}"
            )
            console.print(
                "  application_git_untracked_file_count: "
                f"{escape(str(vcs_identity.get('untracked_file_count')))}"
            )
            console.print(
                "  application_git_untracked_files_hash: "
                f"{escape(str(vcs_identity.get('untracked_files_hash')))}"
            )
        else:
            reason = vcs_identity.get("reason", "unknown")
            console.print(
                f"  application_vcs_provenance: unavailable ({escape(str(reason))})"
            )
    console.print(f"  plan_hash: {record.manifest.plan_hash}")
    if record.manifest.dependency_lock_hash is not None:
        console.print(
            f"  evaluator_dependency_lock_hash: {record.manifest.dependency_lock_hash}"
        )
    for evaluator_id, plugin_identity in (record.manifest.plugin_hashes or {}).items():
        console.print(
            f"  evaluator_plugin: {escape(str(evaluator_id))} "
            f"{escape(str(plugin_identity))}"
        )
    for evaluator_id, model_id in (record.manifest.model_identifiers or {}).items():
        console.print(
            f"  evaluator_model: {escape(str(evaluator_id))} {escape(str(model_id))}"
        )
    benchmark_environment = record.manifest.environment or {}
    if benchmark_environment:
        console.print(
            "  benchmark_environment: "
            f"python={escape(str(benchmark_environment.get('python')))} "
            f"implementation={escape(str(benchmark_environment.get('python_implementation')))} "
            f"platform={escape(str(benchmark_environment.get('platform')))} "
            f"platform_abi={escape(str(benchmark_environment.get('platform_abi')))}"
        )
    console.print(f"  run_seed: {record.manifest.seed}")
    console.print(f"  created_at: {record.created_at}")
    console.print(f"  updated_at: {record.updated_at}")
    console.print(f"  tags: {escape(', '.join(metadata['tags']) or '(none)')}")
    console.print(f"  baselines: {escape(', '.join(metadata['baselines']) or '(none)')}")
    if metadata["note"]:
        console.print(f"  note: {escape(metadata['note'])}")


@app.command("events")
def run_events(
    run_id: str = typer.Argument(..., help="Run whose durable event stream to read."),
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    after: int = typer.Option(
        0,
        "--after",
        min=0,
        max=MAX_SQLITE_INTEGER,
        help="Only emit events after this sequence.",
    ),
    follow: bool = typer.Option(
        False, "--follow", help="Continue polling while a worker owns the run."
    ),
    jsonl: bool = typer.Option(False, "--jsonl", help="Emit one versioned JSON record per line."),
    log_file: Path | None = typer.Option(  # noqa: B008
        None, "--log-file", help="Append this run's JSONL events to a file."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Suppress human event lines; JSONL output remains enabled."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", help="Include each event payload in human-readable output."
    ),
) -> None:
    """Read or follow the durable event history for a run."""
    if quiet and verbose:
        raise error_exit(
            "--quiet cannot be combined with --verbose",
            exit_code=2,
            json_output=False,
            console=console,
            err_console=err_console,
        )
    ws = Workspace.at(workspace or Path.cwd())
    storage = _open_storage(workspace)
    if storage.get_run(run_id) is None:
        storage.db.close()
        raise error_exit(
            f"no run committed with run_id={run_id!r}",
            exit_code=2,
            json_output=False,
            console=console,
            err_console=err_console,
        )
    sink = None
    sink_lock: EventLogLock | None = None
    if log_file is not None:
        target = log_file.expanduser().resolve()
        if target == ws.db_path.resolve():
            storage.db.close()
            raise error_exit(
                "--log-file cannot target the workspace database",
                exit_code=2,
                json_output=False,
                console=console,
                err_console=err_console,
            )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            sink_lock = EventLogLock(target)
            sink_lock.acquire()
            sink_cursor = last_logged_sequence(target, run_id)
            if sink_cursor > latest_stored_sequence(ws, run_id):
                raise ValueError("event log sequence is newer than the workspace run history")
            sink = target.open("a", encoding="utf-8", newline="\n")
        except (OSError, ValueError) as exc:
            if sink_lock is not None:
                sink_lock.release()
            storage.db.close()
            raise error_exit(
                f"could not open event log {target}: {exc}",
                exit_code=2,
                json_output=False,
                console=console,
                err_console=err_console,
            ) from exc
    try:
        cursor = after
        idle_resumable_polls = 0
        while True:
            events = storage.list_run_events(run_id, after=cursor)
            for event in events:
                cursor = int(event["sequence"])
                document = event_document(run_id, event)
                line = json.dumps(document, ensure_ascii=False, sort_keys=True)
                if sink is not None and cursor > sink_cursor:
                    sink.write(line + "\n")
                    sink.flush()
                    sink_cursor = cursor
                if jsonl:
                    typer.echo(line)
                elif not quiet:
                    description = f"{cursor:06d} {event['event_type']}"
                    if verbose:
                        description += " " + json.dumps(
                            event["payload"], ensure_ascii=False, sort_keys=True
                        )
                    console.print(description)
            if not follow:
                break
            record = storage.get_run(run_id)
            if record is None:
                break
            if lease_state(storage, run_id) == "live":
                idle_resumable_polls = 0
            elif record.status not in RESUMABLE_STATES:
                break
            else:
                # A detached worker claims its lease just after the launcher returns.
                idle_resumable_polls += 1
                if idle_resumable_polls >= 20:
                    break
            time.sleep(0.1)
    except KeyboardInterrupt as exc:
        raise typer.Exit(code=130) from exc
    except OSError as exc:
        raise error_exit(
            f"could not write event log: {exc}",
            exit_code=2,
            json_output=False,
            console=console,
            err_console=err_console,
        ) from exc
    finally:
        storage.db.close()
        if sink is not None:
            try:
                sink.close()
            except OSError:
                pass
        if sink_lock is not None:
            sink_lock.release()


@app.command("tag")
def add_tag(
    run_id: str = typer.Argument(..., help="Run to tag."),
    tag: str = typer.Argument(..., help="Tag label (letters, digits, '.', '_' or '-')."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    try:
        tag = normalize_tag(tag)
        storage = _open_storage(workspace)
        try:
            added = storage.add_run_tag(run_id, tag)
        finally:
            storage.db.close()
    except (AibenchError, KeyError) as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    result = {"run_id": run_id, "tag": tag, "added": added}
    if json_output:
        console.print_json(data=result)
    else:
        console.print(f"{'added' if added else 'already present'} tag {escape(tag)} on {escape(run_id)}")


@app.command("untag")
def remove_tag(
    run_id: str = typer.Argument(..., help="Run to remove a tag from."),
    tag: str = typer.Argument(..., help="Tag label to remove."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    try:
        tag = normalize_tag(tag)
        storage = _open_storage(workspace)
        try:
            removed = storage.remove_run_tag(run_id, tag)
        finally:
            storage.db.close()
    except (AibenchError, KeyError) as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    result = {"run_id": run_id, "tag": tag, "removed": removed}
    if json_output:
        console.print_json(data=result)
    else:
        console.print(f"{'removed' if removed else 'not present'} tag {escape(tag)} on {escape(run_id)}")


@app.command("note")
def set_note(
    run_id: str = typer.Argument(..., help="Run to annotate."),
    note: str | None = typer.Argument(None, help="Run note (maximum 2000 characters)."),
    clear: bool = typer.Option(False, "--clear", help="Remove the current note."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    if clear == (note is not None):
        raise error_exit(
            "provide NOTE or --clear, but not both",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    try:
        normalized_note = normalize_note(note) if note is not None else None
        storage = _open_storage(workspace)
        try:
            changed = storage.set_run_note(run_id, normalized_note)
            metadata = storage.list_run_metadata([run_id])[run_id]
        finally:
            storage.db.close()
    except (AibenchError, KeyError) as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    result = {"run_id": run_id, "note": metadata["note"], "changed": changed}
    if json_output:
        console.print_json(data=result)
    else:
        console.print("run note cleared" if clear else "run note updated")


@baseline_app.command("list")
def list_baselines(
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    storage = _open_storage(workspace)
    try:
        baselines = storage.list_baselines()
    finally:
        storage.db.close()
    payload = [_baseline_to_dict(baseline) for baseline in baselines]
    if json_output:
        console.print_json(data=payload)
    elif not payload:
        console.print("No named baselines have been promoted.")
    else:
        for baseline in baselines:
            console.print(
                f"{escape(baseline.alias)} -> {escape(baseline.run_id)} "
                f"approved_by={escape(baseline.approved_by)} promoted_at={escape(baseline.promoted_at)}"
            )


@baseline_app.command("show")
def show_baseline(
    alias: str = typer.Argument(..., help="Named baseline alias."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    try:
        alias = normalize_baseline_alias(alias)
        storage = _open_storage(workspace)
        try:
            baseline = storage.get_baseline(alias)
        finally:
            storage.db.close()
    except RunCatalogError as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    if baseline is None:
        raise error_exit(
            f"no baseline promoted with alias={alias!r}",
            exit_code=2,
            json_output=json_output,
            console=console,
            err_console=err_console,
        )
    payload = _baseline_to_dict(baseline)
    if json_output:
        console.print_json(data=payload)
    else:
        console.print(
            f"{escape(baseline.alias)} -> {escape(baseline.run_id)} "
            f"approved_by={escape(baseline.approved_by)} promoted_at={escape(baseline.promoted_at)}"
        )


@baseline_app.command("promote")
def promote_baseline(
    alias: str = typer.Argument(..., help="Alias to promote, such as production."),
    run_id: str = typer.Argument(..., help="Completed run to promote."),
    approved_by: str = typer.Option(..., "--approved-by", help="Human approving this baseline."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    ws = Workspace.at(workspace or Path.cwd())
    storage = Storage(Database.open_workspace(ws))
    try:
        result = promote_approved_baseline(
            storage,
            ArtifactStore(ws.artifacts_dir),
            alias,
            run_id,
            approved_by=approved_by,
        )
    except AibenchError as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    finally:
        storage.db.close()
    payload = {**_baseline_to_dict(result.baseline), "changed": result.changed}
    if json_output:
        console.print_json(data=payload)
    else:
        console.print(
            f"{'promoted' if result.changed else 'already current'} baseline "
            f"{escape(result.baseline.alias)} -> {escape(result.baseline.run_id)}"
        )


@baseline_app.command("history")
def baseline_history(
    alias: str = typer.Argument(..., help="Named baseline alias."),
    workspace: Path | None = typer.Option(None, "--workspace", help="Project root containing .aibench/."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    try:
        alias = normalize_baseline_alias(alias)
        storage = _open_storage(workspace)
        try:
            history = storage.list_baseline_promotions(alias)
        finally:
            storage.db.close()
    except RunCatalogError as exc:
        raise error_exit(
            str(exc), exit_code=2, json_output=json_output, console=console, err_console=err_console
        ) from exc
    payload = [_promotion_to_dict(item) for item in history]
    if json_output:
        console.print_json(data=payload)
    elif not payload:
        console.print(f"No promotion history for baseline {escape(alias)}.")
    else:
        for item in history:
            console.print(
                f"{escape(item.promoted_at)} {escape(item.alias)} -> {escape(item.run_id)} "
                f"approved_by={escape(item.approved_by)}"
            )


def _baseline_to_dict(baseline: RunBaseline) -> dict[str, str]:
    return {
        "alias": baseline.alias,
        "run_id": baseline.run_id,
        "approved_by": baseline.approved_by,
        "promoted_at": baseline.promoted_at,
    }


def _promotion_to_dict(promotion: BaselinePromotion) -> dict[str, str | None]:
    return {
        "alias": promotion.alias,
        "run_id": promotion.run_id,
        "previous_run_id": promotion.previous_run_id,
        "approved_by": promotion.approved_by,
        "promoted_at": promotion.promoted_at,
    }
