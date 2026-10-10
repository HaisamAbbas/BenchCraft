"""02-T2/02-G3: atomic artifact commit protocol, crash simulation, and orphan cleanup."""

from __future__ import annotations

import multiprocessing
import os
from pathlib import Path

import pytest

from aibench.core.errors import ValidationError
from aibench.core.hashes import bytes_hash
from aibench.core.models import RedactionClass
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database
from aibench.storage.repositories import Storage


def _artifact_lock_worker(
    artifacts_dir: str,
    digest: str,
    attempted: multiprocessing.synchronize.Event,
    acquired: multiprocessing.synchronize.Event,
    release: multiprocessing.synchronize.Event | None,
) -> None:
    from aibench.storage.artifacts import _artifact_digest_lock

    attempted.set()
    with _artifact_digest_lock(Path(artifacts_dir), digest):
        acquired.set()
        if release is not None and not release.wait(10):
            raise TimeoutError("test process was not released from artifact digest lock")


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


def test_concurrent_writers_of_identical_content_all_succeed(store) -> None:
    """Captures are written from worker threads (16-T4). Identical bytes written and
    verified at once used to fail on Windows ("Access is denied" replacing a file another
    thread had open), which failed executions in the 100-case acceptance run."""
    from concurrent.futures import ThreadPoolExecutor

    payloads = [b"shared capture %d" % (i % 3) for i in range(96)]

    def write_and_verify(data: bytes) -> bytes:
        return store.verify_ref(store.write_bytes(data, mime_type="text/plain"))

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert list(pool.map(write_and_verify, payloads)) == payloads
    leftovers = [p for p in store.artifacts_dir.rglob("*") if p.name.startswith(".tmp-")]
    assert not leftovers


def test_artifact_digest_bucket_lock_serializes_processes(store: ArtifactStore) -> None:
    context = multiprocessing.get_context("spawn")
    first_digest = "sha256:" + "a" * 64
    second_digest = "sha256:" + "aa" + "b" * 62
    first_attempted = context.Event()
    first_acquired = context.Event()
    release_first = context.Event()
    second_attempted = context.Event()
    second_acquired = context.Event()
    first = context.Process(
        target=_artifact_lock_worker,
        args=(
            str(store.artifacts_dir),
            first_digest,
            first_attempted,
            first_acquired,
            release_first,
        ),
    )
    second = context.Process(
        target=_artifact_lock_worker,
        args=(
            str(store.artifacts_dir),
            second_digest,
            second_attempted,
            second_acquired,
            None,
        ),
    )

    first.start()
    try:
        assert first_attempted.wait(5)
        assert first_acquired.wait(5)
        second.start()
        assert second_attempted.wait(5)
        assert not second_acquired.wait(0.25)
        release_first.set()
        assert second_acquired.wait(5)
    finally:
        release_first.set()
        first.join(5)
        if second.pid is not None:
            second.join(5)
        if first.is_alive():
            first.terminate()
        if second.is_alive():
            second.terminate()
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_gc_waits_for_the_writer_lock_before_removing_a_stale_temp(store: ArtifactStore) -> None:
    from concurrent.futures import ThreadPoolExecutor

    context = multiprocessing.get_context("spawn")
    payload = b"slow artifact write"
    digest = bytes_hash(payload)
    hex_digest = digest.split(":", 1)[1]
    shard = store.artifacts_dir / hex_digest[:2]
    shard.mkdir()
    temporary = shard / f".tmp-{hex_digest}-active"
    temporary.write_bytes(payload)
    old = temporary.stat().st_mtime - 10_000
    os.utime(temporary, (old, old))

    attempted = context.Event()
    acquired = context.Event()
    release = context.Event()
    writer = context.Process(
        target=_artifact_lock_worker,
        args=(str(store.artifacts_dir), digest, attempted, acquired, release),
    )
    writer.start()
    try:
        assert attempted.wait(5)
        assert acquired.wait(5)
        with ThreadPoolExecutor(max_workers=1) as pool:
            cleanup = pool.submit(store.garbage_collect, set(), grace_seconds=3600)
            assert not cleanup.done()  # GC saw the stale temp but must wait for its writer.
            assert temporary.is_file()
            release.set()
            report = cleanup.result(timeout=5)
        assert temporary in report.removed_paths
    finally:
        release.set()
        writer.join(5)
        if writer.is_alive():
            writer.terminate()
    assert writer.exitcode == 0


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
    stray_dir = store.artifacts_dir / "aa"
    stray_dir.mkdir(parents=True, exist_ok=True)
    stray_temp = stray_dir / ".tmp-orphaned-from-a-crash"
    stray_temp.write_bytes(b"partial")
    old_time = os.path.getmtime(stray_temp) - 10_000
    os.utime(stray_temp, (old_time, old_time))

    report = store.garbage_collect(storage.referenced_artifact_digests(), grace_seconds=0)
    assert not stray_temp.exists()
    assert stray_temp in report.removed_paths


