"""A full disk or failing workspace mid-run (13-T1, §23 "exercise disk-full").

The disk fills while applications are being called: artifact writes start raising
`ENOSPC`, or the run database reports that it is full. Such a failure belongs to the
workspace, not to a case. The run must stop dispatching and stay resumable: no case is
marked failed for it, no call is lost from the accounting, and an effectful call is never
repeated. After space is freed, `resume` finishes the run.

The disk is filled by making the real write path raise the error the OS raises; a real
full volume is not created (see docs/engineering/reports/13.md)."""

from __future__ import annotations

import errno
import json
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import ExecutionStatus, WorkItemState
from aibench.engine.engine import RunState, is_storage_failure
from aibench.security.policy import ExecutionPolicy
from aibench.services.reports import build_report
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import Storage
from tests.engine_support import Harness

cli = CliRunner()


def _sqlite_full_error() -> sqlite3.OperationalError:
    """Model the extended result code SQLite sets when its database is full."""
    error = sqlite3.OperationalError("database or disk is full")
    error.sqlite_errorcode = sqlite3.SQLITE_FULL
    return error


def _fill_disk_after(monkeypatch: pytest.MonkeyPatch, writes: int) -> None:
    """Artifact writes succeed `writes` times, then fail like a full volume, until undone."""
    original = ArtifactStore.write_bytes
    seen = {"n": 0}

    def write_bytes(self: ArtifactStore, data: bytes, **kwargs: Any) -> Any:
        seen["n"] += 1
        if seen["n"] > writes:
            raise OSError(errno.ENOSPC, "No space left on device")
        return original(self, data, **kwargs)

    monkeypatch.setattr(ArtifactStore, "write_bytes", write_bytes)


def _state(h: Harness, run_id: str) -> tuple[Counter[str], Counter[str], dict[str, Any]]:
    storage, artifacts = h.storage()
    try:
        items = Counter(w.state.value for w in storage.list_work_items(run_id))
        ok = Counter(
            a.case_id
            for a in storage.list_execution_attempts(run_id)
            if a.status is ExecutionStatus.OK
        )
        report = build_report(storage, artifacts, run_id)
    finally:
        storage.db.close()
    return items, ok, report


