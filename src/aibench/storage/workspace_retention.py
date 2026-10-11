"""Workspace-local artifact retention policy."""

from __future__ import annotations

import errno
import json
import math
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aibench.core.errors import ValidationError
from aibench.storage import _windows_handles
from aibench.storage.artifacts import DEFAULT_GC_GRACE_SECONDS

POLICY_NAME = "retention.json"
POLICY_VERSION = 1
MAX_POLICY_BYTES = 16 * 1024
MAX_USAGE_ENTRIES = 1_000_000
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def retention_policy_path(workspace_root: Path) -> Path:
    return workspace_root / POLICY_NAME


def load_retention_policy(
    workspace_root: Path,
    *,
    expected_directory_metadata: os.stat_result | None = None,
) -> tuple[float, bool]:
    """Return configured artifact orphan grace seconds and whether it was persisted."""
    current_directory_metadata = _require_workspace_directory(workspace_root)
    directory_metadata = expected_directory_metadata or current_directory_metadata
    if not _same_file(directory_metadata, current_directory_metadata):
        raise ValidationError("workspace directory changed before reading retention policy")
    try:
        raw_bytes = _read_policy_bytes(workspace_root, directory_metadata)
        if raw_bytes is None:
            return DEFAULT_GC_GRACE_SECONDS, False
        if len(raw_bytes) > MAX_POLICY_BYTES:
            raise ValidationError("workspace retention policy exceeds the supported size")
        return _decode_policy(raw_bytes)
    except (OSError, UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"workspace retention policy is invalid: {exc}") from exc


def _decode_policy(raw_bytes: bytes) -> tuple[float, bool]:
    raw = json.loads(
        raw_bytes.decode("utf-8"),
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "artifact_orphan_grace_seconds"}:
        raise ValidationError("workspace retention policy has an unsupported shape")
    if raw["schema_version"] != POLICY_VERSION or isinstance(raw["schema_version"], bool):
        raise ValidationError("workspace retention policy has an unsupported schema version")
    grace = raw["artifact_orphan_grace_seconds"]
    if isinstance(grace, bool) or not isinstance(grace, (int, float)):
        raise ValidationError("artifact orphan grace period must be a finite number")
    try:
        value = float(grace)
    except OverflowError as exc:
        raise ValidationError(
            "artifact orphan grace period is outside the supported range"
        ) from exc
    if not math.isfinite(value) or value < DEFAULT_GC_GRACE_SECONDS:
        raise ValidationError(
            "artifact orphan grace period must be finite and at least "
            f"{DEFAULT_GC_GRACE_SECONDS:g} seconds"
        )
    return value, True


def save_retention_policy(
    workspace_root: Path,
    grace_seconds: float,
    *,
    expected_directory_metadata: os.stat_result | None = None,
) -> dict[str, Any]:
    """Atomically persist the workspace orphan grace period without following links."""
    value = _validate_grace(grace_seconds)
    current_directory_metadata = _require_workspace_directory(workspace_root)
    directory_metadata = expected_directory_metadata or current_directory_metadata
    if not _same_file(directory_metadata, current_directory_metadata):
        raise ValidationError("workspace directory changed before saving retention policy")
    path = retention_policy_path(workspace_root)
    payload = {
        "schema_version": POLICY_VERSION,
        "artifact_orphan_grace_seconds": value,
    }
    try:
        _write_policy_bytes(workspace_root, directory_metadata, _serialize_policy(payload))
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"could not save workspace retention policy: {exc}") from exc
    return {**payload, "path": str(path)}


@dataclass
class _PinnedWorkspaceDirectory:
    path: Path
    metadata: os.stat_result
    descriptor: int | None = None
    handle: int | None = None

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None
        if self.handle is not None:
            _windows_handles.close_handle(self.handle)
            self.handle = None


def load_project_retention_policy(
    project_root: Path,
) -> tuple[float, bool, os.stat_result, Path]:
    """Read retention settings through a workspace directory pinned to its project root."""
    pinned = _open_project_workspace_directory(project_root)
    try:
        raw_bytes = _read_pinned_policy_bytes(pinned)
        if raw_bytes is None:
            value, configured = DEFAULT_GC_GRACE_SECONDS, False
        else:
            if len(raw_bytes) > MAX_POLICY_BYTES:
                raise ValidationError("workspace retention policy exceeds the supported size")
            value, configured = _decode_policy(raw_bytes)
        return value, configured, pinned.metadata, pinned.path
    except (OSError, UnicodeError, ValueError, RecursionError, OverflowError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"workspace retention policy is invalid: {exc}") from exc
    finally:
        pinned.close()


