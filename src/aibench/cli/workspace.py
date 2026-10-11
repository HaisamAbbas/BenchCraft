"""Read and maintain local workspace storage."""

from __future__ import annotations

import os
import sqlite3
import stat
import tempfile
from pathlib import Path
from typing import Any

import typer

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.core.errors import AibenchError, ValidationError
from aibench.storage.artifacts import DEFAULT_GC_GRACE_SECONDS, ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.storage.workspace_backup import (
    create_workspace_backup,
    repair_workspace_from_backup,
    restore_workspace_backup,
)
from aibench.storage.workspace_integrity import check_workspace
from aibench.storage.workspace_retention import (
    load_project_retention_policy,
    measure_workspace_usage,
    save_project_retention_policy,
)
from aibench.tui.render import safe

app = typer.Typer(help="Inspect and safely maintain workspace storage.")
console = Console()
err_console = Console(stderr=True)
_WORKSPACE = typer.Option(None, "--workspace", help="Project root containing .aibench/.")
_JSON = typer.Option(False, "--json", help="Machine-readable output on stdout.")
_MAX_PATHS = 100


def _fail(message: str, *, json_output: bool) -> typer.Exit:
    return error_exit(
        message,
        exit_code=2,
        json_output=json_output,
        console=console,
        err_console=err_console,
    )


