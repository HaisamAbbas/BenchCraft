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
    grace_seconds: float = typer.Option(
        DEFAULT_GC_GRACE_SECONDS,
        "--grace-seconds",
        help="Keep unreferenced files newer than this many seconds.",
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
    if apply and grace_seconds < DEFAULT_GC_GRACE_SECONDS:
        raise _fail(
            "applied garbage collection requires a grace period of at least "
            f"{DEFAULT_GC_GRACE_SECONDS:g} seconds",
            json_output=json_output,
        )

    try:
        project_root = (workspace or Path.cwd()).resolve()
    except (OSError, RuntimeError) as exc:
        raise _fail(f"could not resolve project root: {exc}", json_output=json_output) from exc
    metadata_dir = project_root / ".aibench"
    try:
        metadata = metadata_dir.lstat()
    except FileNotFoundError:
        raise _fail(f"no aibench workspace at {project_root}", json_output=json_output)
    except OSError as exc:
        raise _fail(
            f"could not inspect workspace directory: {exc}", json_output=json_output
        ) from exc
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
    ):
        raise _fail(
            "workspace .aibench directory must not be a link or reparse point",
            json_output=json_output,
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise _fail("workspace .aibench path must be a directory", json_output=json_output)

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
            grace_seconds=grace_seconds,
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
        "grace_seconds": grace_seconds,
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