def save_project_retention_policy(
    project_root: Path, grace_seconds: float
) -> tuple[dict[str, Any], os.stat_result]:
    """Persist settings through a workspace directory pinned to its project root."""
    value = _validate_grace(grace_seconds)
    pinned = _open_project_workspace_directory(project_root, writable=True)
    try:
        payload = _serialize_policy(
            {"schema_version": POLICY_VERSION, "artifact_orphan_grace_seconds": value}
        )
        _write_pinned_policy_bytes(pinned, payload)
        return (
            {
                "schema_version": POLICY_VERSION,
                "artifact_orphan_grace_seconds": value,
                "path": str(pinned.path / POLICY_NAME),
            },
            pinned.metadata,
        )
    except (OSError, ValueError) as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"could not save workspace retention policy: {exc}") from exc
    finally:
        pinned.close()


def _serialize_policy(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _open_project_workspace_directory(
    project_root: Path, *, writable: bool = False
) -> _PinnedWorkspaceDirectory:
    try:
        root = project_root.expanduser().resolve(strict=True)
        root_metadata = root.lstat()
        _validate_regular_directory(root_metadata, "project root")
        workspace_path = root / ".aibench"
        if os.name == "nt":
            project_handle = _windows_handles.open_directory(root, root_metadata)
            workspace_handle: int | None = None
            try:
                project_info = _windows_handles.handle_info(project_handle)
                workspace_entry = next(
                    (
                        entry
                        for entry in _windows_handles.iter_directory(project_handle)
                        if entry.name.casefold() == ".aibench"
                    ),
                    None,
                )
                if workspace_entry is None:
                    raise ValidationError(f"no aibench workspace at {root}")
                if workspace_entry.attributes & _windows_handles.FILE_ATTRIBUTE_REPARSE_POINT:
                    raise ValidationError(
                        "workspace .aibench directory must not be a link or reparse point"
                    )
                if not workspace_entry.attributes & _windows_handles.FILE_ATTRIBUTE_DIRECTORY:
                    raise ValidationError("workspace .aibench path must be a directory")
                workspace_handle = _windows_handles.open_relative(
                    project_handle, ".aibench", directory=True, write=writable
                )
                if workspace_handle is None:
                    raise ValidationError(f"no aibench workspace at {root}")
                info = _windows_handles.handle_info(workspace_handle)
                if (
                    not info.is_directory
                    or info.attributes & _windows_handles.FILE_ATTRIBUTE_REPARSE_POINT
                    or info.volume_id != project_info.volume_id
                    or info.file_id != workspace_entry.file_id
                ):
                    raise ValidationError("workspace directory changed while it was being opened")
                workspace_metadata = _require_workspace_directory(workspace_path)
                if (info.volume_id, info.file_id) != (
                    workspace_metadata.st_dev,
                    workspace_metadata.st_ino,
                ):
                    raise ValidationError("workspace directory changed while it was being opened")
                return _PinnedWorkspaceDirectory(
                    workspace_path, workspace_metadata, handle=workspace_handle
                )
            except BaseException:
                if workspace_handle is not None:
                    _windows_handles.close_handle(workspace_handle)
                raise
            finally:
                _windows_handles.close_handle(project_handle)

        directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if not hasattr(os, "O_DIRECTORY") or not hasattr(os, "O_NOFOLLOW"):
            raise ValidationError(
                "safe retention policy access requires no-follow directory operations"
            )
        project_descriptor = os.open(root, directory_flags)
        workspace_descriptor: int | None = None
        try:
            if not _same_file(root_metadata, os.fstat(project_descriptor)):
                raise ValidationError("project directory changed before workspace access")
            workspace_entry_metadata = os.stat(
                ".aibench", dir_fd=project_descriptor, follow_symlinks=False
            )
            _validate_regular_directory(workspace_entry_metadata, "workspace directory")
            workspace_descriptor = os.open(".aibench", directory_flags, dir_fd=project_descriptor)
            workspace_metadata = os.fstat(workspace_descriptor)
            _validate_regular_directory(workspace_metadata, "workspace directory")
            if not _same_file(workspace_entry_metadata, workspace_metadata):
                raise ValidationError("workspace directory changed while it was being opened")
            return _PinnedWorkspaceDirectory(
                workspace_path, workspace_metadata, descriptor=workspace_descriptor
            )
        except BaseException:
            if workspace_descriptor is not None:
                os.close(workspace_descriptor)
            raise
        finally:
            os.close(project_descriptor)
    except FileNotFoundError as exc:
        raise ValidationError(f"no aibench workspace at {project_root}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValidationError(
                "workspace .aibench directory must not be a link or reparse point"
            ) from exc
        raise ValidationError(f"could not safely open workspace directory: {exc}") from exc


def _read_pinned_policy_bytes(pinned: _PinnedWorkspaceDirectory) -> bytes | None:
    if pinned.handle is not None:
        return _read_policy_bytes_windows(pinned.handle)
    if pinned.descriptor is None:
        raise ValidationError("workspace directory handle is unavailable")
    return _read_policy_bytes_posix(pinned.descriptor)


def _write_pinned_policy_bytes(pinned: _PinnedWorkspaceDirectory, payload: bytes) -> None:
    if pinned.handle is not None:
        _write_policy_bytes_windows_handle(pinned.handle, payload)
        return
    if pinned.descriptor is None:
        raise ValidationError("workspace directory handle is unavailable")
    _write_policy_bytes_posix(pinned.descriptor, payload)


def _read_policy_bytes(workspace_root: Path, expected_directory: os.stat_result) -> bytes | None:
    if os.name == "nt":
        directory_handle = _windows_handles.open_directory(workspace_root, expected_directory)
        try:
            return _read_policy_bytes_windows(directory_handle)
        finally:
            _windows_handles.close_handle(directory_handle)

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(workspace_root, flags)
    try:
        if not _same_file(expected_directory, os.fstat(directory_fd)):
            raise ValidationError("workspace directory changed while reading retention policy")
        return _read_policy_bytes_posix(directory_fd)
    finally:
        os.close(directory_fd)


def _read_policy_bytes_windows(directory_handle: int) -> bytes | None:
    policy_handle: int | None = None
    descriptor: int | None = None
    try:
        policy_handle = _windows_handles.open_relative(
            directory_handle,
            POLICY_NAME,
            directory=False,
            read=True,
            allow_missing=True,
        )
        if policy_handle is None:
            return None
        info = _windows_handles.handle_info(policy_handle)
        if (
            info.attributes & _windows_handles.FILE_ATTRIBUTE_REPARSE_POINT
            or info.is_directory
            or info.link_count != 1
        ):
            raise ValidationError("workspace retention policy must be a regular file, not a link")
        if info.size > MAX_POLICY_BYTES:
            raise ValidationError("workspace retention policy exceeds the supported size")
        descriptor = _windows_handles.handle_to_fd(policy_handle)
        policy_handle = None
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read(MAX_POLICY_BYTES + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if policy_handle is not None:
            _windows_handles.close_handle(policy_handle)


def _read_policy_bytes_posix(directory_fd: int) -> bytes | None:
    try:
        policy_fd = os.open(
            POLICY_NAME,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
    except FileNotFoundError:
        return None
    with os.fdopen(policy_fd, "rb") as handle:
        metadata = os.fstat(handle.fileno())
        _validate_regular(metadata, "workspace retention policy")
        if metadata.st_nlink != 1:
            raise ValidationError("workspace retention policy must not be hard linked")
        if metadata.st_size > MAX_POLICY_BYTES:
            raise ValidationError("workspace retention policy exceeds the supported size")
        return handle.read(MAX_POLICY_BYTES + 1)


def _write_policy_bytes(
    workspace_root: Path, expected_directory: os.stat_result, payload: bytes
) -> None:
    if os.name == "nt":
        directory_handle = _windows_handles.open_directory(
            workspace_root, expected_directory, writable=True
        )
        try:
            _write_policy_bytes_windows_handle(directory_handle, payload)
        finally:
            _windows_handles.close_handle(directory_handle)
        return

    directory_fd = os.open(
        workspace_root,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not _same_file(expected_directory, os.fstat(directory_fd)):
            raise ValidationError("workspace directory changed while saving retention policy")
        _write_policy_bytes_posix(directory_fd, payload)
    finally:
        os.close(directory_fd)


def _write_policy_bytes_posix(directory_fd: int, payload: bytes) -> None:
    temporary_name = f".retention.{secrets.token_hex(8)}.tmp"
    temporary_fd: int | None = None
    try:
        try:
            old_metadata = os.stat(POLICY_NAME, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            old_metadata = None
        if old_metadata is not None:
            _validate_regular(old_metadata, "workspace retention policy")
            if old_metadata.st_nlink != 1:
                raise ValidationError("workspace retention policy must not be hard linked")
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        with os.fdopen(temporary_fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            POLICY_NAME,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_name = ""
        os.fsync(directory_fd)
    finally:
        if temporary_fd is not None:
            os.close(temporary_fd)
        if temporary_name:
            try:
                os.unlink(temporary_name, dir_fd=directory_fd)
            except FileNotFoundError:
                pass


def _write_policy_bytes_windows_handle(directory_handle: int, payload: bytes) -> None:
    temporary_handle: int | None = None
    descriptor: int | None = None
    temporary_name = ""
    try:
        old_handle = _windows_handles.open_relative(
            directory_handle,
            POLICY_NAME,
            directory=False,
            allow_missing=True,
        )
        if old_handle is not None:
            try:
                old_info = _windows_handles.handle_info(old_handle)
                if (
                    old_info.attributes & _windows_handles.FILE_ATTRIBUTE_REPARSE_POINT
                    or old_info.is_directory
                    or old_info.link_count != 1
                ):
                    raise ValidationError(
                        "workspace retention policy must be a regular file, not a link"
                    )
            finally:
                _windows_handles.close_handle(old_handle)

        for _ in range(5):
            temporary_name = f".retention.{secrets.token_hex(8)}.tmp"
            try:
                temporary_handle = _windows_handles.open_relative(
                    directory_handle,
                    temporary_name,
                    directory=False,
                    write=True,
                    create=True,
                )
                break
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
        if temporary_handle is None:
            raise OSError("could not allocate a unique retention policy temporary file")
        descriptor = _windows_handles.handle_to_fd(temporary_handle, write=True)
        temporary_handle = None
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("could not write retention policy")
            view = view[written:]
        os.fsync(descriptor)
        import msvcrt

        raw_handle = int(msvcrt.get_osfhandle(descriptor))
        _windows_handles.rename_relative(raw_handle, directory_handle, POLICY_NAME, replace=True)
        temporary_name = ""
        _windows_handles.flush_directory(directory_handle)
    finally:
        if descriptor is not None:
            raw_handle = int(__import__("msvcrt").get_osfhandle(descriptor))
            if temporary_name:
                try:
                    _windows_handles.delete_handle(raw_handle)
                except OSError:
                    pass
            os.close(descriptor)
        if temporary_handle is not None:
            try:
                _windows_handles.delete_handle(temporary_handle)
            except OSError:
                pass
            _windows_handles.close_handle(temporary_handle)


def measure_workspace_usage(project_root: Path) -> dict[str, Any]:
    """Inventory regular workspace files without following links or creating files."""
    try:
        root = project_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve project root: {exc}") from exc
    try:
        root_metadata = root.lstat()
    except OSError as exc:
        raise ValidationError(f"could not inspect project root: {exc}") from exc
    _validate_regular_directory(root_metadata, "project root")
    workspace_root = root / ".aibench"
    categories: dict[str, dict[str, int]] = {}
    counters = {"files": 0, "bytes": 0, "skipped_unsafe": 0, "scanned_entries": 0}
    if os.name == "nt":
        try:
            project_handle = _windows_handles.open_directory(root, root_metadata)
        except OSError as exc:
            raise ValidationError(f"could not safely open project directory: {exc}") from exc
        workspace_handle: int | None = None
        try:
            try:
                workspace_handle = _windows_handles.open_relative(
                    project_handle, ".aibench", directory=True
                )
            except OSError as exc:
                raise ValidationError(f"could not safely open workspace directory: {exc}") from exc
            if workspace_handle is None:
                raise ValidationError("workspace directory does not exist")
            workspace_info = _windows_handles.handle_info(workspace_handle)
            if (
                not workspace_info.is_directory
                or workspace_info.attributes & _windows_handles.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValidationError("workspace directory must be a real directory, not a link")
            _scan_windows_usage_directory(
                workspace_root,
                workspace_handle,
                (),
                0,
                workspace_info.volume_id,
                categories,
                counters,
            )
        finally:
            if workspace_handle is not None:
                _windows_handles.close_handle(workspace_handle)
            _windows_handles.close_handle(project_handle)
        _require_same_project_root(root, root_metadata, "during usage scan")
        return {
            "workspace": str(workspace_root),
            "files": counters["files"],
            "bytes": counters["bytes"],
            "categories": {key: categories[key] for key in sorted(categories)},
            "skipped_unsafe": counters["skipped_unsafe"],
            "scanned_entries": counters["scanned_entries"],
            "read_only": True,
        }

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor_scan = (
        os.name != "nt"
        and os.open in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
    )
    if not descriptor_scan:
        raise ValidationError("safe workspace usage requires descriptor-relative directory access")

    project_descriptor: int | None = None
    workspace_descriptor: int | None = None
    try:
        try:
            project_descriptor = os.open(root, directory_flags)
            if not _same_file(root_metadata, os.fstat(project_descriptor)):
                raise ValidationError("project directory changed before usage scan")
            workspace_descriptor = os.open(".aibench", directory_flags, dir_fd=project_descriptor)
        except OSError as exc:
            raise ValidationError(f"could not safely open workspace directory: {exc}") from exc
        workspace_metadata = os.fstat(workspace_descriptor)
        _validate_regular_directory(workspace_metadata, "workspace directory")
        _scan_usage_directory(
            workspace_root,
            workspace_metadata,
            (),
            0,
            workspace_descriptor,
            directory_flags,
            workspace_metadata.st_dev,
            categories,
            counters,
        )
    finally:
        if workspace_descriptor is not None:
            os.close(workspace_descriptor)
        if project_descriptor is not None:
            os.close(project_descriptor)

    current_workspace_metadata = _require_workspace_directory(workspace_root)
    if not _same_file(workspace_metadata, current_workspace_metadata):
        raise ValidationError("workspace directory changed during usage scan")
    _require_same_project_root(root, root_metadata, "during usage scan")
    return {
        "workspace": str(workspace_root),
        "files": counters["files"],
        "bytes": counters["bytes"],
        "categories": {key: categories[key] for key in sorted(categories)},
        "skipped_unsafe": counters["skipped_unsafe"],
        "scanned_entries": counters["scanned_entries"],
        "read_only": True,
    }


def _require_same_project_root(root: Path, expected: os.stat_result, phase: str) -> None:
    try:
        current = root.lstat()
    except OSError as exc:
        raise ValidationError(f"project root changed {phase}") from exc
    if (
        _is_unsafe(current)
        or not stat.S_ISDIR(current.st_mode)
        or not _same_file(expected, current)
    ):
        raise ValidationError(f"project root changed {phase}")


def _scan_usage_directory(
    directory: Path,
    expected: os.stat_result,
    parts: tuple[str, ...],
    depth: int,
    descriptor: int | None,
    directory_flags: int,
    workspace_device: int,
    categories: dict[str, dict[str, int]],
    counters: dict[str, int],
) -> None:
    _scan_usage_directory_contents(
        directory,
        expected,
        parts,
        depth,
        descriptor,
        directory_flags,
        workspace_device,
        categories,
        counters,
    )


def _scan_windows_usage_directory(
    directory: Path,
    directory_handle: int,
    parts: tuple[str, ...],
    depth: int,
    workspace_volume: int,
    categories: dict[str, dict[str, int]],
    counters: dict[str, int],
) -> None:
    if depth > 64:
        counters["skipped_unsafe"] += 1
        return

    try:
        entries = _windows_handles.iter_directory(directory_handle)
        for entry in entries:
            counters["scanned_entries"] += 1
            if counters["scanned_entries"] > MAX_USAGE_ENTRIES:
                raise ValidationError(f"workspace usage scan exceeds {MAX_USAGE_ENTRIES} entries")
            if entry.attributes & _windows_handles.FILE_ATTRIBUTE_REPARSE_POINT:
                counters["skipped_unsafe"] += 1
                continue

            entry_is_directory = bool(entry.attributes & _windows_handles.FILE_ATTRIBUTE_DIRECTORY)
            child_parts = (*parts, entry.name)
            child_path = directory / entry.name
            child_handle: int | None = None
            try:
                child_handle = _windows_handles.open_relative(
                    directory_handle,
                    entry.name,
                    directory=entry_is_directory,
                )
                if child_handle is None:
                    counters["skipped_unsafe"] += 1
                    continue
                info = _windows_handles.handle_info(child_handle)
                if (
                    info.attributes & _windows_handles.FILE_ATTRIBUTE_REPARSE_POINT
                    or info.is_directory != entry_is_directory
                    or info.volume_id != workspace_volume
                    or info.file_id != entry.file_id
                ):
                    counters["skipped_unsafe"] += 1
                    continue
                if entry_is_directory:
                    if depth >= 64:
                        counters["skipped_unsafe"] += 1
                    else:
                        _scan_windows_usage_directory(
                            child_path,
                            child_handle,
                            child_parts,
                            depth + 1,
                            workspace_volume,
                            categories,
                            counters,
                        )
                    continue
                if info.link_count != 1:
                    counters["skipped_unsafe"] += 1
                    continue
                category = _usage_category(child_parts)
                values = categories.setdefault(category, {"files": 0, "bytes": 0})
                values["files"] += 1
                values["bytes"] += info.size
                counters["files"] += 1
                counters["bytes"] += info.size
            except OSError:
                counters["skipped_unsafe"] += 1
            finally:
                if child_handle is not None:
                    _windows_handles.close_handle(child_handle)
    except OSError as exc:
        raise ValidationError(f"could not scan workspace usage: {exc}") from exc

    try:
        current_path = directory.lstat()
        pinned_info = _windows_handles.handle_info(directory_handle)
    except OSError as exc:
        raise ValidationError("workspace directory changed during usage scan") from exc
    if (
        _is_unsafe(current_path)
        or not stat.S_ISDIR(current_path.st_mode)
        or current_path.st_dev != pinned_info.volume_id
        or current_path.st_ino != pinned_info.file_id
    ):
        raise ValidationError("workspace directory changed during usage scan")


def _scan_usage_directory_contents(
    directory: Path,
    expected: os.stat_result,
    parts: tuple[str, ...],
    depth: int,
    descriptor: int | None,
    directory_flags: int,
    workspace_device: int,
    categories: dict[str, dict[str, int]],
    counters: dict[str, int],
) -> None:
    if depth > 64:
        counters["skipped_unsafe"] += 1
        return

    if descriptor is None:
        try:
            before = directory.lstat()
        except OSError:
            counters["skipped_unsafe"] += 1
            return
        if (
            _is_unsafe(before)
            or not stat.S_ISDIR(before.st_mode)
            or not _same_file(expected, before)
            or before.st_dev != workspace_device
        ):
            counters["skipped_unsafe"] += 1
            return
        scan_target: Path | int = directory
    else:
        scan_target = descriptor

    child_directories: list[tuple[Path, os.stat_result, tuple[str, ...]]] = []
    try:
        with os.scandir(scan_target) as entries:
            for entry in entries:
                counters["scanned_entries"] += 1
                if counters["scanned_entries"] > MAX_USAGE_ENTRIES:
                    raise ValidationError(
                        f"workspace usage scan exceeds {MAX_USAGE_ENTRIES} entries"
                    )
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    counters["skipped_unsafe"] += 1
                    continue
                if _is_unsafe(metadata):
                    counters["skipped_unsafe"] += 1
                    continue
                child_path = directory / entry.name
                child_parts = (*parts, entry.name)
                if stat.S_ISDIR(metadata.st_mode):
                    if depth >= 64:
                        counters["skipped_unsafe"] += 1
                    elif descriptor is None:
                        try:
                            child_metadata = child_path.lstat()
                        except OSError:
                            counters["skipped_unsafe"] += 1
                            continue
                        if (
                            _is_unsafe(child_metadata)
                            or not stat.S_ISDIR(child_metadata.st_mode)
                            or child_metadata.st_dev != workspace_device
                        ):
                            counters["skipped_unsafe"] += 1
                            continue
                        child_directories.append((child_path, child_metadata, child_parts))
                    else:
                        if metadata.st_dev != workspace_device:
                            counters["skipped_unsafe"] += 1
                            continue
                        try:
                            child_descriptor = os.open(
                                entry.name, directory_flags, dir_fd=descriptor
                            )
                            opened_child = os.fstat(child_descriptor)
                            if not _same_file(metadata, opened_child):
                                os.close(child_descriptor)
                                counters["skipped_unsafe"] += 1
                                continue
                        except OSError:
                            counters["skipped_unsafe"] += 1
                            continue
                        try:
                            _scan_usage_directory(
                                child_path,
                                opened_child,
                                child_parts,
                                depth + 1,
                                child_descriptor,
                                directory_flags,
                                workspace_device,
                                categories,
                                counters,
                            )
                        finally:
                            os.close(child_descriptor)
                    continue
                if descriptor is None:
                    entry_metadata = metadata
                    try:
                        metadata = child_path.lstat()
                    except OSError:
                        counters["skipped_unsafe"] += 1
                        continue
                    if (
                        _is_unsafe(metadata)
                        or not stat.S_ISREG(metadata.st_mode)
                        or metadata.st_nlink != 1
                    ):
                        counters["skipped_unsafe"] += 1
                        continue
                    if _entry_metadata_has_identity(entry_metadata) and not _same_file(
                        entry_metadata, metadata
                    ):
                        counters["skipped_unsafe"] += 1
                        continue
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    counters["skipped_unsafe"] += 1
                    continue
                category = _usage_category(child_parts)
                values = categories.setdefault(category, {"files": 0, "bytes": 0})
                values["files"] += 1
                values["bytes"] += metadata.st_size
                counters["files"] += 1
                counters["bytes"] += metadata.st_size
    except OSError as exc:
        raise ValidationError(f"could not scan workspace usage: {exc}") from exc

    if descriptor is None:
        try:
            after = directory.lstat()
        except OSError as exc:
            raise ValidationError("workspace directory changed during usage scan") from exc
        if _is_unsafe(after) or not _same_file(expected, after):
            raise ValidationError("workspace directory changed during usage scan")
        for child_path, child_metadata, child_parts in child_directories:
            _scan_usage_directory(
                child_path,
                child_metadata,
                child_parts,
                depth + 1,
                None,
                directory_flags,
                workspace_device,
                categories,
                counters,
            )


def _usage_category(parts: tuple[str, ...]) -> str:
    if not parts:
        return "other"
    first = parts[0].casefold()
    if first == "artifacts":
        return "artifacts"
    if first == "sessions":
        return "sessions"
    if first == "dataset-suites":
        return "dataset_suites"
    if first == "logs":
        return "logs"
    if first in {"aibench.db", "aibench.db-wal", "aibench.db-shm", "aibench.db-journal"}:
        return "database"
    return "other"


def _entry_metadata_has_identity(metadata: os.stat_result) -> bool:
    """Windows DirEntry stat omits file identity and link count; lstat fills those in."""
    return bool(metadata.st_dev and metadata.st_ino)


def _is_unsafe(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE
    )


def _validate_grace(grace_seconds: float) -> float:
    if isinstance(grace_seconds, bool) or not math.isfinite(grace_seconds):
        raise ValidationError("artifact orphan grace period must be finite")
    if grace_seconds < DEFAULT_GC_GRACE_SECONDS:
        raise ValidationError(
            f"artifact orphan grace period must be at least {DEFAULT_GC_GRACE_SECONDS:g} seconds"
        )
    return float(grace_seconds)


def _require_workspace_directory(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"could not inspect workspace directory: {exc}") from exc
    _validate_regular_directory(metadata, "workspace directory")
    return metadata


def _validate_regular(metadata: os.stat_result, label: str) -> None:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
        or not stat.S_ISREG(metadata.st_mode)
    ):
        raise ValidationError(f"{label} must be a regular file, not a link")


def _validate_regular_directory(metadata: os.stat_result, label: str) -> None:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or getattr(metadata, "st_file_attributes", 0) & reparse_attribute
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise ValidationError(f"{label} must be a real directory, not a link")


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite value {value} is not permitted")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key {key!r} is not permitted")
        result[key] = value
    return result
