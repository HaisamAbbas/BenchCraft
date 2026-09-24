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

import os
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

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


@dataclass
class GarbageCollectionReport:
    removed_paths: list[Path]
    kept_referenced: int
    kept_within_grace: int


class ArtifactStore:
    """Content-addressed artifact storage under `<workspace>/artifacts/`, sharded by the
    first two hex digest characters to avoid one huge flat directory."""

    def __init__(self, artifacts_dir: Path, *, max_bytes: int = DEFAULT_MAX_ARTIFACT_BYTES) -> None:
        self.artifacts_dir = artifacts_dir
        self.max_bytes = max_bytes
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

    def _path_for_digest(self, digest: str) -> Path:
        hex_digest = digest.split(":", 1)[1] if ":" in digest else digest
        if not hex_digest or any(c in hex_digest for c in ("/", "\\", "..")):
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
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with _digest_lock(digest):
            if not target_path.exists():
                self._write_new(data, target_path)
            # else: content-addressed dedup — identical bytes already committed under this
            # digest, so there is nothing new to write.

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
        fd, tmp_name = tempfile.mkstemp(dir=target_path.parent, prefix=TEMP_PREFIX)
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
        if not path.is_file():
            raise ValidationError(f"artifact file missing: {path}")
        actual_size = path.stat().st_size
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
    ) -> GarbageCollectionReport:
        """Remove files under `artifacts_dir` that are neither referenced in
        `referenced_digests` (a live query of `Storage.referenced_artifact_digests()`) nor
        younger than `grace_seconds` — the grace period protects a file that was just
        written by a concurrent, not-yet-committed `write_bytes` call from being deleted out
        from under it. Never removes a referenced artifact, regardless of age."""
        removed: list[Path] = []
        kept_referenced = 0
        kept_within_grace = 0
        now = time.time()

        if not self.artifacts_dir.exists():
            return GarbageCollectionReport(removed, kept_referenced, kept_within_grace)

        for shard_dir in self.artifacts_dir.iterdir():
            if not shard_dir.is_dir():
                continue
            for file_path in shard_dir.iterdir():
                if not file_path.is_file():
                    continue
                age = now - file_path.stat().st_mtime
                if file_path.name.startswith(TEMP_PREFIX):
                    # An orphaned temp file from an interrupted write (phase 1 crashed
                    # before the atomic rename). Never referenced by definition.
                    if age >= grace_seconds:
                        file_path.unlink(missing_ok=True)
                        removed.append(file_path)
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
                file_path.unlink(missing_ok=True)
                removed.append(file_path)

        return GarbageCollectionReport(removed, kept_referenced, kept_within_grace)


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
    store.verify_ref(ref)
    return storage.commit_artifact_unverified(ref)
