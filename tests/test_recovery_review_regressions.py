"""Regressions for the independent review of Prompt 10 (and the in-process redelivery
race it exposed in an 08 test). Each asserts the corrected behaviour."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest
from rich.console import Console

from aibench.conversation.agent import ConversationAgent
from aibench.core.sessions import ActionKind, ActionState, PendingQuestion, PlanPatch
from aibench.security.redaction import sanitize, strip_terminal_controls
from aibench.sessions.summary import MAX_SUMMARY_CHARS, session_summary
from aibench.tui.app import ChatApp
from tests.session_support import ScriptedProvider, SessionHarness, patch_step, say

KEY = "ABCDEFGHIJKLMNOPQRSTUV"


@pytest.mark.parametrize(
    "hidden",
    [
        f"key sk-\x1b[0m{KEY}",  # an escape splits the key
        f"sk-ABCDEFGH\x00{KEY}",  # a NUL splits it
        f"sk-\u200b{KEY}",  # a zero-width space splits it
        '{"api_key": "hunter2hunter2"}',  # JSON member
        "OPENAI_API_KEY=abcdef123456",  # environment variable
    ],
)
def test_split_or_quoted_credentials_are_redacted(hidden: str) -> None:
    """Review #1 and #6: controls are removed before redaction; more key shapes."""
    cleaned = sanitize(hidden)
    assert "[redacted]" in cleaned
    assert KEY not in cleaned and "hunter2" not in cleaned and "abcdef123456" not in cleaned


def test_an_unterminated_string_sequence_cannot_hide_the_lines_after_it() -> None:
    """Review #5."""
    for introducer in ("\x1b]0;title", "\x1bPdata", "\x9dosc"):
        text = f"ok {introducer}\nWARNING: run failed 3/10\nmore"
        assert strip_terminal_controls(text) == "ok \nWARNING: run failed 3/10\nmore"


def test_bidi_overrides_and_invisible_characters_are_removed() -> None:
    """Review #7: displayed text cannot be visually reordered."""
    assert sanitize("value \u202eevil\u202c done\u2066x\u2069\ufeff") == "value evil donex"


def test_a_dead_cancelling_run_finishes_its_cancellation(tmp_path: Path) -> None:
    """Review #2: a run left `cancelling` by a dead session was stuck; continuing it now
    finishes the cancellation, and the note says so (it is never un-cancelled)."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({c: "slow 0.4" for c in "abcd"}, objectives=("catch wrong answers",))

    async def start() -> str:
        action = await ctl.start_run(action_id="act-1", expected_revision=1)
        await h.wait_for_invocations(1)
        await ctl.close()  # the session ends; the run is interrupted and resumable
        return str(action.run_id)

    run_id = asyncio.run(start())
    ctl.storage.update_run_status(run_id, "cancelling")  # as a session that died mid-cancel
    reopened = h.reopen(ctl)
    report = reopened.reconcile()
    assert report["runs"][0]["condition"] == "interrupted" and report["runs"][0]["resumable"]
    assert any("/stop finishes the cancellation" in note for note in report["notes"])
    before = h.count()

    async def finish() -> None:
        action = await reopened.control_run(ActionKind.RESUME_RUN, action_id="act-2")
        assert action.state is ActionState.DONE
        outcome = await reopened.wait_for_run(run_id)
        assert outcome is not None and outcome.state.value == "cancelled"

    asyncio.run(finish())
    assert h.count() == before  # nothing new dispatched: cancelled, not resumed
    reopened.storage.db.close()


def test_the_summary_stays_bounded_whatever_the_session_holds(tmp_path: Path) -> None:
    """Review #3: objectives, open questions and runs are trimmed too."""
    h = SessionHarness(tmp_path)
    objectives = tuple(f"objective number {i} " + "x" * 150 for i in range(30))
    ctl = h.open_session({"a": "hi"}, objectives=objectives)
    ctl.ask(
        [
            PendingQuestion(
                question_id=f"q-{i}",
                prompt=f"question {i} " + "y" * 190,
                required_fields=("selection",),
                choices=(),
                blocking_scope="conversation",
                draft_revision=0,
            )
            for i in range(30)
        ]
    )
    summary = session_summary(ctl.store, ctl.session_id, earlier_turns=40)
    assert len(json.dumps(summary)) <= MAX_SUMMARY_CHARS
    assert summary["omitted"]  # what was dropped is counted, not silently lost
    assert summary["open_questions"]  # the newest questions survive
    ctl.storage.db.close()


