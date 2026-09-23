"""Bounded summaries of long conversations (§8, 10-T2).

Older turns beyond the conversation window are represented by a summary built only from
structured records — decision IDs and their changes, the user's own corrections, open
questions and run IDs — never by paraphrasing messages and never with run results. It
is deterministic (no model writes it) and size-capped, and it is labelled as references
only: the live session state, reloaded from storage every turn, is the authority. A
summary therefore cannot restate a run's numbers, revive a superseded choice, or carry a
permission: permissions come only from the policy and the grant made when the session
was opened (10-G4).
"""

from __future__ import annotations

import json
from typing import Any

from aibench.core.models import deep_unfreeze
from aibench.sessions.store import SessionStore

MAX_SUMMARY_CHARS = 4_000
_FIELD_CHARS = 200
LABEL = (
    "references only, not authoritative: the session state message holds the current "
    "draft, questions and run status"
)


def _short(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, default=str) if not isinstance(value, str) else value
    return text if len(text) <= _FIELD_CHARS else text[: _FIELD_CHARS - 3] + "..."


def session_summary(store: SessionStore, session_id: str, *, earlier_turns: int) -> dict[str, Any]:
    """A size-capped summary of the conversation so far."""
    session = store.get_session(session_id)
    assert session is not None
    decisions = store.list_decisions(session_id)
    current = next(d for d in decisions if d.decision_id == session.decision_id)
    history = [
        {
            "decision_id": d.decision_id,
            "revision": d.revision,
            "source": d.source,
            "change": _short(deep_unfreeze(d.structured_change)),
        }
        for d in decisions[1:]
    ]
    # Every change after the first draft is the user's: typed commands directly, and the
    # assistant's patches only when grounded in the user's own quoted words.
    corrections = [
        {**h, "via": "command" if h["source"] == "user" else "assistant"} for h in history
    ]
    summary: dict[str, Any] = {
        "kind": LABEL,
        "earlier_turns": earlier_turns,
        "current_revision": session.revision,
        "objectives": [_short(o) for o in current.choices.objectives],
        "decisions": history,
        "user_corrections": corrections,
        "open_questions": [
            {"question_id": q.question_id, "prompt": _short(q.prompt), "revision": q.draft_revision}
            for q in store.questions(session_id, "open")
        ],
        "runs": list(dict.fromkeys(a.run_id for a in store.list_actions(session_id) if a.run_id)),
    }
    return _bounded(summary)


def _bounded(summary: dict[str, Any]) -> dict[str, Any]:
    """Keep the summary within MAX_SUMMARY_CHARS whatever the conversation holds. Drop in
    order of least value: old decision history, then older runs, objectives and
    corrections, then questions beyond the newest few; each drop is counted. The newest
    corrections and open questions are what must survive a long conversation."""
    summary["omitted"] = {}

    def fits() -> bool:
        return len(json.dumps(summary)) <= MAX_SUMMARY_CHARS

    for key, keep in (
        ("decisions", 0),
        ("runs", 5),
        ("objectives", 5),
        ("user_corrections", 5),
        ("open_questions", 5),
        ("user_corrections", 1),
        ("open_questions", 1),
        ("runs", 0),
        ("objectives", 0),
    ):
        while not fits() and len(summary[key]) > keep:
            summary[key].pop(0)
            summary["omitted"][key] = summary["omitted"].get(key, 0) + 1
    if not fits():  # only field values are left; they are already capped
        summary = {"kind": summary["kind"], "omitted": "all details (too large)"}
    return summary
