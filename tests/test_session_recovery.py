"""10-T1/10-T4 recovery: a session process is really killed mid-run (no mocks of the
engine, storage or lease), then the session is reopened.

10-G1: reopening reconstructs current state and never restarts execution without a new
       action.
10-G2: crash/retry cannot duplicate an already accepted start_run action.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import textwrap
import time
from pathlib import Path

from aibench.core.sessions import ActionKind, ActionRequest, ActionState
from aibench.engine.compile import compile_plan, load_policy
from aibench.services.runs import create_run
from aibench.sessions.controller import SessionController
from tests.session_support import SessionHarness

REPO = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent(
    """
    import asyncio, sys
    sys.path.insert(0, {src!r})
    from pathlib import Path
    from aibench.sessions.controller import SessionController
    from aibench.storage.artifacts import ArtifactStore
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    workspace = Workspace(Path({workspace!r}))
    storage = Storage(Database.open_workspace(workspace))
    ctl = SessionController({session!r}, storage=storage,
                            artifacts=ArtifactStore(workspace.artifacts_dir),
                            workspace_root=workspace.root)

    async def main():
        action = await ctl.start_run(action_id={action!r}, expected_revision=1)
        print("started", action.run_id, action.state.value, flush=True)
        await asyncio.sleep(3600)  # the parent kills this process mid-run

    asyncio.run(main())
    """
)


def _start_then_kill(h: SessionHarness, ctl: SessionController, action_id: str) -> str:
    """Start a run from a separate process, wait until the app is invoked, then kill that
    process abruptly (TerminateProcess on Windows, SIGKILL elsewhere)."""
    session_id = ctl.session_id
    ctl.storage.db.close()
    script = h.root / "child.py"
    script.write_text(
        CHILD.format(
            src=str(REPO / "src"),
            workspace=str(h.workspace.root),
            session=session_id,
            action=action_id,
        ),
        encoding="utf-8",
    )
    child = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        line = child.stdout.readline() if child.stdout else ""
        assert line.startswith("started"), (line, child.stderr.read() if child.stderr else "")
        run_id, state = line.split()[1:3]
        assert state == "done"
        deadline = time.monotonic() + 30
        while h.count() < 1 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert h.count() >= 1, "the application was never invoked"
    finally:
        child.kill()
        child.wait(timeout=30)
    return run_id


def _reopen(h: SessionHarness, session_id: str) -> SessionController:
    storage, artifacts = h.storage()
    return SessionController(
        session_id, storage=storage, artifacts=artifacts, workspace_root=h.workspace.root
    )


def test_reopening_after_a_kill_shows_the_real_state_and_restarts_nothing(tmp_path: Path) -> None:
    """10-G1."""
    h = SessionHarness(tmp_path)
    slow = {c: "slow 0.7" for c in "abcd"}
    ctl = h.open_session(slow, objectives=("catch wrong answers",))
    session_id = ctl.session_id
    run_id = _start_then_kill(h, ctl, "act-start")
    invoked = h.count()

    reopened = _reopen(h, session_id)
    report = reopened.reconcile()
    (run,) = report["runs"]
    assert run["run_id"] == run_id
    assert run["stored_status"] == "running"  # what the dead process last wrote
    assert run["condition"] == "interrupted" and run["resumable"]
    assert report["active_run"] is None  # a dead session's run does not hold the slot
    assert any("nothing restarted it" in note for note in report["notes"])
    assert run["missed_events"] > 0  # events it committed before dying, not yet seen
    status = reopened.run_status(run_id)
    assert status["condition"] == "interrupted" and status["provisional"] is False
    time.sleep(1.5)
    assert h.count() == invoked  # reopening and reading state dispatched nothing

    async def resume() -> None:
        action = await reopened.control_run(ActionKind.RESUME_RUN, action_id="act-resume")
        assert action.state is ActionState.DONE
        outcome = await reopened.wait_for_run(run_id)
        assert outcome is not None and outcome.state.value == "completed"
        assert outcome.counts["execution"] == {"succeeded": 4}

    asyncio.run(resume())
    # Effect-free work in flight at the kill may be dispatched once more (at-least-once,
    # ADR 0005); every case is committed exactly once.
    assert 4 <= h.count() <= 5
    reopened.storage.db.close()


def test_a_killed_effectful_run_leaves_unknown_effect_work_for_the_user(tmp_path: Path) -> None:
    """10-T1: in-flight effectful work is reported as unknown effect, never repeated."""
    h = SessionHarness(tmp_path)
    (h.root / "effects.json").write_text(
        json.dumps({"allow_trusted_local": True, "max_effects": "reversible"}), encoding="utf-8"
    )
    storage, artifacts = h.storage()
    data = h.dataset({c: "slow 0.8" for c in "abc"})
    config = json.loads((h.root / h.cli_app(effects="reversible")).read_text())
    assert config["effects"] == "reversible"
    ctl = SessionController.create(
        storage=storage,
        artifacts=artifacts,
        workspace_root=h.workspace.root,
        project_root=h.root,
        application=h.root / "app.json",
        dataset=h.root / data,
        objectives=("catch wrong answers",),
        policy_path=h.root / "effects.json",
        trusted_local=True,
    )
    session_id = ctl.session_id
    run_id = _start_then_kill(h, ctl, "act-start")
    reopened = _reopen(h, session_id)

    async def resume() -> None:
        await reopened.control_run(ActionKind.RESUME_RUN, action_id="act-resume")
        await reopened.wait_for_run(run_id)

    asyncio.run(resume())
    report = reopened.reconcile()
    assert report["unknown_effect"], report
    assert any("unknown effect" in note for note in report["notes"])
    unknown = {item["task_key"] for item in report["unknown_effect"]}
    # The app records each start in its .attempts file (completion is logged only at the
    # end, which a killed invocation never reaches). Unknown-effect work was started at
    # most once — before the kill — and never again.
    attempts = h.log.with_suffix(".attempts")
    started = attempts.read_text().split() if attempts.exists() else []
    for task_key in unknown:
        case = task_key.split(":")[1]
        assert started.count(case) <= 1
    reopened.storage.db.close()


def test_retrying_a_start_after_a_kill_never_starts_a_second_run(tmp_path: Path) -> None:
    """10-G2: the accepted action, redelivered after the crash, returns its run."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({c: "slow 0.7" for c in "ab"}, objectives=("catch wrong answers",))
    session_id = ctl.session_id
    run_id = _start_then_kill(h, ctl, "act-start")
    reopened = _reopen(h, session_id)

    async def retry() -> ActionRequest:
        return await reopened.start_run(action_id="act-start", expected_revision=1)

    again = asyncio.run(retry())
    assert again.state is ActionState.DONE and again.run_id == run_id
    assert h.runs() == [run_id]
    reopened.storage.db.close()


