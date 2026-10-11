"""Path migration preserves immutable plans and repairs only workspace-rooted references."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ValidationError
from aibench.core.models import deep_unfreeze
from aibench.core.plans import ExecutablePlan
from aibench.core.sessions import BenchmarkSession, DecisionRecord
from aibench.engine.compile import freeze_plan
from aibench.sessions.store import SessionStore
from aibench.storage import workspace_paths
from aibench.storage.artifacts import commit_verified_artifact
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.storage.workspace_integrity import check_workspace
from aibench.storage.workspace_paths import (
    _absolute_root,
    _check_workspace_pins,
    _hold_workspace_guards,
    _normalize_reference,
    _rebase_or_preserve,
    _resolve_plan_reference,
    _validate_workspace_paths,
    migrate_workspace_paths,
)
from tests.session_support import SessionHarness

runner = CliRunner()


def test_workspace_migrate_paths_rebases_moved_project_and_preserves_external_paths(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-project"
    destination = tmp_path / "moved-project"
    harness = SessionHarness(source)
    harness.workspace = Workspace.at(source)
    harness.workspace.ensure_directories()
    controller = harness.open_session(
        {"case-a": "hello"},
        objectives=("answers are correct",),
        policy={"allow_trusted_local": True},
    )
    session = controller.session
    decision = controller.current_decision()
    session_dir = harness.workspace.root / "sessions" / session.session_id
    plan_path = session_dir / decision.plan_file
    plan = ExecutablePlan.model_validate_json(plan_path.read_bytes())
    external_dataset = tmp_path / "external-dataset.jsonl"
    external_dataset.write_text('{"case_id":"external","input":"hello"}\n', encoding="utf-8")
    plan = plan.model_copy(update={"dataset": str(external_dataset)})
    _, plan_hash = freeze_plan(plan)
    plan_file = f"plan-{plan_hash.split(':', 1)[-1][:16]}.json"
    plan_path = session_dir / plan_file
    plan_path.write_bytes(plan.model_dump_json().encode("utf-8"))
    draft = deep_unfreeze(decision.draft)
    draft["plan_file"] = plan_file
    draft["plan_hash"] = plan_hash
    decision = decision.model_copy(
        update={
            "choices": decision.choices.model_copy(update={"dataset": str(external_dataset)}),
            "plan_file": plan_file,
            "plan_hash": plan_hash,
            "draft": draft,
        }
    )
    controller.storage.db.connection.execute(
        "UPDATE decision_records SET data = ? WHERE decision_id = ?",
        (decision.model_dump_json(), decision.decision_id),
    )
    plan_bytes = plan_path.read_bytes()

    external_policy = tmp_path / "external-policy.json"
    external_policy.write_text("{}", encoding="utf-8")
    updated_session = session.model_copy(update={"policy_path": str(external_policy)})
    controller.storage.db.connection.execute(
        "UPDATE sessions SET data = ? WHERE session_id = ?",
        (updated_session.model_dump_json(), session.session_id),
    )

    artifact = controller.artifacts.write_bytes(b"captured evidence", mime_type="text/plain")
    commit_verified_artifact(controller.artifacts, controller.storage, artifact)
    controller.storage.db.close()
    source.rename(destination)

    preview = runner.invoke(
        app,
        ["workspace", "migrate-paths", "--workspace", str(destination), "--json"],
    )

    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.stdout)
    assert preview_payload["mode"] == "dry_run"
    assert preview_payload["artifact_uris_to_update"] == 1
    assert preview_payload["sessions_to_update"] == 1
    assert preview_payload["decisions_to_update"] == 1
    assert preview_payload["records_updated"] == 0

    database = Database.open_readonly(Workspace.at(destination).db_path)
    try:
        storage = Storage(database)
        sessions = SessionStore(storage)
        assert storage.get_artifact(artifact.artifact_id).uri == artifact.uri
        moved_session = sessions.get_session(session.session_id)
        moved_decision = sessions.get_decision(decision.decision_id)
        assert moved_session.project_root == str(source)
        assert moved_session.policy_path == str(external_policy)
        assert moved_decision.choices.application == str(source / "app.json")
        assert moved_decision.choices.dataset == str(external_dataset)
    finally:
        database.close()

    applied = runner.invoke(
        app,
        ["workspace", "migrate-paths", "--workspace", str(destination), "--apply", "--json"],
    )

    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["mode"] == "applied"
    assert applied_payload["records_updated"] == 3
    assert applied_payload["external_paths_preserved"] == 2
    database = Database.open_readonly(Workspace.at(destination).db_path)
    try:
        storage = Storage(database)
        sessions = SessionStore(storage)
        moved_session = sessions.get_session(session.session_id)
        moved_decision = sessions.get_decision(decision.decision_id)
        moved_artifact = storage.get_artifact(artifact.artifact_id)
        assert moved_session.project_root == str(destination)
        assert moved_session.policy_path == str(external_policy)
        assert moved_decision.choices.application == str(destination / "app.json")
        assert moved_decision.choices.dataset == str(external_dataset)
        assert (
            moved_artifact.uri
            == (
                Workspace.at(destination).artifacts_dir
                / artifact.digest.split(":", 1)[1][:2]
                / artifact.digest.split(":", 1)[1]
            ).as_posix()
        )
        assert Path(moved_artifact.uri).read_bytes() == b"captured evidence"
    finally:
        database.close()
    moved_plan = (
        Workspace.at(destination).root / "sessions" / session.session_id / decision.plan_file
    )
    assert moved_plan.read_bytes() == plan_bytes
    assert check_workspace(destination)["ok"] is True

    repeated = runner.invoke(
        app,
        ["workspace", "migrate-paths", "--workspace", str(destination), "--json"],
    )
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.stdout)["changed_if_applied"] == 0


def test_workspace_migrate_paths_refuses_active_sessions_without_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    moved = tmp_path / "moved-project"
    harness = SessionHarness(project)
    harness.workspace = Workspace.at(project)
    harness.workspace.ensure_directories()
    controller = harness.open_session({"case-a": "hello"}, objectives=("answers are correct",))
    artifact = controller.artifacts.write_bytes(b"artifact retained", mime_type="text/plain")
    commit_verified_artifact(controller.artifacts, controller.storage, artifact)
    session = controller.session.model_copy(update={"active_run_id": "run-live"})
    controller.storage.db.connection.execute(
        "UPDATE sessions SET active_run_id = ?, data = ? WHERE session_id = ?",
        ("run-live", session.model_dump_json(), session.session_id),
    )
    controller.storage.db.close()
    project.rename(moved)

    result = runner.invoke(
        app,
        ["workspace", "migrate-paths", "--workspace", str(moved), "--apply", "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "active run" in json.loads(result.stdout)["message"]
    database = Database.open_readonly(Workspace.at(moved).db_path)
    try:
        storage = Storage(database)
        raw = database.connection.execute("SELECT data FROM sessions").fetchone()[0]
        unchanged = BenchmarkSession.model_validate_json(raw)
        assert unchanged.project_root == str(project)
        raw_decision = database.connection.execute("SELECT data FROM decision_records").fetchone()[
            0
        ]
        assert DecisionRecord.model_validate_json(raw_decision).choices.application == str(
            project / "app.json"
        )
        assert storage.get_artifact(artifact.artifact_id).uri == artifact.uri
        assert Path(artifact.uri).exists() is False
        moved_artifact = (
            Workspace.at(moved).artifacts_dir
            / artifact.digest.split(":", 1)[1][:2]
            / artifact.digest.split(":", 1)[1]
        )
        assert moved_artifact.is_file()
    finally:
        database.close()


def test_workspace_migrate_paths_rolls_back_if_wal_disappears_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source-project"
    moved = tmp_path / "moved-project"
    harness = SessionHarness(source)
    harness.workspace = Workspace.at(source)
    harness.workspace.ensure_directories()
    controller = harness.open_session({"case-a": "hello"}, objectives=("answers are correct",))
    session_id = controller.session.session_id
    controller.storage.db.close()
    source.rename(moved)

    def unlink_wal(_connection, workspace):
        wal = workspace.db_path.with_name(f"{workspace.db_path.name}-wal")
        assert wal.is_file(), "the path update should have created an active WAL"
        wal.unlink()

    monkeypatch.setattr(workspace_paths, "_verify_artifacts", unlink_wal)
    result = runner.invoke(
        app,
        ["workspace", "migrate-paths", "--workspace", str(moved), "--apply", "--json"],
    )
    assert result.exit_code == 2, result.output
    monkeypatch.undo()

    database = Database.open_readonly(Workspace.at(moved).db_path)
    try:
        raw = database.connection.execute(
            "SELECT data FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        assert BenchmarkSession.model_validate_json(raw).project_root == str(source)
    finally:
        database.close()


def test_workspace_migrate_paths_handles_a_copied_project(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-project"
    destination = tmp_path / "moved-project"
    harness = SessionHarness(source)
    harness.workspace = Workspace.at(source)
    harness.workspace.ensure_directories()
    controller = harness.open_session({"case-a": "hello"}, objectives=("answers are correct",))
    controller.storage.db.close()
    shutil.copytree(source, destination)

    result = runner.invoke(
        app,
        ["workspace", "migrate-paths", "--workspace", str(destination), "--apply", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["mode"] == "applied"


def test_workspace_migrate_paths_rejects_hard_linked_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    linked_database = tmp_path / "linked-aibench.db"
    try:
        os.link(workspace.db_path, linked_database)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    with pytest.raises(ValidationError, match="workspace database must not be hard-linked"):
        migrate_workspace_paths(project)


def test_workspace_migrate_paths_rejects_hard_linked_sqlite_sidecar(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    wal = workspace.db_path.with_name(f"{workspace.db_path.name}-wal")
    linked_wal = tmp_path / "linked-aibench.db-wal"
    wal.write_bytes(b"wal placeholder")
    try:
        os.link(wal, linked_wal)
    except OSError as exc:
        wal.unlink(missing_ok=True)
        pytest.skip(f"hard links are unavailable: {exc}")

    with pytest.raises(ValidationError, match="workspace database sidecar must not be hard-linked"):
        migrate_workspace_paths(project)


def test_workspace_migrate_paths_rejects_nonregular_database_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    workspace.ensure_directories()
    Database.open_workspace(workspace).close()
    (workspace.db_path.parent / f"{workspace.db_path.name}-wal").mkdir()

    def fail_if_opened(_path: Path) -> Database:
        raise AssertionError("database must not be opened before sidecars are validated")

    monkeypatch.setattr(Database, "open_readonly", fail_if_opened)
    with pytest.raises(ValidationError, match="sidecar must be a regular file"):
        migrate_workspace_paths(project)


def test_workspace_migrate_paths_detects_workspace_replacement(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    workspace.ensure_directories()
    Database.open_workspace(workspace).close()
    pins = _validate_workspace_paths(project, workspace)
    backup = workspace.artifacts_dir.with_name("artifacts-old")
    workspace.artifacts_dir.rename(backup)
    workspace.artifacts_dir.mkdir()

    try:
        with pytest.raises(ValidationError, match="artifact store changed"):
            _check_workspace_pins(workspace, pins)
    finally:
        workspace.artifacts_dir.rmdir()
        backup.rename(workspace.artifacts_dir)


@pytest.mark.skipif(os.name != "nt", reason="Windows directory-handle guards are platform-specific")
def test_workspace_migrate_paths_guards_against_project_rename(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    pins = _validate_workspace_paths(project, workspace)

    with _hold_workspace_guards(workspace, pins), pytest.raises(OSError):
        project.rename(tmp_path / "replaced-project")


def test_workspace_migrate_paths_pins_sidecars_created_after_open(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    workspace.ensure_directories()
    Database.open_workspace(workspace).close()
    pins = _validate_workspace_paths(project, workspace)
    sidecar = workspace.db_path.with_name(f"{workspace.db_path.name}-wal")
    replacement = sidecar.with_name("replacement-sidecar")
    replacement.write_bytes(b"replacement")
    sidecar.write_bytes(b"first")

    _check_workspace_pins(workspace, pins)
    sidecar.rename(sidecar.with_name("old-sidecar"))
    replacement.rename(sidecar)

    with pytest.raises(ValidationError, match="sidecar changed"):
        _check_workspace_pins(workspace, pins)


def test_workspace_migrate_paths_fails_if_pinned_wal_disappears(tmp_path: Path) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    pins = _validate_workspace_paths(project, workspace)
    wal = workspace.db_path.with_name(f"{workspace.db_path.name}-wal")
    wal.write_bytes(b"active wal")
    _check_workspace_pins(workspace, pins)
    wal.unlink()

    with pytest.raises(ValidationError, match="active workspace database sidecar disappeared"):
        _check_workspace_pins(workspace, pins)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX open-file descriptor paths")
def test_workspace_migrate_paths_detects_unlinked_open_wal_descriptor(tmp_path: Path) -> None:
    descriptor_directory = Path("/proc/self/fd")
    if not descriptor_directory.is_dir():
        pytest.skip("/proc/self/fd is unavailable")
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    pins = _validate_workspace_paths(project, workspace)
    pins.descriptor_directory = descriptor_directory
    wal = workspace.db_path.with_name(f"{workspace.db_path.name}-wal")
    wal.write_bytes(b"active wal")
    descriptor = os.open(wal, os.O_RDONLY)
    try:
        workspace_paths._refresh_sqlite_sidecar_descriptors(workspace, pins)
        wal.unlink()

        with pytest.raises(ValidationError, match="unlinked database sidecar"):
            _check_workspace_pins(workspace, pins)
    finally:
        os.close(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="requires POSIX open-file descriptor paths")
def test_workspace_migrate_paths_rejects_sidecar_handle_outside_workspace(
    tmp_path: Path,
) -> None:
    descriptor_directory = Path("/proc/self/fd")
    if not descriptor_directory.is_dir():
        pytest.skip("/proc/self/fd is unavailable")
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    pins = _validate_workspace_paths(project, workspace)
    pins.descriptor_directory = descriptor_directory
    external_sidecar = tmp_path / "external" / f"{workspace.db_path.name}-wal"
    external_sidecar.parent.mkdir()
    external_sidecar.write_bytes(b"external wal")
    descriptor = os.open(external_sidecar, os.O_RDONLY)
    try:
        with pytest.raises(ValidationError, match="sidecar outside the workspace"):
            workspace_paths._refresh_sqlite_sidecar_descriptors(workspace, pins)
    finally:
        os.close(descriptor)


def test_frozen_plan_path_resolution_uses_stored_path_flavor(tmp_path: Path) -> None:
    session_directory = tmp_path / "moved" / ".aibench" / "sessions" / "session-1"
    windows_external = r"Z:\shared\application.json"
    posix_external = "/mnt/shared/dataset.json"

    assert _resolve_plan_reference(session_directory, windows_external) == _normalize_reference(
        windows_external
    )
    assert _resolve_plan_reference(session_directory, posix_external) == _normalize_reference(
        posix_external
    )
    assert _resolve_plan_reference(session_directory, r"..\app\application.json") == (
        _normalize_reference(str(session_directory.parent / "app" / "application.json"))
    )


def test_rebase_or_preserve_handles_absolute_paths_in_both_flavors(tmp_path: Path) -> None:
    destination = tmp_path / "moved-project"
    windows_root = r"C:\old-project"
    posix_external = "/mnt/shared/dataset.json"
    windows_external = r"Z:\shared\application.json"

    assert _rebase_or_preserve(posix_external, windows_root, destination) == (
        posix_external,
        True,
    )
    assert _rebase_or_preserve(windows_external, "/old-project", destination) == (
        windows_external,
        True,
    )
    assert _rebase_or_preserve("/old-project/config.json", "/old-project", destination) == (
        str(destination / "config.json"),
        False,
    )


def test_windows_sidecar_handle_cannot_escape_the_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    pins = _validate_workspace_paths(project, workspace)
    pins.windows_handle_baseline = set()
    pins.windows_handle_snapshot_initialized = True
    pins.windows_database_handle = 10
    outside = tmp_path / f"{workspace.db_path.name}-wal"
    monkeypatch.setattr(workspace_paths, "_windows_process_handles", lambda: {10, 11})
    monkeypatch.setattr(
        workspace_paths,
        "_windows_final_path",
        lambda handle: str(workspace.db_path) if handle == 10 else str(outside),
    )
    monkeypatch.setattr(
        workspace_paths,
        "_windows_file_information",
        lambda handle: ((1, pins.database.st_ino), 1, 0),
    )

    with pytest.raises(ValidationError, match="database sidecar outside the workspace"):
        workspace_paths._refresh_windows_sqlite_handles(workspace, pins)


def test_workspace_migrate_paths_preview_scans_one_read_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    original_prepare = workspace_paths._prepare

    def verify_snapshot(connection, selected_workspace, root, integrity, *, apply):
        assert connection.in_transaction
        return original_prepare(connection, selected_workspace, root, integrity, apply=apply)

    monkeypatch.setattr(workspace_paths, "_prepare", verify_snapshot)
    report = migrate_workspace_paths(project)

    assert report["mode"] == "dry_run"


def test_workspace_migrate_paths_pages_large_catalogs(tmp_path: Path) -> None:
    database = Database.open_in_memory()
    try:
        database.connection.execute("CREATE TABLE page_probe (probe_id TEXT PRIMARY KEY)")
        database.connection.executemany(
            "INSERT INTO page_probe (probe_id) VALUES (?)",
            [(f"item-{index:04d}",) for index in range(260)],
        )
        page_sizes = [
            len(batch)
            for batch in workspace_paths._paged_rows(
                database.connection,
                "SELECT probe_id FROM page_probe",
                "probe_id",
            )
        ]
    finally:
        database.close()

    assert page_sizes == [128, 128, 4]


def test_historical_project_root_is_normalized_without_following_new_symlinks(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old-project"
    replacement = tmp_path / "replacement-project"
    replacement.mkdir()
    try:
        old_root.symlink_to(replacement, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    assert _absolute_root(str(old_root), "session-1") == os.path.normpath(str(old_root))


def test_historical_posix_project_root_uses_posix_absolute_rules() -> None:
    assert _absolute_root("/old/project", "session-1") == "/old/project"


@pytest.mark.skipif(os.name == "nt", reason="reproduces a POSIX directory-swap race")
def test_workspace_migrate_paths_rejects_database_swapped_during_sqlite_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source-project"
    moved = tmp_path / "moved-project"
    copied = tmp_path / "copied-project"
    harness = SessionHarness(source)
    harness.workspace = Workspace.at(source)
    harness.workspace.ensure_directories()
    controller = harness.open_session({"case-a": "hello"}, objectives=("answers are correct",))
    session_id = controller.session.session_id
    controller.storage.db.close()
    source.rename(moved)
    shutil.copytree(moved, copied)

    moved_workspace = Workspace.at(moved)
    copied_metadata = Workspace.at(copied).root
    original_metadata = moved_workspace.root
    original_held = tmp_path / "original-metadata-held"
    copied_held = tmp_path / "copied-metadata-held"
    restored = False

    def restore_metadata() -> None:
        nonlocal restored
        if restored:
            return
        original_metadata.rename(copied_held)
        original_held.rename(original_metadata)
        restored = True

    class RestoringConnection:
        def __init__(self, connection):
            object.__setattr__(self, "_connection", connection)

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def __setattr__(self, name, value):
            setattr(self._connection, name, value)

        def close(self):
            try:
                self._connection.close()
            finally:
                restore_metadata()

    real_connect = workspace_paths.sqlite3.connect

    def swap_during_connect(*args, **kwargs):
        original_metadata.rename(original_held)
        copied_metadata.rename(original_metadata)
        try:
            connection = real_connect(*args, **kwargs)
        except BaseException:
            original_metadata.rename(copied_metadata)
            original_held.rename(original_metadata)
            raise
        return RestoringConnection(connection)

    monkeypatch.setattr(workspace_paths.sqlite3, "connect", swap_during_connect)
    with pytest.raises(ValidationError, match="different workspace database file"):
        migrate_workspace_paths(moved, apply=True)
    monkeypatch.undo()
    restore_metadata()

    for db_path in (moved_workspace.db_path, copied_held / "aibench.db"):
        database = Database.open_readonly(db_path)
        try:
            raw = database.connection.execute(
                "SELECT data FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()[0]
            assert BenchmarkSession.model_validate_json(raw).project_root == str(source)
        finally:
            database.close()


def test_workspace_migrate_paths_refuses_external_relative_frozen_plan_drift(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-project"
    destination = tmp_path / "relocated" / "nested" / "moved-project"
    harness = SessionHarness(source)
    harness.workspace = Workspace.at(source)
    harness.workspace.ensure_directories()
    controller = harness.open_session({"case-a": "hello"}, objectives=("answers are correct",))
    session = controller.session
    decision = controller.current_decision()
    session_dir = harness.workspace.root / "sessions" / session.session_id
    plan_path = session_dir / decision.plan_file
    plan = ExecutablePlan.model_validate_json(plan_path.read_bytes())
    external_app = tmp_path / "external-app.json"
    external_app.write_text("{}", encoding="utf-8")
    relative_external_app = Path(os.path.relpath(external_app, session_dir)).as_posix()
    plan = plan.model_copy(update={"application": relative_external_app})
    _, plan_hash = freeze_plan(plan)
    plan_file = f"plan-{plan_hash.split(':', 1)[-1][:16]}.json"
    new_plan_path = session_dir / plan_file
    new_plan_path.write_bytes(plan.model_dump_json().encode("utf-8"))
    draft = deep_unfreeze(decision.draft)
    draft["plan_file"] = plan_file
    draft["plan_hash"] = plan_hash
    updated_decision = decision.model_copy(
        update={
            "choices": decision.choices.model_copy(update={"application": str(external_app)}),
            "plan_file": plan_file,
            "plan_hash": plan_hash,
            "draft": draft,
        }
    )
    controller.storage.db.connection.execute(
        "UPDATE decision_records SET data = ? WHERE decision_id = ?",
        (updated_decision.model_dump_json(), decision.decision_id),
    )
    controller.storage.db.close()
    destination.parent.mkdir(parents=True)
    source.rename(destination)

    result = runner.invoke(
        app,
        ["workspace", "migrate-paths", "--workspace", str(destination), "--apply", "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "change the frozen plan's application path" in json.loads(result.stdout)["message"]
    database = Database.open_readonly(Workspace.at(destination).db_path)
    try:
        sessions = SessionStore(Storage(database))
        unchanged = sessions.get_session(session.session_id)
        untouched = sessions.get_decision(decision.decision_id)
        assert unchanged.project_root == str(source)
        assert untouched.model_dump_json() == updated_decision.model_dump_json()
    finally:
        database.close()