def test_gc_removes_multiple_artifacts_from_one_shard(store) -> None:
    shard = store.artifacts_dir / "aa"
    shard.mkdir()
    first = shard / ("aa" + "0" * 62)
    second = shard / ("aa" + "1" * 62)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    for path in (first, second):
        old = path.stat().st_mtime - 10_000
        os.utime(path, (old, old))

    report = store.garbage_collect(set(), grace_seconds=0)

    assert set(report.removed_paths) == {first, second}
    assert report.bytes_removed == len(b"first") + len(b"second")


def test_gc_finishes_enumeration_before_deleting_candidates(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_shard = store.artifacts_dir / "aa"
    failing_shard = store.artifacts_dir / "bb"
    first_shard.mkdir()
    failing_shard.mkdir()
    candidate = first_shard / ("aa" + "0" * 62)
    candidate.write_bytes(b"stale candidate")
    old = candidate.stat().st_mtime - 10_000
    os.utime(candidate, (old, old))
    original_iterdir = Path.iterdir

    def fail_on_later_shard(path: Path):
        if path == store.artifacts_dir:
            yield first_shard
            yield failing_shard
        elif path == failing_shard:
            raise PermissionError("simulated shard enumeration failure")
        else:
            yield from original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_on_later_shard)

    with pytest.raises(PermissionError, match="enumeration failure"):
        store.garbage_collect(set(), grace_seconds=0)

    assert candidate.is_file()


