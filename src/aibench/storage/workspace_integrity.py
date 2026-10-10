"""Read-only integrity checks for workspace catalogs and their durable files."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from functools import lru_cache
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import ValidationError
from aibench.core.hashes import content_hash
from aibench.core.models import ArtifactRef
from aibench.core.plans import ExecutablePlan
from aibench.core.sessions import DecisionRecord
from aibench.datasets.ingest import ingest_dataset
from aibench.engine.compile import freeze_plan
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.migrations import MIGRATIONS

_MAX_ISSUES = 100
_MAX_CATALOG_ROWS = 1_000_000
_MAX_ARTIFACT_CATALOG_BYTES = 1 << 30
_MAX_SCHEMA_OBJECTS = 4_096
_MAX_SCHEMA_BYTES = 64 * 1024 * 1024
_MAX_DATABASE_SNAPSHOT_BYTES = 1 << 40
_MAX_DECISION_ROWS = 1_000_000
_MAX_DECISION_BYTES = 1 << 30
_MAX_PLAN_BYTES = 32 * 1024 * 1024
_MAX_SUITE_ROWS = 1_000_000
_MAX_SUITE_BYTES = 1 << 30
_MAX_SUITE_FILE_BYTES = 1 << 40
_ARTIFACT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}$")
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
_PLAN_FILE = re.compile(r"^plan-[0-9a-f]{16}\.json$")
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def check_workspace(project_root: Path) -> dict[str, Any]:
    """Check SQLite, artifact, frozen-session-plan, and dataset-suite integrity."""
    try:
        root = project_root.expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve project root: {exc}") from exc
    if not root.is_dir():
        raise ValidationError("project root must be an existing directory")

    workspace = Workspace(root=(root / ".aibench").absolute())
    metadata = _lstat(workspace.root, "workspace directory")
    if _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError("workspace .aibench directory must be a real directory")
    database_path = workspace.db_path
    database_metadata = _lstat(database_path, "workspace database")
    if _is_link(database_metadata) or not stat.S_ISREG(database_metadata.st_mode):
        raise ValidationError("workspace database must be a regular file, not a link")

    result: dict[str, Any] = {
        "workspace": str(workspace.root),
        "ok": False,
        "issues": [],
        "issue_count": 0,
        "issues_truncated": False,
        "checks": {
            "database": {"status": "not_checked"},
            "artifacts": {"status": "not_checked", "records": 0, "verified": 0},
            "session_plans": {"status": "not_checked", "records": 0, "verified": 0},
            "dataset_suites": {"status": "not_checked", "records": 0, "verified": 0},
        },
    }

    with _workspace_database_snapshot(database_path) as snapshot_path:
        snapshot_metadata = _lstat(snapshot_path, "workspace database snapshot")
        database = Database.open_readonly(snapshot_path)
        try:
            _verify_open_database(database, snapshot_path, snapshot_metadata)
            connection = database.connection
            connection.execute("BEGIN")
            _check_sqlite(connection, result)
            current_schema = _check_schema(connection, result)
            if current_schema:
                _check_artifacts(connection, workspace, result)
                _check_session_plans(connection, workspace, result)
                _check_dataset_suites(connection, workspace, result)
            _verify_database_unchanged(snapshot_path, snapshot_metadata)
        finally:
            if database.connection.in_transaction:
                database.connection.execute("ROLLBACK")
            database.close()

    result["ok"] = result["issue_count"] == 0
    for check in result["checks"].values():
        if check["status"] == "not_checked":
            continue
        check["status"] = "ok" if check.pop("issue_count", 0) == 0 else "issues"
    return result


@contextmanager
def _workspace_database_snapshot(database_path: Path) -> Iterator[Path]:
    """Copy a quiescent DB and its WAL/journal for inspection outside the workspace."""
    sources: dict[Path, os.stat_result | None] = {}
    for suffix in ("", "-wal", "-journal"):
        source = Path(f"{database_path}{suffix}")
        metadata = _lstat_optional(source)
        sources[source] = metadata
        if metadata is None:
            if not suffix:
                raise ValidationError("workspace database disappeared before it could be checked")
            continue
        if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(f"workspace SQLite file must be regular: {source.name}")

    wal_path = Path(f"{database_path}-wal")
    shm_path = Path(f"{database_path}-shm")
    wal_metadata = sources[wal_path]
    shm_metadata = _lstat_optional(shm_path)
    if shm_metadata is not None and (
        _is_link(shm_metadata) or not stat.S_ISREG(shm_metadata.st_mode)
    ):
        raise ValidationError(f"workspace SQLite file must be regular: {shm_path.name}")
    if (
        wal_metadata is not None
        and shm_metadata is not None
        and sources[Path(f"{database_path}-journal")] is None
    ):
        # SQLite can take a locked, read-only WAL snapshot without creating sidecars when
        # the shared-memory index already exists. This also includes live committed WAL rows.
        try:
            yield database_path
        finally:
            _verify_database_sources(sources)
        return

    total_bytes = sum(metadata.st_size for metadata in sources.values() if metadata is not None)
    if total_bytes > _MAX_DATABASE_SNAPSHOT_BYTES:
        raise ValidationError("workspace database and sidecars exceed the supported check size")

    with tempfile.TemporaryDirectory(prefix="aibench-check-db-") as temporary:
        snapshot_path = Path(temporary) / database_path.name
        descriptors: dict[Path, int] = {}
        try:
            for source, metadata in sources.items():
                if metadata is not None:
                    descriptors[source] = _open_regular_file(source, metadata)
            with ExitStack() as locks:
                for source, descriptor in descriptors.items():
                    metadata = sources[source]
                    if metadata is None:
                        raise AssertionError("opened SQLite source has no metadata")
                    locks.enter_context(
                        _lock_regular_file(
                            descriptor,
                            metadata.st_size,
                            maximum_bytes=_MAX_DATABASE_SNAPSHOT_BYTES,
                            sqlite_database=source == database_path,
                        )
                    )
                _verify_database_sources(sources)
                for source, metadata in sources.items():
                    if metadata is None:
                        continue
                    descriptor = descriptors[source]
                    target = Path(f"{snapshot_path}{source.name[len(database_path.name) :]}")
                    if os.lseek(descriptor, 0, os.SEEK_SET) != 0:
                        raise ValidationError(
                            f"could not rewind workspace SQLite file: {source.name}"
                        )
                    opened = os.fstat(descriptor)
                    total = 0
                    with target.open("wb") as output:
                        while block := os.read(descriptor, 1024 * 1024):
                            total += len(block)
                            if total > metadata.st_size:
                                raise ValidationError(
                                    f"workspace SQLite file changed while copying: {source.name}"
                                )
                            output.write(block)
                    final = os.fstat(descriptor)
                    current = _lstat(source, "workspace SQLite file")
                    if (
                        total != metadata.st_size
                        or not _same_file(metadata, opened)
                        or not _same_file(opened, final)
                        or not _same_file(final, current)
                    ):
                        raise ValidationError(
                            f"workspace SQLite file changed while copying: {source.name}"
                        )
                _verify_database_sources(sources)
        finally:
            for descriptor in descriptors.values():
                os.close(descriptor)
        journal = next(
            (
                source
                for source, metadata in sources.items()
                if metadata is not None and source.name.endswith("-journal")
            ),
            None,
        )
        if journal is not None:
            # Let SQLite recover a copied hot rollback journal in the private snapshot.
            recovery = sqlite3.connect(str(snapshot_path), isolation_level=None)
            try:
                recovery.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            except sqlite3.Error as exc:
                raise ValidationError(
                    f"could not recover the workspace database snapshot: {exc}"
                ) from exc
            finally:
                recovery.close()
        _verify_database_sources(sources)
        try:
            yield snapshot_path
        finally:
            _verify_database_sources(sources)


def _verify_database_sources(sources: dict[Path, os.stat_result | None]) -> None:
    for path, expected in sources.items():
        current = _lstat_optional(path)
        if expected is None:
            if current is not None:
                raise ValidationError(
                    f"workspace SQLite sidecar appeared during check: {path.name}"
                )
        elif (
            current is None
            or _is_link(current)
            or not stat.S_ISREG(current.st_mode)
            or not _same_file(expected, current)
        ):
            raise ValidationError(f"workspace SQLite file changed during check: {path.name}")


def _check_sqlite(connection: sqlite3.Connection, result: dict[str, Any]) -> None:
    checks = result["checks"]
    database_check = checks["database"]
    database_check["status"] = "checked"
    database_check["integrity_messages"] = []
    database_check["foreign_key_errors"] = []
    try:
        messages = connection.execute("PRAGMA integrity_check(101)").fetchmany(101)
        database_check["integrity_messages"] = [str(row[0])[:512] for row in messages[:100]]
        if len(messages) != 1 or messages[0][0] != "ok":
            _add_issue(
                result,
                "database",
                "sqlite_integrity",
                "aibench.db",
                "SQLite integrity_check reported database corruption",
                details=database_check["integrity_messages"],
            )
            if len(messages) > 100:
                result["issues_truncated"] = True
    except sqlite3.Error as exc:
        _add_issue(result, "database", "sqlite_integrity", "aibench.db", str(exc))
    try:
        rows = connection.execute("PRAGMA foreign_key_check").fetchmany(101)
        database_check["foreign_key_errors"] = [
            {
                "table": str(row[0])[:256],
                "rowid": row[1],
                "parent": str(row[2])[:256],
                "constraint": row[3],
            }
            for row in rows[:100]
        ]
        if rows:
            _add_issue(
                result,
                "database",
                "foreign_key",
                "aibench.db",
                "database contains broken foreign-key references",
                details=database_check["foreign_key_errors"],
            )
            if len(rows) > 100:
                result["issues_truncated"] = True
    except sqlite3.Error as exc:
        _add_issue(result, "database", "foreign_key", "aibench.db", str(exc))


def _check_schema(connection: sqlite3.Connection, result: dict[str, Any]) -> bool:
    check = result["checks"]["database"]
    current = max(migration.version for migration in MIGRATIONS)
    check["expected_schema_version"] = current
    try:
        rows = connection.execute(
            "SELECT version, name FROM schema_migrations LIMIT ?", (len(MIGRATIONS) + 1,)
        ).fetchall()
        versions = {int(row[0]) for row in rows}
        names = {int(row[0]): str(row[1]) for row in rows}
    except (sqlite3.Error, ValueError, TypeError) as exc:
        _add_issue(
            result,
            "database",
            "schema_migrations",
            "aibench.db",
            f"could not read schema migration history: {exc}",
        )
        return False
    expected = {migration.version: migration.name for migration in MIGRATIONS}
    if len(rows) > len(expected):
        _add_issue(
            result,
            "database",
            "schema_migrations_limit",
            "aibench.db",
            "workspace migration history exceeds the supported migration count",
        )
        return False
    missing = sorted(set(expected) - versions)
    unknown = sorted(versions - set(expected))
    mismatched = sorted(v for v in versions & set(expected) if names.get(v) != expected[v])
    check["schema_version"] = max(versions, default=0)
    check["missing_migrations"] = missing
    check["unknown_migrations"] = unknown
    check["mismatched_migrations"] = mismatched
    if missing or unknown or mismatched:
        _add_issue(
            result,
            "database",
            "schema_version",
            "aibench.db",
            "workspace schema does not match this aibench version",
            details={"missing": missing, "unknown": unknown, "mismatched": mismatched},
        )
        return False
    return _check_physical_schema(connection, result)


@lru_cache(maxsize=1)
def _expected_schema_catalog() -> tuple[
    dict[str, frozenset[str]], dict[tuple[str, str], str | None]
]:
    """Get required tables, columns, indexes, and constraints from shipped migrations."""
    database = Database.open_in_memory()
    try:
        connection = database.connection
        table_names = [
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        ]
        tables = {
            table: frozenset(
                str(column[1])
                for column in connection.execute(f"PRAGMA table_info({json.dumps(table)})")
            )
            for table in table_names
        }
        objects = {
            (str(row[0]), str(row[1])): None if row[2] is None else str(row[2])
            for row in connection.execute(
                "SELECT type, name, sql FROM sqlite_master "
                "WHERE type IN ('table', 'index', 'trigger', 'view')"
            )
        }
        return tables, objects
    finally:
        database.close()


def _check_physical_schema(connection: sqlite3.Connection, result: dict[str, Any]) -> bool:
    object_count, object_bytes = connection.execute(
        "SELECT count(*), COALESCE(sum("
        "COALESCE(length(CAST(name AS BLOB)), 0) + "
        "COALESCE(length(CAST(tbl_name AS BLOB)), 0) + "
        "COALESCE(length(CAST(sql AS BLOB)), 0)), 0) FROM sqlite_master "
        "WHERE type IN ('table', 'index', 'trigger', 'view')"
    ).fetchone()
    if object_count > _MAX_SCHEMA_OBJECTS or object_bytes > _MAX_SCHEMA_BYTES:
        _add_issue(
            result,
            "database",
            "physical_schema_limit",
            "aibench.db",
            "workspace schema catalog exceeds the integrity-check size or object limit",
        )
        return False

    expected_tables, expected_objects = _expected_schema_catalog()
    actual_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    missing_tables = sorted(set(expected_tables) - actual_tables)
    missing_columns: dict[str, list[str]] = {}
    for table in sorted(set(expected_tables) & actual_tables):
        actual_columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({json.dumps(table)})")
        }
        missing = sorted(expected_tables[table] - actual_columns)
        if missing:
            missing_columns[table] = missing
    actual_objects = {
        (str(row[0]), str(row[1])): None if row[2] is None else str(row[2])
        for row in connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        )
    }
    missing_objects = sorted(expected_objects.keys() - actual_objects.keys())
    unexpected_objects = sorted(actual_objects.keys() - expected_objects.keys())
    changed_objects = sorted(
        key for key, sql in expected_objects.items() if actual_objects.get(key) != sql
    )
    if (
        missing_tables
        or missing_columns
        or missing_objects
        or unexpected_objects
        or changed_objects
    ):
        _add_issue(
            result,
            "database",
            "physical_schema",
            "aibench.db",
            "workspace tables, columns, indexes, or constraints differ from shipped migrations",
            details={
                "missing_tables": missing_tables,
                "missing_columns": missing_columns,
                "missing_objects": [f"{kind}:{name}" for kind, name in missing_objects],
                "unexpected_objects": [f"{kind}:{name}" for kind, name in unexpected_objects],
                "changed_objects": [f"{kind}:{name}" for kind, name in changed_objects],
            },
        )
        return False
    return True


def _check_artifacts(
    connection: sqlite3.Connection, workspace: Workspace, result: dict[str, Any]
) -> None:
    check = result["checks"]["artifacts"]
    check["status"] = "checked"
    store = ArtifactStore(workspace.artifacts_dir, create=False)
    try:
        count, catalog_bytes = connection.execute(
            "SELECT count(*), COALESCE(sum("
            "COALESCE(length(CAST(artifact_id AS BLOB)), 0) + "
            "COALESCE(length(CAST(digest AS BLOB)), 0) + "
            "COALESCE(length(CAST(uri AS BLOB)), 0) + "
            "COALESCE(length(CAST(mime_type AS BLOB)), 0) + "
            "COALESCE(length(CAST(size_bytes AS BLOB)), 0) + "
            "COALESCE(length(CAST(redaction AS BLOB)), 0) + "
            "COALESCE(length(CAST(run_id AS BLOB)), 0) + "
            "COALESCE(length(CAST(content_hash AS BLOB)), 0)), 0) FROM artifacts"
        ).fetchone()
        count = int(count)
        check["records"] = count
        if count > _MAX_CATALOG_ROWS or catalog_bytes > _MAX_ARTIFACT_CATALOG_BYTES:
            _add_issue(
                result,
                "artifacts",
                "catalog_limit",
                "artifacts",
                "artifact catalog exceeds the integrity-check size or row limit",
            )
            return
        rows = connection.execute(
            "SELECT artifact_id, digest, uri, mime_type, size_bytes, redaction, run_id, "
            "content_hash FROM artifacts ORDER BY artifact_id"
        )
        while batch := rows.fetchmany(128):
            for row in batch:
                artifact_id = str(row[0])[:512]
                try:
                    ref = ArtifactRef.model_validate(
                        {
                            "artifact_id": row[0],
                            "digest": row[1],
                            "uri": row[2],
                            "mime_type": row[3],
                            "size_bytes": row[4],
                            "redaction": row[5],
                            "run_id": row[6],
                        }
                    )
                    if not _ARTIFACT_ID.fullmatch(ref.artifact_id):
                        raise ValueError("artifact ID is not portable")
                    if row[7] != content_hash(ref.model_dump_json()):
                        _add_issue(
                            result,
                            "artifacts",
                            "catalog_hash",
                            artifact_id,
                            "artifact catalog content hash does not match its record",
                        )
                    store.verify_ref_integrity(ref)
                    check["verified"] += 1
                except (
                    PydanticValidationError,
                    TypeError,
                    ValueError,
                    OSError,
                    ValidationError,
                ) as exc:
                    _add_issue(result, "artifacts", "artifact_invalid", artifact_id, str(exc))
    except sqlite3.Error as exc:
        _add_issue(result, "artifacts", "catalog_unreadable", "artifacts", str(exc))


def _check_session_plans(
    connection: sqlite3.Connection, workspace: Workspace, result: dict[str, Any]
) -> None:
    check = result["checks"]["session_plans"]
    check["status"] = "checked"
    try:
        count, total_bytes = connection.execute(
            "SELECT count(*), COALESCE(sum("
            "COALESCE(length(CAST(decision_id AS BLOB)), 0) + "
            "COALESCE(length(CAST(session_id AS BLOB)), 0) + "
            "COALESCE(length(CAST(revision AS BLOB)), 0) + "
            "COALESCE(length(CAST(data AS BLOB)), 0)), 0) FROM decision_records"
        ).fetchone()
        check["records"] = int(count)
        if count > _MAX_DECISION_ROWS or total_bytes > _MAX_DECISION_BYTES:
            _add_issue(
                result,
                "session_plans",
                "catalog_limit",
                "sessions",
                "decision catalog exceeds the integrity-check size limit",
            )
            return
        seen: dict[tuple[str, str], str] = {}
        session_directories: dict[str, bool] = {}
        sessions_root_metadata = _lstat_optional(workspace.root / "sessions")
        sessions_root_valid = bool(
            sessions_root_metadata is not None
            and not _is_link(sessions_root_metadata)
            and stat.S_ISDIR(sessions_root_metadata.st_mode)
        )
        rows = connection.execute(
            "SELECT decision_id, session_id, revision, data FROM decision_records "
            "ORDER BY session_id, revision"
        )
        while batch := rows.fetchmany(128):
            for row in batch:
                try:
                    decision = DecisionRecord.model_validate_json(row[3])
                    if (
                        decision.decision_id != row[0]
                        or decision.session_id != row[1]
                        or decision.revision != row[2]
                    ):
                        raise ValueError("decision catalog columns do not match its record")
                    if not _SESSION_ID.fullmatch(decision.session_id):
                        raise ValueError("session ID is not a safe workspace directory name")
                    if not _PLAN_FILE.fullmatch(decision.plan_file):
                        raise ValueError("plan filename is not a portable frozen-plan name")
                    identity = (decision.session_id, decision.plan_file)
                    existing_hash = seen.get(identity)
                    if existing_hash is not None:
                        if existing_hash != decision.plan_hash:
                            raise ValueError("one frozen-plan filename has conflicting identities")
                        continue
                    seen[identity] = decision.plan_hash
                    if decision.session_id not in session_directories:
                        session_directory = workspace.root / "sessions" / decision.session_id
                        metadata = _lstat_optional(session_directory)
                        session_directories[decision.session_id] = bool(
                            sessions_root_valid
                            and metadata is not None
                            and not _is_link(metadata)
                            and stat.S_ISDIR(metadata.st_mode)
                        )
                    if not session_directories[decision.session_id]:
                        raise FileNotFoundError("session plan directory is missing or unsafe")
                    path = workspace.root / "sessions" / decision.session_id / decision.plan_file
                    _verify_parent_directories(path, workspace.root)
                    raw = _read_regular_file(path, _MAX_PLAN_BYTES)
                    plan = ExecutablePlan.model_validate_json(raw)
                    _, plan_hash = freeze_plan(plan)
                    if plan.plan_id != decision.session_id or plan_hash != decision.plan_hash:
                        raise ValueError("frozen plan identity or hash does not match its decision")
                    check["verified"] += 1
                except (
                    PydanticValidationError,
                    TypeError,
                    ValueError,
                    OSError,
                    ValidationError,
                    RecursionError,
                ) as exc:
                    _add_issue(
                        result,
                        "session_plans",
                        "plan_invalid",
                        str(row[0])[:512],
                        str(exc),
                    )
    except sqlite3.Error as exc:
        _add_issue(result, "session_plans", "catalog_unreadable", "sessions", str(exc))


def _check_dataset_suites(
    connection: sqlite3.Connection, workspace: Workspace, result: dict[str, Any]
) -> None:
    check = result["checks"]["dataset_suites"]
    check["status"] = "checked"
    try:
        count, total_bytes = connection.execute(
            "SELECT count(*), COALESCE(sum("
            "COALESCE(length(CAST(suite_name AS BLOB)), 0) + "
            "COALESCE(length(CAST(suite_version AS BLOB)), 0) + "
            "COALESCE(length(CAST(dataset_content_hash AS BLOB)), 0) + "
            "COALESCE(length(CAST(dataset_path AS BLOB)), 0) + "
            "COALESCE(length(CAST(case_count AS BLOB)), 0) + "
            "COALESCE(length(CAST(description AS BLOB)), 0) + "
            "COALESCE(length(CAST(record_hash AS BLOB)), 0)), 0) FROM dataset_suites"
        ).fetchone()
        check["records"] = int(count)
        if count > _MAX_SUITE_ROWS or total_bytes > _MAX_SUITE_BYTES:
            _add_issue(
                result,
                "dataset_suites",
                "catalog_limit",
                "dataset-suites",
                "dataset-suite catalog exceeds the integrity-check size limit",
            )
            return
        rows = connection.execute(
            "SELECT suite_name, suite_version, dataset_content_hash, dataset_path, case_count, "
            "description, record_hash FROM dataset_suites ORDER BY suite_name, suite_version"
        )
        while batch := rows.fetchmany(128):
            for row in batch:
                name, version, digest, relative, case_count, description, stored_hash = row
                label = f"{str(name)[:64]}@{str(version)[:64]}"
                try:
                    identity = {
                        "suite_name": name,
                        "suite_version": version,
                        "dataset_content_hash": digest,
                        "dataset_path": relative,
                        "case_count": case_count,
                        "description": description,
                    }
                    serialized = json.dumps(identity, sort_keys=True, separators=(",", ":"))
                    if stored_hash != content_hash(serialized):
                        raise ValueError("dataset-suite catalog hash does not match its record")
                    if not isinstance(relative, str) or "\\" in relative:
                        raise ValueError("dataset-suite snapshot path is not portable")
                    posix = PurePosixPath(relative)
                    windows = PureWindowsPath(relative)
                    if (
                        posix.is_absolute()
                        or windows.is_absolute()
                        or windows.drive
                        or not posix.parts
                        or any(part in {"", ".", ".."} for part in posix.parts)
                    ):
                        raise ValueError("dataset-suite snapshot path escapes the workspace")
                    path = workspace.root.joinpath(*posix.parts)
                    _verify_parent_directories(path, workspace.root)
                    _verify_suite_snapshot(path, digest, case_count)
                    check["verified"] += 1
                except (TypeError, ValueError, OSError, RecursionError, ValidationError) as exc:
                    _add_issue(result, "dataset_suites", "snapshot_invalid", label, str(exc))
    except sqlite3.Error as exc:
        _add_issue(result, "dataset_suites", "catalog_unreadable", "dataset-suites", str(exc))


def _verify_suite_snapshot(path: Path, expected_hash: str, expected_count: int) -> None:
    metadata = _lstat(path, "dataset-suite snapshot")
    if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("dataset-suite snapshot must be a regular file")
    if metadata.st_size > _MAX_SUITE_FILE_BYTES:
        raise ValidationError("dataset-suite snapshot exceeds the supported size")
    descriptor = _open_regular_file(path, metadata)
    try:
        opened = os.fstat(descriptor)
        with (
            _lock_regular_file(descriptor, metadata.st_size, maximum_bytes=_MAX_SUITE_FILE_BYTES),
            tempfile.TemporaryDirectory(prefix="aibench-suite-check-") as temporary,
        ):
            snapshot = Path(temporary) / "snapshot.jsonl"
            total = 0
            with snapshot.open("wb") as output:
                while block := os.read(descriptor, 1024 * 1024):
                    total += len(block)
                    if total > metadata.st_size or total > _MAX_SUITE_FILE_BYTES:
                        raise ValidationError("dataset-suite snapshot changed while being read")
                    output.write(block)
            final = os.fstat(descriptor)
            current = _lstat(path, "dataset-suite snapshot")
            if (
                total != metadata.st_size
                or not _same_file(metadata, opened)
                or not _same_file(opened, final)
                or not _same_file(final, current)
            ):
                raise ValidationError("dataset-suite snapshot changed while being checked")
            report = ingest_dataset(
                snapshot,
                retain_cases=False,
                force_disk_backed_dedup=True,
                max_errors=100,
            )
            final = os.fstat(descriptor)
            current = _lstat(path, "dataset-suite snapshot")
            if not _same_file(opened, final) or not _same_file(final, current):
                raise ValidationError("dataset-suite snapshot changed while being checked")
    finally:
        os.close(descriptor)
    if not report.is_valid or report.manifest is None:
        first_error = str(report.errors[0]) if report.errors else "dataset is invalid"
        raise ValidationError(f"dataset-suite snapshot is invalid: {first_error}")
    if report.manifest.content_hash != expected_hash:
        raise ValidationError("dataset-suite snapshot content hash does not match its catalog")
    if report.manifest.case_count != expected_count:
        raise ValidationError("dataset-suite snapshot case count does not match its catalog")


def _read_regular_file(path: Path, maximum: int) -> bytes:
    metadata = _lstat(path, "workspace file")
    if _is_link(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise ValidationError("workspace file must be a regular file")
    if metadata.st_size > maximum:
        raise ValidationError("workspace file exceeds the supported size")
    descriptor = _open_regular_file(path, metadata)
    try:
        with _lock_regular_file(descriptor, metadata.st_size, maximum_bytes=maximum):
            chunks: list[bytes] = []
            total = 0
            while block := os.read(descriptor, min(1024 * 1024, maximum - total + 1)):
                total += len(block)
                if total > maximum or total > metadata.st_size:
                    raise ValidationError("workspace file changed while being read")
                chunks.append(block)
            final = os.fstat(descriptor)
            current = _lstat(path, "workspace file")
    finally:
        os.close(descriptor)
    if (
        total != metadata.st_size
        or not _same_file(metadata, final)
        or not _same_file(final, current)
    ):
        raise ValidationError("workspace file changed while being read")
    return b"".join(chunks)


def _open_regular_file(path: Path, expected: os.stat_result) -> int:
    try:
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

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
            handle = create_file(
                str(path),
                0x80000000,  # GENERIC_READ
                0x00000001,  # FILE_SHARE_READ: deny new write and delete handles.
                None,
                3,  # OPEN_EXISTING
                0x00200000 | 0x08000000,  # OPEN_REPARSE_POINT | SEQUENTIAL_SCAN
                None,
            )
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            except BaseException:
                kernel32.CloseHandle(handle)
                raise
        else:
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
    except OSError as exc:
        raise ValidationError(f"could not safely open {path.name}: {exc}") from exc
    if not stat.S_ISREG(opened.st_mode) or not _same_file(expected, opened):
        os.close(descriptor)
        raise ValidationError(f"{path.name} changed while being checked")
    return descriptor


@contextmanager
def _lock_regular_file(
    descriptor: int,
    size_bytes: int,
    *,
    maximum_bytes: int | None = None,
    sqlite_database: bool = False,
) -> Iterator[None]:
    """Prevent overlapping writes while a workspace file is read and verified."""
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [
                ("internal", ctypes.c_void_p),
                ("internal_high", ctypes.c_void_p),
                ("offset", wintypes.DWORD),
                ("offset_high", wintypes.DWORD),
                ("event", wintypes.HANDLE),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        lock_file = kernel32.LockFileEx
        lock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        lock_file.restype = wintypes.BOOL
        unlock_file = kernel32.UnlockFileEx
        unlock_file.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(Overlapped),
        ]
        unlock_file.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(descriptor)
        lock_end = max(size_bytes + 1, maximum_bytes or 0, 1)
        if sqlite_database:
            # SQLite's Windows VFS uses this byte range for rollback-mode locks.
            sqlite_lock_start = 0x40000000
            sqlite_lock_end = sqlite_lock_start + 512
            ranges = [
                (0, sqlite_lock_start),
                (sqlite_lock_end, max(lock_end - sqlite_lock_end, 0)),
            ]
        else:
            ranges = [(0, lock_end)]
        locked: list[tuple[Overlapped, int, int]] = []
        try:
            for offset, length in ranges:
                if length <= 0:
                    continue
                overlap = Overlapped()
                overlap.offset = offset & 0xFFFFFFFF
                overlap.offset_high = offset >> 32
                length_low = length & 0xFFFFFFFF
                length_high = length >> 32
                if not lock_file(
                    handle,
                    0x00000003,
                    0,
                    length_low,
                    length_high,
                    ctypes.byref(overlap),
                ):
                    raise ValidationError("workspace file is being modified and cannot be locked")
                locked.append((overlap, length_low, length_high))
            yield
        finally:
            for overlap, length_low, length_high in reversed(locked):
                if not unlock_file(
                    handle,
                    0,
                    length_low,
                    length_high,
                    ctypes.byref(overlap),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
        return

    import fcntl

    fcntl_api: Any = fcntl
    try:
        fcntl_api.flock(descriptor, fcntl_api.LOCK_SH | fcntl_api.LOCK_NB)
    except OSError as exc:
        raise ValidationError("workspace file is being modified and cannot be locked") from exc
    try:
        yield
    finally:
        fcntl_api.flock(descriptor, fcntl_api.LOCK_UN)


def _verify_parent_directories(path: Path, workspace_root: Path) -> None:
    try:
        relative = path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValidationError("workspace file path escapes its root") from exc
    current = workspace_root
    for part in relative.parts[:-1]:
        current /= part
        metadata = _lstat_optional(current)
        if metadata is None or _is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError(
                f"workspace parent directory is missing or unsafe: {current.name}"
            )


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise ValidationError(f"could not inspect {label} {path}: {exc}") from exc


def _lstat_optional(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except FileNotFoundError:
        return None


def _is_link(metadata: os.stat_result) -> bool:
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    same = (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and left.st_size == right.st_size
        and left.st_mtime_ns == right.st_mtime_ns
    )
    return same and (os.name == "nt" or left.st_ctime_ns == right.st_ctime_ns)


def _verify_open_database(database: Database, path: Path, expected: os.stat_result) -> None:
    _verify_database_unchanged(path, expected)
    rows = database.connection.execute("PRAGMA database_list").fetchall()
    opened = next((str(row[2]) for row in rows if row[1] == "main"), "")
    if not opened:
        raise ValidationError("workspace database connection has no main database file")
    try:
        same_path = os.path.normcase(str(Path(opened).resolve())) == os.path.normcase(
            str(path.resolve())
        )
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not verify workspace database path: {exc}") from exc
    if not same_path:
        raise ValidationError("workspace database path changed while it was being opened")


def _verify_database_unchanged(path: Path, expected: os.stat_result) -> None:
    current = _lstat(path, "workspace database")
    if _is_link(current) or not stat.S_ISREG(current.st_mode) or not _same_inode(current, expected):
        raise ValidationError("workspace database changed while it was being checked")


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _add_issue(
    result: dict[str, Any],
    category: str,
    code: str,
    path: str,
    message: str,
    *,
    details: Any = None,
) -> None:
    result["issue_count"] += 1
    result["checks"][category]["issue_count"] = result["checks"][category].get("issue_count", 0) + 1
    if len(result["issues"]) >= _MAX_ISSUES:
        result["issues_truncated"] = True
        return
    issue = {"category": category, "code": code, "path": path[:512], "message": message[:1024]}
    if details is not None:
        issue["details"] = details
    result["issues"].append(issue)
