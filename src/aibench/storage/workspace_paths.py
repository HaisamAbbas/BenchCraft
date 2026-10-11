"""Preview and safely rebase paths recorded in a moved local workspace."""

from __future__ import annotations

import ntpath
import os
import posixpath
import re
import sqlite3
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from aibench.core.errors import ValidationError
from aibench.core.hashes import content_hash
from aibench.core.models import ArtifactRef
from aibench.core.sessions import BenchmarkSession, DecisionRecord
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.workspace_integrity import _read_regular_file, check_workspace

_MAX_CATALOG_ROWS = 1_000_000
_MAX_CATALOG_BYTES = 1 << 30
_MAX_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}$")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


@dataclass
class _WorkspacePins:
    project: os.stat_result
    metadata: os.stat_result
    artifacts: os.stat_result
    database: os.stat_result
    sidecars: dict[Path, os.stat_result | None]
    database_open: bool = False
    write_transaction_active: bool = False
    descriptor_directory: Path | None = None
    database_descriptor: int | None = None
    descriptor_baseline: dict[int, tuple[str, int, int]] = field(default_factory=dict)
    sidecar_descriptors: dict[Path, int] = field(default_factory=dict)
    windows_handle_baseline: set[int] = field(default_factory=set)
    windows_handle_snapshot_initialized: bool = False
    windows_database_handle: int | None = None
    windows_sidecar_handles: dict[Path, int] = field(default_factory=dict)


@dataclass(frozen=True)
class _OpenHandleSnapshot:
    descriptor_directory: Path | None
    handles: set[int]
    identities: dict[int, tuple[str, int, int]] = field(default_factory=dict)


def migrate_workspace_paths(project_root: Path, *, apply: bool = False) -> dict[str, Any]:
    """Preview or apply path updates after moving a project directory.

    Only mutable workspace references are changed. Run and experiment records and frozen
    session plans are never rewritten. External paths in sessions and decisions are retained.
    """
    root = _project_root(project_root)
    workspace = Workspace.at(root)
    pins = _validate_workspace_paths(root, workspace)
    with _hold_workspace_guards(workspace, pins):
        plan = _perform_path_migration(root, workspace, pins, apply=apply)
    return _report(root, plan, apply=apply)


def _perform_path_migration(
    root: Path, workspace: Workspace, pins: _WorkspacePins, *, apply: bool
) -> dict[str, Any]:
    if apply:
        database = _open_readwrite(workspace.db_path, workspace, pins)
        try:
            connection = database.connection
            connection.execute("BEGIN IMMEDIATE")
            pins.write_transaction_active = True
            try:
                _check_database_path(database, workspace, pins)
                integrity = check_workspace(root)
                _check_database_path(database, workspace, pins)
                plan = _prepare(connection, workspace, root, integrity, apply=True)
                _check_database_path(database, workspace, pins)
                _verify_artifacts(connection, workspace)
                _check_database_path(database, workspace, pins)
                connection.execute("COMMIT")
                pins.write_transaction_active = False
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                pins.write_transaction_active = False
                raise
        finally:
            database.close()
    else:
        _check_workspace_pins(workspace, pins)
        descriptors_before = _descriptor_snapshot()
        database = Database.open_readonly(workspace.db_path)
        try:
            # sqlite3 can defer opening the main database until its first btree access.
            # Force that open before comparing the process's handles with the pinned
            # workspace file, then begin the read snapshot only after the identity check.
            database.connection.execute("PRAGMA page_count").fetchone()
            _pin_open_database_descriptors(workspace, pins, descriptors_before)
            pins.database_open = True
            connection = database.connection
            connection.execute("BEGIN")
            _check_database_path(database, workspace, pins)
            connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            integrity = check_workspace(root)
            _check_database_path(database, workspace, pins)
            plan = _prepare(connection, workspace, root, integrity, apply=False)
            _check_database_path(database, workspace, pins)
            connection.execute("COMMIT")
        except BaseException:
            if database.connection.in_transaction:
                database.connection.execute("ROLLBACK")
            raise
        finally:
            database.close()

    return plan


def _project_root(value: Path) -> Path:
    try:
        root = value.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve project root: {exc}") from exc
    if not root.is_dir():
        raise ValidationError("project root must be an existing directory")
    return root


def _validate_workspace_paths(root: Path, workspace: Workspace) -> _WorkspacePins:
    project = _require_real_directory(root, "project root")
    metadata = _require_real_directory(root / ".aibench", "workspace directory")
    database = _require_regular_file(workspace.db_path, "workspace database")
    artifacts = _require_real_directory(workspace.artifacts_dir, "artifact store")
    sidecars = _validate_sqlite_sidecars(workspace.db_path)
    return _WorkspacePins(project, metadata, artifacts, database, sidecars)


def _require_real_directory(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"{label} is unavailable: {exc}") from exc
    if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"{label} must be a real directory, not a link")
    return metadata


def _require_regular_file(path: Path, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"{label} is unavailable: {exc}") from exc
    if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"{label} must be a regular file, not a link")
    if metadata.st_nlink != 1:
        raise ValidationError(f"{label} must not be hard-linked")
    return metadata


def _is_link(metadata: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & _REPARSE
    )


def _validate_sqlite_sidecars(
    database_path: Path,
) -> dict[Path, os.stat_result | None]:
    sidecars: dict[Path, os.stat_result | None] = {}
    for suffix in ("-wal", "-shm", "-journal"):
        path = database_path.with_name(database_path.name + suffix)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            sidecars[path] = None
            continue
        except OSError as exc:
            raise ValidationError(f"workspace database sidecar is unavailable: {exc}") from exc
        if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(
                f"workspace database sidecar must be a regular file, not a link: {path.name}"
            )
        if metadata.st_nlink != 1:
            raise ValidationError(
                f"workspace database sidecar must not be hard-linked: {path.name}"
            )
        sidecars[path] = metadata
    return sidecars