def test_a_crash_after_the_run_was_created_but_before_it_was_recorded_is_adopted(
    tmp_path: Path,
) -> None:
    """10-G2: the process died between creating the run and settling the action; the
    retry finds the run through its approval and adopts it instead of creating another."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, objectives=("catch wrong answers",))
    request = ActionRequest(
        action_id="act-1",
        session_id=ctl.session_id,
        source="user",
        kind=ActionKind.START_RUN,
        expected_revision=1,
    )
    ctl.store.record_action(request)
    assert ctl.store.claim_active_run(ctl.session_id, expected=None, value="starting:act-1")
    decision = ctl.current_decision()
    compiled = compile_plan(
        ctl.directory / decision.plan_file, policy=load_policy(None), trusted_local=True
    )
    created = create_run(
        compiled,
        storage=ctl.storage,
        artifacts=ctl.artifacts,
        granted_by=f"session {ctl.session_id}, action act-1 (user)",
    )
    # ...the process dies here. A new process retries the same delivery:
    reopened = _reopen(h, ctl.session_id)
    ctl.storage.db.close()

    async def retry() -> ActionRequest:
        return await reopened.start_run(action_id="act-1", expected_revision=1)

    recovered = asyncio.run(retry())
    assert recovered.state is ActionState.DONE and recovered.run_id == created
    assert "recovered" in (recovered.reason or "")
    assert reopened.session.active_run_id == created
    assert h.runs() == [created] and h.count() == 0  # adopted, not started again
    reopened.storage.db.close()


def test_a_start_that_died_before_creating_a_run_is_closed_after_the_window(
    tmp_path: Path,
) -> None:
    import aibench.sessions.controller as controller_module

    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, objectives=("catch wrong answers",))
    request = ActionRequest(
        action_id="act-1",
        session_id=ctl.session_id,
        source="user",
        kind=ActionKind.START_RUN,
        expected_revision=1,
    )
    ctl.store.record_action(request)
    ctl.store.claim_active_run(ctl.session_id, expected=None, value="starting:act-1")

    async def attempts() -> None:
        pending = await ctl.start_run(action_id="act-1", expected_revision=1)
        assert pending.state is ActionState.REQUESTED  # may still be starting elsewhere
        other = await ctl.start_run(action_id="act-2", expected_revision=1)
        assert other.state is ActionState.REJECTED  # the slot is held while it may start
        original = controller_module.STARTING_TTL_SECONDS
        controller_module.STARTING_TTL_SECONDS = 0.0
        try:
            closed = await ctl.start_run(action_id="act-1", expected_revision=1)
        finally:
            controller_module.STARTING_TTL_SECONDS = original
        assert closed.state is ActionState.REJECTED and "interrupted" in (closed.reason or "")
        assert ctl.session.active_run_id is None
        fresh = await ctl.start_run(action_id="act-3", expected_revision=1)
        assert fresh.state is ActionState.DONE
        await ctl.wait_for_run(fresh.run_id)

    asyncio.run(attempts())
    assert len(h.runs()) == 1
    ctl.storage.db.close()


def test_missed_events_replay_by_sequence_without_repeating_actions(tmp_path: Path) -> None:
    """10-T1: the session cursor makes a reconnecting client replay only what it missed."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi", "b": "hi"}, objectives=("catch wrong answers",))

    async def run() -> str:
        action = await ctl.start_run(action_id="act-1", expected_revision=1)
        await ctl.wait_for_run(action.run_id)
        return str(action.run_id)

    run_id = asyncio.run(run())
    events = ctl.missed_events(run_id)
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    ctl.acknowledge_events(run_id, events[3]["sequence"])
    invoked, actions = h.count(), len(ctl.store.list_actions(ctl.session_id))
    reopened = h.reopen(ctl)
    replay = reopened.missed_events(run_id)
    assert replay == events[4:]  # only what was not yet seen, in order
    reopened.acknowledge_events(run_id, events[1]["sequence"])  # never moves backwards
    assert reopened.missed_events(run_id) == events[4:]
    assert h.count() == invoked and len(reopened.store.list_actions(reopened.session_id)) == actions
    reopened.storage.db.close()