@app.command("gc")
def garbage_collect(
    grace_seconds: float | None = typer.Option(
        None,
        "--grace-seconds",
        help="Override the workspace retention grace period for this preview or collection.",
    ),
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Delete eligible orphan artifacts. Without this option, only preview them.",
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Preview or remove stale, unreferenced content-addressed artifacts."""
    try:
        retained_grace_seconds, policy_configured, metadata, metadata_dir = (
            load_project_retention_policy(workspace or Path.cwd())
        )
    except AibenchError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    effective_grace_seconds = retained_grace_seconds if grace_seconds is None else grace_seconds
    if effective_grace_seconds < 0:
        raise _fail(
            "garbage-collection grace period must be finite and non-negative",
            json_output=json_output,
        )
    if apply and effective_grace_seconds < DEFAULT_GC_GRACE_SECONDS:
        raise _fail(
            "applied garbage collection requires a grace period of at least "
            f"{DEFAULT_GC_GRACE_SECONDS:g} seconds",
            json_output=json_output,
        )
    if apply and policy_configured and effective_grace_seconds < retained_grace_seconds:
        raise _fail(
            "applied garbage collection cannot use a grace period shorter than the "
            "workspace retention policy",
            json_output=json_output,
        )

    ws = Workspace(root=metadata_dir)
    try:
        database_metadata = _workspace_database_metadata(ws.db_path)
    except FileNotFoundError:
        raise _fail(f"no aibench workspace at {ws.root}", json_output=json_output)
    except (OSError, AibenchError) as exc:
        raise _fail(
            f"could not inspect workspace database: {exc}", json_output=json_output
        ) from exc

    try:
        with tempfile.TemporaryDirectory(prefix=".aibench-gc-", dir=ws.root) as snapshot_dir:
            snapshot_path = Path(snapshot_dir) / ws.db_path.name
            _link_workspace_database(ws.db_path, snapshot_path, database_metadata)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{ws.db_path}{suffix}")
                try:
                    sidecar_metadata = sidecar.lstat()
                except FileNotFoundError:
                    continue
                _link_workspace_database(
                    sidecar,
                    Path(f"{snapshot_path}{suffix}"),
                    sidecar_metadata,
                )

            database = Database.open_readonly(snapshot_path)
            try:
                _verify_workspace_database(snapshot_path, database_metadata)
                _verify_opened_database_path(database, snapshot_path)
                _verify_workspace_database(ws.db_path, database_metadata)
                referenced = Storage(database).referenced_artifact_digests()
                _verify_workspace_database(snapshot_path, database_metadata)
                _verify_workspace_database(ws.db_path, database_metadata)
            finally:
                database.close()
        _verify_workspace_database(ws.db_path, database_metadata)
    except (OSError, sqlite3.Error, AibenchError) as exc:
        raise _fail(
            f"could not read workspace artifact references: {exc}", json_output=json_output
        ) from exc

    try:
        report = ArtifactStore(ws.artifacts_dir, create=False).garbage_collect(
            referenced,
            grace_seconds=effective_grace_seconds,
            dry_run=not apply,
            expected_parent_metadata=metadata,
        )
    except (OSError, AibenchError) as exc:
        raise _fail(
            f"could not collect workspace artifacts: {exc}", json_output=json_output
        ) from exc

    candidates = [_workspace_relative(ws, path) for path in report.candidate_paths]
    removed = [_workspace_relative(ws, path) for path in report.removed_paths]
    payload: dict[str, Any] = {
        "mode": "applied" if apply else "dry_run",
        "grace_seconds": effective_grace_seconds,
        "retention_policy_configured": policy_configured,
        "scanned_files": report.scanned_files,
        "kept_referenced": report.kept_referenced,
        "kept_within_grace": report.kept_within_grace,
        "skipped_unsafe": report.skipped_unsafe,
        "candidate_count": len(candidates),
        "candidate_bytes": report.bytes_reclaimable,
        "candidate_paths": candidates[:_MAX_PATHS],
        "candidate_paths_truncated": len(candidates) > _MAX_PATHS,
        "removed_count": len(removed),
        "removed_bytes": report.bytes_removed,
        "removed_paths": removed[:_MAX_PATHS],
        "removed_paths_truncated": len(removed) > _MAX_PATHS,
    }
    if json_output:
        console.print_json(data=payload)
        return

    verb = "removed" if apply else "would remove"
    count = report.bytes_removed if apply else report.bytes_reclaimable
    artifact_count = len(removed) if apply else len(candidates)
    console.print(
        f"{payload['mode']}: {verb} {artifact_count} artifact(s) "
        f"({count} bytes); kept {report.kept_referenced} referenced and "
        f"{report.kept_within_grace} within grace; skipped {report.skipped_unsafe} unsafe path(s)."
    )
    for path in (removed if apply else candidates)[:_MAX_PATHS]:
        console.print(f"  {safe(path)}")
    if len(candidates) > _MAX_PATHS:
        console.print(f"  ... {len(candidates) - _MAX_PATHS} more path(s) omitted")


@app.command("usage")
def usage(
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Show read-only workspace file and byte usage by storage area."""
    try:
        result = measure_workspace_usage(workspace or Path.cwd())
    except (AibenchError, OSError, ValueError) as exc:
        raise _fail(f"could not measure workspace usage: {exc}", json_output=json_output) from exc
    if json_output:
        console.print_json(data=result)
        return
    console.print(
        f"workspace usage: {result['files']} file(s), {result['bytes']} bytes "
        f"({result['skipped_unsafe']} unsafe path(s) skipped)"
    )
    for name, values in result["categories"].items():
        console.print(f"  {safe(name)}: {values['files']} file(s), {values['bytes']} bytes")


@app.command("retention")
def retention(
    grace_seconds: float | None = typer.Option(
        None,
        "--grace-seconds",
        help="Persist the minimum age for orphan artifact collection (at least one hour).",
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Show or set the workspace's orphan artifact retention period."""
    try:
        if grace_seconds is None:
            value, configured, _, metadata_dir = load_project_retention_policy(
                workspace or Path.cwd()
            )
            result: dict[str, Any] = {
                "artifact_orphan_grace_seconds": value,
                "configured": configured,
                "source": "workspace" if configured else "default",
                "path": str(metadata_dir / "retention.json"),
            }
        else:
            result, _ = save_project_retention_policy(workspace or Path.cwd(), grace_seconds)
            result["configured"] = True
            result["source"] = "workspace"
    except (AibenchError, OSError, ValueError) as exc:
        raise _fail(
            f"could not manage workspace retention policy: {exc}", json_output=json_output
        ) from exc
    if json_output:
        console.print_json(data=result)
        return
    console.print(
        f"orphan artifact retention: {result['artifact_orphan_grace_seconds']:g} seconds "
        f"({result['source']})"
    )


@app.command("backup")
def backup(
    destination: Path,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Create a verified archive of the workspace database and durable files."""
    project_root = workspace or Path.cwd()
    try:
        result = create_workspace_backup(project_root, destination)
    except AibenchError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise _fail(f"could not create workspace backup: {exc}", json_output=json_output) from exc
    if json_output:
        console.print_json(data=result)
        return
    console.print(
        f"Backup created at {safe(result['backup'])}: {result['file_count']} file(s), "
        f"{result['artifact_count']} referenced artifact(s), {result['total_bytes']} bytes."
    )


@app.command("check")
def check(
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Check database, artifact, frozen-plan, and dataset-suite integrity."""
    try:
        result = check_workspace(workspace or Path.cwd())
    except (AibenchError, OSError, sqlite3.Error, ValueError) as exc:
        raise _fail(f"could not check workspace: {exc}", json_output=json_output) from exc
    if json_output:
        console.print_json(data=result)
        if not result["ok"]:
            raise typer.Exit(code=1)
    elif result["ok"]:
        console.print(f"workspace is healthy: {safe(result['workspace'])}")
        for name, check_result in result["checks"].items():
            if check_result["status"] == "ok":
                verified = check_result.get("verified")
                records = check_result.get("records")
                count = verified if verified is not None else records
                if count is not None:
                    console.print(f"  {name}: {count} verified")
                else:
                    console.print(f"  {name}: ok")
    else:
        console.print(
            f"workspace has {result['issue_count']} integrity issue(s): {safe(result['workspace'])}"
        )
        for issue in result["issues"]:
            console.print(
                f"  [{safe(issue['category'])}/{safe(issue['code'])}] "
                f"{safe(issue['path'])}: {safe(issue['message'])}"
            )
        if result["issues_truncated"]:
            console.print("  additional issues omitted; use --json for bounded issue details")
        raise typer.Exit(code=1)


@app.command("restore")
def restore(
    archive: Path,
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Restore a verified workspace archive without replacing existing workspace data."""
    project_root = workspace or Path.cwd()
    try:
        result = restore_workspace_backup(archive, project_root)
    except AibenchError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise _fail(f"could not restore workspace backup: {exc}", json_output=json_output) from exc
    if json_output:
        console.print_json(data=result)
        return
    console.print(
        f"Workspace restored at {safe(result['workspace'])}: {result['file_count']} file(s), "
        f"{result['artifact_count']} referenced artifact(s), {result['total_bytes']} bytes."
    )


@app.command("repair")
def repair(
    archive: Path,
    apply: bool = typer.Option(
        False,
        "--apply",
        help="Replace the workspace with the verified backup. The old workspace is retained.",
    ),
    workspace: Path | None = _WORKSPACE,
    json_output: bool = _JSON,
) -> None:
    """Recover a workspace from a verified backup; preview unless --apply is given."""
    try:
        result = repair_workspace_from_backup(archive, workspace or Path.cwd(), apply=apply)
    except AibenchError as exc:
        raise _fail(str(exc), json_output=json_output) from exc
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise _fail(f"could not repair workspace: {exc}", json_output=json_output) from exc
    if json_output:
        console.print_json(data=result)
        return
    if result["mode"] == "dry_run":
        console.print(
            f"Verified {result['backup']['file_count']} backup file(s) for "
            f"{safe(result['workspace'])}; no workspace data changed. "
            "Use --apply to recover from this backup."
        )
        return
    retained = result["retained_workspace"]
    retained_message = f" Previous workspace retained at {safe(retained)}." if retained else ""
    console.print(
        f"Workspace recovered at {safe(result['workspace'])} from "
        f"{result['backup']['file_count']} verified backup file(s).{retained_message}"
    )


def _workspace_relative(workspace: Workspace, path: Path) -> str:
    try:
        return path.relative_to(workspace.root).as_posix()
    except ValueError:
        return "(outside workspace)"


def _validate_workspace_database_file(metadata: os.stat_result) -> None:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ValidationError("workspace database must be a regular file, not a symbolic link")


def _workspace_database_metadata(path: Path) -> os.stat_result:
    metadata = path.lstat()
    _validate_workspace_database_file(metadata)
    return metadata


def _verify_workspace_database(path: Path, expected: os.stat_result) -> None:
    current = path.lstat()
    _validate_workspace_database_file(current)
    if (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValidationError("workspace database changed while reading artifact references")


def _link_workspace_database(
    source: Path,
    target: Path,
    expected: os.stat_result,
) -> None:
    source_metadata = source.lstat()
    _validate_workspace_database_file(source_metadata)
    if (source_metadata.st_dev, source_metadata.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValidationError("workspace database changed while creating a pinned snapshot")
    os.link(source, target, follow_symlinks=False)
    linked_metadata = target.lstat()
    _validate_workspace_database_file(linked_metadata)
    if (linked_metadata.st_dev, linked_metadata.st_ino) != (expected.st_dev, expected.st_ino):
        raise ValidationError("workspace database changed while creating a pinned snapshot")


def _verify_opened_database_path(database: Database, expected_path: Path) -> None:
    rows = database.connection.execute("PRAGMA database_list").fetchall()
    opened_path = next((str(row[2]) for row in rows if row[1] == "main"), "")
    if not opened_path:
        raise ValidationError("workspace database connection has no main database file")
    try:
        opened_absolute = Path(opened_path).resolve()
        expected_absolute = expected_path.resolve()
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve opened workspace database: {exc}") from exc
    if os.path.normcase(str(opened_absolute)) != os.path.normcase(str(expected_absolute)):
        raise ValidationError("workspace database path changed while it was being opened")
