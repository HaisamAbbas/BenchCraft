"""02-T2/02-G3: atomic artifact commit protocol, crash simulation, and orphan cleanup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from aibench.core.errors import ValidationError
from aibench.core.models import RedactionClass
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database
from aibench.storage.repositories import Storage


@pytest.fixture
def store(tmp_path) -> ArtifactStore:
    return ArtifactStore(tmp_path / "artifacts")


@pytest.fixture
def storage(tmp_path):
    db = Database.open(tmp_path / "test.db")
    s = Storage(db)
    yield s
    db.close()


def test_write_bytes_is_readable_and_content_addressed(store) -> None:
    ref = store.write_bytes(b"hello world", mime_type="text/plain")
    assert ref.digest.startswith("sha256:")
    assert ref.size_bytes == len(b"hello world")
    assert store.read_bytes(ref) == b"hello world"
    assert Path(ref.uri).exists()


def test_identical_content_dedupes_to_the_same_path(store) -> None:
    ref1 = store.write_bytes(b"same bytes", mime_type="text/plain")
    ref2 = store.write_bytes(b"same bytes", mime_type="text/plain")
    assert ref1.digest == ref2.digest
    assert ref1.uri == ref2.uri
    assert ref1.artifact_id != ref2.artifact_id  # distinct identity, same content


def test_oversized_payload_is_rejected_before_any_write(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts", max_bytes=10)
    with pytest.raises(ValidationError):
        store.write_bytes(b"this is definitely more than 10 bytes", mime_type="text/plain")
    assert not any((tmp_path / "artifacts").rglob("*"))


def test_invalid_digest_path_traversal_is_rejected(store) -> None:
    with pytest.raises(ValidationError):
        store._path_for_digest("sha256:../../etc/passwd")


def test_read_bytes_rejects_a_uri_outside_the_store(store, tmp_path) -> None:
    """A forged/out-of-root `uri` (not one `write_bytes` ever produced) must never be
    trusted, whether it points elsewhere on disk or tries to escape via `..`."""
    from aibench.core.models import ArtifactRef

    outside_file = tmp_path / "outside.txt"
    outside_file.write_bytes(b"not a real artifact")
    forged = ArtifactRef(
        artifact_id="forged",
        digest="sha256:" + "0" * 64,
        uri=str(outside_file),
        mime_type="text/plain",
        size_bytes=len(b"not a real artifact"),
    )
    with pytest.raises(ValidationError, match="does not resolve inside"):
        store.read_bytes(forged)


def test_read_bytes_rejects_a_traversal_uri(store) -> None:
    from aibench.core.models import ArtifactRef

    forged = ArtifactRef(
        artifact_id="forged",
        digest="sha256:" + "0" * 64,
        uri=str(store.artifacts_dir / ".." / ".." / "escaped.txt"),
        mime_type="text/plain",
        size_bytes=0,
    )
    with pytest.raises(ValidationError, match="does not resolve inside"):
        store.read_bytes(forged)


def test_read_bytes_detects_a_size_mismatch(store) -> None:
    """A forged `size_bytes` (metadata claiming something the real file does not back up)
    must be caught, not trusted."""
    ref = store.write_bytes(b"real content", mime_type="text/plain")
    forged = ref.model_copy(update={"size_bytes": 99999})
    with pytest.raises(ValidationError, match="size mismatch"):
        store.read_bytes(forged)


def test_read_bytes_detects_a_tampered_file(store) -> None:
    """The file on disk is modified after being written (bypassing the store's own atomic
    write path) so its content no longer matches the digest committed for it; reading must
    detect this rather than silently returning the tampered bytes."""
    ref = store.write_bytes(b"original content", mime_type="text/plain")
    tampered = b"tampered-content"
    assert len(tampered) == len(b"original content")  # same size, so this isolates the digest check
    Path(ref.uri).write_bytes(tampered)
    with pytest.raises(ValidationError, match="digest mismatch"):
        store.read_bytes(ref)


def test_verify_ref_rejects_a_missing_file(store) -> None:
    from aibench.core.models import ArtifactRef

    missing = ArtifactRef(
        artifact_id="missing",
        digest="sha256:" + "0" * 64,
        uri=str(store.artifacts_dir / "00" / ("0" * 64)),
        mime_type="text/plain",
        size_bytes=0,
    )
    with pytest.raises(ValidationError, match="missing"):
        store.verify_ref(missing)


def test_no_temp_file_survives_a_successful_write(store) -> None:
    store.write_bytes(b"clean write", mime_type="text/plain")
    temp_files = list(store.artifacts_dir.rglob(".tmp-*"))
    assert temp_files == []


def test_crash_during_physical_write_leaves_no_final_path_and_cleans_up_temp(
    store, monkeypatch
) -> None:
    """Simulates a crash between "bytes flushed to a temp file" and "atomic rename":
    os.replace is monkeypatched to raise. The target content-addressed path must never
    exist afterward — the atomic-rename step is exactly what prevents a half-written file
    from ever being visible at its real path (02-G3)."""
    original_replace = os.replace

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash during rename")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError, match="simulated crash"):
        store.write_bytes(b"never fully committed", mime_type="text/plain")
    monkeypatch.setattr(os, "replace", original_replace)

    # The final content-addressed path was never created...
    from aibench.core.hashes import bytes_hash

    digest = bytes_hash(b"never fully committed")
    final_path = store._path_for_digest(digest)
    assert not final_path.exists()
    # ...and the crashed write's temp file was cleaned up (best-effort, not a real crash).
    assert list(store.artifacts_dir.rglob(".tmp-*")) == []


def test_write_failure_means_no_db_commit_ever_happens(tmp_path, storage) -> None:
    """The two-phase protocol at the call-site level: if phase 1 (`write_bytes`) raises, a
    real caller's code never reaches phase 2 (`commit_verified_artifact`) because the
    `ArtifactRef` it needs was never returned. Proven end to end rather than by asserting on
    internals."""
    from aibench.storage.artifacts import commit_verified_artifact

    tiny_store = ArtifactStore(tmp_path / "artifacts", max_bytes=5)

    def write_then_commit(data: bytes) -> None:
        ref = tiny_store.write_bytes(data, mime_type="text/plain")  # may raise
        commit_verified_artifact(tiny_store, storage, ref)  # unreachable if the line above raised

    with pytest.raises(ValidationError):
        write_then_commit(b"this payload is too large for the 5-byte limit")

    assert storage.referenced_artifact_digests() == set()


def test_orphan_file_is_removed_after_grace_period_but_referenced_ones_are_not(
    store, storage
) -> None:
    from aibench.storage.artifacts import commit_verified_artifact

    committed_ref = store.write_bytes(b"referenced content", mime_type="text/plain")
    commit_verified_artifact(store, storage, committed_ref)

    orphan_ref = store.write_bytes(b"orphaned content", mime_type="text/plain")
    # orphan_ref is deliberately never committed to storage — simulates a crash between
    # phase 1 (write) and phase 2 (DB commit).

    report = store.garbage_collect(storage.referenced_artifact_digests(), grace_seconds=0)

    assert Path(committed_ref.uri).exists()
    assert not Path(orphan_ref.uri).exists()
    assert Path(orphan_ref.uri) in report.removed_paths
    assert report.kept_referenced == 1


def test_orphan_within_grace_period_is_not_removed(store, storage) -> None:
    orphan_ref = store.write_bytes(b"very recent orphan", mime_type="text/plain")
    report = store.garbage_collect(storage.referenced_artifact_digests(), grace_seconds=3600)
    assert Path(orphan_ref.uri).exists()
    assert report.kept_within_grace >= 1
    assert report.removed_paths == []


def test_stale_temp_file_from_a_crashed_write_is_garbage_collected(store, storage) -> None:
    stray_dir = store.artifacts_dir / "zz"
    stray_dir.mkdir(parents=True, exist_ok=True)
    stray_temp = stray_dir / ".tmp-orphaned-from-a-crash"
    stray_temp.write_bytes(b"partial")
    old_time = os.path.getmtime(stray_temp) - 10_000
    os.utime(stray_temp, (old_time, old_time))

    report = store.garbage_collect(storage.referenced_artifact_digests(), grace_seconds=0)
    assert not stray_temp.exists()
    assert stray_temp in report.removed_paths


def test_redaction_and_run_id_round_trip_through_commit(store, storage) -> None:
    from aibench.core.models import RunManifest
    from aibench.storage.artifacts import commit_verified_artifact

    storage.commit_run(
        RunManifest(run_id="r1", dataset_hash="sha256:d", application_hash="sha256:a", plan_hash="sha256:p")
    )
    ref = store.write_bytes(
        b"restricted payload",
        mime_type="application/json",
        run_id="r1",
        redaction=RedactionClass.RESTRICTED,
    )
    commit_verified_artifact(store, storage, ref)
    fetched = storage.get_artifact(ref.artifact_id)
    assert fetched.redaction == RedactionClass.RESTRICTED
    assert fetched.run_id == "r1"


def test_commit_verified_artifact_accepts_a_genuine_ref(store, storage) -> None:
    from aibench.storage.artifacts import commit_verified_artifact

    ref = store.write_bytes(b"genuine payload", mime_type="text/plain")
    assert commit_verified_artifact(store, storage, ref) is True
    assert storage.get_artifact(ref.artifact_id) is not None


def test_commit_verified_artifact_rejects_a_forged_ref_before_it_reaches_the_db(
    store, storage
) -> None:
    """The recommended fix from the review: verify before committing, not just before
    reading. A ref whose declared size doesn't match the real file must never reach the
    database at all."""
    from aibench.core.errors import ValidationError
    from aibench.storage.artifacts import commit_verified_artifact

    ref = store.write_bytes(b"genuine payload", mime_type="text/plain")
    forged = ref.model_copy(update={"size_bytes": 1})
    with pytest.raises(ValidationError, match="size mismatch"):
        commit_verified_artifact(store, storage, forged)
    assert storage.get_artifact(forged.artifact_id) is None