def test_a_full_disk_stops_the_run_resumably_and_resume_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    cases = {f"c{i}": "hi" for i in range(6)}
    run_id = h.create(
        h.plan(
            dataset=h.dataset(cases),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    _fill_disk_after(monkeypatch, writes=3)
    outcome = h.execute(run_id)
    monkeypatch.undo()  # space freed

    assert outcome.state is RunState.INTERRUPTED
    assert any("workspace storage failed" in w for w in outcome.warnings)
    assert any("No space left on device" in w for w in outcome.warnings)
    items, _, _ = _state(h, run_id)
    assert items["failed"] == 0  # a full disk is not a case failure
    assert items["running"] >= 1  # the call in flight is left for recovery to settle

    assert h.execute(run_id).state is RunState.COMPLETED
    items, ok, report = _state(h, run_id)
    assert set(items) == {"succeeded"}
    assert ok == Counter(dict.fromkeys(cases, 1))  # every case recorded exactly once
    calls = h.count()
    app = report["application"]
    # Calls lost with the full disk are counted, as an upper bound: never fewer than the
    # application received.
    assert app["uncommitted_dispatches"] >= calls - len(cases) >= 1
    assert app["attempts"] + app["uncommitted_dispatches"] >= calls


def test_a_full_disk_never_repeats_an_effectful_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.runner_support import load_example, serving

    server = load_example("effect_counter_app").make_server(port=0)
    h = Harness(tmp_path)
    with serving(server) as base:
        (tmp_path / "effect.json").write_text(
            json.dumps(
                {
                    "application_id": "booking",
                    "runner": "http",
                    "target": "b",
                    "revision": "fixture-v1",
                    "effects": "irreversible",
                    "transport": {"kind": "http", "url": f"{base}/book"},
                    "input_binding": {"fields": {"/destination": "/input"}},
                }
            ),
            encoding="utf-8",
        )
        run_id = h.create(
            h.plan(dataset=h.dataset({"trip": "Dubai"}), application="effect.json"),
            policy=ExecutionPolicy(max_effects="irreversible"),
        )
        _fill_disk_after(monkeypatch, writes=0)
        assert h.execute(run_id).state is RunState.INTERRUPTED
        monkeypatch.undo()
        assert server.count == 1  # booked, but nothing could be recorded

        outcome = h.execute(run_id)
        time.sleep(0.2)
    assert server.count == 1  # never booked twice
    assert outcome.counts["execution"] == {"unknown_effect": 1}


def test_a_full_run_database_is_a_storage_failure_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi", "b": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    original = Storage.commit_execution_attempt
    fired = {"n": 0}

    def full(self: Storage, result: Any) -> Any:
        if not fired["n"]:
            fired["n"] += 1
            raise _sqlite_full_error()
        return original(self, result)

    monkeypatch.setattr(Storage, "commit_execution_attempt", full)
    outcome = h.execute(run_id)
    monkeypatch.undo()
    assert outcome.state is RunState.INTERRUPTED
    assert any("database or disk is full" in w for w in outcome.warnings)
    assert h.execute(run_id).state is RunState.COMPLETED
    items, ok, _ = _state(h, run_id)
    assert set(items) == {"succeeded"} and ok == Counter({"a": 1, "b": 1})


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (OSError(errno.ENOSPC, "No space left on device"), True),
        (OSError(errno.EIO, "I/O error"), True),
        (OSError(errno.EROFS, "Read-only file system"), True),
        (_sqlite_full_error(), True),
        (OSError(errno.ENOENT, "No such file"), False),  # one item's problem, not the disk
        (ValueError("bad value"), False),
    ],
)
def test_only_workspace_failures_stop_a_run(exc: BaseException, expected: bool) -> None:
    assert is_storage_failure(exc) is expected


def test_the_cli_explains_a_full_disk_and_resume_finishes_the_quickstart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through `aibench`: the packaged quickstart, its real subprocess app."""
    project = tmp_path / "quickstart"
    assert cli.invoke(app, ["init", str(project)]).exit_code == 0
    app_config_path = project / "support.app.json"
    app_config = json.loads(app_config_path.read_text(encoding="utf-8"))
    app_config["environment_digest"] = "test-runtime-pin"
    app_config_path.write_text(json.dumps(app_config), encoding="utf-8")
    _fill_disk_after(monkeypatch, writes=4)
    stopped = cli.invoke(app, ["run", str(project), "--workspace", str(project)])
    monkeypatch.undo()
    assert stopped.exit_code == 130, stopped.output  # interrupted, resumable
    assert "workspace storage failed" in stopped.output
    [line] = [ln for ln in stopped.output.splitlines() if "resume with:" in ln]
    run_id = line.split()[-1]

    status = cli.invoke(app, ["runs", "status", run_id, "--workspace", str(project), "--json"])
    assert status.exit_code == 0, status.output
    data = json.loads(status.stdout)
    assert data["status"] == "interrupted"
    assert any("No space left on device" in w for w in data["warnings"])
    assert data["needs_attention"] == []  # nothing was blamed on a case

    resumed = cli.invoke(app, ["resume", run_id, "--workspace", str(project), "--json"])
    # The quickstart's own application failure (support-010) makes it incomplete (3).
    assert resumed.exit_code == 3, resumed.output
    finished = json.loads(resumed.stdout)
    assert finished["state"] == "completed"
    assert finished["counts"]["execution"] == {"failed": 1, "succeeded": 9}
    assert finished["warnings"] == []
    assert WorkItemState.RUNNING.value not in finished["counts"]["evaluation"]
