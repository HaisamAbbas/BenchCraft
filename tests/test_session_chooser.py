"""Choosing a session to resume: sessions that hold work are listed with what they check
and whether they ran, newest first; sessions opened and abandoned (no objective, no run)
are hidden and reused, so they do not pile up. Found when an empty session was picked by
mistake among six that all looked alike."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from aibench.cli import chat as chat_cli
from aibench.sessions.summary import SessionLine, describe_sessions
from tests.session_support import SessionHarness

NOW = datetime(2026, 9, 29, 6, 12, tzinfo=UTC)


def _line(session_id: str, *objectives: str, runs: int = 0) -> SessionLine:
    return SessionLine(session_id, 1, NOW, tuple(objectives), runs)


def test_a_session_is_empty_only_without_objectives_and_runs() -> None:
    assert _line("a").empty
    assert not _line("b", "answers are correct").empty
    assert not _line("c", runs=1).empty


def test_pick_lists_only_sessions_with_work_and_reuses_empty_ones() -> None:
    real = _line("real", "traffic correctness")
    empties = [_line("empty-new"), _line("empty-old")]
    seen: list[tuple[list[str], int]] = []

    def chooser(worth: list[SessionLine], hidden: int) -> str | None:
        seen.append(([line.session_id for line in worth], hidden))
        return "real"

    # Newest first, as the store lists them: the empty ones are never offered.
    picked = chat_cli._pick([empties[0], real, empties[1]], send=False, chooser=chooser)
    assert picked == ("real", None) and seen == [(["real"], 2)]

    # Nothing worth resuming: no prompt at all, the newest empty session is reused.
    seen.clear()
    assert chat_cli._pick(empties, send=False, chooser=chooser) == ("empty-new", None)
    assert seen == []


def test_pick_without_a_terminal_needs_exactly_one_session_with_work() -> None:
    def chooser(_worth: list[SessionLine], _hidden: int) -> str | None:
        raise AssertionError("no prompt without a terminal")

    one = [_line("empty"), _line("real", "x")]
    assert chat_cli._pick(one, send=True, chooser=chooser) == ("real", None)
    assert chat_cli._pick([_line("empty")], send=True, chooser=chooser) == ("empty", None)
    session_id, problem = chat_cli._pick(
        [_line("a", "x"), _line("b", "y")], send=True, chooser=chooser
    )
    assert session_id is None and problem and "2 sessions" in problem and "--resume" in problem


def test_the_chooser_shows_what_each_session_checks_and_whether_it_ran(monkeypatch, capsys) -> None:
    long_objective = "answers are correct and never invent fines " + "x" * 80
    sessions = [
        _line("ses-new", "traffic correctness", runs=2),
        _line("ses-old", long_objective, "second", "third", "fourth"),
    ]
    monkeypatch.setattr("builtins.input", lambda _prompt: "2")
    assert chat_cli._choose(sessions, hidden_empty=3) == "ses-old"
    shown = capsys.readouterr().out
    assert "[1] ses-new | 2026-09-29 06:12 | traffic correctness | 2 run(s)" in shown
    assert "not run yet" in shown and "(+2 more)" in shown
    assert "..." in shown and "x" * 80 not in shown  # a long objective is cut
    assert "3 empty session(s) not listed; a new session reuses one" in shown

    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    assert chat_cli._choose(sessions) is None


def test_describing_real_sessions_separates_work_from_abandoned_ones(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    worked = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
    empty = h.open_session({"a": "answer"})
    ran = h.open_session({"a": "answer"}, objectives=("catch wrong answers",))
    try:

        async def run() -> str:
            started = await ran.start_run(action_id="run-1", expected_revision=1)
            done = await ran.wait_for_run(started.run_id)
            assert done is not None and done.state.value == "completed"
            return started.run_id

        run_id = asyncio.run(run())
        sessions = [worked.store.get_session(s.session_id) for s in (worked, empty, ran)]
        by_id = {
            line.session_id: line
            for line in describe_sessions(worked.store, [s for s in sessions if s is not None])
        }
        assert by_id[worked.session_id].objectives == ("catch wrong answers",)
        assert not by_id[worked.session_id].empty and by_id[worked.session_id].runs == 0
        assert by_id[empty.session_id].empty
        assert by_id[ran.session_id].runs == 1 and run_id
    finally:
        for controller in (worked, empty, ran):
            controller.storage.db.close()
