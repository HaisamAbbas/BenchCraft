"""Immutable, workspace-local dataset suite versions."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from aibench.core.errors import AibenchError
from aibench.datasets.ingest import iter_jsonl_lines
from aibench.datasets.transform import DatasetTransformError, _iter_canonical_cases
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import DatasetSuiteRecord, Storage

_SUITE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_SUITE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
_MAX_DESCRIPTION_LENGTH = 500


class DatasetSuiteError(AibenchError):
    """A named dataset suite could not be safely registered or resolved."""


class _SuiteRegistrationLock:
    """Serialize one suite-version publication across processes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        lock_file = _open_lock_file_without_following(self.path)
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                while True:
                    try:
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.05)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
        except BaseException as exc:
            lock_file.close()
            if isinstance(exc, (ImportError, OSError)):
                raise OSError(f"could not acquire suite registration lock {self.path}") from exc
            raise
        self._file = lock_file

    def release(self) -> None:
        lock_file = self._file
        if lock_file is None:
            return
        self._file = None
        try:
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        finally:
            lock_file.close()


def _open_lock_file_without_following(path: Path) -> BinaryIO:
    """Open a regular lock file without following a workspace-planted link."""
    if os.name == "nt":
        return _open_windows_lock_file_without_following(path)

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("this platform cannot safely open suite registration lock files")
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | os.O_APPEND | no_follow,
        0o600,
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("suite registration lock must be a private regular file")
        return os.fdopen(descriptor, "a+b")
    except BaseException:
        os.close(descriptor)
        raise


