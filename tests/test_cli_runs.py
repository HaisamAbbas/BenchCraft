"""02-T3: `aibench runs list` / `aibench runs show RUN_ID` CLI."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import RunManifest
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

runner = CliRunner()


def _seed_run(project_root: Path, run_id: str = "r1", status: str = "created") -> None:
    ws = Workspace.at(project_root)
    db = Database.open_workspace(ws)
    storage = Storage(db)
    storage.commit_run(
        RunManifest(
            run_id=run_id,
            dataset_hash="sha256:d",
            application_hash="sha256:a",
            plan_hash="sha256:p",
        )
    )
    if status != "created":
        storage.update_run_status(run_id, status)
    db.close()


def test_runs_list_empty_workspace(tmp_path) -> None:
    result = runner.invoke(app, ["runs", "list", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "No runs" in result.stdout


def test_runs_list_shows_committed_run(tmp_path) -> None:
    _seed_run(tmp_path)
    result = runner.invoke(app, ["runs", "list", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "r1" in result.stdout
    assert "created" in result.stdout


def test_runs_list_json_output(tmp_path) -> None:
    _seed_run(tmp_path)
    result = runner.invoke(app, ["runs", "list", "--workspace", str(tmp_path), "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert len(payload["data"]) == 1
    assert payload["data"][0]["run_id"] == "r1"
    assert payload["data"][0]["status"] == "created"


def test_runs_list_filters_by_status(tmp_path) -> None:
    _seed_run(tmp_path, run_id="r1", status="created")
    _seed_run(tmp_path, run_id="r2", status="succeeded")
    result = runner.invoke(
        app, ["runs", "list", "--workspace", str(tmp_path), "--status", "succeeded", "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert [r["run_id"] for r in payload["data"]] == ["r2"]


def test_runs_show_existing_run(tmp_path) -> None:
    _seed_run(tmp_path)
    result = runner.invoke(app, ["runs", "show", "r1", "--workspace", str(tmp_path)])
    assert result.exit_code == 0
    assert "r1" in result.stdout
    assert "created" in result.stdout


def test_runs_show_json_output(tmp_path) -> None:
    _seed_run(tmp_path)
    result = runner.invoke(
        app, ["runs", "show", "r1", "--workspace", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["run_id"] == "r1"
    assert payload["dataset_hash"] == "sha256:d"


def test_runs_show_missing_run_exits_nonzero(tmp_path) -> None:
    result = runner.invoke(app, ["runs", "show", "does-not-exist", "--workspace", str(tmp_path)])
    assert result.exit_code == 2


def test_runs_survive_across_separate_cli_invocations(tmp_path) -> None:
    """Each CLI invocation opens and closes its own Database — this is the restart-safety
    contract exercised through the actual user-facing surface, not just the Storage API."""
    _seed_run(tmp_path)
    result1 = runner.invoke(app, ["runs", "list", "--workspace", str(tmp_path), "--json"])
    result2 = runner.invoke(app, ["runs", "show", "r1", "--workspace", str(tmp_path), "--json"])
    assert result1.exit_code == 0
    assert result2.exit_code == 0
    payload = json.loads(result1.stdout)
    assert payload["data"][0]["run_id"] == "r1"
    assert payload["_cli"]["schema"] == "aibench.cli-output/1"
    assert payload["_cli"]["exit_code"] == result1.exit_code
    assert json.loads(result2.stdout)["run_id"] == "r1"
