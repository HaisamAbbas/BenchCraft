"""08-T3/T4: the session controller's typed commands call the same services as the
headless commands, against a real instrumented application and the real engine."""

from __future__ import annotations

import asyncio
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.sessions import ActionKind, ActionState, PlanPatch
from aibench.engine.compile import compile_plan, load_policy
from aibench.services.runs import run_status
from aibench.sessions.controller import SessionController
from tests.session_support import SessionHarness

FOUR = {"a": "hi", "b": "hi", "c": "hi", "d": "hi"}


def test_a_session_run_is_the_headless_run_of_the_same_reviewed_plan(tmp_path: Path) -> None:
    """Shared services: the reviewed plan file compiles to the same identity headlessly,
    and status comes from `services.runs.run_status`."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    decision = ctl.current_decision()
    headless = compile_plan(
        ctl.directory / decision.plan_file, policy=load_policy(None), trusted_local=True
    )
    assert headless.plan_hash == decision.plan_hash

    async def scenario() -> str:
        action = await ctl.start_run(action_id="act-1", expected_revision=1)
        await ctl.wait_for_run(action.run_id)
        return str(action.run_id)

    run_id = asyncio.run(scenario())
    record = ctl.storage.get_run(run_id)
    assert record.manifest.plan_hash == decision.plan_hash
    assert record.status == "completed"
    session_view = ctl.run_status(run_id)
    headless_view = run_status(ctl.storage, run_id)
    assert {k: session_view[k] for k in headless_view} == headless_view
    assert session_view["provisional"] is False
    events = ctl.run_events(run_id)
    assert events[0]["event_type"] == "run_created"
    assert ctl.run_events(run_id, after=events[-2]["sequence"]) == events[-1:]  # replay
    approval = ctl.storage.get_approval(f"{run_id}:approval")
    assert approval.granted_by == f"session {ctl.session_id}, action act-1 (user)"
    ctl.storage.db.close()


def test_missing_information_blocks_and_missing_permission_denies(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR)  # no objective: the draft needs information

    async def scenario() -> None:
        blocked = await ctl.start_run(action_id="act-1", expected_revision=1)
        assert blocked.state is ActionState.BLOCKED
        assert {f["kind"] for f in blocked.findings} == {"missing_information"}

    asyncio.run(scenario())
    ctl.storage.db.close()

    h2 = SessionHarness(tmp_path / "denied")
    denied_ctl = h2.open_session(FOUR, objectives=("catch wrong answers",), trusted=False)

    async def denied() -> None:
        denied_action = await denied_ctl.start_run(action_id="act-1", expected_revision=1)
        assert denied_action.state is ActionState.DENIED
        assert all(f["kind"] == "missing_permission" for f in denied_action.findings)

    asyncio.run(denied())
    assert h.runs() == [] and h2.runs() == [] and h.count() == 0 and h2.count() == 0
    assert denied_ctl.state()["draft"]["missing_permission"]  # visible before any action
    denied_ctl.storage.db.close()


def test_a_run_targets_the_reviewed_revision_and_one_runs_at_a_time(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "slow 0.5", "b": "slow 0.5"}, objectives=("catch wrong answers",))
    ctl.apply_patch(PlanPatch(limit=1), expected_revision=1)

    async def scenario() -> None:
        stale = await ctl.start_run(action_id="act-old", expected_revision=1)
        assert stale.state is ActionState.REJECTED and "current revision is 2" in (
            stale.reason or ""
        )
        first = await ctl.start_run(action_id="act-1", expected_revision=2)
        assert first.state is ActionState.DONE
        second = await ctl.start_run(action_id="act-2", expected_revision=2)
        assert second.state is ActionState.REJECTED and "still active" in (second.reason or "")
        # A scope change during the run makes a new draft; the run keeps its frozen plan.
        change = ctl.apply_patch(PlanPatch(limit=2), expected_revision=2)
        assert change.status == "applied" and change.active_run == first.run_id
        outcome = await ctl.wait_for_run(first.run_id)
        assert outcome is not None and outcome.counts["execution"] == {"succeeded": 1}

    asyncio.run(scenario())
    assert h.count() == 1
    ctl.storage.db.close()


def test_pause_resume_and_cancel_go_through_run_control(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    slow = {c: "slow 0.4" for c in ("a", "b", "c", "d", "e", "f")}
    ctl = h.open_session(slow, objectives=("catch wrong answers",))

    async def scenario() -> None:
        start = await ctl.start_run(action_id="act-start", expected_revision=1)
        run_id = start.run_id
        await h.wait_for_invocations(1)
        pause = await ctl.control_run(ActionKind.PAUSE_RUN, action_id="act-pause")
        assert pause.state is ActionState.DONE
        for _ in range(100):
            if ctl.run_status(run_id)["status"] == "paused":
                break
            await asyncio.sleep(0.05)
        assert ctl.run_status(run_id)["status"] == "paused"
        paused_at = h.count()
        await asyncio.sleep(0.6)
        assert h.count() == paused_at  # nothing new dispatched while paused
        resume = await ctl.control_run(ActionKind.RESUME_RUN, action_id="act-resume")
        assert resume.state is ActionState.DONE
        await h.wait_for_invocations(paused_at + 1)
        cancel = await ctl.control_run(ActionKind.CANCEL_RUN, action_id="act-cancel")
        assert cancel.state is ActionState.DONE
        outcome = await ctl.wait_for_run(run_id)
        assert outcome is not None and outcome.state.value == "cancelled"
        again = await ctl.control_run(ActionKind.CANCEL_RUN, action_id="act-cancel")
        assert again == cancel  # redelivery: the stored record, nothing re-applied
        late = await ctl.control_run(ActionKind.PAUSE_RUN, action_id="act-late")
        assert late.state is ActionState.REJECTED
        controls = [
            e["payload"]["action"]
            for e in ctl.run_events(run_id)
            if e["event_type"] == "control_requested"
        ]
        assert controls == ["pause_run", "resume_run", "cancel_run"]

    asyncio.run(scenario())
    assert h.count() < 6
    ctl.storage.db.close()


def test_reopening_never_restarts_a_run_and_resume_continues_it(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    slow = {c: "slow 0.4" for c in ("a", "b", "c", "d")}
    ctl = h.open_session(slow, objectives=("catch wrong answers",))

    async def leave() -> str:
        start = await ctl.start_run(action_id="act-start", expected_revision=1)
        await h.wait_for_invocations(1)
        await ctl.close()  # exiting: stop new dispatch, keep the run resumable
        return str(start.run_id)

    run_id = asyncio.run(leave())
    reopened = h.reopen(ctl)
    assert reopened.run_status(run_id)["status"] == "interrupted"
    done_before = h.count()
    assert done_before < 4

    async def come_back() -> None:
        await asyncio.sleep(0.3)
        assert h.count() == done_before  # opening the session started nothing
        resume = await reopened.control_run(ActionKind.RESUME_RUN, action_id="act-resume")
        assert resume.state is ActionState.DONE
        outcome = await reopened.wait_for_run(run_id)
        assert outcome is not None and outcome.state.value == "completed"

    asyncio.run(come_back())
    assert h.count() == 4  # each case once: resumed, not repeated
    reopened.storage.db.close()


def test_runs_outside_the_session_cannot_be_controlled_or_queried(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    other = h.create(h.plan(dataset="data.jsonl", application="app.json"))

    async def scenario() -> None:
        action = await ctl.control_run(ActionKind.CANCEL_RUN, action_id="act-x", run_id=other)
        assert action.state is ActionState.REJECTED

    asyncio.run(scenario())
    try:
        ctl.run_status(other)
    except Exception as exc:  # noqa: BLE001
        assert "not started in this session" in str(exc)
    else:
        raise AssertionError("a foreign run was readable through the session")
    try:
        ctl.compare_runs(other, other)
    except Exception as exc:  # noqa: BLE001
        assert "not started in this session" in str(exc)
    else:
        raise AssertionError("a foreign run was comparable through the session")
    ctl.storage.db.close()


def test_failures_and_case_evidence_come_from_committed_results(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    rows = [
        {"case_id": "ok", "input": "hi", "expected_output": "yes"},
        {"case_id": "wrong", "input": "hi", "expected_output": "no"},
        {"case_id": "crash", "input": "crash", "expected_output": "yes"},
    ]
    ctl = h.open_session({}, rows=rows, objectives=("catch wrong answers",))

    async def scenario() -> None:
        start = await ctl.start_run(action_id="act-1", expected_revision=1)
        await ctl.wait_for_run(start.run_id)

    asyncio.run(scenario())
    failures = ctl.failures()
    assert [f["case_id"] for f in failures["metric_failures"]] == ["wrong"]
    assert failures["metric_failures"][0]["decision"] == "fail"
    assert [f["case_id"] for f in failures["application_failures"]] == ["crash"]
    assert failures["provisional"] is False
    evidence = ctl.case_evidence("wrong")
    assert evidence["results"][0]["decision"] == "fail"
    assert evidence["golden"]["reference"]["answer"] == "no"
    assert ctl.explain_metric("native.contains")["selected"] is False
    ctl.storage.db.close()


def test_sessions_cli_shows_the_conversation_decisions_and_actions(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR)
    turn = ctl.record_command("/objective catch wrong answers", message_id="c-1")
    ctl.apply_patch(
        PlanPatch(add_objectives=("catch wrong answers",)),
        expected_revision=1,
        source_turn_id=turn.turn_id,
    )
    session_id = ctl.session_id
    ctl.storage.db.close()
    runner = CliRunner()
    listed = runner.invoke(app, ["sessions", "list", "--workspace", str(h.root / "project")])
    assert listed.exit_code == 0 and session_id in listed.output
    shown = runner.invoke(
        app, ["sessions", "show", session_id, "--workspace", str(h.root / "project"), "--json"]
    )
    assert shown.exit_code == 0, shown.output
    assert '"revision": 2' in shown.output and "/objective catch wrong answers" in shown.output
    text = runner.invoke(
        app, ["sessions", "show", session_id, "--workspace", str(h.root / "project")]
    )
    assert "[1] user: /objective catch wrong answers" in text.output
    missing = runner.invoke(
        app, ["sessions", "show", "nope", "--workspace", str(h.root / "project")]
    )
    assert missing.exit_code == 2


def test_answers_are_reused_and_a_dataset_change_invalidates_stale_questions(
    tmp_path: Path,
) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("make sure answers match what support says",))
    (question,) = ctl.store.questions(ctl.session_id, "open")
    assert question.prompt.startswith("Which concept does")
    answered = ctl.apply_patch(
        PlanPatch(
            objective_concepts={"make sure answers match what support says": ("correctness",)},
            answers=(question.question_id,),
        ),
        expected_revision=1,
    )
    assert answered.status == "applied"
    draft = ctl.state()["draft"]
    assert draft["executable"] and draft["metrics"][0]["metric"] == "native.exact_match@1.0.0"
    assert ctl.store.get_question(ctl.session_id, question.question_id).status == "answered"
    # The answer is part of the choices, so later drafts reuse it instead of asking again.
    later = ctl.apply_patch(PlanPatch(limit=2), expected_revision=2)
    assert later.status == "applied" and ctl.store.questions(ctl.session_id, "open") == []

    # A question open when the dataset changes is stale; the new draft asks its own.
    ctl.apply_patch(PlanPatch(add_objectives=("respond in valid JSON",)), expected_revision=3)
    open_before = {q.question_id for q in ctl.store.questions(ctl.session_id, "open")}
    assert open_before
    other = tmp_path / "other.jsonl"
    other.write_text('{"case_id": "z", "input": "hi"}\n', encoding="utf-8")
    moved = ctl.apply_patch(PlanPatch(dataset="other.jsonl"), expected_revision=4)
    assert moved.status == "applied"
    statuses = {q.question_id: q.status for q in ctl.store.questions(ctl.session_id)}
    reasked = {q.question_id for q in ctl.store.questions(ctl.session_id, "open")}
    assert all(statuses[q] == "stale" for q in open_before - reasked)
    assert all(q.draft_revision == 5 for q in ctl.store.questions(ctl.session_id, "open"))
    ctl.storage.db.close()


def test_patches_that_do_not_fit_are_rejected_without_a_new_revision(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    cases = [
        (PlanPatch(remove_objectives=("not stated",)), "no stated objective"),
        (PlanPatch(objective_concepts={"catch wrong answers": ("vibes",)}), "unknown concepts"),
        (PlanPatch(dataset="missing.jsonl"), "does not exist"),
        (PlanPatch(budgets={"max_cost_usd": 5}), "estimated_cost_per_application_call_usd"),
        (PlanPatch(add_objectives=("catch wrong answers",)), "changes nothing"),
    ]
    for patch, problem in cases:
        result = ctl.apply_patch(patch, expected_revision=1)
        assert result.status in ("rejected", "unchanged") and result.revision == 1
        assert problem in " ".join(result.problems), result.problems
    assert [d.revision for d in ctl.store.list_decisions(ctl.session_id)] == [1]
    ctl.storage.db.close()


def test_resume_reports_a_run_that_could_not_continue(tmp_path: Path) -> None:
    """A resume refused by the engine (another live session holds the run's lease) is
    reported as not done, instead of claiming the run continued."""
    import os
    import socket
    import time

    h = SessionHarness(tmp_path)
    ctl = h.open_session({c: "slow 0.4" for c in "abc"}, objectives=("catch wrong answers",))

    async def leave() -> str:
        start = await ctl.start_run(action_id="act-start", expected_revision=1)
        await h.wait_for_invocations(1)
        await ctl.close()
        return str(start.run_id)

    run_id = asyncio.run(leave())
    reopened = h.reopen(ctl)
    reopened.storage.acquire_run_lease(
        run_id,
        owner="another-terminal",
        host=socket.gethostname(),
        pid=os.getpid(),  # alive: the lease is not stale
        now=time.time(),
        is_stale=lambda _: True,
    )

    async def resume() -> None:
        action = await reopened.control_run(ActionKind.RESUME_RUN, action_id="act-resume")
        assert action.state is ActionState.REJECTED
        assert "another session" in (action.reason or "")

    asyncio.run(resume())
    reopened.storage.db.close()


def test_the_run_slot_is_taken_by_compare_and_set(tmp_path: Path) -> None:
    """A second process that saw the same empty slot cannot take it once the first has."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session(FOUR, objectives=("catch wrong answers",))
    storage, artifacts = h.storage()  # a second connection, as a second terminal would have
    other = SessionController(
        ctl.session_id, storage=storage, artifacts=artifacts, workspace_root=h.workspace.root
    )
    assert other.store.claim_active_run(other.session_id, expected=None, value="starting:x")
    assert not ctl.store.claim_active_run(ctl.session_id, expected=None, value="starting:y")
    assert ctl.session.active_run_id == "starting:x"
    other.storage.db.close()
    ctl.storage.db.close()