def _open_windows_lock_file_without_following(path: Path) -> BinaryIO:
    """Open a Windows lock handle itself so CreateFile does not follow reparse points."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

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
    get_file_info = kernel32.GetFileInformationByHandleEx
    get_file_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_file_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    generic_read = 0x80000000
    generic_write = 0x40000000
    share_read = 0x00000001
    share_write = 0x00000002
    open_always = 4
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    file_attribute_reparse_point = 0x00000400
    handle = create_file(
        str(path),
        generic_read | generic_write,
        share_read | share_write,
        None,
        open_always,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())

    descriptor: int | None = None
    try:
        info = FileAttributeTagInfo()
        # FileAttributeTagInfo is information class 9 in the Windows API.
        if not get_file_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        if info.attributes & file_attribute_reparse_point:
            raise OSError("suite registration lock cannot be a symbolic link or reparse point")

        descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_APPEND | os.O_BINARY)
        handle = None
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("suite registration lock must be a private regular file")
        lock_file = os.fdopen(descriptor, "a+b")
        descriptor = None
        return lock_file
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if handle is not None:
            close_handle(handle)


def parse_suite_reference(reference: str) -> tuple[str, str]:
    if len(reference) > 130 or reference.count("@") != 1:
        raise DatasetSuiteError("dataset suite reference must use NAME@VERSION")
    name, version = reference.split("@", 1)
    _validate_identity(name, version)
    return name, version


def _validate_identity(name: str, version: str) -> None:
    if not _SUITE_NAME.fullmatch(name):
        raise DatasetSuiteError(
            "suite name must start with a lowercase letter and contain only lowercase "
            "letters, digits, underscores, or hyphens (max 64 characters)"
        )
    if name in _WINDOWS_RESERVED_NAMES:
        raise DatasetSuiteError(f"suite name {name!r} is reserved on Windows")
    if not _SUITE_VERSION.fullmatch(version) or "@" in version:
        raise DatasetSuiteError(
            "suite version must start with a letter or digit and contain only letters, "
            "digits, dot, underscore, plus, or hyphen (max 64 characters)"
        )


def _workspace_root(project_root: Path) -> tuple[Path, Workspace]:
    try:
        root = project_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DatasetSuiteError(f"could not resolve project root: {exc}") from exc
    if not root.is_dir():
        raise DatasetSuiteError(f"project root is not a directory: {root}")
    return root, Workspace.at(root)


def register_dataset_suite(
    project_root: Path,
    name: str,
    version: str,
    source: Path,
    *,
    description: str = "",
) -> dict[str, Any]:
    """Validate and snapshot a JSONL dataset as one immutable NAME@VERSION."""
    _validate_identity(name, version)
    if len(description) > _MAX_DESCRIPTION_LENGTH:
        raise DatasetSuiteError(
            f"suite description must be at most {_MAX_DESCRIPTION_LENGTH} characters"
        )

    root, workspace = _workspace_root(project_root)
    candidate = source.expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        origin = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise DatasetSuiteError(f"could not resolve dataset source: {exc}") from exc
    if not origin.is_file() or origin.suffix.lower() != ".jsonl":
        raise DatasetSuiteError("suite source must be an existing JSONL file")

    workspace.ensure_directories()
    suite_directory = workspace.root / "dataset-suites" / name
    try:
        suite_directory.mkdir(parents=True, exist_ok=True)
        resolved_suite_directory = suite_directory.resolve(strict=True)
        if not resolved_suite_directory.is_relative_to(workspace.root.resolve()):
            raise DatasetSuiteError("suite snapshot directory escapes the workspace")
    except (OSError, RuntimeError) as exc:
        raise DatasetSuiteError(f"could not create suite snapshot directory: {exc}") from exc

    # Version labels are case-sensitive identities, but Windows filesystems are generally
    # case-insensitive and also reserve device names such as CON. Encode the exact ASCII label
    # so every accepted version maps to a distinct, portable filename.
    snapshot = suite_directory / f"{version.encode('ascii').hex()}.jsonl"
    version_key = version.encode("ascii").hex()
    registration_lock = _SuiteRegistrationLock(suite_directory / f".register-{version_key}.lock")
    lock_acquired = False
    if snapshot.is_symlink():
        raise DatasetSuiteError(f"suite snapshot path is a symbolic link: {snapshot}")

    staging: tempfile.TemporaryDirectory[str] | None = None
    stage_file: Path | None = None
    connection: sqlite3.Connection | None = None
    database: Database | None = None
    registered = False
    created = False
    cleanup_warning = False
    cleanup_path: str | None = None
    snapshot_created = False
    case_count = 0
    content_hasher = hashlib.sha256()
    try:
        staging = tempfile.TemporaryDirectory(prefix=".suite-stage-", dir=suite_directory)
        stage_dir = Path(staging.name)
        stage_file = stage_dir / "dataset.jsonl"
        with origin.open("rb") as source_handle, stage_file.open("wb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())

        connection = sqlite3.connect("")
        connection.execute("PRAGMA temp_store = FILE")
        connection.execute("CREATE TABLE case_ids (case_id TEXT PRIMARY KEY)")

        def hash_source_line(_line_number: int, line: str) -> None:
            content_hasher.update(line.encode("utf-8"))

        for line_number, case, _canonical, _warnings in _iter_canonical_cases(
            stage_file, on_source_line=hash_source_line
        ):
            try:
                connection.execute("INSERT INTO case_ids (case_id) VALUES (?)", (case.case_id,))
            except sqlite3.IntegrityError as exc:
                raise DatasetSuiteError(
                    f"line {line_number}: duplicate case ID {case.case_id!r}; "
                    "suite versions require unique explicit IDs"
                ) from exc
            case_count += 1
        if case_count == 0:
            raise DatasetSuiteError("suite source contains no dataset records")
        connection.close()
        connection = None

        content_hash_value = "sha256:" + content_hasher.hexdigest()
        registration_lock.acquire()
        lock_acquired = True
        try:
            database = Database.open_workspace(workspace)
            storage = Storage(database)
            existing_record = storage.get_dataset_suite(name, version)
            if (
                existing_record is not None
                and existing_record.dataset_content_hash != content_hash_value
            ):
                raise DatasetSuiteError(
                    f"suite version {name}@{version} already has a different snapshot"
                )

            if snapshot.is_symlink():
                raise DatasetSuiteError(f"suite snapshot path is a symbolic link: {snapshot}")
            if snapshot.exists():
                if not snapshot.is_file():
                    raise DatasetSuiteError(
                        f"suite snapshot path is not a regular file: {snapshot}"
                    )
                try:
                    existing_hash = _content_hash(snapshot)
                except DatasetSuiteError:
                    if existing_record is not None:
                        raise
                    # A previous process may have stopped after publishing the file but
                    # before committing the catalog row. It is safe to replace only this
                    # unreferenced orphan, while holding the version lock.
                    snapshot.unlink()
                else:
                    if existing_hash != content_hash_value:
                        if existing_record is not None:
                            raise DatasetSuiteError(
                                f"suite version {name}@{version} already has a different snapshot"
                            )
                        snapshot.unlink()

            if not snapshot.exists():
                try:
                    os.link(stage_file, snapshot)
                    snapshot_created = True
                except FileExistsError:
                    # This should only be possible for a writer that does not honor our
                    # lock. Recheck safely instead of replacing an unknown concurrent file.
                    if snapshot.is_symlink() or _content_hash(snapshot) != content_hash_value:
                        raise DatasetSuiteError(
                            f"suite version {name}@{version} already has a different snapshot"
                        )
                except OSError as exc:
                    raise DatasetSuiteError(f"could not publish suite snapshot: {exc}") from exc

            relative_snapshot = snapshot.relative_to(workspace.root).as_posix()
            created = storage.register_dataset_suite(
                suite_name=name,
                suite_version=version,
                dataset_content_hash=content_hash_value,
                dataset_path=relative_snapshot,
                case_count=case_count,
                description=description,
            )
            registered = True
            record = storage.get_dataset_suite(name, version)
            assert record is not None
        finally:
            if database is not None:
                database.close()
            database = None
    except BaseException as exc:
        if snapshot_created and not registered:
            _remove_snapshot_if_unreferenced(workspace, name, version, snapshot)
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, DatasetSuiteError):
            raise
        if isinstance(exc, AibenchError):
            raise DatasetSuiteError(f"could not register dataset suite: {exc}") from exc
        if isinstance(exc, (DatasetTransformError, OSError, sqlite3.Error)):
            raise DatasetSuiteError(f"could not register dataset suite: {exc}") from exc
        raise
    finally:
        try:
            if database is not None:
                database.close()
            if connection is not None:
                connection.close()
            if staging is not None:
                active_error = sys.exception()
                try:
                    staging.cleanup()
                except OSError as cleanup_error:
                    if registered:
                        cleanup_warning = True
                        cleanup_path = staging.name
                    else:
                        message = (
                            f"temporary suite staging directory {staging.name} could not be removed: "
                            f"{cleanup_error}"
                        )
                        if active_error is not None:
                            raise DatasetSuiteError(f"{active_error}; {message}") from active_error
                        raise DatasetSuiteError(message) from cleanup_error
        finally:
            if lock_acquired:
                registration_lock.release()

    return {
        "registered": True,
        "created": created,
        "suite": _record_payload(record),
        "snapshot": str(workspace.root / record.dataset_path),
        "temporary_cleanup_warning": cleanup_warning,
        "temporary_directory": cleanup_path,
    }


def list_dataset_suites(project_root: Path, *, name: str | None = None) -> list[DatasetSuiteRecord]:
    if name is not None and not _SUITE_NAME.fullmatch(name):
        raise DatasetSuiteError("invalid suite name")
    _root, workspace = _workspace_root(project_root)
    if not workspace.db_path.is_file():
        return []
    try:
        database = Database.open_readonly(workspace.db_path)
        try:
            try:
                return Storage(database).list_dataset_suites(name)
            except sqlite3.OperationalError as exc:
                if "no such table: dataset_suites" in str(exc).lower():
                    return []
                raise
        finally:
            database.close()
    except (OSError, sqlite3.Error) as exc:
        raise DatasetSuiteError(f"could not list dataset suites: {exc}") from exc


def show_dataset_suite(project_root: Path, reference: str) -> dict[str, Any]:
    snapshot, record = resolve_dataset_suite(project_root, reference)
    return {"suite": _record_payload(record), "snapshot": str(snapshot), "available": True}


def resolve_dataset_suite(project_root: Path, reference: str) -> tuple[Path, DatasetSuiteRecord]:
    """Return the validated immutable snapshot selected by a pinned suite reference."""
    name, version = parse_suite_reference(reference)
    record, snapshot, workspace = _lookup_dataset_suite(project_root, name, version)
    if _content_hash(snapshot) != record.dataset_content_hash:
        raise DatasetSuiteError(
            f"registered suite {name}@{version} has changed; restore its workspace snapshot"
        )
    if snapshot.stat().st_size == 0:
        raise DatasetSuiteError(f"registered suite snapshot is empty: {snapshot}")
    try:
        resolved = snapshot.resolve(strict=True)
        if not resolved.is_relative_to(workspace.root.resolve()):
            raise DatasetSuiteError("suite snapshot escapes the workspace")
    except (OSError, RuntimeError) as exc:
        raise DatasetSuiteError(f"could not resolve suite snapshot: {exc}") from exc
    return resolved, record


def _lookup_dataset_suite(
    project_root: Path, name: str, version: str
) -> tuple[DatasetSuiteRecord, Path, Workspace]:
    _root, workspace = _workspace_root(project_root)
    if not workspace.db_path.is_file():
        raise DatasetSuiteError(f"no dataset suite catalog in workspace {workspace.root}")
    try:
        database = Database.open_readonly(workspace.db_path)
        try:
            record = Storage(database).get_dataset_suite(name, version)
        finally:
            database.close()
    except sqlite3.OperationalError as exc:
        if "no such table: dataset_suites" in str(exc).lower():
            raise DatasetSuiteError(
                "workspace has no dataset suite catalog; register a suite with "
                "`aibench dataset suites register` first"
            ) from exc
        raise DatasetSuiteError(f"could not read dataset suite catalog: {exc}") from exc
    except sqlite3.Error as exc:
        raise DatasetSuiteError(f"could not read dataset suite catalog: {exc}") from exc
    if record is None:
        raise DatasetSuiteError(f"no registered dataset suite {name}@{version}")
    relative = PurePosixPath(record.dataset_path)
    windows_relative = PureWindowsPath(record.dataset_path)
    if (
        relative.is_absolute()
        or windows_relative.is_absolute()
        or windows_relative.drive
        or ".." in relative.parts
        or ".." in windows_relative.parts
        or not relative.parts
    ):
        raise DatasetSuiteError("registered suite snapshot path is invalid")
    snapshot = workspace.root.joinpath(*relative.parts)
    if snapshot.is_symlink() or not snapshot.is_file():
        raise DatasetSuiteError(f"registered suite snapshot is missing or unsafe: {snapshot}")
    try:
        resolved = snapshot.resolve(strict=True)
        if not resolved.is_relative_to(workspace.root.resolve()):
            raise DatasetSuiteError("suite snapshot escapes the workspace")
    except (OSError, RuntimeError) as exc:
        raise DatasetSuiteError(f"could not resolve suite snapshot: {exc}") from exc
    return record, resolved, workspace


def _remove_snapshot_if_unreferenced(
    workspace: Workspace, name: str, version: str, snapshot: Path
) -> None:
    """Remove our failed publication unless a committed catalog row already references it."""
    if workspace.db_path.is_file():
        try:
            database = Database.open_readonly(workspace.db_path)
            try:
                if Storage(database).get_dataset_suite(name, version) is not None:
                    return
            except sqlite3.OperationalError:
                pass
            finally:
                database.close()
        except (OSError, sqlite3.Error):
            # If catalog state cannot be inspected safely, retain the file rather than
            # risk deleting a snapshot another completed registration now references.
            return
    try:
        snapshot.unlink(missing_ok=True)
    except OSError:
        pass


def _content_hash(path: Path) -> str:
    hasher = hashlib.sha256()
    try:
        for _line_number, line in iter_jsonl_lines(path):
            hasher.update(line.encode("utf-8"))
    except (AibenchError, OSError, ValueError) as exc:
        raise DatasetSuiteError(f"could not read suite snapshot {path}: {exc}") from exc
    return "sha256:" + hasher.hexdigest()


def _record_payload(record: DatasetSuiteRecord) -> dict[str, Any]:
    return {
        "name": record.suite_name,
        "version": record.suite_version,
        "reference": f"{record.suite_name}@{record.suite_version}",
        "content_hash": record.dataset_content_hash,
        "case_count": record.case_count,
        "description": record.description,
        "created_at": record.created_at,
    }