def _check_workspace_pins(workspace: Workspace, pins: _WorkspacePins) -> None:
    current = (
        _require_real_directory(workspace.root.parent, "project root"),
        _require_real_directory(workspace.root, "workspace directory"),
        _require_real_directory(workspace.artifacts_dir, "artifact store"),
        _require_regular_file(workspace.db_path, "workspace database"),
    )
    expected = (pins.project, pins.metadata, pins.artifacts, pins.database)
    labels = ("project root", "workspace directory", "artifact store", "workspace database")
    for label, observed, pinned in zip(labels, current, expected, strict=True):
        if (observed.st_dev, observed.st_ino) != (pinned.st_dev, pinned.st_ino):
            raise ValidationError(f"{label} changed while migration was running")

    if pins.database_descriptor is not None and pins.descriptor_directory is not None:
        try:
            opened_database = os.fstat(pins.database_descriptor)
        except OSError as exc:
            raise ValidationError(
                f"opened workspace database handle is unavailable: {exc}"
            ) from exc
        if (opened_database.st_dev, opened_database.st_ino) != (
            pins.database.st_dev,
            pins.database.st_ino,
        ):
            raise ValidationError("opened workspace database no longer matches the pinned file")

    _refresh_sqlite_sidecar_descriptors(workspace, pins)

    for path, original in pins.sidecars.items():
        try:
            observed = path.lstat()
        except FileNotFoundError:
            is_journal = path.name.endswith("-journal")
            expected_active = original is not None and (
                not is_journal or not pins.database_open or pins.write_transaction_active
            )
            if path in pins.sidecar_descriptors or expected_active:
                raise ValidationError(f"active workspace database sidecar disappeared: {path.name}")
            # SQLite can remove a rollback journal when a transaction closes.
            continue
        except OSError as exc:
            raise ValidationError(f"workspace database sidecar is unavailable: {exc}") from exc
        if _is_link(observed) or not stat.S_ISREG(observed.st_mode):
            raise ValidationError(
                f"workspace database sidecar must be a regular file, not a link: {path.name}"
            )
        if observed.st_nlink != 1:
            raise ValidationError(
                f"workspace database sidecar must not be hard-linked: {path.name}"
            )
        if original is None:
            pins.sidecars[path] = observed
        elif (observed.st_dev, observed.st_ino) != (original.st_dev, original.st_ino):
            raise ValidationError(
                f"workspace database sidecar changed during migration: {path.name}"
            )
        descriptor = pins.sidecar_descriptors.get(path)
        if descriptor is not None and pins.descriptor_directory is not None:
            try:
                opened_sidecar = (pins.descriptor_directory / str(descriptor)).stat()
            except OSError as exc:
                raise ValidationError(
                    f"opened workspace database sidecar is unavailable: {path.name}"
                ) from exc
            if (opened_sidecar.st_dev, opened_sidecar.st_ino) != (
                observed.st_dev,
                observed.st_ino,
            ):
                raise ValidationError(f"opened workspace database sidecar changed: {path.name}")


def _refresh_sqlite_sidecar_descriptors(workspace: Workspace, pins: _WorkspacePins) -> None:
    if os.name == "nt":
        _refresh_windows_sqlite_handles(workspace, pins)
        return
    directory = pins.descriptor_directory
    if directory is None:
        return
    expected = {
        f"{workspace.db_path.name}-{suffix}": workspace.db_path.with_name(
            f"{workspace.db_path.name}-{suffix}"
        )
        for suffix in ("wal", "shm", "journal")
    }
    for name in os.listdir(directory):
        if not name.isdigit():
            continue
        descriptor = int(name)
        try:
            target, opened = _descriptor_identity(directory, descriptor)
        except OSError:
            continue
        identity = (target, opened.st_dev, opened.st_ino)
        if pins.descriptor_baseline.get(descriptor) == identity:
            continue
        deleted = target.endswith(" (deleted)") or opened.st_nlink == 0
        cleaned_target = target.removesuffix(" (deleted)")
        basename = Path(cleaned_target).name
        sidecar = expected.get(basename)
        if sidecar is None:
            continue
        if os.path.normcase(os.path.normpath(cleaned_target)) != os.path.normcase(
            os.path.normpath(str(sidecar))
        ):
            raise ValidationError(
                f"SQLite opened a database sidecar outside the workspace: {basename}"
            )
        if deleted and not (basename.endswith("-journal") and not pins.write_transaction_active):
            raise ValidationError(f"SQLite is using an unlinked database sidecar: {basename}")
        if deleted:
            continue
        try:
            current = sidecar.lstat()
        except OSError as exc:
            raise ValidationError(
                f"could not verify opened database sidecar {basename}: {exc}"
            ) from exc
        if _is_link(current) or not stat.S_ISREG(current.st_mode):
            raise ValidationError(f"workspace database sidecar is not a regular file: {basename}")
        if current.st_nlink != 1:
            raise ValidationError(f"workspace database sidecar is hard-linked: {basename}")
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise ValidationError(f"opened workspace database sidecar changed: {basename}")
        pins.sidecars[sidecar] = current
        pins.sidecar_descriptors[sidecar] = descriptor


