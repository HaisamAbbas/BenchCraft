"""Read-only dashboard queries backed by the same stored-result services as the CLI.

This module deliberately opens a read-only SQLite connection for each request. A
dashboard is a local viewer, not another workspace writer or benchmark engine.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from aibench.core.errors import AibenchError
from aibench.services import reports
from aibench.services.comparison import compare_runs
from aibench.sessions.store import SessionStore
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

MAX_DASHBOARD_PAGE = 100
MAX_DASHBOARD_SOURCE_ROWS = 5_000
_T = TypeVar("_T")


class DashboardNotFound(AibenchError):
    """A requested stored record does not exist."""


class DashboardWorkLimitExceeded(AibenchError):
    """A report or comparison would exceed the dashboard's stored-data work budget."""


class DashboardEndpointNotFound(AibenchError):
    """An API path is not implemented by the dashboard."""


def _require_bounded_run(storage: Storage, run_id: str, dataset_hash: str) -> None:
    count = storage.count_dashboard_source_rows(
        run_id, dataset_hash, limit=MAX_DASHBOARD_SOURCE_ROWS
    )
    if count > MAX_DASHBOARD_SOURCE_ROWS:
        raise DashboardWorkLimitExceeded(run_id)


def _read_storage(workspace: Workspace, query: Callable[[Storage], _T]) -> _T:
    db = Database.open_readonly(workspace.db_path)
    try:
        # Keep preflight limits and service reads on one SQLite snapshot. Writers may
        # continue in WAL mode, but data committed after the preflight is excluded here.
        db.connection.execute("BEGIN")
        result = query(Storage(db))
        db.connection.execute("COMMIT")
        return result
    finally:
        db.close()


def list_dashboard_runs(
    workspace: Workspace, *, limit: int = 50, offset: int = 0
) -> list[dict[str, Any]]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_DASHBOARD_PAGE
    ):
        raise ValueError(f"limit must be between 1 and {MAX_DASHBOARD_PAGE}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a non-negative integer")

    def query(storage: Storage) -> list[dict[str, Any]]:
        records = storage.search_runs(limit=limit, offset=offset)
        metadata = storage.list_run_metadata(record.manifest.run_id for record in records)
        result = []
        for record in records:
            run_id = record.manifest.run_id
            result.append(
                {
                    "run_id": run_id,
                    "status": record.status,
                    "created_at": record.created_at,
                    "committed_at": record.committed_at,
                    "dataset_hash": record.manifest.dataset_hash,
                    "application_hash": record.manifest.application_hash,
                    "plan_hash": record.manifest.plan_hash,
                    "tags": metadata[run_id]["tags"],
                    "baselines": metadata[run_id]["baselines"],
                }
            )
        return result

    return _read_storage(workspace, query)


def list_dashboard_sessions(workspace: Workspace, *, limit: int = 50) -> list[dict[str, Any]]:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_DASHBOARD_PAGE
    ):
        raise ValueError(f"limit must be between 1 and {MAX_DASHBOARD_PAGE}")

    def query(storage: Storage) -> list[dict[str, Any]]:
        sessions = SessionStore(storage).list_sessions(limit=limit)
        return [
            {
                "session_id": session.session_id,
                "revision": session.revision,
                "active_run_id": session.active_run_id,
                "created_at": session.created_at.isoformat(),
                "updated_at": session.updated_at.isoformat(),
            }
            for session in sessions
        ]

    return _read_storage(workspace, query)


def dashboard_run_report(workspace: Workspace, run_id: str) -> dict[str, Any]:
    def query(storage: Storage) -> dict[str, Any]:
        record = storage.get_run(run_id)
        if record is None:
            raise DashboardNotFound(run_id)
        _require_bounded_run(storage, run_id, record.manifest.dataset_hash)
        report = reports.build_report(
            storage,
            ArtifactStore(workspace.artifacts_dir, create=False),
            run_id,
            include_content=False,
        )
        return reports.report_facts(report, evidence_limit=10)

    return _read_storage(workspace, query)


def dashboard_compare(
    workspace: Workspace, baseline_run_id: str, current_run_id: str
) -> dict[str, Any]:
    def query(storage: Storage) -> dict[str, Any]:
        for run_id in (baseline_run_id, current_run_id):
            record = storage.get_run(run_id)
            if record is None:
                raise DashboardNotFound(run_id)
            _require_bounded_run(storage, run_id, record.manifest.dataset_hash)
        return compare_runs(
            storage,
            ArtifactStore(workspace.artifacts_dir, create=False),
            baseline_run_id,
            current_run_id,
            bootstrap_replicates=2_000,
        )

    return _read_storage(workspace, query)
