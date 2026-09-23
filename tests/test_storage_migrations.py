"""02-T1/02-G3: migration application, idempotency, and atomic-DDL failure behavior."""

from __future__ import annotations

import sqlite3

import pytest

from aibench.storage.migrations import MIGRATIONS, applied_versions, apply_migrations


def _connect_in_memory() -> sqlite3.Connection:
    """No temp directory needed: these tests exercise pure migration/DDL logic against an
    in-memory database. Only `test_reopening_database_reapplies_migrations_idempotently`
    below genuinely needs a real file, since it tests that two separate connections to the
    *same* database see consistent state."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_apply_migrations_applies_all_versions_in_order() -> None:
    conn = _connect_in_memory()
    applied = apply_migrations(conn)
    assert applied == [m.version for m in MIGRATIONS]
    assert applied_versions(conn) == {m.version for m in MIGRATIONS}
    conn.close()


def test_apply_migrations_is_idempotent() -> None:
    conn = _connect_in_memory()
    apply_migrations(conn)
    second_call = apply_migrations(conn)
    assert second_call == []  # nothing new to apply
    conn.close()


def test_reopening_database_reapplies_migrations_idempotently(tmp_path) -> None:
    """Simulates process restart: a fresh connection to the same file must not error or
    duplicate schema objects."""
    db_path = tmp_path / "restart.db"
    conn1 = sqlite3.connect(str(db_path), isolation_level=None)
    conn1.execute("PRAGMA foreign_keys = ON")
    apply_migrations(conn1)
    conn1.close()

    conn2 = sqlite3.connect(str(db_path), isolation_level=None)
    conn2.execute("PRAGMA foreign_keys = ON")
    applied_again = apply_migrations(conn2)
    assert applied_again == []
    tables = {
        row[0]
        for row in conn2.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "runs" in tables
    conn2.close()


def test_expected_tables_exist_after_migration() -> None:
    conn = _connect_in_memory()
    apply_migrations(conn)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    expected = {
        "datasets",
        "cases",
        "applications",
        "profiles",
        "plans",
        "runs",
        "work_items",
        "execution_attempts",
        "evaluation_attempts",
        "metric_results",
        "artifacts",
        "usage_events",
        "approvals",
        "schema_migrations",
    }
    assert expected.issubset(tables)
    # run_events and run_leases arrived with the engine (Prompt 06, migrations 5-6; ADR 0005).
    assert {"run_events", "run_leases"} <= tables
    # Session/conversation tables are explicitly reserved for Prompt 08 (02-T4).
    reserved_for_prompt_08 = {
        "sessions",
        "conversation_turns",
        "decision_records",
        "pending_questions",
        "action_requests",
    }
    assert reserved_for_prompt_08.isdisjoint(tables)


def test_foreign_keys_are_enforced() -> None:
    conn = _connect_in_memory()
    apply_migrations(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cases (dataset_content_hash, case_id, data, committed_at) "
            "VALUES ('sha256:does-not-exist', 'c1', '{}', 'now')"
        )


def test_a_failing_migration_leaves_no_partial_schema() -> None:
    """A migration script that fails partway must not leave orphan tables behind, and must
    not be recorded as applied — atomic DDL via BEGIN/COMMIT (see migrations.py)."""
    from dataclasses import dataclass

    from aibench.storage import migrations as migrations_module

    @dataclass(frozen=True)
    class _Broken:
        version: int
        name: str
        sql: str

    broken = _Broken(
        version=9999,
        name="broken_migration",
        sql="CREATE TABLE should_not_persist (id INTEGER); THIS IS NOT VALID SQL;",
    )

    conn = _connect_in_memory()
    original_migrations = migrations_module.MIGRATIONS
    migrations_module.MIGRATIONS = (*original_migrations, broken)  # type: ignore[assignment]
    try:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn)
    finally:
        migrations_module.MIGRATIONS = original_migrations  # type: ignore[assignment]

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert "should_not_persist" not in tables
    assert 9999 not in applied_versions(conn)
    conn.close()


def test_upgrading_a_pre_remediation_database_backfills_content_hash_columns() -> None:
    """The exact scenario a code review asked for: a database created before migration 3
    (which added the `content_hash`/`manifest_hash` columns used for full-content conflict
    detection) existed must upgrade cleanly, with existing rows correctly backfilled — so a
    later commit of byte-identical content is still recognized as idempotent, not rejected
    as a false-positive `ConflictError`."""
    import sqlite3

    from aibench.core.errors import ConflictError
    from aibench.core.models import ArtifactRef, WorkItem
    from aibench.storage import migrations as migrations_module
    from aibench.storage.db import Database
    from aibench.storage.repositories import Storage

    conn = _connect_in_memory()
    original_migrations = migrations_module.MIGRATIONS
    pre_remediation_migrations = tuple(m for m in original_migrations if m.version <= 2)
    assert len(pre_remediation_migrations) == 2  # sanity: versions 1 and 2 only
    migrations_module.MIGRATIONS = pre_remediation_migrations
    try:
        apply_migrations(conn)  # the database exactly as it looked before migration 3
        # Legacy rows, inserted the way the pre-fix code would have — no content_hash column
        # exists on work_items/artifacts yet at this point.
        conn.execute(
            "INSERT INTO runs (run_id, dataset_hash, application_hash, plan_hash, "
            "content_hash, status, data, created_at, committed_at, updated_at) VALUES "
            "('r1', 'sha256:d', 'sha256:a', 'sha256:p', 'x', 'created', '{}', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', "
            "'2026-01-01T00:00:00+00:00')"
        )
        legacy_work_item = WorkItem(
            work_item_id="w1", run_id="r1", task_key="case:c1:exec", kind="execution"
        )
        conn.execute(
            "INSERT INTO work_items (work_item_id, run_id, task_key, kind, state, attempt, "
            "data, committed_at, updated_at) VALUES (?, 'r1', 'case:c1:exec', 'execution', "
            "'pending', 0, ?, 'now', 'now')",
            (legacy_work_item.work_item_id, legacy_work_item.model_dump_json()),
        )
        conn.execute(
            "INSERT INTO artifacts (artifact_id, digest, uri, mime_type, size_bytes, "
            "redaction, run_id, committed_at) VALUES "
            "('a1', 'sha256:aaa', '/legacy/path', 'text/plain', 3, 'none', 'r1', 'now')"
        )
    finally:
        migrations_module.MIGRATIONS = original_migrations

    # Upgrade: apply the full, current migration set from migration 3 onward.
    applied = apply_migrations(conn)
    assert applied == [3, 4, 5, 6]

    db = Database(conn)
    storage = Storage(db)

    # The backfilled content_hash must match what committing this exact content fresh would
    # compute — proven by the retry being a no-op, not a conflict.
    assert storage.commit_work_item(legacy_work_item) is False

    legacy_artifact = ArtifactRef(
        artifact_id="a1",
        digest="sha256:aaa",
        uri="/legacy/path",
        mime_type="text/plain",
        size_bytes=3,
        run_id="r1",
    )
    assert storage.commit_artifact_unverified(legacy_artifact) is False

    # A genuinely different work item reusing the same (run_id, task_key) is still rejected
    # — the backfill didn't just make everything permissive.
    with pytest.raises(sqlite3.IntegrityError):
        storage.commit_work_item(
            WorkItem(work_item_id="w2", run_id="r1", task_key="case:c1:exec", kind="execution")
        )

    # And a genuinely different artifact under the same artifact_id is still a conflict.
    with pytest.raises(ConflictError):
        storage.commit_artifact_unverified(legacy_artifact.model_copy(update={"size_bytes": 999}))

    db.close()


def test_migration_4_rekeys_existing_evaluation_attempts() -> None:
    """A database scored before migration 4 keeps its attempts, which gain repetition,
    binding and scoring keys from their stored JSON; rescoring then continues numbering."""
    from aibench.core.models import Decision, EvaluationResult, ExecutionStatus, RunManifest
    from aibench.storage import migrations as migrations_module
    from aibench.storage.db import Database
    from aibench.storage.repositories import Storage

    conn = _connect_in_memory()
    conn.row_factory = sqlite3.Row  # as Database.open does; Storage reads rows by name
    original = migrations_module.MIGRATIONS
    try:
        migrations_module.MIGRATIONS = tuple(m for m in original if m.version < 4)  # type: ignore[assignment]
        apply_migrations(conn)
    finally:
        migrations_module.MIGRATIONS = original  # type: ignore[assignment]
    legacy = EvaluationResult(
        result_id="old",
        run_id="r1",
        case_id="c1",
        metric_id="native.exact_match",
        metric_version="1.0.0",
        status=ExecutionStatus.OK,
        decision=Decision.PASS,
        repetition_id=2,
        binding_hash="sha256:b",
        scoring_id="score-old",
    )
    storage = Storage(Database(conn))
    storage.commit_run(
        RunManifest(run_id="r1", dataset_hash="d", application_hash="a", plan_hash="p")
    )
    conn.execute(
        "INSERT INTO evaluation_attempts (run_id, case_id, metric_id, attempt_number, status, "
        "decision, content_hash, data, committed_at) VALUES ('r1','c1','native.exact_match',0,"
        "'ok','pass','h',?, 'now')",
        (legacy.model_dump_json(),),
    )
    assert apply_migrations(conn) == [4, 5, 6]
    row = conn.execute(
        "SELECT repetition_id, binding_hash, scoring_id FROM evaluation_attempts"
    ).fetchone()
    assert tuple(row) == (2, "sha256:b", "score-old")
    assert storage.list_evaluation_attempts("r1") == [legacy]
    assert (
        storage.next_evaluation_attempt_number("r1", "c1", 2, "native.exact_match", "sha256:b") == 1
    )
