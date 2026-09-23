"""08-T1: sessions, turns, questions, decisions and action requests persist, and the storage
invariants hold on their own — compare-and-set revisions, one decision per revision, one
stored turn per delivery ID, one record per action ID."""

from __future__ import annotations

from pathlib import Path

import pytest

from aibench.core.errors import ConflictError
from aibench.core.sessions import (
    ActionKind,
    ActionRequest,
    ActionState,
    ConversationTurn,
    PlanPatch,
)
from aibench.core.sessions import (
    PendingQuestion as SessionPendingQuestion,
)
from aibench.planning.drafts import PendingQuestion as DraftPendingQuestion
from aibench.sessions.store import StaleRevision
from tests.session_support import SessionHarness


def test_plan_and_session_use_the_same_pending_question_model() -> None:
    assert DraftPendingQuestion is SessionPendingQuestion


def test_a_session_and_its_first_draft_survive_a_reopen(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi", "b": "hi"})
    first = ctl.state()
    assert first["revision"] == 1
    assert not first["draft"]["executable"]  # no objective yet: missing information
    assert first["draft"]["missing_information"]
    assert [q["prompt"] for q in first["open_questions"]] == ["What should this benchmark check?"]
    result = ctl.apply_patch(
        PlanPatch(add_objectives=("catch wrong answers",)), expected_revision=1
    )
    assert result.status == "applied"

    reopened = h.reopen(ctl)
    state = reopened.state()
    assert state["revision"] == 2
    assert state["choices"]["objectives"] == ["catch wrong answers"]
    assert state["draft"]["executable"]
    decisions = reopened.store.list_decisions(reopened.session_id)
    assert [d.revision for d in decisions] == [1, 2]
    assert decisions[1].supersedes == decisions[0].decision_id
    assert decisions[1].structured_change == {"add_objectives": ("catch wrong answers",)}
    # The answered-by-drafting question is no longer open.
    assert reopened.store.questions(reopened.session_id, "open") == []
    reopened.storage.db.close()


def test_revisions_advance_only_by_compare_and_set(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"})
    first = ctl.current_decision()
    second = first.model_copy(update={"decision_id": "x:d2", "revision": 2})
    ctl.store.commit_decision(second, expected_revision=1, questions=())
    with pytest.raises(StaleRevision) as info:
        ctl.store.commit_decision(
            first.model_copy(update={"decision_id": "x:d2b", "revision": 2}),
            expected_revision=1,
            questions=(),
        )
    assert (info.value.expected, info.value.current) == (1, 2)
    assert ctl.session.revision == 2 and ctl.session.decision_id == "x:d2"
    ctl.storage.db.close()


def test_a_delivery_id_stores_one_turn_and_a_turn_gets_one_reply(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"})

    def user(turn_id: str) -> ConversationTurn:
        return ConversationTurn(
            turn_id=turn_id,
            session_id=ctl.session_id,
            sequence=1,
            role="user",
            kind="message",
            content="hello",
            message_id="m-1",
        )

    first, new = ctl.store.append_turn(user("t-1"))
    again, new_again = ctl.store.append_turn(user("t-2"))
    assert new and not new_again and again.turn_id == first.turn_id

    def reply(turn_id: str) -> ConversationTurn:
        return ConversationTurn(
            turn_id=turn_id,
            session_id=ctl.session_id,
            sequence=1,
            role="assistant",
            kind="reply",
            content="hi",
            replies_to=first.turn_id,
        )

    r1, _ = ctl.store.append_turn(reply("r-1"))
    r2, new_reply = ctl.store.append_turn(reply("r-2"))
    assert not new_reply and r2.turn_id == r1.turn_id
    assert [t.sequence for t in ctl.store.turns(ctl.session_id)] == [1, 2]
    ctl.storage.db.close()


def test_an_action_id_is_recorded_once_and_settles_once(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"})
    request = ActionRequest(
        action_id="act-1", session_id=ctl.session_id, source="user", kind=ActionKind.START_RUN
    )
    stored, new = ctl.store.record_action(request)
    replay, new_again = ctl.store.record_action(request.model_copy(update={"expected_revision": 9}))
    assert new and not new_again and replay.expected_revision is None
    settled = ctl.store.settle_action(stored, ActionState.REJECTED, reason="test")
    assert settled.state is ActionState.REJECTED
    with pytest.raises(ConflictError):
        ctl.store.settle_action(stored, ActionState.DONE)
    assert ctl.store.get_action("act-1").state is ActionState.REJECTED
    ctl.storage.db.close()


def test_deleting_nothing_about_runs_sessions_do_not_own_run_records(tmp_path: Path) -> None:
    """§14: run records are not children of sessions; a run row has no session key."""
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "hi"})
    columns = {row[1] for row in ctl.storage.conn.execute("PRAGMA table_info(runs)")}
    assert "session_id" not in columns
    ctl.storage.db.close()