def test_gc_reports_successful_deletions_when_a_later_lock_fails(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Iterator
    from contextlib import contextmanager

    import aibench.storage.artifacts as artifacts_module

    first_shard = store.artifacts_dir / "aa"
    second_shard = store.artifacts_dir / "bb"
    first_shard.mkdir()
    second_shard.mkdir()
    first = first_shard / ("aa" + "0" * 62)
    second = second_shard / ("bb" + "0" * 62)
    for path in (first, second):
        path.write_bytes(b"stale candidate")
        old = path.stat().st_mtime - 10_000
        os.utime(path, (old, old))

    original_iterdir = Path.iterdir

    def ordered_iterdir(path: Path):
        if path == store.artifacts_dir:
            yield first_shard
            yield second_shard
        else:
            yield from original_iterdir(path)

    @contextmanager
    def fail_second_digest(_artifacts_dir: Path, digest: str) -> Iterator[None]:
        if digest == f"sha256:{second.name}":
            raise OSError("simulated lock failure")
        yield

    monkeypatch.setattr(Path, "iterdir", ordered_iterdir)
    monkeypatch.setattr(artifacts_module, "_artifact_digest_lock", fail_second_digest)

    report = store.garbage_collect(set(), grace_seconds=0)

    assert report.candidate_paths == [first, second]
    assert report.removed_paths == [first]
    assert report.bytes_removed == len(b"stale candidate")
    assert report.skipped_unsafe == 1
    assert not first.exists()
    assert second.is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX dirfd race regression")
def test_gc_deletes_from_pinned_shard_if_path_is_swapped_for_symlink(
    store: ArtifactStore, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import aibench.storage.artifacts as artifacts_module

    shard = store.artifacts_dir / "aa"
    shard.mkdir()
    object_path = shard / ("aa" + "0" * 62)
    object_path.write_bytes(b"inside artifact store")
    old = object_path.stat().st_mtime - 10_000
    os.utime(object_path, (old, old))
    outside = tmp_path / "outside"
    outside.mkdir()
    external_object = outside / object_path.name
    external_object.write_bytes(b"external data")
    moved_shard = store.artifacts_dir / "aa-moved"
    original_unlink = os.unlink

    def swap_then_unlink(name: str | bytes, *, dir_fd: int | None = None) -> None:
        if dir_fd is not None and name == object_path.name:
            shard.rename(moved_shard)
            shard.symlink_to(outside, target_is_directory=True)
        original_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(artifacts_module.os, "unlink", swap_then_unlink)

    report = store.garbage_collect(set(), grace_seconds=0)

    assert report.removed_paths == [object_path]
    assert not (moved_shard / object_path.name).exists()
    assert external_object.read_bytes() == b"external data"


def test_reusing_old_orphan_refreshes_its_gc_grace_period(store) -> None:
    original = store.write_bytes(b"reused orphan", mime_type="text/plain")
    _old = Path(original.uri).stat().st_mtime - 10_000
    os.utime(original.uri, (_old, _old))

    reused = store.write_bytes(b"reused orphan", mime_type="application/octet-stream")
    report = store.garbage_collect(set(), grace_seconds=3600)

    assert reused.digest == original.digest
    assert Path(reused.uri).is_file()
    assert reused.uri not in {str(path) for path in report.candidate_paths}
    assert report.kept_within_grace == 1


def test_verified_commit_refreshes_old_artifact_before_catalog_commit(store, storage) -> None:
    from aibench.storage.artifacts import commit_verified_artifact

    ref = store.write_bytes(b"stale reference", mime_type="text/plain")
    old = Path(ref.uri).stat().st_mtime - 10_000
    os.utime(ref.uri, (old, old))

    commit_verified_artifact(store, storage, ref)
    report = store.garbage_collect(set(), grace_seconds=3600)

    assert report.candidate_paths == []
    assert report.kept_within_grace == 1
    assert storage.get_artifact(ref.artifact_id) == ref


def test_verified_commit_rejects_noncanonical_content_addressed_uri(store, storage) -> None:
    from aibench.storage.artifacts import commit_verified_artifact

    ref = store.write_bytes(b"canonical path", mime_type="text/plain")
    alternate = store.artifacts_dir / "alternate" / "payload.bin"
    alternate.parent.mkdir()
    alternate.write_bytes(b"canonical path")
    forged = ref.model_copy(update={"uri": str(alternate)})

    with pytest.raises(ValidationError, match="content-addressed digest path"):
        commit_verified_artifact(store, storage, forged)
    assert storage.get_artifact(ref.artifact_id) is None


def test_gc_rejects_an_artifact_store_root_symlink(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "artifacts"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    store = ArtifactStore(link, create=False)
    with pytest.raises(ValidationError, match="real directory"):
        store.garbage_collect(set(), grace_seconds=0)


def test_write_bytes_rejects_hardlinked_object_without_touching_external_file(
    tmp_path,
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    payload = b"outside hardlink target"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload)
    outside_mtime = outside.stat().st_mtime_ns
    digest = bytes_hash(payload)
    object_path = store._path_for_digest(digest)
    object_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(outside, object_path)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    with pytest.raises(ValidationError, match="private regular file"):
        store.write_bytes(payload, mime_type="application/octet-stream")

    assert outside.read_bytes() == payload
    assert outside.stat().st_mtime_ns == outside_mtime


def test_artifact_gc_excludes_hardlinks_from_preview(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    payload = b"external artifact candidate"
    outside = tmp_path / "outside.bin"
    outside.write_bytes(payload)
    object_path = store._path_for_digest(bytes_hash(payload))
    object_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(outside, object_path)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")
    old = object_path.stat().st_mtime - 10_000
    os.utime(object_path, (old, old))

    report = store.garbage_collect(set(), grace_seconds=0, dry_run=True)

    assert report.candidate_paths == []
    assert report.bytes_reclaimable == 0
    assert report.skipped_unsafe == 1
    assert outside.read_bytes() == payload


def test_redaction_and_run_id_round_trip_through_commit(store, storage) -> None:
    from aibench.core.models import RunManifest
    from aibench.storage.artifacts import commit_verified_artifact

    storage.commit_run(
        RunManifest(
            run_id="r1", dataset_hash="sha256:d", application_hash="sha256:a", plan_hash="sha256:p"
        )
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
