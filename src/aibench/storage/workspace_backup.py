"""Verified backups and non-destructive restores for local workspaces."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import sys
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast

from aibench.core.errors import ValidationError
from aibench.core.hashes import content_hash
from aibench.core.models import ArtifactRef
from aibench.storage.artifacts import ArtifactStore, _artifact_digest_lock
from aibench.storage.db import Database, Workspace

_FORMAT = "aibench-workspace-backup"
_FORMAT_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_MAX_ENTRIES = 1_000_000
_MAX_CATALOG_ROWS = _MAX_ENTRIES
_MAX_TOTAL_BYTES = 1 << 40
_MAX_MANIFEST_BYTES = 64 * 1024 * 1024
_MAX_PATH_COMPONENTS = 250_000
_MAX_CENTRAL_DIRECTORY_BYTES = 256 * 1024 * 1024
_MAX_ARCHIVE_BYTES = _MAX_TOTAL_BYTES + _MAX_CENTRAL_DIRECTORY_BYTES + _MAX_MANIFEST_BYTES
_MAX_CATALOG_FIELD_BYTES = 256 * 1024
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_TRANSIENT_DIRS = (".aibench-gc-", ".aibench-backup-", ".suite-stage-")


class _NoArtifactCatalog(Exception):
    """Internal control flow for databases created before artifact storage."""


def create_workspace_backup(project_root: Path, destination: Path) -> dict[str, Any]:
    """Create a checksummed ZIP snapshot without changing the source workspace."""
    workspace, workspace_identity, database_identity = _existing_workspace(project_root)
    destination_path = _new_archive_path(destination)
    if _is_inside(destination_path, workspace.root):
        raise ValidationError("backup destination must be outside the .aibench workspace")

    stage_parent = destination_path.parent
    stage_name: str | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=".aibench-backup-", dir=stage_parent) as stage:
            stage_root = Path(stage)
            payload = stage_root / "workspace"
            payload.mkdir()
            _snapshot_database(
                workspace, payload / "aibench.db", workspace_identity, database_identity
            )
            artifact_count, artifact_bytes = _snapshot_referenced_artifacts(
                workspace,
                payload,
                payload / "aibench.db",
                workspace_identity,
                (payload / "aibench.db").stat().st_size,
            )
            database_bytes = (payload / "aibench.db").stat().st_size
            if (
                1 + artifact_count > _MAX_ENTRIES
                or database_bytes + artifact_bytes > _MAX_TOTAL_BYTES
            ):
                raise ValidationError("workspace is too large for the supported backup format")
            auxiliary_files, directories = _workspace_files(
                workspace,
                workspace_identity,
                initial_file_count=1 + artifact_count,
                initial_total_bytes=database_bytes + artifact_bytes,
            )
            used_bytes = database_bytes + artifact_bytes
            for relative, source, metadata, parent_identity in auxiliary_files:
                _copy_regular_file(
                    source,
                    payload / Path(*relative.parts),
                    expected_size=metadata.st_size,
                    expected_metadata=metadata,
                    expected_parent=parent_identity,
                    max_bytes=_MAX_TOTAL_BYTES - used_bytes,
                )
                used_bytes += metadata.st_size
            _verify_directories_unchanged(directories)

            files = _file_manifest(payload)
            manifest = {
                "format": _FORMAT,
                "format_version": _FORMAT_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "files": files,
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
                raise ValidationError("workspace manifest exceeds the supported backup size")
            stage_name = _write_archive(stage_root / "backup.zip", payload, manifest_bytes, files)
            with Path(stage_name).open("r+b") as handle:
                os.fsync(handle.fileno())
            # A hard link publishes atomically and fails if another process created the
            # destination after preflight. Both paths are on the same filesystem.
            os.link(stage_name, destination_path, follow_symlinks=False)
            return {
                "backup": str(destination_path),
                "file_count": len(files),
                "artifact_count": artifact_count,
                "total_bytes": sum(item["size_bytes"] for item in files),
                "format_version": _FORMAT_VERSION,
            }
    except FileExistsError as exc:
        raise ValidationError(f"backup destination already exists: {destination_path}") from exc
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise ValidationError(f"could not create workspace backup: {exc}") from exc


def restore_workspace_backup(archive_path: Path, project_root: Path) -> dict[str, Any]:
    """Restore a validated backup into an absent `.aibench/` directory."""
    archive = _regular_file(archive_path, "backup archive")
    root, root_identity = _project_root(project_root)
    target = root / ".aibench"

    try:
        with _pin_project_root(root, root_identity) as root_fd:
            target = root / ".aibench"
            try:
                target.lstat()
            except FileNotFoundError:
                pass
            else:
                raise ValidationError(f"restore destination already exists: {target}")
            with _pinned_zip_archive(archive) as zipped:
                manifest, members = _read_and_validate_archive(zipped)
                _verify_project_root(root, root_identity)
                stage_name: str | None = None
                stage_fd: int | None = None
                payload_fd: int | None = None
                database_fd: int | None = None
                database_identity: tuple[int, int] | None = None
                database_size: int | None = None
                database_sha256: str | None = None
                extraction_fds: list[int] = []
                published = False
                stage: Path | None = None
                payload: Path | None = None
                try:
                    if os.name == "nt":
                        stage = Path(tempfile.mkdtemp(prefix=".aibench-restore-", dir=root))
                        created_stage = stage.lstat()
                        if _is_link(created_stage) or not stat.S_ISDIR(created_stage.st_mode):
                            raise ValidationError("restore staging path is not a real directory")
                        stage_fd = _open_windows_directory_fd(stage, desired_access=0x10080)
                        try:
                            opened_stage = os.fstat(stage_fd)
                            current_stage = stage.lstat()
                            if (
                                _is_link(current_stage)
                                or not stat.S_ISDIR(opened_stage.st_mode)
                                or (created_stage.st_dev, created_stage.st_ino)
                                != (opened_stage.st_dev, opened_stage.st_ino)
                                or (current_stage.st_dev, current_stage.st_ino)
                                != (opened_stage.st_dev, opened_stage.st_ino)
                            ):
                                raise ValidationError(
                                    "restore staging directory changed while it was opened"
                                )
                        except BaseException:
                            os.close(stage_fd)
                            stage_fd = None
                            raise
                        payload = stage / "workspace"
                        payload.mkdir(mode=0o700)
                        payload_fd = _open_windows_directory_fd(payload, desired_access=0x10080)
                    else:
                        stage_name, stage_fd, payload_fd, payload = _create_pinned_restore_stage(
                            root_fd
                        )
                        stage = _directory_fd_path(root_fd) / stage_name
                    if stage is None or payload is None:
                        raise ValidationError("restore staging directory could not be created")
                    _extract_verified_files(
                        zipped,
                        members,
                        manifest,
                        payload,
                        payload_fd=payload_fd,
                        pinned_directories=extraction_fds,
                    )
                    database_record = next(
                        record
                        for record in manifest["files"]
                        if record["path"] == "workspace/aibench.db"
                    )
                    (
                        artifact_count,
                        database_fd,
                        database_identity,
                        database_size,
                        database_sha256,
                    ) = _relocate_and_verify_database(
                        payload,
                        target,
                        expected_size=database_record["size_bytes"],
                        expected_sha256=database_record["sha256"],
                    )
                    _verify_project_root(root, root_identity)
                    if (
                        database_identity is None
                        or database_size is None
                        or database_sha256 is None
                    ):
                        raise ValidationError("restored database could not be pinned")
                    if database_fd is None:
                        _verify_database_path_digest(
                            payload / "aibench.db",
                            database_identity,
                            database_size,
                            database_sha256,
                        )
                    else:
                        _verify_pinned_database_digest(
                            payload / "aibench.db",
                            database_fd,
                            database_identity,
                            database_size,
                            database_sha256,
                        )
                    if os.name == "nt":
                        _verify_windows_pinned_payload(stage, payload_fd)
                        for descriptor in reversed(extraction_fds):
                            os.close(descriptor)
                        extraction_fds.clear()
                    else:
                        _verify_pinned_payload(stage_fd, payload_fd)
                    _publish_directory(
                        payload,
                        target,
                        root_fd=root_fd,
                        source_dir_fd=stage_fd,
                        source_name="workspace" if stage_fd is not None else None,
                        source_fd=payload_fd,
                    )
                    published = True
                    _verify_project_root(root, root_identity)
                    _verify_database_path_digest(
                        target / "aibench.db",
                        database_identity,
                        database_size,
                        database_sha256,
                    )
                finally:
                    if database_fd is not None and not published:
                        os.close(database_fd)
                        database_fd = None
                    if (
                        database_identity is not None
                        and database_size is not None
                        and database_sha256 is not None
                    ):
                        database_path = (
                            target if published else (payload or target)
                        ) / "aibench.db"
                        if os.name == "nt":
                            with suppress(OSError, ValidationError):
                                _set_database_readonly_at_path(
                                    database_path,
                                    database_identity,
                                    database_size,
                                    database_sha256,
                                    readonly=False,
                                )
                        elif database_fd is not None and published:
                            os.chmod(database_fd, 0o600)
                    if os.name == "nt":
                        for descriptor in reversed(extraction_fds):
                            os.close(descriptor)
                        _cleanup_windows_restore_stage(
                            stage, stage_fd, payload, payload_fd, published=published
                        )
                    else:
                        for descriptor in reversed(extraction_fds):
                            os.close(descriptor)
                        if not published and payload_fd is not None:
                            _clear_directory_fd(payload_fd)
                        if payload_fd is not None:
                            os.close(payload_fd)
                        if stage_fd is not None and root_fd is not None and stage_name is not None:
                            _remove_pinned_restore_stage(root_fd, stage_fd, stage_name)
                            os.close(stage_fd)
                    if database_fd is not None:
                        os.close(database_fd)
    except ValidationError:
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile, json.JSONDecodeError, zlib.error) as exc:
        raise ValidationError(f"could not restore workspace backup: {exc}") from exc

    return {
        "workspace": str(target),
        "file_count": len(manifest["files"]),
        "artifact_count": artifact_count,
        "total_bytes": sum(item["size_bytes"] for item in manifest["files"]),
        "format_version": _FORMAT_VERSION,
    }


def _existing_workspace(
    project_root: Path,
) -> tuple[Workspace, tuple[int, int], dict[str, tuple[int, int] | None]]:
    root, root_identity = _project_root(project_root)
    _verify_project_root(root, root_identity)
    metadata = root / ".aibench"
    try:
        info = metadata.lstat()
    except FileNotFoundError as exc:
        raise ValidationError(f"no aibench workspace at {metadata}") from exc
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValidationError("workspace .aibench path must be a real directory")
    identity = (info.st_dev, info.st_ino)
    workspace = Workspace(root=metadata)
    _regular_file(workspace.db_path, "workspace database")
    database_identity: dict[str, tuple[int, int] | None] = {}
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{workspace.db_path}{suffix}")
        try:
            database_info = path.lstat()
        except FileNotFoundError:
            if suffix == "":
                raise ValidationError("workspace database disappeared during backup discovery")
            database_identity[suffix] = None
            continue
        if _is_link(database_info) or not stat.S_ISREG(database_info.st_mode):
            raise ValidationError(f"workspace SQLite file must be regular: {path.name}")
        database_identity[suffix] = (database_info.st_dev, database_info.st_ino)
    _verify_workspace_identity(workspace, identity)
    return workspace, identity, database_identity


def _project_root(path: Path) -> tuple[Path, tuple[int, int]]:
    candidate = path.expanduser().absolute()
    try:
        candidate_info = candidate.lstat()
        if _is_link(candidate_info) or not stat.S_ISDIR(candidate_info.st_mode):
            raise ValidationError("project root must be a real directory")
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve project root: {exc}") from exc
    candidate_identity = (candidate_info.st_dev, candidate_info.st_ino)
    with _pin_project_root(candidate, candidate_identity):
        try:
            root = candidate.resolve(strict=True)
            info = root.lstat()
        except (OSError, RuntimeError) as exc:
            raise ValidationError(f"could not resolve project root: {exc}") from exc
        if (
            _is_link(info)
            or not stat.S_ISDIR(info.st_mode)
            or (info.st_dev, info.st_ino) != candidate_identity
        ):
            raise ValidationError("project root changed while it was being resolved")
    return root, candidate_identity


def _new_archive_path(path: Path) -> Path:
    candidate = path.expanduser()
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"backup destination parent must exist: {exc}") from exc
    if not parent.is_dir():
        raise ValidationError("backup destination parent must be a directory")
    destination = parent / candidate.name
    if not candidate.name or candidate.name in {".", ".."}:
        raise ValidationError("backup destination must name a file")
    try:
        destination.lstat()
    except FileNotFoundError:
        return destination
    raise ValidationError(f"backup destination already exists: {destination}")


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _regular_file(path: Path, label: str) -> Path:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValidationError(f"could not inspect {label}: {exc}") from exc
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"{label} must be a regular file, not a link")
    return path


def _require_real_directory(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValidationError(f"could not inspect {label}: {exc}") from exc
    if _is_link(info) or not stat.S_ISDIR(info.st_mode):
        raise ValidationError(f"{label} must be a real directory")


def _snapshot_database(
    workspace: Workspace,
    destination: Path,
    workspace_identity: tuple[int, int],
    database_identity: dict[str, tuple[int, int] | None],
) -> None:
    _verify_workspace_identity(workspace, workspace_identity)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".aibench-backup-", dir=workspace.root) as snapshot_dir:
        source_path = workspace.db_path
        pinned_path = Path(snapshot_dir) / source_path.name
        expected = database_identity.copy()
        for suffix in ("", "-wal", "-shm"):
            _verify_workspace_identity(workspace, workspace_identity)
            live_path = Path(f"{source_path}{suffix}")
            pinned = Path(f"{pinned_path}{suffix}")
            try:
                before = live_path.lstat()
            except FileNotFoundError:
                if expected[suffix] is not None:
                    raise ValidationError("workspace SQLite files changed before backup snapshot")
                continue
            if _is_link(before) or not stat.S_ISREG(before.st_mode):
                raise ValidationError(f"workspace SQLite file must be regular: {live_path.name}")
            if expected[suffix] != (before.st_dev, before.st_ino):
                raise ValidationError("workspace SQLite files changed before backup snapshot")
            os.link(live_path, pinned, follow_symlinks=False)
            linked = pinned.lstat()
            if (linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino):
                raise ValidationError("workspace database changed while pinning its snapshot")
        _verify_sqlite_identity(source_path, expected)
        with Database.open_readonly(pinned_path) as source:
            _check_open_database_path(source, pinned_path)
            page_size_row = source.connection.execute("PRAGMA page_size").fetchone()
            page_size = page_size_row[0] if page_size_row is not None else 4096
            maximum_pages = _MAX_TOTAL_BYTES // page_size
            if maximum_pages < 1:
                raise ValidationError("workspace database exceeds the supported backup size")
            target = sqlite3.connect(destination)
            try:
                target.execute(f"PRAGMA page_size = {page_size}")
                target.execute(f"PRAGMA max_page_count = {maximum_pages}")
                try:
                    source.connection.backup(target, pages=256, sleep=0.05)
                except sqlite3.OperationalError as exc:
                    if "full" in str(exc).casefold():
                        raise ValidationError(
                            "workspace database exceeds the supported backup size"
                        ) from exc
                    raise
                target.commit()
            finally:
                target.close()
        _verify_sqlite_identity(source_path, expected)
        _verify_workspace_identity(workspace, workspace_identity)
    _regular_file(destination, "database snapshot")


def _verify_sqlite_identity(source_path: Path, expected: dict[str, tuple[int, int] | None]) -> None:
    for suffix, identity in expected.items():
        path = Path(f"{source_path}{suffix}")
        try:
            info = path.lstat()
        except FileNotFoundError as exc:
            if identity is None:
                continue
            raise ValidationError("workspace SQLite files changed during backup") from exc
        if (
            identity is None
            or _is_link(info)
            or not stat.S_ISREG(info.st_mode)
            or (info.st_dev, info.st_ino) != identity
        ):
            raise ValidationError("workspace SQLite files changed during backup")


def _check_open_database_path(database: Database, expected: Path) -> None:
    rows = database.connection.execute("PRAGMA database_list").fetchall()
    opened = next((str(row[2]) for row in rows if row[1] == "main"), "")
    if not opened:
        raise ValidationError("workspace database connection has no main database file")
    try:
        matches = os.path.normcase(str(Path(opened).resolve())) == os.path.normcase(
            str(expected.resolve())
        )
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve opened database path: {exc}") from exc
    if not matches:
        raise ValidationError("workspace database path changed while it was being opened")


def _snapshot_referenced_artifacts(
    workspace: Workspace,
    payload: Path,
    database_snapshot: Path,
    workspace_identity: tuple[int, int],
    database_bytes: int,
) -> tuple[int, int]:
    _verify_workspace_identity(workspace, workspace_identity)
    store = ArtifactStore(workspace.artifacts_dir, create=False)
    database = Database.open_readonly(database_snapshot)
    rows: Iterable[sqlite3.Row]
    try:
        try:
            rows = database.connection.execute(
                "SELECT artifact_id, digest, uri, mime_type, size_bytes, redaction, run_id, content_hash "
                "FROM artifacts ORDER BY digest, artifact_id"
            )
        except sqlite3.OperationalError as exc:
            if "no such table: artifacts" in str(exc).lower():
                rows = ()
            else:
                raise

        copied: dict[str, int] = {}
        total_bytes = 0
        catalog_rows = 0
        for row in rows:
            catalog_rows += 1
            if catalog_rows > _MAX_ENTRIES:
                raise ValidationError("workspace has too many artifact records for a backup")
            ref = ArtifactRef(
                artifact_id=row["artifact_id"],
                digest=row["digest"],
                uri=row["uri"],
                mime_type=row["mime_type"],
                size_bytes=row["size_bytes"],
                redaction=row["redaction"],
                run_id=row["run_id"],
            )
            if row["content_hash"] != content_hash(ref.model_dump_json()):
                raise ValidationError(
                    f"artifact catalog record failed its content check: {ref.artifact_id}"
                )
            source = store._path_for_digest(ref.digest)
            _require_real_directory(workspace.artifacts_dir, "artifact store")
            _require_real_directory(source.parent, "artifact shard")
            expected_uri = os.path.normcase(os.path.abspath(source))
            if os.path.normcase(os.path.abspath(ref.uri)) != expected_uri:
                raise ValidationError(f"artifact {ref.artifact_id!r} has a non-canonical URI")
            if ref.digest in copied:
                if copied[ref.digest] != ref.size_bytes:
                    raise ValidationError(
                        "workspace catalog has conflicting sizes for one artifact"
                    )
                continue
            if 1 + len(copied) >= _MAX_ENTRIES or (
                database_bytes + total_bytes + ref.size_bytes > _MAX_TOTAL_BYTES
            ):
                raise ValidationError("workspace is too large for the supported backup format")
            target = payload / "artifacts" / source.parent.name / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            _verify_workspace_identity(workspace, workspace_identity)
            with _artifact_digest_lock(workspace.artifacts_dir, ref.digest):
                _copy_regular_file(
                    source,
                    target,
                    expected_size=ref.size_bytes,
                    expected_digest=ref.digest.removeprefix("sha256:"),
                )
            copied[ref.digest] = ref.size_bytes
            total_bytes += ref.size_bytes
            _verify_workspace_identity(workspace, workspace_identity)
        _verify_workspace_identity(workspace, workspace_identity)
    finally:
        database.close()
    for suffix in ("-wal", "-shm", "-journal"):
        with suppress(FileNotFoundError):
            Path(f"{database_snapshot}{suffix}").unlink()
    return len(copied), total_bytes


def _verify_workspace_identity(workspace: Workspace, expected: tuple[int, int]) -> None:
    try:
        current = workspace.root.lstat()
    except OSError as exc:
        raise ValidationError("workspace directory changed during backup") from exc
    if (
        _is_link(current)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != expected
    ):
        raise ValidationError("workspace directory changed during backup")


def _verify_project_root(root: Path, expected: tuple[int, int]) -> None:
    try:
        current = root.lstat()
    except OSError as exc:
        raise ValidationError("project root changed during restore") from exc
    if (
        _is_link(current)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != expected
    ):
        raise ValidationError("project root changed during restore")


@contextmanager
def _pin_project_root(root: Path, expected: tuple[int, int]) -> Iterator[int | None]:
    """Keep restore staging and publication tied to the selected directory object."""
    descriptors: list[int] = []
    if os.name == "nt":
        try:
            for directory in _windows_directory_chain(root):
                access = 0x82 if directory == root else 0x80
                descriptor = _open_windows_directory_fd(directory, desired_access=access)
                descriptors.append(descriptor)
                info = os.fstat(descriptor)
                current = directory.lstat()
                if (
                    _is_link(current)
                    or not stat.S_ISDIR(info.st_mode)
                    or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino)
                ):
                    raise ValidationError("project path changed while restore was pinning it")
        except BaseException:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
            raise
    else:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptors.append(os.open(root, flags))

    try:
        descriptor = descriptors[-1]
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != expected:
            raise ValidationError("project root changed before restore could pin it")
        _verify_project_root(root, expected)
        yield descriptor
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _windows_directory_chain(root: Path) -> list[Path]:
    parts = root.parts
    current = Path(root.anchor)
    chain: list[Path] = []
    for part in parts[1:]:
        current /= part
        chain.append(current)
    return chain or [root]


def _open_windows_directory_fd(
    path: Path, *, desired_access: int = 0x80, share_mode: int = 0x03
) -> int:
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(str(path), desired_access, share_mode, None, 3, 0x02000000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"could not open directory handle: {path}")
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_windows_regular_file_fd(
    path: Path, *, share_mode: int = 0x03, desired_access: int = 0x80000180
) -> int:
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    # FILE_READ_ATTRIBUTES alone does not participate in Windows sharing checks;
    # request read access too so an open handle without FILE_SHARE_DELETE prevents
    # a concurrent unlink/rename while SQLite opens the path.
    handle = create_file(str(path), desired_access, share_mode, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"could not pin database file: {path}")
    try:
        return msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _open_pinned_database_file(path: Path, *, share_mode: int = 0x03) -> int:
    try:
        if os.name == "nt":
            descriptor = _open_windows_regular_file_fd(path, share_mode=share_mode)
        else:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise ValidationError(f"could not pin restored database: {exc}") from exc
    try:
        _verify_pinned_database_file(path, descriptor)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _verify_pinned_database_file(path: Path, descriptor: int) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise ValidationError("restored database changed while it was being opened") from exc
    pinned = os.fstat(descriptor)
    if (
        _is_link(current)
        or not stat.S_ISREG(current.st_mode)
        or not stat.S_ISREG(pinned.st_mode)
        or current.st_nlink != 1
        or pinned.st_nlink != 1
        or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise ValidationError("restored database changed while it was being opened")


def _directory_fd_path(descriptor: int | None) -> Path:
    if descriptor is None or os.name == "nt":
        raise ValidationError("directory-relative restore is unavailable on this platform")
    candidates = (Path("/proc/self/fd"), Path("/dev/fd"))
    base = next((candidate for candidate in candidates if candidate.is_dir()), None)
    if base is None:
        raise ValidationError("cannot anchor restore staging to the project directory")
    return base / str(descriptor)


def _create_pinned_restore_stage(root_fd: int | None) -> tuple[str, int, int, Path]:
    if root_fd is None:
        raise ValidationError("project directory handle is unavailable")
    directory_path = _directory_fd_path(root_fd)
    stage_name: str | None = None
    for _attempt in range(100):
        candidate = f".aibench-restore-{secrets.token_hex(8)}"
        try:
            os.mkdir(candidate, mode=0o700, dir_fd=root_fd)
        except FileExistsError:
            continue
        stage_name = candidate
        break
    if stage_name is None:
        raise OSError(errno.EEXIST, "could not allocate a restore staging directory")
    stage_fd: int | None = None
    payload_fd: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        stage_fd = os.open(stage_name, flags, dir_fd=root_fd)
        os.mkdir("workspace", mode=0o700, dir_fd=stage_fd)
        payload_fd = os.open("workspace", flags, dir_fd=stage_fd)
        payload = _directory_fd_path(payload_fd)
        return stage_name, stage_fd, payload_fd, payload
    except BaseException:
        if payload_fd is not None:
            os.close(payload_fd)
        if stage_fd is not None:
            os.close(stage_fd)
        shutil.rmtree(directory_path / stage_name, ignore_errors=True)
        raise


def _verify_pinned_payload(stage_fd: int | None, payload_fd: int | None) -> None:
    if stage_fd is None or payload_fd is None:
        raise ValidationError("restore staging directory handle is unavailable")
    try:
        current = os.stat("workspace", dir_fd=stage_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValidationError("restore staging directory changed before publication") from exc
    pinned = os.fstat(payload_fd)
    if not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (
        pinned.st_dev,
        pinned.st_ino,
    ):
        raise ValidationError("restore staging directory changed before publication")


def _verify_windows_pinned_payload(stage: Path, payload_fd: int | None) -> None:
    if payload_fd is None:
        raise ValidationError("restore staging directory handle is unavailable")
    try:
        current = (stage / "workspace").lstat()
    except FileNotFoundError as exc:
        raise ValidationError("restore staging directory changed before publication") from exc
    pinned = os.fstat(payload_fd)
    if (
        _is_link(current)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise ValidationError("restore staging directory changed before publication")


def _remove_pinned_restore_stage(root_fd: int, stage_fd: int, stage_name: str) -> None:
    try:
        stage_path = _directory_fd_path(stage_fd)
        for name in os.listdir(stage_path):
            try:
                info = os.stat(name, dir_fd=stage_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode) and not _is_link(info):
                    shutil.rmtree(name, dir_fd=stage_fd)
                else:
                    os.unlink(name, dir_fd=stage_fd)
            except FileNotFoundError:
                continue
    except OSError:
        pass
    with suppress(FileNotFoundError):
        os.rmdir(stage_name, dir_fd=root_fd)


def _cleanup_windows_restore_stage(
    stage: Path | None,
    stage_fd: int | None,
    payload: Path | None,
    payload_fd: int | None,
    *,
    published: bool,
) -> None:
    """Remove only the directories pinned by Windows handles, never a swapped path."""
    try:
        if not published and payload is not None and payload_fd is not None:
            shutil.rmtree(payload, ignore_errors=True)
            try:
                _mark_windows_directory_handle_for_delete(payload_fd)
            except OSError:
                # Leave the pinned stage behind if exact-handle deletion is unavailable.
                return
        if payload_fd is not None:
            os.close(payload_fd)
            payload_fd = None
        if stage is not None and stage_fd is not None:
            shutil.rmtree(stage, ignore_errors=True)
            try:
                if not any(stage.iterdir()):
                    _mark_windows_directory_handle_for_delete(stage_fd)
            except OSError:
                pass
    finally:
        if payload_fd is not None:
            os.close(payload_fd)
        if stage_fd is not None:
            os.close(stage_fd)


def _mark_windows_directory_handle_for_delete(descriptor: int) -> None:
    import msvcrt
    from ctypes import wintypes

    delete_file = wintypes.BOOL(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_file_information.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(descriptor)
    if not set_file_information(handle, 4, ctypes.byref(delete_file), ctypes.sizeof(delete_file)):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))


def _clear_directory_fd(descriptor: int) -> None:
    directory_path = _directory_fd_path(descriptor)
    for name in os.listdir(directory_path):
        try:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode) and not _is_link(info):
                shutil.rmtree(name, dir_fd=descriptor)
            else:
                os.unlink(name, dir_fd=descriptor)
        except FileNotFoundError:
            continue


def _workspace_files(
    workspace: Workspace,
    workspace_identity: tuple[int, int],
    *,
    initial_file_count: int,
    initial_total_bytes: int,
) -> tuple[
    list[tuple[PurePosixPath, Path, os.stat_result, tuple[Path, tuple[int, int]]]],
    list[tuple[Path, PurePosixPath, os.stat_result]],
]:
    """Enumerate auxiliary durable workspace files without following links."""
    found: list[tuple[PurePosixPath, Path, os.stat_result, tuple[Path, tuple[int, int]]]] = []
    directories: list[tuple[Path, PurePosixPath, os.stat_result]] = []
    portable_names: set[str] = set()
    file_count = initial_file_count
    total_bytes = initial_total_bytes
    scanned_entries = 0
    reserved = {
        "aibench.db": "aibench.db",
        "aibench.db-wal": "aibench.db-wal",
        "aibench.db-shm": "aibench.db-shm",
        "aibench.db-journal": "aibench.db-journal",
        "artifacts": "artifacts",
    }
    root_info = workspace.root.lstat()
    if (
        _is_link(root_info)
        or not stat.S_ISDIR(root_info.st_mode)
        or (root_info.st_dev, root_info.st_ino) != workspace_identity
    ):
        raise ValidationError("workspace root changed while creating its backup")
    stack: list[tuple[Path, PurePosixPath, os.stat_result]] = [
        (workspace.root, PurePosixPath(), root_info)
    ]
    while stack:
        directory, relative_dir, expected_directory = stack.pop()
        before = directory.lstat()
        if (
            _is_link(before)
            or not stat.S_ISDIR(before.st_mode)
            or (before.st_dev, before.st_ino)
            != (expected_directory.st_dev, expected_directory.st_ino)
        ):
            raise ValidationError(f"workspace directory changed during backup: {relative_dir}")
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned_entries += 1
                if scanned_entries > _MAX_ENTRIES:
                    raise ValidationError(
                        "workspace has too many files for the supported backup format"
                    )
                name = entry.name
                relative = relative_dir / name
                if not relative_dir.parts:
                    canonical = reserved.get(unicodedata.normalize("NFD", name).casefold())
                    if canonical is not None:
                        if name != canonical:
                            raise ValidationError(
                                "workspace has a root path that collides with reserved storage"
                            )
                        continue
                if (
                    name.startswith(_TRANSIENT_DIRS)
                    or name.startswith(".tmp-")
                    or name.casefold().endswith(".lock")
                ):
                    continue
                archive_path = "workspace/" + relative.as_posix()
                _validate_archive_path(archive_path)
                portable_name = unicodedata.normalize("NFD", archive_path).casefold()
                if portable_name in portable_names:
                    raise ValidationError(
                        "workspace has paths that collide on portable filesystems"
                    )
                portable_names.add(portable_name)
                _verify_directory_path(directory, expected_directory)
                info = Path(entry.path).lstat()
                _verify_directory_path(directory, expected_directory)
                if _is_link(info):
                    raise ValidationError(
                        f"workspace contains a link that cannot be backed up: {relative}"
                    )
                if stat.S_ISDIR(info.st_mode):
                    stack.append((Path(entry.path), relative, info))
                elif stat.S_ISREG(info.st_mode):
                    file_count += 1
                    total_bytes += info.st_size
                    if file_count > _MAX_ENTRIES or total_bytes > _MAX_TOTAL_BYTES:
                        raise ValidationError(
                            "workspace is too large for the supported backup format"
                        )
                    found.append(
                        (
                            relative,
                            Path(entry.path),
                            info,
                            (directory, (expected_directory.st_dev, expected_directory.st_ino)),
                        )
                    )
                else:
                    raise ValidationError(f"workspace contains an unsupported file: {relative}")
        after = directory.lstat()
        if _is_link(after) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise ValidationError(f"workspace directory changed during backup: {relative_dir}")
        directories.append((directory, relative_dir, expected_directory))
    if len(found) > _MAX_ENTRIES or total_bytes > _MAX_TOTAL_BYTES:
        raise ValidationError("workspace is too large for the supported backup format")
    return sorted(found, key=lambda pair: pair[0].as_posix().casefold()), directories


def _verify_directory_path(path: Path, expected: os.stat_result | tuple[int, int]) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise ValidationError(f"workspace directory changed during backup: {path}") from exc
    if isinstance(expected, os.stat_result):
        identity = (expected.st_dev, expected.st_ino)
    else:
        identity = expected
    if (
        _is_link(current)
        or not stat.S_ISDIR(current.st_mode)
        or (current.st_dev, current.st_ino) != identity
    ):
        raise ValidationError(f"workspace directory changed during backup: {path}")


def _verify_directories_unchanged(
    directories: list[tuple[Path, PurePosixPath, os.stat_result]],
) -> None:
    for path, relative, expected in directories:
        current = path.lstat()
        if (
            _is_link(current)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (expected.st_dev, expected.st_ino)
        ):
            raise ValidationError(f"workspace directory changed during backup: {relative}")


def _copy_regular_file(
    source: Path,
    destination: Path,
    *,
    expected_size: int | None = None,
    expected_digest: str | None = None,
    expected_metadata: os.stat_result | None = None,
    expected_parent: tuple[Path, tuple[int, int]] | None = None,
    max_bytes: int | None = None,
) -> None:
    if expected_parent is not None:
        _verify_directory_path(*expected_parent)
    info = source.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"backup input must be a regular file: {source}")
    if expected_size is not None and info.st_size != expected_size:
        raise ValidationError(f"artifact size mismatch for {source}")
    if expected_metadata is not None and (
        (info.st_dev, info.st_ino) != (expected_metadata.st_dev, expected_metadata.st_ino)
        or info.st_size != expected_metadata.st_size
    ):
        raise ValidationError(f"file changed since workspace inventory: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source, flags)
    digest = hashlib.sha256()
    copied = 0
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(
            opened.st_mode
        ):
            raise ValidationError(f"file changed while it was being read: {source}")
        with (
            os.fdopen(descriptor, "rb", closefd=False) as source_handle,
            destination.open("xb") as destination_handle,
        ):
            _restrict_file_permissions(destination_handle.fileno())
            while block := source_handle.read(1024 * 1024):
                copied += len(block)
                if (max_bytes is not None and copied > max_bytes) or (
                    expected_size is not None and copied > expected_size
                ):
                    raise ValidationError(f"file exceeded its backup size bound: {source}")
                digest.update(block)
                destination_handle.write(block)
            destination_handle.flush()
            os.fsync(destination_handle.fileno())
    except BaseException:
        with suppress(OSError):
            destination.unlink()
        raise
    finally:
        os.close(descriptor)
    if copied != info.st_size or (expected_size is not None and copied != expected_size):
        destination.unlink(missing_ok=True)
        raise ValidationError(f"file changed size while it was being read: {source}")
    after = source.lstat()
    if expected_parent is not None:
        _verify_directory_path(*expected_parent)
    if (
        (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino)
        or after.st_size != info.st_size
        or after.st_mtime_ns != info.st_mtime_ns
    ):
        destination.unlink(missing_ok=True)
        raise ValidationError(f"file changed while it was being read: {source}")
    if expected_digest is not None and digest.hexdigest() != expected_digest:
        destination.unlink(missing_ok=True)
        raise ValidationError(f"artifact digest mismatch for {source}")


def _file_manifest(payload: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    total = 0
    manifest_bytes = 128
    for path in sorted(payload.rglob("*"), key=lambda value: value.relative_to(payload).as_posix()):
        if not path.is_file():
            continue
        info = path.lstat()
        if _is_link(info) or not stat.S_ISREG(info.st_mode):
            raise ValidationError(f"backup staging contains an unsafe file: {path}")
        size, digest = _hash_file(path)
        total += size
        if total > _MAX_TOTAL_BYTES or len(records) >= _MAX_ENTRIES:
            raise ValidationError("workspace is too large for the supported backup format")
        record = {
            "path": "workspace/" + path.relative_to(payload).as_posix(),
            "size_bytes": size,
            "sha256": digest,
        }
        record_bytes = len(
            json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
                "utf-8"
            )
        )
        manifest_bytes += record_bytes + 1
        if manifest_bytes > _MAX_MANIFEST_BYTES:
            raise ValidationError("workspace manifest exceeds the supported backup size")
        records.append(record)
    if not any(item["path"] == "workspace/aibench.db" for item in records):
        raise ValidationError("workspace database snapshot was not created")
    _validate_portable_file_paths(item["path"] for item in records)
    return records


def _hash_file(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return size, digest.hexdigest()


def _write_archive(path: Path, payload: Path, manifest: bytes, files: list[dict[str, Any]]) -> str:
    try:
        with zipfile.ZipFile(
            path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zipped:
            zipped.writestr(_MANIFEST_NAME, manifest)
            for record in files:
                relative = PurePosixPath(record["path"]).parts[1:]
                file = payload.joinpath(*relative)
                zipped.write(file, record["path"])
        if os.name != "nt":
            path.chmod(0o600)
    except OSError:
        path.unlink(missing_ok=True)
        raise
    return str(path)


@contextmanager
def _pinned_zip_archive(
    path: Path,
    *,
    max_archive_bytes: int = _MAX_ARCHIVE_BYTES,
    max_entries: int = _MAX_ENTRIES + 1,
    max_central_directory_bytes: int = _MAX_CENTRAL_DIRECTORY_BYTES,
) -> Iterator[zipfile.ZipFile]:
    """Parse a bounded private snapshot so ZIP metadata cannot change after preflight."""
    descriptor: int | None = None
    try:
        if os.name == "nt":
            descriptor = _open_windows_regular_file_fd(path, share_mode=0x01)
        else:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        pinned = os.fstat(descriptor)
        current = path.lstat()
        if (
            _is_link(current)
            or not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(pinned.st_mode)
            or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
        ):
            raise ValidationError("backup archive changed while it was being opened")
        if pinned.st_size > max_archive_bytes:
            raise ValidationError("backup archive has an unsupported size")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            with tempfile.TemporaryFile(mode="w+b", prefix=".aibench-restore-") as snapshot:
                remaining = pinned.st_size
                while remaining:
                    block = source.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValidationError("backup archive changed while it was being copied")
                    snapshot.write(block)
                    remaining -= len(block)
                after = os.fstat(source.fileno())
                current = path.lstat()
                if (
                    (after.st_dev, after.st_ino) != (pinned.st_dev, pinned.st_ino)
                    or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                    != (pinned.st_size, pinned.st_mtime_ns, pinned.st_ctime_ns)
                    or _is_link(current)
                    or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
                ):
                    raise ValidationError("backup archive changed while it was being copied")
                snapshot.flush()
                snapshot.seek(0)
                _preflight_zip_archive(
                    cast(BinaryIO, snapshot),
                    max_archive_bytes=max_archive_bytes,
                    max_entries=max_entries,
                    max_central_directory_bytes=max_central_directory_bytes,
                )
                with zipfile.ZipFile(snapshot, "r") as zipped:
                    yield zipped
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError(f"could not open backup archive: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _preflight_zip_archive(
    archive: BinaryIO,
    *,
    max_archive_bytes: int = _MAX_ARCHIVE_BYTES,
    max_entries: int = _MAX_ENTRIES + 1,
    max_central_directory_bytes: int = _MAX_CENTRAL_DIRECTORY_BYTES,
) -> None:
    """Bound ZIP metadata before ZipFile materializes its central directory."""
    import struct

    size = os.fstat(archive.fileno()).st_size
    if size < 22 or size > max_archive_bytes:
        raise ValidationError("backup archive has an unsupported size")
    tail_size = min(size, 22 + 65_535)
    archive.seek(size - tail_size)
    tail = archive.read(tail_size)
    signature = b"PK\x05\x06"
    eocd_index = tail.rfind(signature)
    while eocd_index >= 0:
        if eocd_index + 22 <= len(tail):
            comment_length = struct.unpack_from("<H", tail, eocd_index + 20)[0]
            if eocd_index + 22 + comment_length == len(tail):
                break
        eocd_index = tail.rfind(signature, 0, eocd_index)
    if eocd_index < 0:
        raise ValidationError("backup archive has no valid ZIP end record")

    eocd_offset = size - tail_size + eocd_index
    (
        _signature,
        disk_number,
        central_disk,
        disk_entries,
        total_entries,
        central_size,
        central_offset,
        _comment_length,
    ) = struct.unpack_from("<4s4H2LH", tail, eocd_index)
    if disk_number != 0 or central_disk != 0 or disk_entries != total_entries:
        raise ValidationError("multi-disk backup archives are not supported")

    if total_entries == 0xFFFF or central_size == 0xFFFFFFFF or central_offset == 0xFFFFFFFF:
        locator_offset = eocd_offset - 20
        if locator_offset < 0:
            raise ValidationError("backup archive has an invalid ZIP64 locator")
        archive.seek(locator_offset)
        locator = archive.read(20)
        if len(locator) != 20:
            raise ValidationError("backup archive has an invalid ZIP64 locator")
        locator_signature, locator_disk, zip64_offset, disk_count = struct.unpack("<4sLQL", locator)
        if (
            locator_signature != b"PK\x06\x07"
            or locator_disk != 0
            or disk_count != 1
            or zip64_offset + 56 > locator_offset
        ):
            raise ValidationError("backup archive has an invalid ZIP64 locator")
        archive.seek(zip64_offset)
        zip64 = archive.read(56)
        if len(zip64) != 56:
            raise ValidationError("backup archive has an invalid ZIP64 end record")
        (
            zip64_signature,
            record_size,
            _version_made,
            _version_needed,
            zip64_disk,
            zip64_central_disk,
            zip64_disk_entries,
            total_entries,
            central_size,
            central_offset,
        ) = struct.unpack("<4sQ2H2L4Q", zip64)
        if (
            zip64_signature != b"PK\x06\x06"
            or record_size < 44
            or zip64_offset + 12 + record_size > locator_offset
            or zip64_disk != 0
            or zip64_central_disk != 0
            or zip64_disk_entries != total_entries
        ):
            raise ValidationError("backup archive has an invalid ZIP64 end record")
        central_end_limit = zip64_offset
    else:
        central_end_limit = eocd_offset

    if total_entries > max_entries:
        raise ValidationError("backup archive has an invalid number of entries")
    if central_size > max_central_directory_bytes:
        raise ValidationError("backup archive central directory exceeds the supported size")
    if central_offset + central_size > central_end_limit:
        raise ValidationError("backup archive has an invalid central directory")
    archive.seek(0)


def _read_and_validate_archive(
    zipped: zipfile.ZipFile,
) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    infos = zipped.infolist()
    if not infos or len(infos) > _MAX_ENTRIES + 1:
        raise ValidationError("backup archive has an invalid number of entries")
    members: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    for info in infos:
        name = info.filename
        portable_name = unicodedata.normalize("NFD", name).casefold()
        if name in members or portable_name in folded:
            raise ValidationError("backup archive contains duplicate or case-colliding paths")
        folded.add(portable_name)
        _validate_archive_path(name)
        if info.is_dir() or _zip_member_is_link(info):
            raise ValidationError("backup archive contains a directory or link entry")
        if info.flag_bits & 0x1:
            raise ValidationError("encrypted backup entries are not supported")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValidationError("backup archive uses an unsupported compression method")
        members[name] = info
    _validate_portable_file_paths(name for name in members if name != _MANIFEST_NAME)
    manifest_info = members.get(_MANIFEST_NAME)
    if manifest_info is None or manifest_info.file_size > _MAX_MANIFEST_BYTES:
        raise ValidationError("backup archive has no valid manifest")
    try:
        raw_manifest = zipped.read(manifest_info)
        manifest = json.loads(raw_manifest)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        zipfile.BadZipFile,
        RecursionError,
    ) as exc:
        raise ValidationError("backup manifest is malformed") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != _FORMAT
        or manifest.get("format_version") != _FORMAT_VERSION
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValidationError("backup format is unsupported")
    files = manifest["files"]
    if len(files) != len(members) - 1 or len(files) > _MAX_ENTRIES:
        raise ValidationError("backup manifest does not match the archive contents")
    seen: set[str] = set()
    total = 0
    for entry in files:
        if not isinstance(entry, dict):
            raise ValidationError("backup manifest contains an invalid file record")
        record_path = entry.get("path")
        size = entry.get("size_bytes")
        digest = entry.get("sha256")
        if (
            not isinstance(record_path, str)
            or record_path == _MANIFEST_NAME
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValidationError("backup manifest contains an invalid file record")
        _validate_archive_path(record_path)
        if (
            record_path in seen
            or record_path not in members
            or members[record_path].file_size != size
        ):
            raise ValidationError("backup manifest does not match the archive contents")
        seen.add(record_path)
        total += size
        if total > _MAX_TOTAL_BYTES:
            raise ValidationError("backup archive exceeds the supported uncompressed size")
    if seen != set(members) - {_MANIFEST_NAME} or "workspace/aibench.db" not in seen:
        raise ValidationError("backup archive is missing required workspace files")
    return manifest, members


def _validate_archive_path(name: str) -> None:
    if not name or "\\" in name or "\x00" in name or name.startswith("/"):
        raise ValidationError("backup archive contains an unsafe path")
    path = PurePosixPath(name)
    if (
        path.is_absolute()
        or path.as_posix() != name
        or len(path.parts) > 128
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValidationError("backup archive contains an unsafe path")
    if name != _MANIFEST_NAME and (len(path.parts) < 2 or path.parts[0] != "workspace"):
        raise ValidationError("backup archive contains an unexpected path")
    reserved = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    for part in path.parts:
        stem = part.rstrip(" .").split(".", 1)[0].upper()
        if (
            part.endswith((".", " "))
            or any(ord(char) < 32 or char in '<>:"|?*' for char in part)
            or stem in reserved
            or re.fullmatch(r"(?:COM|LPT)[1-9¹²³]", stem) is not None
        ):
            raise ValidationError("backup archive contains a path that is not portable")
    if (
        len(path.parts) == 2
        and path.parts[0] == "workspace"
        and unicodedata.normalize("NFD", path.parts[1]).casefold()
        in {"aibench.db-wal", "aibench.db-shm", "aibench.db-journal"}
    ):
        raise ValidationError("backup archive contains transient SQLite files")


def _validate_portable_file_paths(paths: Iterable[str]) -> None:
    files: dict[str, tuple[str, ...]] = {}
    directories: dict[str, tuple[str, ...]] = {}
    parsed: list[tuple[str, ...]] = []
    component_count = 0
    for name in paths:
        component_count += name.count("/") + 1
        if component_count > _MAX_PATH_COMPONENTS:
            raise ValidationError("backup archive contains too many path components")
        parts = PurePosixPath(name).parts
        parsed.append(parts)
        folded = unicodedata.normalize("NFD", name).casefold()
        if folded in files:
            raise ValidationError("backup archive contains duplicate or case-colliding paths")
        files[folded] = parts
        for length in range(1, len(parts)):
            prefix_parts = parts[:length]
            prefix = unicodedata.normalize("NFD", "/".join(prefix_parts)).casefold()
            prior = directories.get(prefix)
            if prior is not None and prior != prefix_parts:
                raise ValidationError("backup archive contains colliding directory paths")
            directories[prefix] = prefix_parts
    for parts in parsed:
        for length in range(1, len(parts)):
            prefix = unicodedata.normalize("NFD", "/".join(parts[:length])).casefold()
            if prefix in files:
                raise ValidationError("backup archive has a file used as a directory")


def _zip_member_is_link(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    dos_attributes = info.external_attr & 0xFFFF
    return file_type not in {0, stat.S_IFREG} or bool(dos_attributes & _REPARSE)


def _extract_verified_files(
    zipped: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
    payload: Path,
    *,
    payload_fd: int | None = None,
    pinned_directories: list[int] | None = None,
) -> None:
    if pinned_directories is None:
        pinned_directories = []
    by_path = {entry["path"]: entry for entry in manifest["files"]}
    directory_fds: dict[tuple[str, ...], int] = {}
    if payload_fd is not None and os.name != "nt":
        directory_fds[()] = payload_fd
    windows_directory_fds: dict[tuple[str, ...], int] = {}
    if os.name == "nt" and payload_fd is not None:
        windows_directory_fds[()] = payload_fd
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)

    def ensure_directory(parts: tuple[str, ...]) -> int | Path:
        if os.name == "nt":
            current = payload
            windows_prefix: tuple[str, ...] = ()
            for part in parts:
                current /= part
                windows_prefix += (part,)
                if windows_prefix not in windows_directory_fds:
                    current.mkdir(mode=0o700, exist_ok=True)
                    descriptor = _open_windows_directory_fd(current)
                    info = os.fstat(descriptor)
                    path_info = current.lstat()
                    if (
                        _is_link(path_info)
                        or not stat.S_ISDIR(info.st_mode)
                        or (info.st_dev, info.st_ino) != (path_info.st_dev, path_info.st_ino)
                    ):
                        os.close(descriptor)
                        raise ValidationError("restore staging directory changed during extraction")
                    windows_directory_fds[windows_prefix] = descriptor
                    pinned_directories.append(descriptor)
            return current

        if payload_fd is None:
            raise ValidationError("restore staging directory handle is unavailable")
        prefix: tuple[str, ...] = ()
        for part in parts:
            parent_fd = directory_fds[prefix]
            child = prefix + (part,)
            if child not in directory_fds:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                descriptor = os.open(part, directory_flags, dir_fd=parent_fd)
                entry_info = os.stat(part, dir_fd=parent_fd, follow_symlinks=False)
                descriptor_info = os.fstat(descriptor)
                if not stat.S_ISDIR(entry_info.st_mode) or (
                    entry_info.st_dev,
                    entry_info.st_ino,
                ) != (descriptor_info.st_dev, descriptor_info.st_ino):
                    os.close(descriptor)
                    raise ValidationError("restore staging directory changed during extraction")
                directory_fds[child] = descriptor
                pinned_directories.append(descriptor)
            prefix = child
        return directory_fds[prefix]

    for name in sorted(by_path):
        relative = PurePosixPath(name).parts[1:]
        parent = ensure_directory(relative[:-1])
        record = by_path[name]
        digest = hashlib.sha256()
        copied = 0
        if os.name == "nt":
            if not isinstance(parent, Path):
                raise ValidationError("restore staging directory handle is unavailable")
            target = parent / relative[-1]
            destination_context = target.open("xb")
        else:
            if not isinstance(parent, int):
                raise ValidationError("restore staging directory handle is unavailable")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(relative[-1], flags, 0o600, dir_fd=parent)
            destination_context = os.fdopen(descriptor, "wb")
        with zipped.open(members[name], "r") as source, destination_context as destination:
            _restrict_file_permissions(destination.fileno())
            while block := source.read(1024 * 1024):
                copied += len(block)
                if copied > record["size_bytes"]:
                    raise ValidationError(f"backup entry exceeds its declared size: {name}")
                digest.update(block)
                destination.write(block)
            destination.flush()
            os.fsync(destination.fileno())
        if copied != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
            raise ValidationError(f"backup entry failed its integrity check: {name}")


def _restrict_file_permissions(descriptor: int) -> None:
    if os.name != "nt":
        os.chmod(descriptor, 0o600)


def _relocate_and_verify_database(
    payload: Path,
    final_workspace: Path,
    *,
    expected_size: int,
    expected_sha256: str,
) -> tuple[int, int | None, tuple[int, int], int, str]:
    database_path = _regular_file(payload / "aibench.db", "restored workspace database")
    database_descriptor = _open_pinned_database_file(database_path)
    database_fd: int | None = database_descriptor
    connection: sqlite3.Connection | None = None
    artifact_count = 0
    try:
        try:
            _verify_database_manifest(database_descriptor, expected_size, expected_sha256)
            connection = sqlite3.connect(database_path)
            _verify_pinned_database_file(database_path, database_descriptor)
            check = connection.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                raise ValidationError("backup database failed SQLite integrity_check")
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchone()
            if foreign_key_errors is not None:
                raise ValidationError("backup database contains broken foreign-key references")
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(artifacts)").fetchall()
            }
            if not columns:
                # Databases created before artifact storage are still restorable.
                connection.commit()
                raise _NoArtifactCatalog
            required = {
                "artifact_id",
                "digest",
                "uri",
                "mime_type",
                "size_bytes",
                "redaction",
                "run_id",
                "content_hash",
            }
            if not required.issubset(columns):
                raise ValidationError("backup database artifact schema is unsupported")
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, _MAX_CATALOG_FIELD_BYTES)
            catalog_rows = connection.execute("SELECT count(*) FROM artifacts").fetchone()
            if catalog_rows is None or catalog_rows[0] > _MAX_CATALOG_ROWS:
                raise ValidationError("backup catalog has too many artifact records")
            try:
                invalid_metadata = connection.execute(
                    "SELECT 1 FROM artifacts WHERE "
                    "typeof(artifact_id) != 'text' OR length(artifact_id) > 512 OR "
                    "typeof(digest) != 'text' OR length(digest) > 71 OR "
                    "typeof(uri) != 'text' OR length(uri) > 32767 OR "
                    "typeof(mime_type) != 'text' OR length(mime_type) > 1024 OR "
                    "typeof(redaction) != 'text' OR length(redaction) > 32 OR "
                    "(run_id IS NOT NULL AND "
                    "(typeof(run_id) != 'text' OR length(run_id) > 512)) OR "
                    "typeof(content_hash) != 'text' OR length(content_hash) > 71 LIMIT 1"
                ).fetchone()
            except sqlite3.Error as exc:
                raise ValidationError(
                    "backup catalog contains invalid or oversized artifact metadata"
                ) from exc
            if invalid_metadata is not None:
                raise ValidationError(
                    "backup catalog contains invalid or oversized artifact metadata"
                )
            verified: dict[str, int] = {}
            last_rowid: int | None = None
            processed_rows = 0
            while True:
                if last_rowid is None:
                    rows = connection.execute(
                        "SELECT rowid, artifact_id, digest, uri, mime_type, size_bytes, redaction, "
                        "run_id, content_hash FROM artifacts ORDER BY rowid LIMIT 512"
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT rowid, artifact_id, digest, uri, mime_type, size_bytes, redaction, "
                        "run_id, content_hash FROM artifacts WHERE rowid > ? ORDER BY rowid LIMIT 512",
                        (last_rowid,),
                    ).fetchall()
                if not rows:
                    break
                processed_rows += len(rows)
                if processed_rows > _MAX_CATALOG_ROWS:
                    raise ValidationError("backup catalog has too many artifact records")
                for row in rows:
                    (
                        _rowid,
                        artifact_id,
                        digest,
                        original_uri,
                        mime_type,
                        size_bytes,
                        redaction,
                        run_id,
                        original_hash,
                    ) = row
                    match = (
                        re.fullmatch(r"sha256:([0-9a-f]{64})", digest)
                        if isinstance(digest, str)
                        else None
                    )
                    if match is None or not isinstance(size_bytes, int) or size_bytes < 0:
                        raise ValidationError(
                            f"backup catalog has invalid artifact metadata: {artifact_id}"
                        )
                    hex_digest = match.group(1)
                    path = payload / "artifacts" / hex_digest[:2] / hex_digest
                    if digest not in verified:
                        _verify_file_digest(path, size_bytes, hex_digest)
                        verified[digest] = size_bytes
                    elif verified[digest] != size_bytes:
                        raise ValidationError(
                            "backup catalog has conflicting sizes for one artifact"
                        )
                    original_ref = ArtifactRef(
                        artifact_id=artifact_id,
                        digest=digest,
                        uri=original_uri,
                        mime_type=mime_type,
                        size_bytes=size_bytes,
                        redaction=redaction,
                        run_id=run_id,
                    )
                    if original_hash != content_hash(original_ref.model_dump_json()):
                        raise ValidationError(
                            f"backup artifact catalog record is invalid: {artifact_id}"
                        )
                    relocated_uri = (
                        final_workspace / "artifacts" / hex_digest[:2] / hex_digest
                    ).as_posix()
                    ref = ArtifactRef(
                        artifact_id=artifact_id,
                        digest=digest,
                        uri=relocated_uri,
                        mime_type=mime_type,
                        size_bytes=size_bytes,
                        redaction=redaction,
                        run_id=run_id,
                    )
                    connection.execute(
                        "UPDATE artifacts SET uri = ?, content_hash = ? WHERE artifact_id = ?",
                        (relocated_uri, content_hash(ref.model_dump_json()), artifact_id),
                    )
                last_rowid = rows[-1][0]
            artifact_count = len(verified)
            connection.commit()
            check = connection.execute("PRAGMA integrity_check").fetchone()
            if check is None or check[0] != "ok":
                raise ValidationError("restored database failed SQLite integrity_check")
        except _NoArtifactCatalog:
            artifact_count = 0
        except BaseException:
            if connection is not None:
                connection.rollback()
            raise
        finally:
            if connection is not None:
                connection.close()
        pinned_identity = (
            os.fstat(database_descriptor).st_dev,
            os.fstat(database_descriptor).st_ino,
        )
        database_fd, final_size, final_sha256 = _protect_database_until_publication(
            database_path, database_descriptor
        )
        identity = pinned_identity
        return artifact_count, database_fd, identity, final_size, final_sha256
    except BaseException:
        if database_fd is not None:
            with suppress(OSError):
                os.close(database_fd)
        raise


def _hash_descriptor(descriptor: int) -> tuple[int, str]:
    initial = os.fstat(descriptor)
    if not stat.S_ISREG(initial.st_mode) or initial.st_size > _MAX_TOTAL_BYTES:
        raise ValidationError("restored database is not a supported regular file")
    position = os.lseek(descriptor, 0, os.SEEK_CUR)
    digest = hashlib.sha256()
    size = 0
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        while block := os.read(descriptor, 1024 * 1024):
            size += len(block)
            if size > _MAX_TOTAL_BYTES:
                raise ValidationError("restored database exceeds the supported size")
            digest.update(block)
    finally:
        os.lseek(descriptor, position, os.SEEK_SET)
    final = os.fstat(descriptor)
    if size != initial.st_size or (
        initial.st_dev,
        initial.st_ino,
        initial.st_size,
        initial.st_mtime_ns,
    ) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
        raise ValidationError("restored database changed while it was being checked")
    return size, digest.hexdigest()


def _verify_database_manifest(descriptor: int, expected_size: int, expected_sha256: str) -> None:
    pinned = os.fstat(descriptor)
    if pinned.st_nlink != 1 or pinned.st_size != expected_size:
        raise ValidationError("restored database does not match its archive manifest")
    actual_size, actual_sha256 = _hash_descriptor(descriptor)
    if actual_size != expected_size or actual_sha256 != expected_sha256:
        raise ValidationError("restored database does not match its archive manifest")


def _protect_database_until_publication(path: Path, descriptor: int) -> tuple[int | None, int, str]:
    size, digest = _hash_descriptor(descriptor)
    if os.name == "nt":
        try:
            _set_windows_file_readonly(descriptor, readonly=True)
            _verify_pinned_database_file(path, descriptor)
            if _hash_descriptor(descriptor) != (size, digest):
                raise ValidationError("restored database changed before publication")
        except BaseException:
            with suppress(OSError):
                _set_windows_file_readonly(descriptor, readonly=False)
            raise
        os.close(descriptor)
        return None, size, digest

    os.chmod(descriptor, 0o400)
    _verify_pinned_database_file(path, descriptor)
    if _hash_descriptor(descriptor) != (size, digest):
        raise ValidationError("restored database changed before publication")
    return descriptor, size, digest


def _verify_pinned_database_digest(
    path: Path,
    descriptor: int,
    expected_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
) -> None:
    _verify_pinned_database_file(path, descriptor)
    pinned = os.fstat(descriptor)
    if (pinned.st_dev, pinned.st_ino) != expected_identity:
        raise ValidationError("restored database changed before publication")
    if _hash_descriptor(descriptor) != (expected_size, expected_sha256):
        raise ValidationError("restored database changed before publication")


def _verify_database_path_digest(
    path: Path,
    expected_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
) -> None:
    descriptor = _open_pinned_database_file(path)
    try:
        pinned = os.fstat(descriptor)
        if (pinned.st_dev, pinned.st_ino) != expected_identity:
            raise ValidationError("restored database changed before publication")
        if _hash_descriptor(descriptor) != (expected_size, expected_sha256):
            raise ValidationError("restored database changed before publication")
    finally:
        os.close(descriptor)


def _set_windows_file_readonly(descriptor: int, *, readonly: bool) -> None:
    import msvcrt
    from ctypes import wintypes

    class FileBasicInfo(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL
    set_info = kernel32.SetFileInformationByHandle
    set_info.argtypes = [wintypes.HANDLE, wintypes.INT, wintypes.LPVOID, wintypes.DWORD]
    set_info.restype = wintypes.BOOL
    info = FileBasicInfo()
    handle = msvcrt.get_osfhandle(descriptor)
    if not get_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))
    if readonly:
        info.FileAttributes |= 0x01
    else:
        info.FileAttributes &= ~0x01
    if not set_info(handle, 0, ctypes.byref(info), ctypes.sizeof(info)):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error))


def _set_database_readonly_at_path(
    path: Path,
    expected_identity: tuple[int, int],
    expected_size: int,
    expected_sha256: str,
    *,
    readonly: bool,
) -> None:
    descriptor = _open_pinned_database_file(path)
    try:
        pinned = os.fstat(descriptor)
        if (pinned.st_dev, pinned.st_ino) != expected_identity:
            raise ValidationError("restored database changed before publication")
        if _hash_descriptor(descriptor) != (expected_size, expected_sha256):
            raise ValidationError("restored database changed before publication")
        _set_windows_file_readonly(descriptor, readonly=readonly)
    finally:
        os.close(descriptor)


def _verify_file_digest(path: Path, expected_size: int, expected_hex: str) -> None:
    info = path.lstat()
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_size != expected_size:
        raise ValidationError(f"backup is missing a valid artifact object: {path.name}")
    size, digest = _hash_file(path)
    if size != expected_size or digest != expected_hex:
        raise ValidationError(f"backup artifact failed its digest check: {path.name}")


def _publish_directory(
    source: Path,
    target: Path,
    *,
    root_fd: int | None = None,
    source_dir_fd: int | None = None,
    source_name: str | None = None,
    source_fd: int | None = None,
) -> None:
    try:
        if root_fd is not None and os.name != "nt":
            os.stat(target.name, dir_fd=root_fd, follow_symlinks=False)
        else:
            target.lstat()
    except FileNotFoundError:
        pass
    else:
        raise ValidationError(f"restore destination already exists: {target}")
    try:
        _rename_directory_without_replacement(
            source,
            target,
            root_dir_fd=root_fd,
            source_dir_fd=source_dir_fd,
            source_name=source_name,
            source_fd=source_fd,
        )
    except FileExistsError as exc:
        raise ValidationError(f"restore destination already exists: {target}") from exc
    if os.name != "nt" and root_fd is not None and source_fd is not None:
        _verify_published_directory(root_fd, source_fd, target)


def _verify_published_directory(root_fd: int, source_fd: int, target: Path) -> None:
    try:
        actual = os.stat(target.name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise ValidationError("restore destination changed during publication") from exc
    source = os.fstat(source_fd)
    if stat.S_ISDIR(actual.st_mode) and (actual.st_dev, actual.st_ino) == (
        source.st_dev,
        source.st_ino,
    ):
        return
    raise ValidationError("restore staging directory changed during publication")


def _rename_directory_without_replacement(
    source: Path,
    target: Path,
    *,
    root_dir_fd: int | None = None,
    source_dir_fd: int | None = None,
    source_name: str | None = None,
    source_fd: int | None = None,
) -> None:
    if os.name == "nt":
        if source_fd is None:
            raise OSError(errno.ENOTSUP, "handle-based restore publication is unavailable")
        _rename_windows_directory_handle(source_fd, target)
        return
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable")
        renameat2.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        renameat2.restype = ctypes.c_int
        old_directory = source_dir_fd if source_dir_fd is not None else -100
        new_directory = root_dir_fd if root_dir_fd is not None else -100
        old_path = source_name if source_dir_fd is not None else str(source)
        new_path = target.name if root_dir_fd is not None else str(target)
        if old_path is None:
            raise OSError(errno.EINVAL, "anchored restore source path is missing")
        result = renameat2(
            old_directory,
            os.fsencode(old_path),
            new_directory,
            os.fsencode(new_path),
            1,
        )
        if result != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise FileExistsError(error, os.strerror(error), str(target))
            raise OSError(error, os.strerror(error), str(target))
        return
    if sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        if source_dir_fd is not None or root_dir_fd is not None:
            renameatx_np = getattr(libc, "renameatx_np", None)
            if renameatx_np is None or source_name is None or root_dir_fd is None:
                raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable")
            renameatx_np.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameatx_np.restype = ctypes.c_int
            result = renameatx_np(
                source_dir_fd if source_dir_fd is not None else -100,
                os.fsencode(source_name),
                root_dir_fd,
                os.fsencode(target.name),
                0x00000004,
            )
            if result != 0:
                error = ctypes.get_errno()
                if error == errno.EEXIST:
                    raise FileExistsError(error, os.strerror(error), str(target))
                raise OSError(error, os.strerror(error), str(target))
            return
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is None:
            raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable")
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004)
        if result != 0:
            error = ctypes.get_errno()
            if error == errno.EEXIST:
                raise FileExistsError(error, os.strerror(error), str(target))
            raise OSError(error, os.strerror(error), str(target))
        return
    raise OSError(errno.ENOTSUP, "atomic no-replace directory rename is unavailable")


def _rename_windows_directory_handle(source_fd: int, target: Path) -> None:
    import msvcrt
    from ctypes import wintypes

    class FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("ReplaceIfExists", wintypes.BOOL),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        ]

    encoded_name = str(target).encode("utf-16-le")
    name_offset = FileRenameInfo.FileName.offset
    size = ctypes.sizeof(FileRenameInfo) + len(encoded_name)
    buffer = ctypes.create_string_buffer(size)
    info = ctypes.cast(buffer, ctypes.POINTER(FileRenameInfo)).contents
    info.ReplaceIfExists = 0
    info.RootDirectory = None
    info.FileNameLength = len(encoded_name)
    ctypes.memmove(ctypes.addressof(info) + name_offset, encoded_name, len(encoded_name))

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_file_information = kernel32.SetFileInformationByHandle
    set_file_information.argtypes = [
        wintypes.HANDLE,
        wintypes.INT,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_file_information.restype = wintypes.BOOL
    succeeded = set_file_information(msvcrt.get_osfhandle(source_fd), 3, buffer, size)
    if not succeeded:
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, "restore destination already exists", str(target))
        raise OSError(error, ctypes.FormatError(error), str(target))