@contextmanager
def _hold_workspace_guards(workspace: Workspace, pins: _WorkspacePins) -> Iterator[None]:
    """On Windows, hold handles that deny rename/delete during SQLite path opens."""
    if os.name != "nt":
        yield
        return

    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("last_access_time", FileTime),
            ("last_write_time", FileTime),
            ("volume_serial", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    invalid_handle = ctypes.c_void_p(-1).value
    handles: list[int] = []
    entries = [
        (workspace.root.parent, pins.project, True),
        (workspace.root, pins.metadata, True),
        (workspace.artifacts_dir, pins.artifacts, True),
        (workspace.db_path, pins.database, False),
    ]
    try:
        for path, pinned, is_directory in entries:
            flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
            if is_directory:
                flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
            handle = create_file(
                str(path),
                0x00000080,  # FILE_READ_ATTRIBUTES
                0x00000001 | 0x00000002,  # FILE_SHARE_READ | FILE_SHARE_WRITE
                None,
                3,  # OPEN_EXISTING
                flags,
                None,
            )
            if handle == invalid_handle:
                raise ValidationError(
                    f"could not pin workspace path {path}: {ctypes.WinError(ctypes.get_last_error())}"
                )
            handles.append(handle)
            info = ByHandleFileInformation()
            if not get_info(handle, ctypes.byref(info)):
                raise ValidationError(
                    f"could not identify workspace path {path}: {ctypes.WinError(ctypes.get_last_error())}"
                )
            file_index = (info.file_index_high << 32) | info.file_index_low
            if info.attributes & _REPARSE or file_index != pinned.st_ino:
                raise ValidationError(
                    f"workspace path changed while migration was starting: {path}"
                )
        _check_workspace_pins(workspace, pins)
        yield
    finally:
        for handle in reversed(handles):
            close_handle(handle)


def _open_readwrite(path: Path, workspace: Workspace, pins: _WorkspacePins) -> Database:
    _check_workspace_pins(workspace, pins)
    descriptors_before = _descriptor_snapshot()
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=rw", uri=True, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA page_count").fetchone()
        database = Database(connection)
        pins.database_open = True
        _pin_open_database_descriptors(workspace, pins, descriptors_before)
        _check_database_path(database, workspace, pins)
        return database
    except BaseException:
        try:
            connection.close()  # type: ignore[possibly-undefined]
        except (NameError, sqlite3.Error):
            pass
        raise


def _descriptor_snapshot() -> _OpenHandleSnapshot:
    if os.name == "nt":
        return _OpenHandleSnapshot(None, _windows_process_handles())
    for candidate in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            descriptors = {int(name) for name in os.listdir(candidate) if name.isdigit()}
        except OSError:
            continue
        identities = {}
        for descriptor in descriptors:
            try:
                target, metadata = _descriptor_identity(candidate, descriptor)
            except OSError:
                continue
            identities[descriptor] = (target, metadata.st_dev, metadata.st_ino)
        return _OpenHandleSnapshot(candidate, descriptors, identities)
    raise ValidationError("cannot verify SQLite file identity: open descriptors are not enumerable")


def _descriptor_target(directory: Path, descriptor: int) -> str:
    """Resolve a process descriptor to its opened path on supported POSIX platforms.

    Darwin exposes descriptor entries under ``/dev/fd`` but does not implement
    ``readlink`` for them. Its ``F_GETPATH`` command provides the same identity path.
    """
    if sys.platform == "darwin":
        import fcntl

        command = getattr(fcntl, "F_GETPATH", None)
        if command is None:
            raise OSError("Darwin does not expose F_GETPATH for descriptor verification")
        raw = fcntl.fcntl(descriptor, command, b"\0" * 1024)
        path = raw.split(b"\0", 1)[0]
        if not path:
            raise OSError("Darwin returned an empty path for an open descriptor")
        return os.fsdecode(path)
    return os.readlink(directory / str(descriptor))


def _descriptor_identity(directory: Path, descriptor: int) -> tuple[str, os.stat_result]:
    """Read a descriptor path and inode from one stable open handle.

    Resolving ``/dev/fd/N`` and then statting that path can observe different files if
    another thread closes and reuses N between the two operations. ``fstat`` brackets
    the path lookup so such a reuse is rejected instead of combining unrelated identity
    data. This matters on macOS, where descriptors are discovered through ``/dev/fd``.
    """
    before = os.fstat(descriptor)
    target = _descriptor_target(directory, descriptor)
    after = os.fstat(descriptor)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise OSError("descriptor changed while its identity was being verified")
    return target, after


