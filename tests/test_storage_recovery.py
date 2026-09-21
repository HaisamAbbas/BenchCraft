"""02-T4/02-G1: restart-safe loading — closing and reopening a `Database` against the same
workspace must preserve every previously committed record and artifact, and must not
duplicate or corrupt anything on the reopen itself."""

from __future__ import annotations

from aibench.core.models import (
    ExecutionResult,
    ExecutionStatus,
    RunManifest,
)
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage


def test_workspace_layout_is_created_on_first_open(tmp_path) -> None:
    ws = Workspace.at(tmp_path)
    assert not ws.root.exists()
    db = Database.open_workspace(ws)
    assert ws.root.exists()
    assert ws.artifacts_dir.exists()
    assert ws.db_path.exists()
    db.close()


def test_committed_run_survives_close_and_reopen(tmp_path) -> None:
    ws = Workspace.at(tmp_path)
    db1 = Database.open_workspace(ws)
    storage1 = Storage(db1)
    manifest = RunManifest(
        run_id="r1", dataset_hash="sha256:d", application_hash="sha256:a", plan_hash="sha256:p"
    )
    storage1.commit_run(manifest)
    storage1.update_run_status("r1", "succeeded")
    db1.close()

    db2 = Database.open_workspace(ws)
    storage2 = Storage(db2)
    record = storage2.get_run("r1")
    assert record is not None
    assert record.status == "succeeded"
    assert record.manifest.dataset_hash == "sha256:d"
    db2.close()


def test_committed_execution_attempts_survive_restart(tmp_path) -> None:
    ws = Workspace.at(tmp_path)
    db1 = Database.open_workspace(ws)
    storage1 = Storage(db1)
    storage1.commit_run(
        RunManifest(run_id="r1", dataset_hash="sha256:d", application_hash="sha256:a", plan_hash="sha256:p")
    )
    storage1.commit_execution_attempt(
        ExecutionResult(
            execution_id="e1", run_id="r1", case_id="c1", status=ExecutionStatus.OK, output="ok"
        )
    )
    db1.close()

    db2 = Database.open_workspace(ws)
    storage2 = Storage(db2)
    fetched = storage2.get_execution_attempt("e1")
    assert fetched is not None
    assert fetched.output == "ok"
    db2.close()


def test_committed_artifact_bytes_and_reference_survive_restart(tmp_path) -> None:
    ws = Workspace.at(tmp_path)
    db1 = Database.open_workspace(ws)
    storage1 = Storage(db1)
    artifact_store = ArtifactStore(ws.artifacts_dir)
    ref = artifact_store.write_bytes(b"durable payload", mime_type="text/plain")
    commit_verified_artifact(artifact_store, storage1, ref)
    db1.close()

    # A fresh ArtifactStore instance too — nothing cached in Python state, everything read
    # back from disk/DB.
    db2 = Database.open_workspace(ws)
    storage2 = Storage(db2)
    artifact_store2 = ArtifactStore(ws.artifacts_dir)
    fetched_ref = storage2.get_artifact(ref.artifact_id)
    assert fetched_ref is not None
    assert artifact_store2.read_bytes(fetched_ref) == b"durable payload"
    db2.close()


def test_retrying_a_commit_after_restart_does_not_duplicate_it(tmp_path) -> None:
    """The scenario 02-G2/02-T4 describe: a caller crashes right after a logical commit
    (before it could confirm success to whatever invoked it) and, on restart, retries the
    same commit. That retry must be a no-op, not a duplicate or an error."""
    ws = Workspace.at(tmp_path)
    manifest = RunManifest(
        run_id="r1", dataset_hash="sha256:d", application_hash="sha256:a", plan_hash="sha256:p"
    )

    db1 = Database.open_workspace(ws)
    storage1 = Storage(db1)
    assert storage1.commit_run(manifest) is True
    db1.close()  # simulated crash right here, before the caller "knew" it succeeded

    db2 = Database.open_workspace(ws)
    storage2 = Storage(db2)
    assert storage2.commit_run(manifest) is False  # retry: no-op, not an error
    assert len(storage2.list_runs()) == 1
    db2.close()


def test_multiple_restarts_are_stable(tmp_path) -> None:
    ws = Workspace.at(tmp_path)
    for i in range(5):
        db = Database.open_workspace(ws)
        storage = Storage(db)
        storage.commit_run(
            RunManifest(
                run_id=f"r{i}",
                dataset_hash="sha256:d",
                application_hash="sha256:a",
                plan_hash="sha256:p",
            )
        )
        db.close()

    db = Database.open_workspace(ws)
    storage = Storage(db)
    assert {r.manifest.run_id for r in storage.list_runs(limit=10)} == {
        f"r{i}" for i in range(5)
    }
    db.close()


def test_database_uses_wal_journal_mode(tmp_path) -> None:
    db = Database.open(tmp_path / "wal_test.db")
    mode = db.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    db.close()


def test_database_enforces_foreign_keys(tmp_path) -> None:
    db = Database.open(tmp_path / "fk_test.db")
    enforced = db.connection.execute("PRAGMA foreign_keys").fetchone()[0]
    assert enforced == 1
    db.close()
