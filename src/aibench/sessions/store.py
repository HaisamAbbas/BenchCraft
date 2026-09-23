"""Session persistence (§14, 08-T1). Uses the workspace's single writer connection, like
`storage.repositories.Storage`; every multi-row change is one transaction.

Invariants enforced here, not by callers:
- a session's revision advances only by compare-and-set against the revision the change
  was made from, and each revision has one decision (`StaleRevision` otherwise);
- a user turn is stored once per client delivery ID, and a user turn has at most one reply;
- an action ID is recorded once; a redelivery gets the original record back.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from aibench.core.errors import ConflictError
from aibench.core.models import utcnow
from aibench.core.sessions import (
    ActionRequest,
    ActionState,
    BenchmarkSession,
    ConversationTurn,
    DecisionRecord,
    PendingQuestion,
)
from aibench.storage.repositories import Storage


class StaleRevision(ConflictError):
    """A change was made against a session revision that is no longer current."""

    def __init__(self, expected: int, current: int) -> None:
        self.expected = expected
        self.current = current
        super().__init__(
            f"the draft moved on: this change was made against revision {expected}, "
            f"the current revision is {current}"
        )


def _now() -> str:
    return utcnow().isoformat()


class SessionStore:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    @property
    def conn(self) -> sqlite3.Connection:
        return self.storage.conn

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ sessions

    def create_session(
        self,
        session: BenchmarkSession,
        decision: DecisionRecord,
        questions: Iterable[PendingQuestion],
    ) -> None:
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO sessions (session_id, revision, active_run_id, data, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.revision,
                    None,
                    session.model_dump_json(),
                    _now(),
                    _now(),
                ),
            )
            self._insert_decision(conn, decision)
            self._put_questions(conn, session.session_id, decision.revision, questions, ())

    def get_session(self, session_id: str) -> BenchmarkSession | None:
        row = self.conn.execute(
            "SELECT data FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return BenchmarkSession.model_validate_json(row["data"]) if row else None

    def list_sessions(self, limit: int = 100) -> list[BenchmarkSession]:
        rows = self.conn.execute(
            "SELECT data FROM sessions ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [BenchmarkSession.model_validate_json(r["data"]) for r in rows]

    def _update_session(self, conn: sqlite3.Connection, session: BenchmarkSession) -> None:
        conn.execute(
            "UPDATE sessions SET revision = ?, active_run_id = ?, data = ?, updated_at = ? "
            "WHERE session_id = ?",
            (
                session.revision,
                session.active_run_id,
                session.model_dump_json(),
                _now(),
                session.session_id,
            ),
        )

    def update_session(self, session_id: str, **changes: Any) -> BenchmarkSession:
        """Update fields that are not guarded by the revision (active run, presented
        revision). Revision changes go through `commit_decision`."""
        assert "revision" not in changes and "decision_id" not in changes
        with self._transaction() as conn:
            current = self._locked_session(conn, session_id)
            updated = current.model_copy(update={**changes, "updated_at": utcnow()})
            self._update_session(conn, updated)
        return updated

    def claim_active_run(self, session_id: str, *, expected: str | None, value: str | None) -> bool:
        """Compare-and-set the session's active run: applied only if it is still
        `expected`, so two processes cannot both take the session's single run slot."""
        with self._transaction() as conn:
            current = self._locked_session(conn, session_id)
            if current.active_run_id != expected:
                return False
            self._update_session(
                conn, current.model_copy(update={"active_run_id": value, "updated_at": utcnow()})
            )
        return True

    def _locked_session(self, conn: sqlite3.Connection, session_id: str) -> BenchmarkSession:
        row = conn.execute(
            "SELECT data FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return BenchmarkSession.model_validate_json(row["data"])

    # ------------------------------------------------------------------ decisions

    def _insert_decision(self, conn: sqlite3.Connection, decision: DecisionRecord) -> None:
        conn.execute(
            "INSERT INTO decision_records (decision_id, session_id, revision, data, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                decision.decision_id,
                decision.session_id,
                decision.revision,
                decision.model_dump_json(),
                _now(),
            ),
        )

    def commit_decision(
        self,
        decision: DecisionRecord,
        *,
        expected_revision: int,
        questions: Iterable[PendingQuestion],
        answered: Iterable[str] = (),
    ) -> BenchmarkSession:
        """Accept `decision` as the next revision, only if the session is still at
        `expected_revision`. The new draft's questions become the open ones: earlier open
        questions it no longer asks become stale, and `answered` ones are closed."""
        with self._transaction() as conn:
            session = self._locked_session(conn, decision.session_id)
            if session.revision != expected_revision:
                raise StaleRevision(expected_revision, session.revision)
            assert decision.revision == expected_revision + 1
            self._insert_decision(conn, decision)
            updated = session.model_copy(
                update={
                    "revision": decision.revision,
                    "decision_id": decision.decision_id,
                    "updated_at": utcnow(),
                }
            )
            self._update_session(conn, updated)
            self._put_questions(conn, session.session_id, decision.revision, questions, answered)
        return updated

    def get_decision(self, decision_id: str) -> DecisionRecord | None:
        row = self.conn.execute(
            "SELECT data FROM decision_records WHERE decision_id = ?", (decision_id,)
        ).fetchone()
        return DecisionRecord.model_validate_json(row["data"]) if row else None

    def decision_at(self, session_id: str, revision: int) -> DecisionRecord | None:
        row = self.conn.execute(
            "SELECT data FROM decision_records WHERE session_id = ? AND revision = ?",
            (session_id, revision),
        ).fetchone()
        return DecisionRecord.model_validate_json(row["data"]) if row else None

    def list_decisions(self, session_id: str) -> list[DecisionRecord]:
        rows = self.conn.execute(
            "SELECT data FROM decision_records WHERE session_id = ? ORDER BY revision",
            (session_id,),
        ).fetchall()
        return [DecisionRecord.model_validate_json(r["data"]) for r in rows]

    # ------------------------------------------------------------------ questions

    def _put_questions(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        revision: int,
        questions: Iterable[PendingQuestion],
        answered: Iterable[str],
    ) -> None:
        answered = set(answered)
        asked = {q.question_id: q for q in questions}
        for question_id in answered - set(asked):
            self._set_question_status(conn, session_id, question_id, "answered")
        conn.execute(
            "UPDATE pending_questions SET status = 'stale', data = json_set(data, "
            "'$.status', 'stale'), updated_at = ? WHERE session_id = ? AND status = 'open'",
            (_now(), session_id),
        )
        for question in asked.values():
            self._upsert_question(
                conn, session_id, question.model_copy(update={"draft_revision": revision})
            )

    def _set_question_status(
        self, conn: sqlite3.Connection, session_id: str, question_id: str, status: str
    ) -> None:
        conn.execute(
            "UPDATE pending_questions SET status = ?, data = json_set(data, '$.status', ?), "
            "updated_at = ? WHERE session_id = ? AND question_id = ?",
            (status, status, _now(), session_id, question_id),
        )

    def _upsert_question(
        self, conn: sqlite3.Connection, session_id: str, question: PendingQuestion
    ) -> None:
        conn.execute(
            "INSERT INTO pending_questions (session_id, question_id, draft_revision, status, "
            "data, updated_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (session_id, question_id) "
            "DO UPDATE SET draft_revision = excluded.draft_revision, status = excluded.status, "
            "data = excluded.data, updated_at = excluded.updated_at",
            (
                session_id,
                question.question_id,
                question.draft_revision,
                question.status,
                question.model_dump_json(),
                _now(),
            ),
        )

    def ask(self, session_id: str, questions: Iterable[PendingQuestion]) -> None:
        """Record questions asked in conversation (not by a draft) as open."""
        with self._transaction() as conn:
            for question in questions:
                self._upsert_question(conn, session_id, question)

    def questions(self, session_id: str, status: str | None = None) -> list[PendingQuestion]:
        sql = "SELECT data FROM pending_questions WHERE session_id = ?"
        params: tuple[object, ...] = (session_id,)
        if status is not None:
            sql += " AND status = ?"
            params += (status,)
        rows = self.conn.execute(sql + " ORDER BY draft_revision, question_id", params).fetchall()
        return [PendingQuestion.model_validate_json(r["data"]) for r in rows]

    def get_question(self, session_id: str, question_id: str) -> PendingQuestion | None:
        row = self.conn.execute(
            "SELECT data FROM pending_questions WHERE session_id = ? AND question_id = ?",
            (session_id, question_id),
        ).fetchone()
        return PendingQuestion.model_validate_json(row["data"]) if row else None

    # ------------------------------------------------------------------ turns

    def append_turn(self, turn: ConversationTurn) -> tuple[ConversationTurn, bool]:
        """Store a turn with the next sequence number. A user turn whose delivery ID was
        already stored returns the stored turn and False; so does a second reply."""
        with self._transaction() as conn:
            existing = None
            if turn.message_id is not None:
                existing = conn.execute(
                    "SELECT data FROM conversation_turns WHERE session_id = ? AND role = ? "
                    "AND message_id = ?",
                    (turn.session_id, turn.role, turn.message_id),
                ).fetchone()
            elif turn.replies_to is not None:
                existing = conn.execute(
                    "SELECT data FROM conversation_turns WHERE replies_to = ?",
                    (turn.replies_to,),
                ).fetchone()
            if existing is not None:
                return ConversationTurn.model_validate_json(existing["data"]), False
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) FROM conversation_turns WHERE session_id = ?",
                (turn.session_id,),
            ).fetchone()
            stored = turn.model_copy(update={"sequence": int(row[0]) + 1})
            conn.execute(
                "INSERT INTO conversation_turns (turn_id, session_id, sequence, role, "
                "message_id, replies_to, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stored.turn_id,
                    stored.session_id,
                    stored.sequence,
                    stored.role,
                    stored.message_id,
                    stored.replies_to,
                    stored.model_dump_json(),
                    _now(),
                ),
            )
        return stored, True

    def reply_to(self, turn_id: str) -> ConversationTurn | None:
        row = self.conn.execute(
            "SELECT data FROM conversation_turns WHERE replies_to = ?", (turn_id,)
        ).fetchone()
        return ConversationTurn.model_validate_json(row["data"]) if row else None

    def turns(self, session_id: str, *, last: int | None = None) -> list[ConversationTurn]:
        rows = self.conn.execute(
            "SELECT data FROM conversation_turns WHERE session_id = ? ORDER BY sequence",
            (session_id,),
        ).fetchall()
        turns = [ConversationTurn.model_validate_json(r["data"]) for r in rows]
        return turns[-last:] if last else turns

    # ------------------------------------------------------------------ actions

    def record_action(self, action: ActionRequest) -> tuple[ActionRequest, bool]:
        """Record a new action request. If its ID is already recorded, return the stored
        request and False: a redelivered action is never carried out twice."""
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT data FROM action_requests WHERE action_id = ?", (action.action_id,)
            ).fetchone()
            if row is not None:
                return ActionRequest.model_validate_json(row["data"]), False
            conn.execute(
                "INSERT INTO action_requests (action_id, session_id, kind, state, run_id, data, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    action.action_id,
                    action.session_id,
                    action.kind.value,
                    action.state.value,
                    action.run_id,
                    action.model_dump_json(),
                    _now(),
                    _now(),
                ),
            )
        return action, True

    def settle_action(
        self,
        action: ActionRequest,
        state: ActionState,
        *,
        reason: str | None = None,
        run_id: str | None = None,
        findings: tuple[dict[str, Any], ...] = (),
    ) -> ActionRequest:
        """Move a requested action to its outcome (once: a settled action stays settled)."""
        updated = action.model_copy(
            update={
                "state": state,
                "reason": reason,
                "run_id": run_id or action.run_id,
                "findings": findings,
                "updated_at": utcnow(),
            }
        )
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE action_requests SET state = ?, run_id = ?, data = ?, updated_at = ? "
                "WHERE action_id = ? AND state = ?",
                (
                    state.value,
                    updated.run_id,
                    updated.model_dump_json(),
                    _now(),
                    action.action_id,
                    ActionState.REQUESTED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise ConflictError(f"action {action.action_id} was already settled")
        return updated

    def get_action(self, action_id: str) -> ActionRequest | None:
        row = self.conn.execute(
            "SELECT data FROM action_requests WHERE action_id = ?", (action_id,)
        ).fetchone()
        return ActionRequest.model_validate_json(row["data"]) if row else None

    def list_actions(self, session_id: str) -> list[ActionRequest]:
        rows = self.conn.execute(
            "SELECT data FROM action_requests WHERE session_id = ? ORDER BY created_at, rowid",
            (session_id,),
        ).fetchall()
        return [ActionRequest.model_validate_json(r["data"]) for r in rows]