def test_corrections_made_in_conversation_are_kept_as_user_corrections(tmp_path: Path) -> None:
    """Review #4: an assistant patch is grounded in the user's own words, so it is the
    user's correction and survives trimming like a typed command."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({c: "hi" for c in "abcd"}, objectives=("catch wrong answers",))
    provider = ScriptedProvider([patch_step("Use 2 cases", sample={"size": 2}), say("Done.")])
    asyncio.run(ConversationAgent(ctl, provider).handle_message("Use 2 cases."))
    summary = session_summary(ctl.store, ctl.session_id, earlier_turns=20)
    (correction,) = summary["user_corrections"]
    assert correction["via"] == "assistant" and correction["revision"] == 2
    ctl.storage.db.close()


def test_a_lost_slot_after_creating_a_run_does_not_start_it(tmp_path: Path) -> None:
    """Review #8: if the starting window expired and another start took the slot, the
    created run is not launched (one active run per session)."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, objectives=("catch wrong answers",))
    store = ctl.store
    original = store.claim_active_run
    calls = {"n": 0}

    def claim(session_id: str, *, expected: str | None, value: str | None) -> bool:
        calls["n"] += 1
        if calls["n"] == 2:  # the claim after create_run: someone else holds the slot
            original(session_id, expected=expected, value="run-someone-else")
            return False
        return original(session_id, expected=expected, value=value)

    store.claim_active_run = claim  # type: ignore[method-assign]
    action = asyncio.run(ctl.start_run(action_id="act-1", expected_revision=1))
    assert action.state is ActionState.REJECTED and "slot was taken" in (action.reason or "")
    assert ctl.live_runs() == [] and h.count() == 0
    ctl.storage.db.close()


def test_a_just_created_run_without_a_lease_is_starting_not_interrupted(tmp_path: Path) -> None:
    """Review #9: the one-step window between creating a run and taking its lease."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, objectives=("catch wrong answers",))
    run_id = h.create(h.plan(dataset="data.jsonl", application="app.json"))
    assert ctl.run_condition(run_id)["condition"] == "starting"
    ctl.storage.db.close()


def test_event_cursors_advance_atomically_across_sessions(tmp_path: Path) -> None:
    """Review #11: two processes acknowledging events never move a cursor backwards or
    drop another run's cursor."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"}, objectives=("catch wrong answers",))
    storage, artifacts = h.storage()
    from aibench.sessions.controller import SessionController

    other = SessionController(
        ctl.session_id, storage=storage, artifacts=artifacts, workspace_root=h.workspace.root
    )
    ctl.store.advance_event_cursor(ctl.session_id, "run-a", 7)
    other.store.advance_event_cursor(ctl.session_id, "run-b", 3)
    ctl.store.advance_event_cursor(ctl.session_id, "run-a", 2)  # stale: ignored
    assert dict(other.session.event_cursors) == {"run-a": 7, "run-b": 3}
    other.storage.db.close()
    ctl.storage.db.close()


def test_reopening_replays_notable_missed_events_in_order(tmp_path: Path) -> None:
    """Review #10: missed events are shown (by sequence), not only counted."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi", "b": "crash"}, objectives=("catch wrong answers",))

    async def run() -> str:
        action = await ctl.start_run(action_id="act-1", expected_revision=1)
        await ctl.wait_for_run(action.run_id)
        return str(action.run_id)

    run_id = asyncio.run(run())
    reopened = h.reopen(ctl)
    buffer = io.StringIO()
    chat = ChatApp(reopened, provider=None, console=Console(file=buffer, width=200))
    chat._banner()
    shown = buffer.getvalue()
    assert "event(s) since you last looked" in shown
    assert "exec:b:r0 failed" in shown  # the crashed case, replayed
    assert "run session ended completed" in shown
    positions = [shown.index("exec:b:r0 failed"), shown.index("run session ended")]
    assert positions == sorted(positions)
    assert reopened.missed_events(run_id) == []  # acknowledged once shown
    reopened.storage.db.close()


def test_an_accepted_patch_does_not_count_against_replay() -> None:
    """Sanity: PlanPatch round-trips (used by the correction test above)."""
    assert PlanPatch(sample={"size": 2}).scope_fields() == {"selection"}
