"""Artifact commit protocol (02-T2, §14).

Two independent phases, matching the specification exactly: "write temporary bytes, flush
and atomically rename on the same filesystem, then commit the reference transaction. A crash
can leave an orphan artifact, which garbage collection can remove after a grace period. A
result must never reference a half-written artifact."

Phase 1 (`ArtifactStore.write_bytes`) writes to a temp file in the same target directory,
flushes and fsyncs it, then `os.replace`s it onto the content-addressed final path — an
atomic rename on the same filesystem, so the final path is always either absent or complete;
there is no window where a reader can see a partially written file at that path. If this
phase raises (simulating a crash during the write), the final path was never created and no
`ArtifactRef` is returned, so phase 2 can never run.

Phase 2 (`commit_verified_artifact`, below) re-verifies the `ArtifactRef` against the real
file — path-safe, exists, correct size and digest — and only then commits it via
`Storage.commit_artifact_unverified` (`repositories.py`), a separate, ordinary transactional
DB insert. `commit_artifact_unverified` is deliberately named to make clear it performs no
filesystem check itself; `commit_verified_artifact` is the path real callers should use, so a
forged or stale reference can never reach the database. A crash between the two phases leaves
an orphan file on disk (bytes present, no DB reference) — expected, and cleaned up by
`ArtifactStore.garbage_collect` after `grace_seconds`, guarded against deleting anything still
referenced in the DB or written too recently to safely assume it is orphaned.
"""

from __future__ import annotations

import math
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from aibench.core.errors import ValidationError
from aibench.core.hashes import bytes_hash
from aibench.core.models import ArtifactRef, RedactionClass

if TYPE_CHECKING:
    from aibench.storage.repositories import Storage

DEFAULT_MAX_ARTIFACT_BYTES = 200 * 1024 * 1024  # 200 MB
DEFAULT_GC_GRACE_SECONDS = 3600.0
TEMP_PREFIX = ".tmp-"

# Captures are written from worker threads (16-T4). Identical bytes share one final path,
# and on Windows replacing a file another thread has open fails, so writers of the same
# digest take turns within a process.
_DIGEST_LOCKS: dict[str, threading.Lock] = {}
_DIGEST_LOCKS_GUARD = threading.Lock()


def _digest_lock(digest: str) -> threading.Lock:
    with _DIGEST_LOCKS_GUARD:
        return _DIGEST_LOCKS.setdefault(digest, threading.Lock())


@contextmanager
def _artifact_digest_lock(artifacts_dir: Path, digest: str) -> Iterator[None]:
    """Lock one digest bucket in this process and across other workspace processes.

    Persistent lock files are sharded by the first digest byte, bounding workspace lock-file
    growth to 256 files even when a benchmark writes millions of distinct artifacts.
    """
    hex_digest = digest.split(":", 1)[-1]
    if re.fullmatch(r"[0-9a-f]{64}", hex_digest) is None:
        raise ValidationError("invalid artifact digest for storage lock")
    _require_real_directory(artifacts_dir, "artifact store")
    lock_name = hex_digest[:2]
    lock_key = f"{artifacts_dir.resolve()}:{lock_name}"
    with _digest_lock(lock_key):
        lock_dir = artifacts_dir / ".locks"
        lock_dir.mkdir(exist_ok=True)
        lock_dir_metadata = lock_dir.lstat()
        if _is_reparse_or_symlink(lock_dir, lock_dir_metadata) or not stat.S_ISDIR(
            lock_dir_metadata.st_mode
        ):
            raise ValidationError("artifact lock directory must be a real directory")
        lock_file = _open_artifact_lock_file_without_following(lock_dir / f"{lock_name}.lock")
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

                fcntl_api: Any = fcntl
                fcntl_api.flock(lock_file.fileno(), fcntl_api.LOCK_EX)
            try:
                yield
            finally:
                lock_file.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl_api = fcntl
                    fcntl_api.flock(lock_file.fileno(), fcntl_api.LOCK_UN)
        finally:
            lock_file.close()


