"""LightRAG's embedding server was down, so it answered "No relevant context found for the
query." to all 15 questions, with HTTP 200. Every execution counted as a success, the run
said "completed", and only the scores (and a head-to-head judge) showed something was wrong.
A finished run where every case got the same answer now says so in its status."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aibench.services.runs import run_status
from tests.session_support import SessionHarness

APP = r"""
import json, pathlib, sys
request = json.load(sys.stdin)
answers = json.loads(pathlib.Path("answers.json").read_text())
print(json.dumps({"output": answers.get(request["case_id"], answers.get("*"))}))
"""

QUESTIONS = {"a": "How long do refunds take?", "b": "Can I return a used item?", "c": "Hi?"}


def _status_after_run(tmp_path: Path, answers: dict[str, str]) -> list[str]:
    h = SessionHarness(tmp_path)
    (h.root / "app.py").write_text(APP, encoding="utf-8")
    (h.root / "answers.json").write_text(json.dumps(answers), encoding="utf-8")
    ctl = h.open_session(QUESTIONS, objectives=("catch wrong answers",))
    try:

        async def go() -> str:
            started = await ctl.start_run(action_id="act-1", expected_revision=1)
            done = await ctl.wait_for_run(started.run_id)
            assert done is not None and done.counts["execution"] == {"succeeded": 3}, done
            return started.run_id

        run_id = asyncio.run(go())
        chat_view = ctl.run_status(run_id)  # /status in the chat
        assert chat_view["warnings"] == run_status(ctl.storage, run_id)["warnings"]
        return list(chat_view["warnings"])
    finally:
        ctl.storage.db.close()


def test_a_run_where_every_case_got_the_same_answer_says_so(tmp_path: Path) -> None:
    warnings = _status_after_run(tmp_path, {"*": "No relevant context found for the query."})
    [warning] = [w for w in warnings if "same answer" in w]
    assert warning.startswith("every case got the same answer (3 answers): ")
    assert "'No relevant context found for the query.'" in warning
    assert "failing without reporting an error" in warning


def test_different_answers_raise_no_warning(tmp_path: Path) -> None:
    answers = {"a": "Five days.", "b": "No.", "c": "Hello!"}
    assert not [w for w in _status_after_run(tmp_path, answers) if "same answer" in w]