def _pin_open_database_descriptors(
    workspace: Workspace,
    pins: _WorkspacePins,
    snapshot: _OpenHandleSnapshot,
) -> None:
    if snapshot.descriptor_directory is None:
        _pin_open_windows_database_handles(workspace, pins, snapshot.handles)
        return
    directory = snapshot.descriptor_directory
    opened = set(os.listdir(directory))
    main_descriptors: list[int] = []
    sidecar_descriptors: dict[Path, int] = {}
    expected_names = {
        workspace.db_path.name: None,
        f"{workspace.db_path.name}-wal": workspace.db_path.with_name(
            f"{workspace.db_path.name}-wal"
        ),
        f"{workspace.db_path.name}-shm": workspace.db_path.with_name(
            f"{workspace.db_path.name}-shm"
        ),
        f"{workspace.db_path.name}-journal": workspace.db_path.with_name(
            f"{workspace.db_path.name}-journal"
        ),
    }
    for name in opened:
        if not name.isdigit():
            continue
        descriptor = int(name)
        try:
            target, metadata = _descriptor_identity(directory, descriptor)
        except OSError:
            continue
        identity = (target, metadata.st_dev, metadata.st_ino)
        # A descriptor number can be re-used after the snapshot. Skip only descriptors
        # whose target and file identity stayed unchanged across SQLite's open.
        if snapshot.identities.get(descriptor) == identity:
            continue
        deleted = target.endswith(" (deleted)") or metadata.st_nlink == 0
        cleaned_target = target.removesuffix(" (deleted)")
        basename = Path(cleaned_target).name
        if basename not in expected_names:
            continue
        expected_path = expected_names[basename] or workspace.db_path
        if os.path.normcase(os.path.normpath(cleaned_target)) != os.path.normcase(
            os.path.normpath(str(expected_path))
        ):
            label = (
                "workspace database" if basename == workspace.db_path.name else "database sidecar"
            )
            raise ValidationError(f"SQLite opened a {label} outside the workspace: {basename}")
        if deleted and not (basename.endswith("-journal") and not pins.write_transaction_active):
            raise ValidationError(f"SQLite opened an unlinked database file: {basename}")
        if deleted:
            continue
        if basename == workspace.db_path.name:
            main_descriptors.append(descriptor)
            if (metadata.st_dev, metadata.st_ino) != (pins.database.st_dev, pins.database.st_ino):
                raise ValidationError("SQLite opened a different workspace database file")
        else:
            sidecar = expected_names[basename]
            assert sidecar is not None
            sidecar_descriptors[sidecar] = descriptor
            pinned = pins.sidecars.get(sidecar)
            if pinned is None:
                pins.sidecars[sidecar] = metadata
            elif (metadata.st_dev, metadata.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise ValidationError(f"SQLite opened a different database sidecar: {basename}")
    if not main_descriptors:
        raise ValidationError("could not uniquely identify SQLite's opened workspace database")
    pins.descriptor_directory = directory
    pins.descriptor_baseline = snapshot.identities
    # Several descriptors can refer to the same SQLite database (for example, an
    # inherited or duplicated handle). Every candidate was checked against the pinned
    # path and inode above, so pin a deterministic one while continuing to inspect all
    # descriptors for replacement or escape attempts.
    pins.database_descriptor = min(main_descriptors)
    pins.sidecar_descriptors.update(sidecar_descriptors)


def _windows_process_handles() -> set[int]:
    import ctypes

    class HandleEntry(ctypes.Structure):
        _fields_ = [
            ("object", ctypes.c_void_p),
            ("process_id", ctypes.c_size_t),
            ("handle_value", ctypes.c_size_t),
            ("granted_access", ctypes.c_ulong),
            ("creator_backtrace_index", ctypes.c_ushort),
            ("object_type_index", ctypes.c_ushort),
            ("handle_attributes", ctypes.c_ulong),
            ("reserved", ctypes.c_ulong),
        ]

    ntdll = ctypes.WinDLL("ntdll")
    query = ntdll.NtQuerySystemInformation
    query.argtypes = (
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.POINTER(ctypes.c_ulong),
    )
    query.restype = ctypes.c_long
    size = 1 << 20
    status_length_mismatch = -1073741820
    for _ in range(8):
        buffer = ctypes.create_string_buffer(size)
        required = ctypes.c_ulong()
        status = query(64, buffer, size, ctypes.byref(required))
        if status >= 0:
            header_size = ctypes.sizeof(ctypes.c_size_t) * 2
            count = ctypes.c_size_t.from_buffer(buffer).value
            entries = ctypes.cast(
                ctypes.addressof(buffer) + header_size, ctypes.POINTER(HandleEntry)
            )
            process_id = os.getpid()
            return {
                int(entries[index].handle_value)
                for index in range(count)
                if entries[index].process_id == process_id
            }
        if status != status_length_mismatch:
            raise ValidationError(
                f"cannot inspect SQLite file handles on Windows (NTSTATUS 0x{status & 0xFFFFFFFF:08x})"
            )
        size = max(size * 2, required.value * 2)
    raise ValidationError("cannot inspect SQLite file handles on Windows: handle table is unstable")


def _windows_file_information(handle: int) -> tuple[tuple[int, int], int, int]:
    import ctypes
    from ctypes import wintypes

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation_time", FileTime),
            ("last_access_time", FileTime),
            ("last_write_time", FileTime),
            ("volume_serial", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = (wintypes.HANDLE, ctypes.POINTER(ByHandleFileInformation))
    get_info.restype = wintypes.BOOL
    info = ByHandleFileInformation()
    if not get_info(wintypes.HANDLE(handle), ctypes.byref(info)):
        raise ValidationError(
            f"could not identify SQLite file handle: {ctypes.WinError(ctypes.get_last_error())}"
        )
    identity = (int(info.volume_serial), (info.file_index_high << 32) | info.file_index_low)
    return identity, int(info.number_of_links), int(info.attributes)


def _windows_final_path(handle: int) -> str:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_path = kernel32.GetFinalPathNameByHandleW
    get_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_path.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(32768)
    length = get_path(wintypes.HANDLE(handle), buffer, len(buffer), 0)
    if length == 0 or length >= len(buffer):
        raise ValidationError(
            f"could not resolve SQLite file handle path: {ctypes.WinError(ctypes.get_last_error())}"
        )
    target = buffer.value
    if target.startswith("\\\\?\\UNC\\"):
        return "\\\\" + target[8:]
    if target.startswith("\\\\?\\"):
        return target[4:]
    return target


def _windows_normalized_path(value: str) -> str:
    return ntpath.normcase(ntpath.normpath(value))


def _pin_open_windows_database_handles(
    workspace: Workspace, pins: _WorkspacePins, before: set[int]
) -> None:
    expected_paths = {
        workspace.db_path.name.casefold(): workspace.db_path,
        f"{workspace.db_path.name}-wal".casefold(): workspace.db_path.with_name(
            f"{workspace.db_path.name}-wal"
        ),
        f"{workspace.db_path.name}-shm".casefold(): workspace.db_path.with_name(
            f"{workspace.db_path.name}-shm"
        ),
        f"{workspace.db_path.name}-journal".casefold(): workspace.db_path.with_name(
            f"{workspace.db_path.name}-journal"
        ),
    }
    current = _windows_process_handles()
    main_handles: list[int] = []
    sidecar_handles: dict[Path, int] = {}
    for handle in current - before:
        try:
            target = _windows_final_path(handle)
        except ValidationError:
            continue
        expected = expected_paths.get(ntpath.basename(target).casefold())
        if expected is None:
            continue
        if _windows_normalized_path(target) != _windows_normalized_path(str(expected)):
            label = "workspace database" if expected == workspace.db_path else "database sidecar"
            raise ValidationError(f"SQLite opened a {label} outside the workspace: {target}")
        identity, links, attributes = _windows_file_information(handle)
        if attributes & _REPARSE:
            raise ValidationError(f"SQLite opened a reparse-point database file: {target}")
        if links != 1:
            raise ValidationError(f"SQLite opened a hard-linked database file: {target}")
        if expected == workspace.db_path:
            if identity[1] != pins.database.st_ino:
                raise ValidationError("SQLite opened a different workspace database file")
            main_handles.append(handle)
        else:
            pinned = pins.sidecars.get(expected)
            if pinned is not None and identity[1] != pinned.st_ino:
                raise ValidationError(
                    f"SQLite opened a different database sidecar: {expected.name}"
                )
            if pinned is None:
                pins.sidecars[expected] = os.stat(target, follow_symlinks=False)
            sidecar_handles[expected] = handle
    if not main_handles:
        raise ValidationError("could not identify SQLite's opened workspace database handle")
    pins.windows_handle_baseline = before
    pins.windows_handle_snapshot_initialized = True
    pins.windows_database_handle = main_handles[0]
    pins.windows_sidecar_handles.update(sidecar_handles)


def _refresh_windows_sqlite_handles(workspace: Workspace, pins: _WorkspacePins) -> None:
    if not pins.windows_handle_snapshot_initialized:
        return
    expected_paths = {
        workspace.db_path.name.casefold(): workspace.db_path,
        f"{workspace.db_path.name}-wal".casefold(): workspace.db_path.with_name(
            f"{workspace.db_path.name}-wal"
        ),
        f"{workspace.db_path.name}-shm".casefold(): workspace.db_path.with_name(
            f"{workspace.db_path.name}-shm"
        ),
        f"{workspace.db_path.name}-journal".casefold(): workspace.db_path.with_name(
            f"{workspace.db_path.name}-journal"
        ),
    }
    current = _windows_process_handles()
    for handle in current - pins.windows_handle_baseline:
        try:
            target = _windows_final_path(handle)
        except ValidationError:
            continue
        expected = expected_paths.get(ntpath.basename(target).casefold())
        if expected is None:
            continue
        if _windows_normalized_path(target) != _windows_normalized_path(str(expected)):
            label = "workspace database" if expected == workspace.db_path else "database sidecar"
            raise ValidationError(f"SQLite opened a {label} outside the workspace: {target}")
        identity, links, attributes = _windows_file_information(handle)
        if attributes & _REPARSE or links != 1:
            raise ValidationError(f"SQLite opened an unsafe database file handle: {target}")
        if expected == workspace.db_path:
            if identity[1] != pins.database.st_ino:
                raise ValidationError("SQLite opened a different workspace database file")
            pins.windows_database_handle = handle
        else:
            pinned = pins.sidecars.get(expected)
            if pinned is None:
                pins.sidecars[expected] = os.stat(target, follow_symlinks=False)
            elif identity[1] != pinned.st_ino:
                raise ValidationError(
                    f"SQLite opened a different database sidecar: {expected.name}"
                )
            pins.windows_sidecar_handles[expected] = handle

    handles_to_check = dict(pins.windows_sidecar_handles)
    if pins.windows_database_handle is None:
        raise ValidationError("SQLite workspace database handle is no longer pinned")
    try:
        database_target = _windows_final_path(pins.windows_database_handle)
    except ValidationError as exc:
        raise ValidationError("opened workspace database handle is unavailable") from exc
    if _windows_normalized_path(database_target) != _windows_normalized_path(
        str(workspace.db_path)
    ):
        raise ValidationError("opened workspace database handle changed")
    database_identity, database_links, database_attributes = _windows_file_information(
        pins.windows_database_handle
    )
    if (
        database_identity[1] != pins.database.st_ino
        or database_links != 1
        or database_attributes & _REPARSE
    ):
        raise ValidationError("opened workspace database handle changed")
    for path, handle in handles_to_check.items():
        try:
            target = _windows_final_path(handle)
        except ValidationError as exc:
            raise ValidationError(
                f"opened workspace database sidecar is unavailable: {path.name}"
            ) from exc
        if _windows_normalized_path(target) != _windows_normalized_path(str(path)):
            raise ValidationError(f"opened workspace database sidecar changed: {path.name}")
        identity, links, attributes = _windows_file_information(handle)
        pinned = pins.sidecars.get(path)
        if pinned is None or identity[1] != pinned.st_ino or links != 1 or attributes & _REPARSE:
            raise ValidationError(f"opened workspace database sidecar changed: {path.name}")


def _check_database_path(
    database: Database,
    workspace: Workspace,
    pins: _WorkspacePins,
) -> None:
    try:
        opened = database.connection.execute("PRAGMA database_list").fetchall()
        main_path = next((str(row[2]) for row in opened if row[1] == "main"), "")
        if not main_path:
            raise ValidationError("workspace database connection has no main database file")
        _check_workspace_pins(workspace, pins)
        if os.path.normcase(str(Path(main_path).resolve())) != os.path.normcase(
            str(workspace.db_path.resolve())
        ):
            raise ValidationError("workspace database path changed while it was being opened")
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        raise ValidationError(f"could not verify workspace database: {exc}") from exc


def _prepare(
    connection: sqlite3.Connection,
    workspace: Workspace,
    root: Path,
    integrity: dict[str, Any],
    *,
    apply: bool,
) -> dict[str, Any]:
    _validate_integrity_report(integrity)
    root_text = str(root)
    counts = _catalog_counts(connection)
    store = ArtifactStore(workspace.artifacts_dir, create=False)
    artifact_updates = 0
    artifact_query = (
        "SELECT artifact_id, digest, uri, mime_type, size_bytes, redaction, run_id, content_hash "
        "FROM artifacts"
    )
    for batch in _paged_rows(connection, artifact_query, "artifact_id"):
        for row in batch:
            artifact_id = str(row["artifact_id"])
            if not _MAX_ARTIFACT_ID.fullmatch(artifact_id):
                raise ValidationError(f"artifact ID is invalid: {artifact_id[:100]}")
            try:
                ref = ArtifactRef.model_validate(
                    {
                        "artifact_id": row["artifact_id"],
                        "digest": row["digest"],
                        "uri": row["uri"],
                        "mime_type": row["mime_type"],
                        "size_bytes": row["size_bytes"],
                        "redaction": row["redaction"],
                        "run_id": row["run_id"],
                    }
                )
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"artifact {artifact_id!r} has invalid catalog data: {exc}"
                ) from exc
            if row["content_hash"] != content_hash(ref.model_dump_json()):
                raise ValidationError(
                    f"artifact {artifact_id!r} catalog hash does not match its record"
                )
            new_uri = store._path_for_digest(ref.digest).as_posix()
            if _normalized_path(ref.uri) != _normalized_path(new_uri):
                if not _is_workspace_artifact_uri(ref.uri):
                    raise ValidationError(
                        f"artifact {artifact_id!r} URI is not rooted in a workspace artifact store"
                    )
                ref = ref.model_copy(update={"uri": new_uri})
                artifact_updates += 1
                if apply:
                    _update_one(
                        connection,
                        "UPDATE artifacts SET uri = ?, content_hash = ? WHERE artifact_id = ?",
                        (ref.uri, content_hash(ref.model_dump_json()), artifact_id),
                        "artifact",
                        artifact_id,
                    )
            store.verify_ref_integrity(ref)

    session_updates = 0
    external_preserved = 0
    decision_updates = 0
    decision_query = (
        "SELECT d.decision_id, d.session_id, d.revision, d.data, "
        "s.revision AS session_revision, s.active_run_id, s.data AS session_data "
        "FROM decision_records AS d JOIN sessions AS s ON s.session_id = d.session_id"
    )
    for batch in _paged_rows(connection, decision_query, "d.decision_id"):
        for row in batch:
            try:
                decision = DecisionRecord.model_validate_json(row["data"])
                session = BenchmarkSession.model_validate_json(row["session_data"])
            except (TypeError, ValueError) as exc:
                raise ValidationError(
                    f"decision {row['decision_id']!r} or its session is invalid: {exc}"
                ) from exc
            if (
                decision.decision_id != row["decision_id"]
                or decision.session_id != row["session_id"]
                or decision.revision != row["revision"]
            ):
                raise ValidationError(
                    f"decision {row['decision_id']!r} catalog columns do not match data"
                )
            if (
                session.session_id != row["session_id"]
                or session.revision != row["session_revision"]
                or session.active_run_id != row["active_run_id"]
            ):
                raise ValidationError(
                    f"session {row['session_id']!r} catalog columns do not match data"
                )
            if session.active_run_id is not None or row["active_run_id"] is not None:
                raise ValidationError(
                    f"session {session.session_id!r} has an active run; finish or detach it before migration"
                )
            previous_root = _absolute_root(session.project_root, session.session_id)
            app_path, app_external = _rebase_or_preserve(
                decision.choices.application, previous_root, root
            )
            dataset_path, dataset_external = _rebase_or_preserve(
                decision.choices.dataset, previous_root, root
            )
            external_preserved += int(app_external) + int(dataset_external)
            choices = decision.choices.model_copy(
                update={"application": app_path, "dataset": dataset_path}
            )
            updated = decision.model_copy(update={"choices": choices})
            _check_frozen_plan_paths(workspace, root, decision, updated)
            serialized = updated.model_dump_json()
            if serialized != row["data"]:
                decision_updates += 1
                if apply:
                    _update_one(
                        connection,
                        "UPDATE decision_records SET data = ? WHERE decision_id = ?",
                        (serialized, decision.decision_id),
                        "decision",
                        decision.decision_id,
                    )

    session_query = "SELECT session_id, revision, active_run_id, data FROM sessions"
    for batch in _paged_rows(connection, session_query, "session_id"):
        for row in batch:
            try:
                session = BenchmarkSession.model_validate_json(row["data"])
            except (TypeError, ValueError) as exc:
                raise ValidationError(f"session {row['session_id']!r} is invalid: {exc}") from exc
            if (
                session.session_id != row["session_id"]
                or session.revision != row["revision"]
                or session.active_run_id != row["active_run_id"]
            ):
                raise ValidationError(
                    f"session {row['session_id']!r} catalog columns do not match data"
                )
            if session.active_run_id is not None or row["active_run_id"] is not None:
                raise ValidationError(
                    f"session {session.session_id!r} has an active run; finish or detach it before migration"
                )
            previous_root = _absolute_root(session.project_root, session.session_id)
            policy_path = session.policy_path
            if policy_path is not None:
                policy_path, was_external = _rebase_or_preserve(policy_path, previous_root, root)
                external_preserved += int(was_external)
            updated_session = session.model_copy(
                update={"project_root": root_text, "policy_path": policy_path}
            )
            serialized = updated_session.model_dump_json()
            if serialized != row["data"]:
                session_updates += 1
                if apply:
                    _update_one(
                        connection,
                        "UPDATE sessions SET data = ? WHERE session_id = ?",
                        (serialized, session.session_id),
                        "session",
                        session.session_id,
                    )

    return {
        "artifact_updates": artifact_updates,
        "session_updates": session_updates,
        "decision_updates": decision_updates,
        "external_paths_preserved": external_preserved,
        "workspace": str(workspace.root),
        "project_root": root_text,
        **counts,
    }


def _catalog_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    queries = {
        "artifacts": (
            "SELECT count(*), COALESCE(sum("
            "length(CAST(artifact_id AS BLOB)) + length(CAST(digest AS BLOB)) + "
            "length(CAST(uri AS BLOB)) + length(CAST(mime_type AS BLOB)) + "
            "length(CAST(size_bytes AS BLOB)) + length(CAST(redaction AS BLOB)) + "
            "COALESCE(length(CAST(run_id AS BLOB)), 0) + "
            "length(CAST(content_hash AS BLOB)) + length(CAST(committed_at AS BLOB))), 0) "
            "FROM artifacts"
        ),
        "sessions": (
            "SELECT count(*), COALESCE(sum("
            "length(CAST(session_id AS BLOB)) + length(CAST(revision AS BLOB)) + "
            "COALESCE(length(CAST(active_run_id AS BLOB)), 0) + "
            "length(CAST(data AS BLOB)) + length(CAST(created_at AS BLOB)) + "
            "length(CAST(updated_at AS BLOB))), 0) FROM sessions"
        ),
        "decision_records": (
            "SELECT count(*), COALESCE(sum("
            "length(CAST(decision_id AS BLOB)) + length(CAST(session_id AS BLOB)) + "
            "length(CAST(revision AS BLOB)) + length(CAST(data AS BLOB)) + "
            "length(CAST(created_at AS BLOB))), 0) FROM decision_records"
        ),
    }
    for table, query in queries.items():
        count, size = connection.execute(query).fetchone()
        if count > _MAX_CATALOG_ROWS or size > _MAX_CATALOG_BYTES:
            raise ValidationError(f"{table} catalog exceeds workspace migration limits")
        counts[f"{table}_scanned"] = int(count)
    return counts


def _paged_rows(
    connection: sqlite3.Connection, select_sql: str, key_column: str
) -> Iterator[list[sqlite3.Row]]:
    """Yield bounded keyset pages so large catalogs do not become million-row lists."""
    last_key: str | None = None
    while True:
        if last_key is None:
            sql = f"{select_sql} ORDER BY {key_column} LIMIT 128"
            rows = connection.execute(sql).fetchall()
        else:
            sql = f"{select_sql} WHERE {key_column} > ? ORDER BY {key_column} LIMIT 128"
            rows = connection.execute(sql, (last_key,)).fetchall()
        if not rows:
            return
        yield rows
        last_key = str(rows[-1][key_column.split(".")[-1]])


def _update_one(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[str, ...],
    label: str,
    identity: str,
) -> None:
    cursor = connection.execute(sql, parameters)
    if cursor.rowcount != 1:
        raise ValidationError(f"{label} {identity!r} changed during migration")


def _validate_integrity_report(report: dict[str, Any]) -> None:
    if report.get("issues_truncated"):
        raise ValidationError("workspace integrity report is truncated; repair it before migration")
    if report.get("checks", {}).get("database", {}).get("status") != "ok":
        raise ValidationError("workspace database integrity must pass before path migration")
    for name in ("session_plans", "dataset_suites"):
        if report.get("checks", {}).get(name, {}).get("status") != "ok":
            raise ValidationError(
                f"workspace {name.replace('_', ' ')} must pass before path migration"
            )
    checks = report.get("checks", {}).get("artifacts", {})
    if checks.get("status") not in {"ok", "issues"}:
        raise ValidationError("workspace artifact catalog could not be checked")
    for issue in report.get("issues", []):
        if issue.get("category") != "artifacts" or issue.get("code") != "artifact_invalid":
            raise ValidationError(
                f"workspace integrity issue must be repaired before path migration: "
                f"{issue.get('category')}/{issue.get('code')} at {issue.get('path')}"
            )
        message = str(issue.get("message", ""))
        if not (
            "does not resolve inside" in message
            or "does not match its content-addressed digest path" in message
        ):
            raise ValidationError(
                f"workspace artifact integrity issue must be repaired before path migration: "
                f"{issue.get('path')} ({message})"
            )


def _absolute_root(value: str, session_id: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"session {session_id!r} has an invalid project root")
    if _path_flavor(value) == "windows":
        windows = PureWindowsPath(value)
        if not windows.is_absolute():
            raise ValidationError(f"session {session_id!r} project root is not absolute")
        return ntpath.normpath(value)
    path = PurePosixPath(value)
    if not path.is_absolute():
        raise ValidationError(f"session {session_id!r} project root is not absolute")
    return posixpath.normpath(value)


def _rebase_or_preserve(value: str, source_root: str, destination_root: Path) -> tuple[str, bool]:
    if not isinstance(value, str) or not value:
        raise ValidationError("session path is empty or invalid")
    source = _path_flavor(source_root)
    target = _path_flavor(value)
    if source == "windows":
        if target == "posix":
            if not PurePosixPath(value).is_absolute():
                raise ValidationError(f"session dependency is not absolute: {value}")
            return value, True
        source_path = PureWindowsPath(source_root)
        value_path = PureWindowsPath(value)
        if not value_path.is_absolute():
            raise ValidationError(f"session dependency is not absolute: {value}")
        try:
            parts = value_path.relative_to(source_path).parts
        except ValueError:
            return value, True
        if any(part in {"", ".", ".."} for part in parts):
            raise ValidationError(f"session dependency escapes its project root: {value}")
        rebased = destination_root.joinpath(*parts).resolve(strict=False)
        if not _is_inside(rebased, destination_root):
            raise ValidationError("session dependency escapes the destination project")
        return str(rebased), False
    if target == "windows":
        if not PureWindowsPath(value).is_absolute():
            raise ValidationError(f"session dependency is not absolute: {value}")
        return value, True
    posix_value_path = PurePosixPath(value)
    if not posix_value_path.is_absolute():
        raise ValidationError(f"session dependency is not absolute: {value}")
    normalized_value = PurePosixPath(posixpath.normpath(value))
    normalized_source = PurePosixPath(posixpath.normpath(source_root))
    try:
        parts = normalized_value.relative_to(normalized_source).parts
    except ValueError:
        return value, True
    if any(part in {"", ".", ".."} for part in parts):
        raise ValidationError(f"session dependency escapes its project root: {value}")
    rebased = destination_root.joinpath(*parts).resolve(strict=False)
    if not _is_inside(rebased, destination_root):
        raise ValidationError("session dependency escapes the destination project")
    return str(rebased), False


def _path_flavor(value: str) -> str:
    path = PureWindowsPath(value)
    return "windows" if path.drive or ("\\" in value and not value.startswith("/")) else "posix"


def _is_inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_workspace_artifact_uri(uri: str) -> bool:
    windows = PureWindowsPath(uri)
    if windows.drive or ("\\" in uri and not uri.startswith("/")):
        path: PureWindowsPath | PurePosixPath = windows
        if not windows.is_absolute():
            return False
    else:
        path = PurePosixPath(uri)
        if not path.is_absolute():
            return False
    parts = path.parts
    if len(parts) < 5:
        return False
    return (
        parts[-4] == ".aibench"
        and parts[-3] == "artifacts"
        and parts[-2] == parts[-1][:2]
        and bool(re.fullmatch(r"[0-9a-f]{64}", parts[-1]))
    )


def _normalized_path(value: str) -> str:
    if _path_flavor(value) == "windows":
        return str(PureWindowsPath(value)).casefold()
    return os.path.normcase(os.path.normpath(value))


def _check_frozen_plan_paths(
    workspace: Workspace,
    destination_root: Path,
    old_decision: DecisionRecord,
    new_decision: DecisionRecord,
) -> None:
    """Ensure an immutable relative plan still resolves to its preserved/rebased inputs."""
    from aibench.core.plans import ExecutablePlan
    from aibench.engine.compile import freeze_plan

    session_dir = workspace.root / "sessions" / old_decision.session_id
    plan_path = session_dir / old_decision.plan_file
    try:
        raw = _read_regular_file(plan_path, 32 * 1024 * 1024)
        plan = ExecutablePlan.model_validate_json(raw)
        _, digest = freeze_plan(plan)
    except (OSError, ValueError) as exc:
        raise ValidationError(
            f"could not verify frozen plan for decision {old_decision.decision_id!r}: {exc}"
        ) from exc
    if plan.plan_id != old_decision.session_id or digest != old_decision.plan_hash:
        raise ValidationError(f"frozen plan for decision {old_decision.decision_id!r} has changed")

    new_base = destination_root / ".aibench" / "sessions" / old_decision.session_id
    for label, plan_value, new_choice in (
        ("application", plan.application, new_decision.choices.application),
        ("dataset", plan.dataset, new_decision.choices.dataset),
    ):
        new_resolved = _resolve_plan_reference(new_base, plan_value)
        if new_resolved != _normalize_reference(new_choice):
            raise ValidationError(
                f"moving this workspace would change the frozen plan's {label} path; "
                "the immutable plan cannot be rewritten"
            )


def _resolve_plan_reference(base: Path, value: str) -> str:
    if _path_flavor(value) == "windows":
        windows_path = PureWindowsPath(ntpath.normpath(value))
        if windows_path.is_absolute():
            return _normalize_reference(str(windows_path))
        if windows_path.drive or windows_path.root:
            raise ValidationError(f"cannot safely resolve drive-relative frozen-plan path: {value}")
        return _normalize_reference(os.path.normpath(str(base.joinpath(*windows_path.parts))))

    posix_path = PurePosixPath(posixpath.normpath(value))
    if posix_path.is_absolute():
        return _normalize_reference(str(posix_path))
    return _normalize_reference(os.path.normpath(str(base / Path(*posix_path.parts))))


def _normalize_reference(value: str) -> str:
    if _path_flavor(value) == "windows":
        return str(PureWindowsPath(ntpath.normpath(value))).casefold()
    return posixpath.normpath(value)


def _verify_artifacts(connection: sqlite3.Connection, workspace: Workspace) -> None:
    store = ArtifactStore(workspace.artifacts_dir, create=False)
    query = (
        "SELECT artifact_id, digest, uri, mime_type, size_bytes, redaction, run_id, content_hash "
        "FROM artifacts"
    )
    for batch in _paged_rows(connection, query, "artifact_id"):
        for row in batch:
            ref = ArtifactRef.model_validate(
                {
                    "artifact_id": row["artifact_id"],
                    "digest": row["digest"],
                    "uri": row["uri"],
                    "mime_type": row["mime_type"],
                    "size_bytes": row["size_bytes"],
                    "redaction": row["redaction"],
                    "run_id": row["run_id"],
                }
            )
            if row["content_hash"] != content_hash(ref.model_dump_json()):
                raise ValidationError(
                    f"artifact {ref.artifact_id!r} catalog hash changed during migration"
                )
            store.verify_ref_integrity(ref)


def _report(root: Path, plan: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    updated = plan["artifact_updates"] + plan["session_updates"] + plan["decision_updates"]
    return {
        "mode": "applied" if apply else "dry_run",
        "workspace": plan["workspace"],
        "project_root": str(root),
        "artifact_records_scanned": plan["artifacts_scanned"],
        "session_records_scanned": plan["sessions_scanned"],
        "decision_records_scanned": plan["decision_records_scanned"],
        "artifact_uris_to_update": plan["artifact_updates"],
        "sessions_to_update": plan["session_updates"],
        "decisions_to_update": plan["decision_updates"],
        "external_paths_preserved": plan["external_paths_preserved"],
        "records_updated": updated if apply else 0,
        "changed_if_applied": updated,
        "ok": True,
    }