def _open_artifact_lock_file_without_following(path: Path) -> BinaryIO:
    """Open a regular per-digest lock file without following links or reparse points."""
    if os.name == "nt":
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

        generic_read_write = 0x80000000 | 0x40000000
        share_read_write = 0x00000001 | 0x00000002
        open_always = 4
        file_attribute_normal = 0x00000080
        open_reparse_point = 0x00200000
        reparse_point = 0x00000400
        handle = create_file(
            str(path),
            generic_read_write,
            share_read_write,
            None,
            open_always,
            file_attribute_normal | open_reparse_point,
            None,
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        descriptor: int | None = None
        try:
            info = FileAttributeTagInfo()
            if not get_file_info(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & reparse_point:
                raise OSError("artifact lock cannot be a symbolic link or reparse point")
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDWR | os.O_APPEND | os.O_BINARY)
            handle = None
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise OSError("artifact lock must be a private regular file")
            lock_file = os.fdopen(descriptor, "a+b")
            descriptor = None
            return lock_file
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if handle is not None:
                close_handle(handle)

    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("this platform cannot safely open artifact lock files")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | no_follow, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("artifact lock must be a private regular file")
        return os.fdopen(descriptor, "a+b")
    except BaseException:
        os.close(descriptor)
        raise


@dataclass
class GarbageCollectionReport:
    candidate_paths: list[Path]
    removed_paths: list[Path]
    kept_referenced: int
    kept_within_grace: int
    skipped_unsafe: int
    scanned_files: int
    bytes_reclaimable: int
    bytes_removed: int


class ArtifactStore:
    """Content-addressed artifact storage under `<workspace>/artifacts/`, sharded by the
    first two hex digest characters to avoid one huge flat directory."""

    def __init__(
        self,
        artifacts_dir: Path,
        *,
        max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES,
        create: bool = True,
    ) -> None:
        self.artifacts_dir = artifacts_dir
        self.max_bytes = max_bytes
        if create:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_digest(self, digest: str) -> Path:
        prefix, separator, hex_digest = digest.partition(":")
        if (
            separator != ":"
            or prefix != "sha256"
            or re.fullmatch(r"[0-9a-f]{64}", hex_digest) is None
        ):
            raise ValidationError(f"invalid artifact digest: {digest!r}")
        shard = hex_digest[:2]
        return self.artifacts_dir / shard / hex_digest

    def _resolve_and_validate_uri(self, uri: str) -> Path:
        """Reject any `ArtifactRef.uri` that does not resolve to a path inside
        `artifacts_dir` — a forged, symlinked, or otherwise out-of-root URI (e.g.
        `../../etc/passwd`, an absolute path elsewhere, or a path containing a symlink that
        escapes the store) must never be read or trusted as an artifact."""
        try:
            resolved = Path(uri).resolve(strict=False)
        except (OSError, ValueError) as exc:
            raise ValidationError(f"artifact uri could not be resolved: {uri!r}") from exc
        artifacts_root = self.artifacts_dir.resolve()
        try:
            resolved.relative_to(artifacts_root)
        except ValueError as exc:
            raise ValidationError(
                f"artifact uri {uri!r} does not resolve inside {artifacts_root}"
            ) from exc
        return resolved

    def write_bytes(
        self,
        data: bytes,
        *,
        mime_type: str,
        run_id: str | None = None,
        redaction: RedactionClass = RedactionClass.NONE,
        artifact_id: str | None = None,
    ) -> ArtifactRef:
        """Phase 1: durably and atomically write `data`, returning the `ArtifactRef` to
        commit in phase 2. Never partially visible at its final path (see module docstring).
        """
        if len(data) > self.max_bytes:
            raise ValidationError(
                f"artifact of {len(data)} bytes exceeds the {self.max_bytes}-byte limit"
            )

        digest = bytes_hash(data)
        target_path = self._path_for_digest(digest)
        _require_real_directory(self.artifacts_dir, "artifact store")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        _require_real_directory(target_path.parent, "artifact shard")

        with _artifact_digest_lock(self.artifacts_dir, digest):
            try:
                metadata = target_path.lstat()
            except FileNotFoundError:
                self._write_new(data, target_path)
            else:
                if (
                    _is_reparse_or_symlink(target_path, metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    raise ValidationError("artifact object path must be a private regular file")
                try:
                    # Refresh the grace window when content-addressed bytes are reused. A
                    # maintenance process must not mistake a stale orphan being recommitted
                    # for an abandoned file between this write and its catalog transaction.
                    _refresh_artifact_mtime_without_following(target_path)
                except FileNotFoundError:
                    # A concurrent GC removed the stale orphan after the lstat. Publish it
                    # again atomically before returning a reference.
                    self._write_new(data, target_path)
                except OSError as exc:
                    raise ValidationError(
                        f"could not refresh artifact grace period: {exc}"
                    ) from exc

        return ArtifactRef(
            artifact_id=artifact_id or uuid.uuid4().hex,
            digest=digest,
            uri=target_path.as_posix(),
            mime_type=mime_type,
            size_bytes=len(data),
            redaction=redaction,
            run_id=run_id,
        )

    @staticmethod
    def _write_new(data: bytes, target_path: Path) -> None:
        fd, tmp_name = tempfile.mkstemp(
            dir=target_path.parent,
            prefix=f"{TEMP_PREFIX}{target_path.name}-",
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.replace(tmp_name, target_path)  # atomic on the same filesystem
            except PermissionError:
                # Another process committed the same digest first and has it open (Windows
                # refuses the replace). Its bytes are identical by construction.
                if not target_path.is_file():
                    raise
                os.unlink(tmp_name)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def verify_ref(self, ref: ArtifactRef) -> bytes:
        """Validate that `ref` truthfully describes a real file in this store — path inside
        `artifacts_dir`, file exists, size matches, and content hashes to `ref.digest` —
        and return its bytes. Raises `ValidationError` on any mismatch (forged metadata,
        tampered/truncated file, or a `uri` outside the store). This is the check the
        artifact protocol requires "before committing or reading" (§14); `write_bytes`
        already guarantees it by construction for refs it produced itself, but any
        `ArtifactRef` a caller did not just receive from `write_bytes` — including one about
        to be committed to storage, or one loaded back out of storage — must be verified
        before it is trusted.
        """
        path = self._resolve_and_validate_uri(ref.uri)
        canonical_path = self._path_for_digest(ref.digest)
        supplied_path = Path(os.path.abspath(ref.uri))
        canonical_absolute_path = Path(os.path.abspath(canonical_path))
        if os.path.normcase(supplied_path) != os.path.normcase(canonical_absolute_path):
            raise ValidationError("artifact uri does not match its content-addressed digest path")

        _require_real_directory(self.artifacts_dir, "artifact store")
        try:
            shard_metadata = canonical_path.parent.lstat()
        except FileNotFoundError as exc:
            raise ValidationError(f"artifact file missing: {canonical_path}") from exc
        if _is_reparse_or_symlink(canonical_path.parent, shard_metadata) or not stat.S_ISDIR(
            shard_metadata.st_mode
        ):
            raise ValidationError("artifact shard must be a real directory, not a link or file")
        try:
            file_metadata = canonical_path.lstat()
        except FileNotFoundError as exc:
            raise ValidationError(f"artifact file missing: {canonical_path}") from exc
        if (
            _is_reparse_or_symlink(canonical_path, file_metadata)
            or not stat.S_ISREG(file_metadata.st_mode)
            or file_metadata.st_nlink != 1
        ):
            raise ValidationError("artifact object path must be a private regular file")

        actual_size = file_metadata.st_size
        if actual_size != ref.size_bytes:
            raise ValidationError(
                f"artifact {ref.artifact_id!r} size mismatch: ref says {ref.size_bytes}, "
                f"file is {actual_size} bytes"
            )
        data = path.read_bytes()
        actual_digest = bytes_hash(data)
        if actual_digest != ref.digest:
            raise ValidationError(
                f"artifact {ref.artifact_id!r} digest mismatch: ref says {ref.digest}, "
                f"file hashes to {actual_digest}"
            )
        return data

    def read_bytes(self, ref: ArtifactRef) -> bytes:
        """Verified read: never trusts `ref.uri` blindly (see `verify_ref`)."""
        return self.verify_ref(ref)

    def exists(self, digest: str) -> bool:
        return self._path_for_digest(digest).exists()

    def garbage_collect(
        self,
        referenced_digests: set[str],
        *,
        grace_seconds: float = DEFAULT_GC_GRACE_SECONDS,
        dry_run: bool = False,
        expected_parent_metadata: os.stat_result | None = None,
    ) -> GarbageCollectionReport:
        """Remove files under `artifacts_dir` that are neither referenced in
        `referenced_digests` (a live query of `Storage.referenced_artifact_digests()`) nor
        younger than `grace_seconds` — the grace period protects a file that was just
        written by a concurrent, not-yet-committed `write_bytes` call from being deleted out
        from under it. Never removes a referenced artifact, regardless of age."""
        if not math.isfinite(grace_seconds) or grace_seconds < 0:
            raise ValidationError("garbage-collection grace period must be finite and non-negative")

        candidates: list[Path] = []
        removed: list[Path] = []
        deletion_candidates: list[tuple[Path, os.stat_result, Path, os.stat_result]] = []
        kept_referenced = 0
        kept_within_grace = 0
        skipped_unsafe = 0
        scanned_files = 0
        bytes_reclaimable = 0
        bytes_removed = 0
        now = time.time()

        try:
            root_metadata = self.artifacts_dir.lstat()
        except FileNotFoundError:
            return GarbageCollectionReport(
                candidates,
                removed,
                kept_referenced,
                kept_within_grace,
                skipped_unsafe,
                scanned_files,
                bytes_reclaimable,
                bytes_removed,
            )
        except OSError as exc:
            raise ValidationError(f"could not inspect artifact store: {exc}") from exc
        if _is_reparse_or_symlink(self.artifacts_dir, root_metadata) or not stat.S_ISDIR(
            root_metadata.st_mode
        ):
            raise ValidationError("artifact store must be a real directory, not a link or file")
        try:
            parent_metadata = self.artifacts_dir.parent.lstat()
        except OSError as exc:
            raise ValidationError(f"could not inspect artifact store parent: {exc}") from exc
        if (
            _is_reparse_or_symlink(self.artifacts_dir.parent, parent_metadata)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or (
                expected_parent_metadata is not None
                and not _same_inode(parent_metadata, expected_parent_metadata)
            )
        ):
            raise ValidationError("artifact store parent changed or is not a real directory")

        for shard_dir in self.artifacts_dir.iterdir():
            if len(shard_dir.name) != 2 or re.fullmatch(r"[0-9a-f]{2}", shard_dir.name) is None:
                continue
            try:
                shard_metadata = shard_dir.lstat()
            except OSError:
                skipped_unsafe += 1
                continue
            if _is_reparse_or_symlink(shard_dir, shard_metadata):
                skipped_unsafe += 1
                continue
            if not stat.S_ISDIR(shard_metadata.st_mode):
                continue
            if shard_metadata.st_dev != root_metadata.st_dev:
                skipped_unsafe += 1
                continue
            for file_path in shard_dir.iterdir():
                try:
                    metadata = file_path.lstat()
                except OSError:
                    skipped_unsafe += 1
                    continue
                if (
                    _is_reparse_or_symlink(file_path, metadata)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                ):
                    skipped_unsafe += 1
                    continue
                if file_path.name.startswith(TEMP_PREFIX):
                    pass
                elif not _is_artifact_object_name(shard_dir.name, file_path.name):
                    continue
                scanned_files += 1
                age = now - metadata.st_mtime
                if file_path.name.startswith(TEMP_PREFIX):
                    # An orphaned temp file from an interrupted write (phase 1 crashed
                    # before the atomic rename). Never referenced by definition.
                    if age >= grace_seconds:
                        candidates.append(file_path)
                        deletion_candidates.append((shard_dir, shard_metadata, file_path, metadata))
                        bytes_reclaimable += metadata.st_size
                    else:
                        kept_within_grace += 1
                    continue

                digest = f"sha256:{file_path.name}"
                if digest in referenced_digests:
                    kept_referenced += 1
                    continue
                if age < grace_seconds:
                    kept_within_grace += 1
                    continue
                candidates.append(file_path)
                deletion_candidates.append((shard_dir, shard_metadata, file_path, metadata))
                bytes_reclaimable += metadata.st_size

        if not dry_run:
            for shard_dir, shard_metadata, file_path, metadata in deletion_candidates:
                try:
                    did_remove = _unlink_if_still_regular(
                        self.artifacts_dir,
                        root_metadata,
                        parent_metadata,
                        shard_dir,
                        shard_metadata,
                        file_path,
                        metadata,
                    )
                except (OSError, ValidationError):
                    skipped_unsafe += 1
                    continue
                if did_remove:
                    removed.append(file_path)
                    bytes_removed += metadata.st_size
                else:
                    skipped_unsafe += 1

        return GarbageCollectionReport(
            candidates,
            removed,
            kept_referenced,
            kept_within_grace,
            skipped_unsafe,
            scanned_files,
            bytes_reclaimable,
            bytes_removed,
        )


def _is_reparse_or_symlink(path: Path, metadata: os.stat_result) -> bool:
    if stat.S_ISLNK(metadata.st_mode) or path.is_symlink():
        return True
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    file_attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(file_attributes & reparse_attribute)


def _require_real_directory(path: Path, description: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise ValidationError(f"could not inspect {description}: {exc}") from exc
    if _is_reparse_or_symlink(path, metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"{description} must be a real directory, not a link or file")
    return metadata


def _refresh_artifact_mtime_without_following(path: Path) -> None:
    if os.name != "nt":
        os.utime(path, None, follow_symlinks=False)
        return

    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    class FileStandardInfo(ctypes.Structure):
        _fields_ = [
            ("allocation_size", ctypes.c_longlong),
            ("end_of_file", ctypes.c_longlong),
            ("number_of_links", wintypes.DWORD),
            ("delete_pending", ctypes.c_ubyte),
            ("directory", ctypes.c_ubyte),
        ]

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
    get_system_time = kernel32.GetSystemTimeAsFileTime
    get_system_time.argtypes = [ctypes.POINTER(wintypes.FILETIME)]
    get_system_time.restype = None
    set_file_time = kernel32.SetFileTime
    set_file_time.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    set_file_time.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    file_write_attributes = 0x00000100
    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    file_attribute_normal = 0x00000080
    file_flag_open_reparse_point = 0x00200000
    file_attribute_reparse_point = 0x00000400
    handle = create_file(
        str(path),
        file_write_attributes,
        share_read_write,
        None,
        open_existing,
        file_attribute_normal | file_flag_open_reparse_point,
        None,
    )
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = FileAttributeTagInfo()
        if not get_file_info(handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            raise ctypes.WinError(ctypes.get_last_error())
        standard = FileStandardInfo()
        if not get_file_info(handle, 1, ctypes.byref(standard), ctypes.sizeof(standard)):
            raise ctypes.WinError(ctypes.get_last_error())
        if attributes.attributes & file_attribute_reparse_point:
            raise ValidationError("artifact object path is a symbolic link or reparse point")
        if standard.directory or standard.number_of_links != 1:
            raise ValidationError("artifact object path must be a private regular file")
        now = wintypes.FILETIME()
        get_system_time(ctypes.byref(now))
        if not set_file_time(handle, None, None, ctypes.byref(now)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        close_handle(handle)


def _is_artifact_object_name(shard: str, name: str) -> bool:
    return (
        len(shard) == 2
        and re.fullmatch(r"[0-9a-f]{2}", shard) is not None
        and len(name) == 64
        and re.fullmatch(r"[0-9a-f]{64}", name) is not None
        and name.startswith(shard)
    )


def _unlink_if_still_regular(
    root: Path,
    expected_root: os.stat_result,
    expected_parent: os.stat_result,
    shard_dir: Path,
    expected_shard: os.stat_result,
    path: Path,
    expected_file: os.stat_result,
) -> bool:
    """Serialize with writers in every process and recheck before deleting a candidate."""
    if path.name.startswith(TEMP_PREFIX):
        temp_digest = _temporary_file_digest(path.name)
        if temp_digest is None:
            return _unlink_if_still_regular_locked(
                root,
                expected_root,
                expected_parent,
                shard_dir,
                expected_shard,
                path,
                expected_file,
            )
        with _artifact_digest_lock(root, f"sha256:{temp_digest}"):
            return _unlink_if_still_regular_locked(
                root,
                expected_root,
                expected_parent,
                shard_dir,
                expected_shard,
                path,
                expected_file,
            )
    with _artifact_digest_lock(root, f"sha256:{path.name}"):
        return _unlink_if_still_regular_locked(
            root,
            expected_root,
            expected_parent,
            shard_dir,
            expected_shard,
            path,
            expected_file,
        )


def _temporary_file_digest(name: str) -> str | None:
    if not name.startswith(TEMP_PREFIX):
        return None
    candidate = name[len(TEMP_PREFIX) :].split("-", 1)[0]
    return candidate if re.fullmatch(r"[0-9a-f]{64}", candidate) else None


def _unlink_if_still_regular_locked(
    root: Path,
    expected_root: os.stat_result,
    expected_parent: os.stat_result,
    shard_dir: Path,
    expected_shard: os.stat_result,
    path: Path,
    expected_file: os.stat_result,
) -> bool:
    if os.name == "nt":
        return _unlink_windows_anchored(
            root,
            expected_root,
            expected_parent,
            shard_dir,
            expected_shard,
            path,
            expected_file,
        )
    return _unlink_posix_anchored(
        root,
        expected_root,
        expected_parent,
        shard_dir,
        expected_shard,
        path,
        expected_file,
    )


def _unlink_posix_anchored(
    root: Path,
    expected_root: os.stat_result,
    expected_parent: os.stat_result,
    shard_dir: Path,
    expected_shard: os.stat_result,
    path: Path,
    expected_file: os.stat_result,
) -> bool:
    """Delete relative to pinned, no-follow directory descriptors on POSIX."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None:
        return False
    project_fd: int | None = None
    parent_fd: int | None = None
    root_fd: int | None = None
    shard_fd: int | None = None
    try:
        project_fd = os.open(root.parent.parent, os.O_RDONLY | directory | no_follow)
        parent_fd = os.open(
            root.parent.name,
            os.O_RDONLY | directory | no_follow,
            dir_fd=project_fd,
        )
        parent_metadata = os.fstat(parent_fd)
        if not stat.S_ISDIR(parent_metadata.st_mode) or not _same_inode(
            parent_metadata, expected_parent
        ):
            return False

        root_fd = os.open(
            root.name,
            os.O_RDONLY | directory | no_follow,
            dir_fd=parent_fd,
        )
        root_metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(root_metadata.st_mode) or not _same_inode(root_metadata, expected_root):
            return False

        shard_fd = os.open(
            shard_dir.name,
            os.O_RDONLY | directory | no_follow,
            dir_fd=root_fd,
        )
        shard_metadata = os.fstat(shard_fd)
        if (
            not stat.S_ISDIR(shard_metadata.st_mode)
            or shard_metadata.st_dev != root_metadata.st_dev
            or not _same_inode(shard_metadata, expected_shard)
        ):
            return False

        file_metadata = os.stat(path.name, dir_fd=shard_fd, follow_symlinks=False)
        if (
            stat.S_ISLNK(file_metadata.st_mode)
            or not stat.S_ISREG(file_metadata.st_mode)
            or file_metadata.st_nlink != 1
            or not _same_file_identity(file_metadata, expected_file)
        ):
            return False
        os.unlink(path.name, dir_fd=shard_fd)
        return True
    except OSError:
        return False
    finally:
        if shard_fd is not None:
            os.close(shard_fd)
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        if project_fd is not None:
            os.close(project_fd)


def _unlink_windows_anchored(
    root: Path,
    expected_root: os.stat_result,
    expected_parent: os.stat_result,
    shard_dir: Path,
    expected_shard: os.stat_result,
    path: Path,
    expected_file: os.stat_result,
) -> bool:
    """Delete by handle while no-delete-share directory handles pin its parents."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    class FileStandardInfo(ctypes.Structure):
        _fields_ = [
            ("allocation_size", ctypes.c_longlong),
            ("end_of_file", ctypes.c_longlong),
            ("number_of_links", wintypes.DWORD),
            ("delete_pending", ctypes.c_ubyte),
            ("directory", ctypes.c_ubyte),
        ]

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", ctypes.c_ubyte)]

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
    set_file_info = kernel32.SetFileInformationByHandle
    set_file_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_file_info.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    share_read_write = 0x00000001 | 0x00000002
    open_existing = 3
    open_reparse_point = 0x00200000
    backup_semantics = 0x02000000
    reparse_point = 0x00000400
    delete_access = 0x00010000
    read_attributes = 0x00000080
    list_directory = 0x00000001

    def open_descriptor(target: Path, access: int, flags: int) -> int:
        handle = create_file(
            str(target),
            access,
            share_read_write,  # Excluding FILE_SHARE_DELETE pins the opened entry.
            None,
            open_existing,
            open_reparse_point | flags,
            None,
        )
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            handle = None
            return descriptor
        finally:
            if handle is not None:
                close_handle(handle)

    descriptors: list[int] = []
    try:
        project_fd = open_descriptor(
            root.parent.parent,
            read_attributes | list_directory,
            backup_semantics,
        )
        descriptors.append(project_fd)
        parent_fd = open_descriptor(
            root.parent,
            read_attributes | list_directory,
            backup_semantics,
        )
        descriptors.append(parent_fd)
        parent_metadata = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or not _same_inode(parent_metadata, expected_parent)
            or getattr(parent_metadata, "st_file_attributes", 0) & reparse_point
        ):
            return False

        root_fd = open_descriptor(root, read_attributes | list_directory, backup_semantics)
        descriptors.append(root_fd)
        root_metadata = os.fstat(root_fd)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or not _same_inode(root_metadata, expected_root)
            or getattr(root_metadata, "st_file_attributes", 0) & reparse_point
        ):
            return False

        shard_fd = open_descriptor(
            shard_dir,
            read_attributes | list_directory,
            backup_semantics,
        )
        descriptors.append(shard_fd)
        shard_metadata = os.fstat(shard_fd)
        if (
            not stat.S_ISDIR(shard_metadata.st_mode)
            or shard_metadata.st_dev != root_metadata.st_dev
            or not _same_inode(shard_metadata, expected_shard)
            or getattr(shard_metadata, "st_file_attributes", 0) & reparse_point
        ):
            return False

        file_fd = open_descriptor(path, delete_access | read_attributes, 0)
        descriptors.append(file_fd)
        file_metadata = os.fstat(file_fd)
        file_handle = msvcrt.get_osfhandle(file_fd)
        standard = FileStandardInfo()
        attributes = FileAttributeTagInfo()
        if not get_file_info(
            file_handle, 1, ctypes.byref(standard), ctypes.sizeof(standard)
        ) or not get_file_info(file_handle, 9, ctypes.byref(attributes), ctypes.sizeof(attributes)):
            return False
        if (
            attributes.attributes & reparse_point
            or standard.directory
            or standard.number_of_links != 1
            or not _same_file_identity(file_metadata, expected_file)
        ):
            return False

        disposition = FileDispositionInfo(1)
        return bool(
            set_file_info(
                file_handle,
                4,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            )
        )
    except OSError:
        return False
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def commit_verified_artifact(store: ArtifactStore, storage: Storage, ref: ArtifactRef) -> bool:
    """The only recommended way to commit an `ArtifactRef`: verify it truthfully describes a
    real file in `store` (path-safe, exists, correct size and digest —
    `ArtifactStore.verify_ref`) *before* it ever reaches the database, then commit it through
    `Storage.commit_artifact_unverified` (itself idempotent-or-conflict on the complete
    `ArtifactRef` content). A ref returned directly by `store.write_bytes(...)` is already
    guaranteed truthful by construction, but this helper is the safe default for any
    `ArtifactRef` — including ones reconstructed or passed across a boundary — so a forged or
    stale reference can never reach the database. Real callers (Prompt 03's runners onward)
    should call this, not `storage.commit_artifact_unverified` directly.
    """
    with _artifact_digest_lock(store.artifacts_dir, ref.digest):
        store.verify_ref(ref)
        object_path = store._path_for_digest(ref.digest)
        try:
            _refresh_artifact_mtime_without_following(object_path)
        except (OSError, ValidationError) as exc:
            raise ValidationError(f"could not refresh artifact grace period: {exc}") from exc
        return storage.commit_artifact_unverified(ref)
