"""02-T3: `aibench runs list` / `aibench runs show RUN_ID` CLI."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli import runs as runs_cli
from aibench.cli.event_stream import EventLogLock, RunEventStream
from aibench.cli.main import app
from aibench.cli.run import _preflight_event_log
from aibench.core.models import RunManifest
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

runner = CliRunner()


def _seed_run(
    project_root: Path,
    run_id: str = "r1",
    status: str = "created",
    *,
    seed: int | None = None,
    parameters: dict[str, object] | None = None,
    dependency_lock_hash: str | None = None,
    plugin_hashes: dict[str, object] | None = None,
    environment: dict[str, object] | None = None,
) -> None:
    ws = Workspace.at(project_root)
    db = Database.open_workspace(ws)
    storage = Storage(db)
    storage.commit_run(
        RunManifest(
            run_id=run_id,
            dataset_hash="sha256:d",
            application_hash="sha256:a",
            plan_hash="sha256:p",
            dependency_lock_hash=dependency_lock_hash,
            plugin_hashes=plugin_hashes or {},
            environment=environment or {},
            seed=seed,
            parameters=parameters or {},
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


def test_runs_events_jsonl_schema_and_after_cursor(tmp_path) -> None:
    _seed_run(tmp_path)
    db = Database.open_workspace(Workspace.at(tmp_path))
    storage = Storage(db)
    storage.append_run_event("r1", "run_started", {"attempt": 1})
    storage.append_run_event("r1", "item_state", {"state": "running"})
    db.close()

    result = runner.invoke(
        app, ["runs", "events", "r1", "--workspace", str(tmp_path), "--jsonl"]
    )
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [item["sequence"] for item in records] == [1, 2]
    assert records[0]["schema"] == "aibench.run-event/1"
    assert records[0]["run_id"] == "r1"
    assert records[0]["event_type"] == "run_started"
    assert records[0]["payload"] == {"attempt": 1}
    assert isinstance(records[0]["created_at"], str)

    resumed = runner.invoke(
        app,
        ["runs", "events", "r1", "--workspace", str(tmp_path), "--after", "1", "--jsonl"],
    )
    assert resumed.exit_code == 0, resumed.output
    assert [json.loads(line)["sequence"] for line in resumed.stdout.splitlines()] == [2]


def test_runs_events_rejects_cursor_above_sqlite_integer_range(tmp_path) -> None:
    _seed_run(tmp_path)
    result = runner.invoke(
        app,
        [
            "runs",
            "events",
            "r1",
            "--workspace",
            str(tmp_path),
            "--after",
            "9223372036854775808",
        ],
    )
    assert result.exit_code == 2
    assert "Traceback" not in result.output


def test_runs_events_rejects_a_log_file_with_an_active_writer(tmp_path) -> None:
    _seed_run(tmp_path)
    db = Database.open_workspace(Workspace.at(tmp_path))
    Storage(db).append_run_event("r1", "run_started", {})
    db.close()
    log_file = tmp_path / "events.jsonl"
    writer = RunEventStream(tmp_path, "r1", log_file=log_file)
    writer.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not log_file.exists():
            time.sleep(0.01)
        result = runner.invoke(
            app,
            [
                "runs",
                "events",
                "r1",
                "--workspace",
                str(tmp_path),
                "--follow",
                "--log-file",
                str(log_file),
            ],
        )
        assert result.exit_code == 2
        assert "already in use by another writer" in result.output
    finally:
        writer.close()

    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert [record["sequence"] for record in records] == [1]


def test_run_log_preflight_holds_lock_until_stream_owns_it(tmp_path) -> None:
    _seed_run(tmp_path)
    log_file = tmp_path / "events.jsonl"
    log_lock = _preflight_event_log(tmp_path, log_file)
    assert log_lock is not None and log_lock.is_held

    contender = EventLogLock(log_file.resolve())
    with pytest.raises(ValueError, match="already in use"):
        contender.acquire()

    stream = RunEventStream(tmp_path, "r1", log_file=log_file, log_lock=log_lock)
    assert log_lock.is_held
    stream.close()
    assert not log_lock.is_held


def test_runs_events_log_file_contains_jsonl_even_in_quiet_mode(tmp_path) -> None:
    _seed_run(tmp_path)
    db = Database.open_workspace(Workspace.at(tmp_path))
    storage = Storage(db)
    storage.append_run_event("r1", "run_started", {"attempt": 1})
    db.close()
    log_file = tmp_path / "events.jsonl"

    result = runner.invoke(
        app,
        [
            "runs",
            "events",
            "r1",
            "--workspace",
            str(tmp_path),
            "--log-file",
            str(log_file),
            "--quiet",
        ],
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert records[0]["event_type"] == "run_started"
    assert records[0]["payload"] == {"attempt": 1}

    db = Database.open_workspace(Workspace.at(tmp_path))
    storage = Storage(db)
    storage.append_run_event("r1", "item_state", {"state": "complete"})
    db.close()
    again = runner.invoke(
        app,
        [
            "runs",
            "events",
            "r1",
            "--workspace",
            str(tmp_path),
            "--log-file",
            str(log_file),
            "--quiet",
        ],
    )
    assert again.exit_code == 0, again.output
    records = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines()]
    assert [record["sequence"] for record in records] == [1, 2]


def test_runs_events_follow_emits_events_appended_during_follow(tmp_path, monkeypatch) -> None:
    _seed_run(tmp_path, status="running")
    db = Database.open_workspace(Workspace.at(tmp_path))
    storage = Storage(db)
    storage.append_run_event("r1", "run_started", {})
    db.close()
    live = [True]
    monkeypatch.setattr(runs_cli, "lease_state", lambda *_args: "live" if live[0] else None)

    def append_completion() -> None:
        time.sleep(0.2)
        follow_db = Database.open_workspace(Workspace.at(tmp_path))
        follow_storage = Storage(follow_db)
        follow_storage.append_run_event("r1", "run_session_ended", {"state": "completed"})
        follow_storage.update_run_status("r1", "completed")
        follow_db.close()
        live[0] = False

    writer = threading.Thread(target=append_completion)
    writer.start()
    result = runner.invoke(
        app, ["runs", "events", "r1", "--workspace", str(tmp_path), "--follow", "--jsonl"]
    )
    writer.join(timeout=5)
    assert not writer.is_alive()
    assert result.exit_code == 0, result.output
    records = [json.loads(line) for line in result.stdout.splitlines()]
    assert [record["event_type"] for record in records] == [
        "run_started",
        "run_session_ended",
    ]


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


def test_runs_show_exposes_seed_and_local_source_provenance(tmp_path) -> None:
    source = {"code": "sha256:source"}
    environment = {"kind": "python", "dependencies": "sha256:dependencies"}
    vcs = {
        "kind": "git",
        "commit": "a" * 40,
        "tracked_worktree": "dirty",
        "tracked_diff_hash": "sha256:diff",
        "untracked_file_count": 2,
        "untracked_files_hash": "sha256:untracked",
    }
    benchmark_environment = {
        "python": "3.12.1",
        "python_implementation": "CPython",
        "platform": "win32",
        "platform_abi": "win-amd64",
    }
    plugins = {"native.exact_match": "aibench.native==1"}
    _seed_run(
        tmp_path,
        seed=1729,
        dependency_lock_hash="sha256:evaluator-dependencies",
        plugin_hashes=plugins,
        environment=benchmark_environment,
        parameters={
            "application_identity_basis": {"kind": "local_source_content_hash"},
            "application_code_identity": source,
            "application_environment_identity": environment,
            "application_vcs_identity": vcs,
        },
    )

    result = runner.invoke(
        app, ["runs", "show", "r1", "--workspace", str(tmp_path), "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["seed"] == 1729
    assert payload["dependency_lock_hash"] == "sha256:evaluator-dependencies"
    assert payload["plugin_hashes"] == plugins
    assert payload["benchmark_environment"] == benchmark_environment
    assert payload["application_identity"]["source"] == source
    assert payload["application_identity"]["environment"] == environment
    assert payload["application_identity"]["version_control"] == vcs

    human = runner.invoke(app, ["runs", "show", "r1", "--workspace", str(tmp_path)])
    assert human.exit_code == 0, human.output
    assert "run_seed: 1729" in human.stdout
    assert "application_git_commit" in human.stdout
    assert "tracked_worktree: dirty" in human.stdout
    assert "application_git_untracked_file_count: 2" in human.stdout
    assert "application_git_untracked_files_hash: sha256:untracked" in human.stdout
    assert "evaluator_dependency_lock_hash: sha256:evaluator-dependencies" in human.stdout
    assert "platform=win32" in human.stdout


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
