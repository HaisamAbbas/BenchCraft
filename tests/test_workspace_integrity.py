"""Workspace checks validate catalogs and the durable files that benchmark runs depend on."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.datasets.suites import register_dataset_suite
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.storage.workspace_integrity import check_workspace
from tests.session_support import SessionHarness

cli = CliRunner()


def _project(path: Path) -> tuple[Path, Workspace]:
    path.mkdir(parents=True)
    workspace = Workspace.at(path)
    Database.open_workspace(workspace).close()
    return path, workspace


def test_workspace_check_is_read_only_and_reports_a_healthy_workspace(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    wal_path = Path(f"{workspace.db_path}-wal")
    shm_path = Path(f"{workspace.db_path}-shm")
    assert not wal_path.exists()
    assert not shm_path.exists()

    result = cli.invoke(app, ["workspace", "check", "--workspace", str(project), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["issue_count"] == 0
    assert payload["checks"]["database"]["integrity_messages"] == ["ok"]
    assert payload["checks"]["artifacts"] == {
        "status": "ok",
        "records": 0,
        "verified": 0,
    }
    assert not list(workspace.root.glob(".aibench-check-*"))
    assert not wal_path.exists()
    assert not shm_path.exists()


def test_workspace_check_reports_artifact_content_corruption(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    database = Database.open_workspace(workspace)
    store = ArtifactStore(workspace.artifacts_dir)
    reference = store.write_bytes(b"good bytes", mime_type="text/plain")
    commit_verified_artifact(store, Storage(database), reference)
    database.close()
    Path(reference.uri).write_bytes(b"evil bytes")

    result = check_workspace(project)

    assert result["ok"] is False
    assert result["checks"]["artifacts"]["records"] == 1
    assert result["checks"]["artifacts"]["verified"] == 0
    assert any(
        issue["category"] == "artifacts"
        and issue["code"] == "artifact_invalid"
        and "digest mismatch" in issue["message"]
        for issue in result["issues"]
    )
    assert Path(reference.uri).read_bytes() == b"evil bytes"


def test_workspace_check_cli_returns_one_and_json_for_integrity_issues(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    database = Database.open_workspace(workspace)
    store = ArtifactStore(workspace.artifacts_dir)
    reference = store.write_bytes(b"healthy", mime_type="text/plain")
    commit_verified_artifact(store, Storage(database), reference)
    database.close()
    Path(reference.uri).write_bytes(b"tampered")

    result = cli.invoke(app, ["workspace", "check", "--workspace", str(project), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["issue_count"] == 1
    assert payload["issues"][0]["category"] == "artifacts"
    assert Path(reference.uri).read_bytes() == b"tampered"


def test_workspace_check_validates_frozen_plans_and_suite_snapshots(tmp_path: Path) -> None:
    harness = SessionHarness(tmp_path / "project")
    controller = harness.open_session({"case-a": "input"}, objectives=("check plan",))
    project = harness.root / "project"
    decision = controller.current_decision()
    suite_source = tmp_path / "suite.jsonl"
    suite_source.write_text('{"case_id":"suite-case","input":"value"}\n', encoding="utf-8")
    register_dataset_suite(project, "support", "1.0.0", suite_source)
    controller.storage.db.close()

    healthy = check_workspace(project)

    assert healthy["ok"] is True, healthy["issues"]
    assert healthy["checks"]["session_plans"]["records"] >= 1
    assert healthy["checks"]["session_plans"]["verified"] >= 1
    assert healthy["checks"]["dataset_suites"] == {
        "status": "ok",
        "records": 1,
        "verified": 1,
    }

    plan = harness.workspace.root / "sessions" / controller.session_id / decision.plan_file
    plan.write_text("{not a frozen plan}", encoding="utf-8")
    damaged_plan = check_workspace(project)
    assert damaged_plan["ok"] is False
    assert any(issue["category"] == "session_plans" for issue in damaged_plan["issues"])

    snapshot = next((Workspace.at(project).root / "dataset-suites").rglob("*.jsonl"))
    snapshot.write_text('{"case_id":"suite-case","input":"other"}\n', encoding="utf-8")
    damaged = check_workspace(project)

    assert damaged["ok"] is False
    assert any(issue["code"] == "plan_invalid" for issue in damaged["issues"])
    assert any(issue["code"] == "snapshot_invalid" for issue in damaged["issues"])


def test_workspace_check_parses_a_pinned_suite_snapshot_during_path_swap(
    tmp_path: Path, monkeypatch
) -> None:
    project, workspace = _project(tmp_path / "project")
    suite_source = tmp_path / "suite.jsonl"
    valid = b'{"case_id":"suite-case","input":"value"}\n'
    corrupted = b'{"case_id":"suite-case","input":"other"}\n'
    suite_source.write_bytes(valid)
    register_dataset_suite(project, "support", "1.0.0", suite_source)
    snapshot = next((workspace.root / "dataset-suites").rglob("*.jsonl"))
    original_metadata = snapshot.stat()
    snapshot.write_bytes(corrupted)
    os.utime(snapshot, ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns))

    from aibench.storage import workspace_integrity as integrity_module

    real_ingest = integrity_module.ingest_dataset

    def swap_source_while_ingesting(path: Path, **kwargs):
        if Path(path).resolve() == snapshot.resolve():
            try:
                snapshot.write_bytes(valid)
                report = real_ingest(path, **kwargs)
            finally:
                snapshot.write_bytes(corrupted)
                os.utime(
                    snapshot,
                    ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
                )
            return report
        return real_ingest(path, **kwargs)

    monkeypatch.setattr(integrity_module, "ingest_dataset", swap_source_while_ingesting)

    result = check_workspace(project)

    assert result["ok"] is False
    assert any(issue["code"] == "snapshot_invalid" for issue in result["issues"])
    assert snapshot.read_bytes() == corrupted


def test_workspace_check_reports_deeply_nested_suite_json_as_invalid(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    suite_source = tmp_path / "suite.jsonl"
    suite_source.write_text('{"case_id":"suite-case","input":"value"}\n', encoding="utf-8")
    register_dataset_suite(project, "support", "1.0.0", suite_source)
    snapshot = next((workspace.root / "dataset-suites").rglob("*.jsonl"))
    snapshot.write_text("[" * 5_000 + "]" * 5_000 + "\n", encoding="utf-8")

    result = check_workspace(project)

    assert result["ok"] is False
    assert any(
        issue["category"] == "dataset_suites" and issue["code"] == "snapshot_invalid"
        for issue in result["issues"]
    )


def test_workspace_check_reports_foreign_key_violations(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    connection = sqlite3.connect(workspace.db_path)
    try:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "INSERT INTO cases (dataset_content_hash, case_id, data, committed_at) "
            "VALUES ('sha256:missing', 'orphan', '{}', 'test')"
        )
        connection.commit()
    finally:
        connection.close()

    result = check_workspace(project)

    assert result["ok"] is False
    assert result["checks"]["database"]["foreign_key_errors"]
    assert any(issue["code"] == "foreign_key" for issue in result["issues"])


def test_workspace_check_includes_committed_wal_rows_without_changing_workspace(
    tmp_path: Path,
) -> None:
    project, workspace = _project(tmp_path / "project")
    database = Database.open_workspace(workspace)
    try:
        database.connection.execute("PRAGMA foreign_keys = OFF")
        database.connection.execute(
            "INSERT INTO cases (dataset_content_hash, case_id, data, committed_at) "
            "VALUES ('sha256:missing', 'wal-orphan', '{}', 'test')"
        )
        wal_path = Path(f"{workspace.db_path}-wal")
        assert wal_path.is_file() and wal_path.stat().st_size > 0

        result = check_workspace(project)

        assert result["ok"] is False
        assert result["checks"]["database"]["foreign_key_errors"]
        assert any(issue["code"] == "foreign_key" for issue in result["issues"])
        assert wal_path.is_file() and wal_path.stat().st_size > 0
    finally:
        database.close()


def test_workspace_check_locks_windows_database_during_snapshot_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows byte-range locking is platform-specific")
    project, workspace = _project(tmp_path / "project")
    database = Database.open_workspace(workspace)
    large_payload = "x" * (2 * 1024 * 1024)
    database.connection.execute(
        "INSERT INTO runs (run_id, dataset_hash, application_hash, plan_hash, content_hash, "
        "status, data, created_at, committed_at, updated_at) "
        "VALUES ('large-run', 'dataset', 'application', 'plan', 'content', 'finished', ?, "
        "'test', 'test', 'test')",
        (large_payload,),
    )
    database.close()
    original = workspace.db_path.read_bytes()
    assert len(original) > 1024 * 1024

    from aibench.storage import workspace_integrity as integrity_module

    real_read = os.read
    attempts: list[str] = []

    def rewrite_after_first_block(descriptor: int, size: int) -> bytes:
        block = real_read(descriptor, size)
        if block and not attempts:
            try:
                workspace.db_path.write_bytes(b"x" * len(original))
            except OSError:
                attempts.append("blocked")
            else:
                attempts.append("unexpectedly wrote")
        return block

    monkeypatch.setattr(integrity_module.os, "read", rewrite_after_first_block)

    result = check_workspace(project)

    assert attempts == ["blocked"]
    assert result["ok"] is True, result["issues"]
    assert workspace.db_path.read_bytes() == original


def test_workspace_check_detects_missing_physical_schema_tables(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    connection = sqlite3.connect(workspace.db_path)
    try:
        connection.execute("DROP TABLE decision_records")
        connection.commit()
    finally:
        connection.close()

    result = check_workspace(project)

    assert result["ok"] is False
    schema_issue = next(issue for issue in result["issues"] if issue["code"] == "physical_schema")
    assert "decision_records" in schema_issue["details"]["missing_tables"]


def test_workspace_check_detects_missing_physical_schema_columns(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    connection = sqlite3.connect(workspace.db_path)
    try:
        connection.execute("ALTER TABLE decision_records RENAME COLUMN data TO data_removed")
        connection.commit()
    finally:
        connection.close()

    result = check_workspace(project)

    assert result["ok"] is False
    schema_issue = next(issue for issue in result["issues"] if issue["code"] == "physical_schema")
    assert "data" in schema_issue["details"]["missing_columns"]["decision_records"]


def test_workspace_check_detects_missing_unique_schema_indexes(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    connection = sqlite3.connect(workspace.db_path)
    try:
        connection.execute("DROP INDEX idx_turns_reply")
        connection.commit()
    finally:
        connection.close()

    result = check_workspace(project)

    assert result["ok"] is False
    schema_issue = next(issue for issue in result["issues"] if issue["code"] == "physical_schema")
    assert "index:idx_turns_reply" in schema_issue["details"]["missing_objects"]


def test_workspace_check_detects_unsupported_sabotage_triggers(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    connection = sqlite3.connect(workspace.db_path)
    try:
        connection.executescript(
            "CREATE TRIGGER unexpected_sabotage AFTER INSERT ON runs BEGIN DELETE FROM cases; END;"
            "CREATE TRIGGER sqliteXsabotage AFTER INSERT ON runs BEGIN DELETE FROM cases; END;"
        )
        connection.commit()
    finally:
        connection.close()

    result = check_workspace(project)

    assert result["ok"] is False
    schema_issue = next(issue for issue in result["issues"] if issue["code"] == "physical_schema")
    assert "trigger:unexpected_sabotage" in schema_issue["details"]["unexpected_objects"]
    assert "trigger:sqliteXsabotage" in schema_issue["details"]["unexpected_objects"]


def test_workspace_check_detects_reserved_trigger_injected_into_schema(tmp_path: Path) -> None:
    project, workspace = _project(tmp_path / "project")
    connection = sqlite3.connect(workspace.db_path)
    try:
        version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        connection.execute("PRAGMA writable_schema = ON")
        connection.execute(
            "INSERT INTO sqlite_master(type, name, tbl_name, rootpage, sql) "
            "VALUES ('trigger', ?, 'runs', 0, ?) ",
            (
                "sqlite_sabotage",
                (
                    "CREATE TRIGGER sqlite_sabotage AFTER INSERT ON runs "
                    "BEGIN SELECT RAISE(ABORT, 'sabotaged'); END"
                ),
            ),
        )
        connection.execute(f"PRAGMA schema_version = {version + 1}")
        connection.execute("PRAGMA writable_schema = OFF")
        connection.commit()
    finally:
        connection.close()

    result = check_workspace(project)

    assert result["ok"] is False
    schema_issue = next(issue for issue in result["issues"] if issue["code"] == "physical_schema")
    assert "trigger:sqlite_sabotage" in schema_issue["details"]["unexpected_objects"]


def test_workspace_check_requires_an_existing_workspace_without_creating_one(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = cli.invoke(app, ["workspace", "check", "--workspace", str(project), "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not (project / ".aibench").exists()
