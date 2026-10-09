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


def test_runs_list_empty_filter_does_not_claim_workspace_is_empty(tmp_path) -> None:
    _seed_run(tmp_path)
    result = runner.invoke(
        app,
        ["runs", "list", "--workspace", str(tmp_path), "--query", "no-match"],
    )
    assert result.exit_code == 0
    assert "No runs matched" in result.stdout
    assert "No runs committed" not in result.stdout


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


def test_run_annotations_filters_pagination_and_baseline_management(tmp_path) -> None:
    for run_id in ("r1", "r2", "r3"):
        _seed_run(tmp_path, run_id)

    tagged = runner.invoke(
        app, ["runs", "tag", "r3", "Release", "--workspace", str(tmp_path), "--json"]
    )
    assert tagged.exit_code == 0, tagged.output
    assert json.loads(tagged.stdout)["tag"] == "release"
    note = runner.invoke(
        app,
        ["runs", "note", "r3", "candidate for launch", "--workspace", str(tmp_path), "--json"],
    )
    assert note.exit_code == 0, note.output
    assert json.loads(note.stdout)["note"] == "candidate for launch"

    matching = runner.invoke(
        app, ["runs", "list", "--query", "launch", "--workspace", str(tmp_path), "--json"]
    )
    assert matching.exit_code == 0, matching.output
    assert [item["run_id"] for item in json.loads(matching.stdout)["data"]] == ["r3"]
    assert json.loads(matching.stdout)["data"][0]["tags"] == ["release"]

    page = runner.invoke(
        app,
        ["runs", "list", "--limit", "1", "--offset", "1", "--workspace", str(tmp_path), "--json"],
    )
    assert page.exit_code == 0, page.output
    assert len(json.loads(page.stdout)["data"]) == 1
    exact_tag = runner.invoke(
        app, ["runs", "list", "--tag", "release", "--workspace", str(tmp_path), "--json"]
    )
    assert [item["run_id"] for item in json.loads(exact_tag.stdout)["data"]] == ["r3"]

    storage = Storage(Database.open_workspace(Workspace.at(tmp_path)))
    storage.promote_baseline("production", "r3", "release-manager")
    storage.promote_baseline("production", "r2", "release-manager-2")
    storage.db.close()
    baseline_list = runner.invoke(
        app, ["runs", "baseline", "list", "--workspace", str(tmp_path), "--json"]
    )
    assert baseline_list.exit_code == 0, baseline_list.output
    assert json.loads(baseline_list.stdout)["data"][0]["run_id"] == "r2"
    history = runner.invoke(
        app,
        ["runs", "baseline", "history", "production", "--workspace", str(tmp_path), "--json"],
    )
    assert history.exit_code == 0, history.output
    history_rows = json.loads(history.stdout)["data"]
    assert len(history_rows) == 2 and history_rows[0]["previous_run_id"] == "r3"

    show = runner.invoke(app, ["runs", "show", "r3", "--workspace", str(tmp_path), "--json"])
    assert show.exit_code == 0, show.output
    assert json.loads(show.stdout)["tags"] == ["release"]
    assert json.loads(show.stdout)["baselines"] == []


def test_baseline_promotion_requires_a_completed_run_and_approver(tmp_path) -> None:
    _seed_run(tmp_path, "r1", status="running")
    result = runner.invoke(
        app,
        [
            "runs",
            "baseline",
            "promote",
            "production",
            "r1",
            "--approved-by",
            "qa",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "only completed runs" in result.output


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
