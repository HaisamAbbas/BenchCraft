"""Release hardening (13-T2): what an installed release does with a workspace from a
different version."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from aibench.core.errors import WorkspaceTooNew
from aibench.storage.db import Database, Workspace
from aibench.storage.migrations import MIGRATIONS

SRC = Path(__file__).resolve().parents[1] / "src"


def _newer_workspace(root: Path) -> Path:
    """A workspace whose database a newer aibench has migrated one step further."""
    Database.open_workspace(Workspace.at(root)).close()
    db = Workspace.at(root).db_path
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (MIGRATIONS[-1].version + 1, "from_the_future", "2099-01-01T00:00:00+00:00"),
        )
    return db


def test_a_workspace_from_a_newer_aibench_is_refused_not_written(tmp_path: Path) -> None:
    db = _newer_workspace(tmp_path)
    before = db.read_bytes()
    with pytest.raises(WorkspaceTooNew, match="upgraded by a newer aibench"):
        Database.open_workspace(Workspace.at(tmp_path))
    assert db.read_bytes() == before


def test_the_cli_reports_a_newer_workspace_without_a_traceback(tmp_path: Path) -> None:
    _newer_workspace(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(SRC) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    result = subprocess.run(
        [sys.executable, "-m", "aibench", "runs", "list", "--workspace", str(tmp_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 2, result.stderr
    assert "upgraded by a newer aibench" in result.stderr
    assert "Traceback" not in result.stderr


def test_resume_after_an_upgrade_refuses_only_a_changed_metric(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run's metric identities (evaluator ID, semantic version, parameters) are frozen.
    If an upgrade changed a metric's semantic version, resume refuses and calls nothing:
    one run never mixes two meanings of a metric. A packaging-only upgrade (same metric
    versions) resumes; each result records the implementation that produced it."""
    from aibench.evaluators.native import ExactMatch
    from aibench.services.runs import RunError
    from tests.engine_support import Harness

    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    changed = ExactMatch.manifest.model_copy(update={"version": "1.1.0"})
    monkeypatch.setattr(ExactMatch, "manifest", changed)
    with pytest.raises(RunError, match="version drift"):
        h.execute(run_id)
    assert h.count() == 0

    repackaged = ExactMatch.manifest.model_copy(
        update={"version": "1.0.0", "plugin_version": "0.1.0rc2"}
    )
    monkeypatch.setattr(ExactMatch, "manifest", repackaged)
    assert h.execute(run_id).counts["execution"] == {"succeeded": 1}
    storage, _ = h.storage()
    try:
        [attempt] = storage.list_evaluation_attempts(run_id)
    finally:
        storage.db.close()
    assert attempt.provenance["plugin_version"] == "0.1.0rc2"
