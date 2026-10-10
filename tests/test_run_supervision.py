"""Detached worker lifecycle and durable cross-process run controls."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.engine.engine import RunController
from aibench.services import runs as runs_service
from tests.engine_support import Harness

runner = CliRunner()


def _invoke_control(project: Path, run_id: str, action: str) -> dict[str, object]:
    result = runner.invoke(
        app,
        ["runs", "control", run_id, action, "--workspace", str(project), "--json"],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _status(project: Path, run_id: str) -> dict[str, object]:
    result = runner.invoke(app, ["runs", "status", run_id, "--workspace", str(project), "--json"])
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def _wait_for(project: Path, run_id: str, predicate, timeout: float = 45.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        latest = _status(project, run_id)
        if predicate(latest):
            return latest
        time.sleep(0.05)
    raise AssertionError(f"run did not reach expected supervision state: {latest}")


def test_detached_run_can_be_paused_resumed_and_cancelled(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plan = harness.plan(
        dataset=harness.dataset({f"case-{i}": "slow 0.3" for i in range(8)}),
        application=harness.cli_app(),
    )
    project = harness.workspace.root.parent
    event_log = tmp_path / "detached-run-events.jsonl"
    launched = runner.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--workspace",
            str(project),
            "--trust-local-app",
            "--application-concurrency",
            "1",
            "--evaluation-concurrency",
            "1",
            "--detach",
            "--log-file",
            str(event_log),
            "--json",
        ],
    )
    assert launched.exit_code == 0, launched.output
    launch_data = json.loads(launched.stdout)
    run_id = launch_data["run_id"]
    worker = launch_data["worker"]
    assert worker["state"] == "started"
    assert (project / worker["log_path"]).is_file()

    try:
        _invoke_control(project, run_id, "pause")
        paused = _wait_for(project, run_id, lambda data: data["status"] == "paused")
        completed_at_pause = harness.count()
        time.sleep(0.4)
        assert harness.count() == completed_at_pause
        assert paused["supervision"]["control"]["desired_state"] == "paused"
        assert paused["supervision"]["detached_worker"]["state"] == "running"

        resumed = runner.invoke(app, ["resume", run_id, "--workspace", str(project), "--json"])
        assert resumed.exit_code == 0, resumed.output
        assert json.loads(resumed.stdout)["accepted_by"] == "live worker"
        _wait_for(project, run_id, lambda _data: harness.count() > completed_at_pause)
        _invoke_control(project, run_id, "cancel")
        cancelled = _wait_for(project, run_id, lambda data: data["status"] == "cancelled")
        assert cancelled["supervision"]["control"]["desired_state"] == "cancelled"
        assert cancelled["supervision"]["control"]["sequence"] == 3
        assert cancelled["supervision"]["worker_lease"] is None
        assert harness.count() < 8
        deadline = time.monotonic() + 10
        logged_events: list[dict[str, object]] = []
        while time.monotonic() < deadline:
            if event_log.exists():
                logged_events = [
                    json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()
                ]
                if any(event["event_type"] == "run_session_ended" for event in logged_events):
                    break
            time.sleep(0.05)
        assert logged_events
        assert [event["sequence"] for event in logged_events] == sorted(
            event["sequence"] for event in logged_events
        )
        assert all(event["run_id"] == run_id for event in logged_events)
        assert any(event["event_type"] == "run_session_ended" for event in logged_events)
        rejected_resume = runner.invoke(
            app, ["runs", "control", run_id, "resume", "--workspace", str(project), "--json"]
        )
        assert rejected_resume.exit_code == 2
        storage, _ = harness.storage()
        try:
            events = storage.list_run_events(run_id)
            requested = [
                event["payload"]["action"]
                for event in events
                if event["event_type"] == "run_control_requested"
            ]
            applied = [
                event["payload"]["control_sequence"]
                for event in events
                if event["event_type"] == "run_control_applied"
            ]
            assert requested == ["pause", "resume", "cancel"]
            assert applied == [1, 2, 3]
        finally:
            storage.db.close()
    finally:
        state = _status(project, run_id)
        if state["status"] not in {"completed", "cancelled", "failed", "interrupted"}:
            _invoke_control(project, run_id, "cancel")
            _wait_for(
                project,
                run_id,
                lambda data: data["status"] in {"cancelled", "completed", "failed"},
            )


def test_offline_cancel_is_preserved_without_application_or_evaluator_startup(
    tmp_path: Path, monkeypatch
) -> None:
    harness = Harness(tmp_path)
    plan = harness.plan(
        dataset=harness.dataset({"case-a": "should never run"}),
        application=harness.cli_app(),
    )
    project = harness.workspace.root.parent
    run_id = harness.create(plan)
    (tmp_path / "app.py").write_text("print('source changed after cancellation')\n")

    def fail_evaluator_startup(*_args, **_kwargs):
        raise AssertionError("pending cancellation must not load evaluators")

    monkeypatch.setattr(
        runs_service,
        "_frozen_registry",
        fail_evaluator_startup,
    )

    request = _invoke_control(project, run_id, "cancel")
    assert request["worker_lease"] is None
    assert request["desired_state"] == "cancelled"

    resumed = runner.invoke(app, ["resume", run_id, "--workspace", str(project), "--json"])
    assert resumed.exit_code == 3, resumed.output
    assert json.loads(resumed.stdout)["state"] == "cancelled"
    assert harness.count() == 0
    status = _status(project, run_id)
    assert status["status"] == "cancelled"
    assert status["supervision"]["control"]["desired_state"] == "cancelled"


def test_resume_finalizes_legacy_cancelling_run_as_durable_cancel(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plan = harness.plan(
        dataset=harness.dataset({"case-a": "should never run"}),
        application=harness.cli_app(),
    )
    project = harness.workspace.root.parent
    run_id = harness.create(plan)
    storage, _ = harness.storage()
    try:
        storage.update_run_status(run_id, "cancelling")
        assert storage.get_run_control_state(run_id) is None
    finally:
        storage.db.close()

    resumed = runner.invoke(app, ["resume", run_id, "--workspace", str(project), "--json"])
    assert resumed.exit_code == 3, resumed.output
    assert json.loads(resumed.stdout)["state"] == "cancelled"
    assert harness.count() == 0
    storage, _ = harness.storage()
    try:
        control = storage.get_run_control_state(run_id)
        assert control is not None
        assert (control.desired_state, control.sequence) == ("cancelled", 1)
        requested = [
            event["payload"]["action"]
            for event in storage.list_run_events(run_id)
            if event["event_type"] == "run_control_requested"
        ]
        assert requested == ["cancel"]
    finally:
        storage.db.close()


def test_durable_resume_releases_a_live_worker_from_first_interrupt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plan = harness.plan(
        dataset=harness.dataset({"case-a": "should run after resume"}),
        application=harness.cli_app(),
    )
    run_id = harness.create(plan)
    project = harness.workspace.root.parent
    controller = RunController()

    async def request_resume_during_interrupt(current: RunController, _harness: Harness) -> None:
        current.interrupt()
        result = _invoke_control(project, run_id, "resume")
        assert result["desired_state"] == "running"

    outcome = harness.execute(run_id, controller=controller, during=request_resume_during_interrupt)
    assert outcome.state.value == "completed"
    assert harness.count() == 1
    storage, _ = harness.storage()
    try:
        applied = [
            event["payload"]["control_sequence"]
            for event in storage.list_run_events(run_id)
            if event["event_type"] == "run_control_applied"
        ]
        assert applied == [1]
    finally:
        storage.db.close()


def test_durable_pause_replaces_a_live_workers_first_interrupt(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plan = harness.plan(
        dataset=harness.dataset({"case-a": "should run after resume"}),
        application=harness.cli_app(),
    )
    run_id = harness.create(plan)
    project = harness.workspace.root.parent
    controller = RunController()

    async def pause_then_resume(current: RunController, _harness: Harness) -> None:
        current.interrupt()
        paused = _invoke_control(project, run_id, "pause")
        assert paused["desired_state"] == "paused"
        while current.state.value != "paused":
            await asyncio.sleep(0.01)
        assert harness.count() == 0
        resumed = _invoke_control(project, run_id, "resume")
        assert resumed["desired_state"] == "running"

    outcome = harness.execute(run_id, controller=controller, during=pause_then_resume)
    assert outcome.state.value == "completed"
    assert harness.count() == 1
    storage, _ = harness.storage()
    try:
        applied = [
            event["payload"]["control_sequence"]
            for event in storage.list_run_events(run_id)
            if event["event_type"] == "run_control_applied"
        ]
        assert applied == [1, 2]
    finally:
        storage.db.close()


def test_detached_process_is_not_mistaken_for_a_later_lease_owner(monkeypatch) -> None:
    monkeypatch.setattr(runs_service, "_pid_alive", lambda _pid: False)
    status = runs_service._detached_worker_view(
        {
            "event_type": "detached_worker_started",
            "payload": {"pid": os.getpid() + 1, "host": socket.gethostname()},
        },
        worker_lease="live",
        lease_owner=(socket.gethostname(), os.getpid()),
    )
    assert status == {
        "state": "exited",
        "owns_lease": False,
        "pid": os.getpid() + 1,
    }
