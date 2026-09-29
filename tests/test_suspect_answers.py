"""Answers that look like the application failing while reporting success. A benchmark run
of a real app scored 15 of 15 answers as zeros because every answer was "Error: 401 Invalid
API Key" returned with a success status; the report now says so before anything else."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from aibench.services.reports import build_report, report_facts
from aibench.services.suspect_answers import looks_like_error
from aibench.tui import render
from tests.test_reports import Project, _rows

REAL_ERRORS = [
    "Error: Error code: 401 - {'error': {'message': 'Invalid API Key'}}",
    "Error code: 429 - rate limit reached for model",
    "error: something went wrong",
    "Traceback (most recent call last):\n  File ...",
    "Exception: connection refused",
    "The request failed: Invalid API key provided.",
    "Rate limit reached for requests, retry later",
    "500 Internal Server Error",
    {"answer": "Error: Invalid API Key"},
    {"data": {"text": "Error code: 503 - Service Unavailable"}},
]
REAL_ANSWERS = [
    "The fine for riding without a helmet is 1,000 rupees.",
    "Errors in the report should be fixed before filing.",  # mentions errors, is an answer
    "I don't have information about that in the provided context.",
    "Speed limits: 50 km/h in cities. Error handling is described in chapter 4.",
    {"answer": "The speed limit is 50 km/h."},
    "",
    None,
]


@pytest.mark.parametrize("answer", REAL_ERRORS)
def test_an_error_returned_as_an_answer_is_recognised(answer: object) -> None:
    assert looks_like_error(answer)


@pytest.mark.parametrize("answer", REAL_ANSWERS)
def test_a_real_answer_is_not_flagged(answer: object) -> None:
    assert not looks_like_error(answer)


def test_only_the_start_of_a_long_answer_counts() -> None:
    assert not looks_like_error("A" * 500 + " Error code: 500")
    assert looks_like_error("Error: " + "A" * 500)


def test_the_report_and_terminal_summary_warn_about_error_answers(tmp_path: Path) -> None:
    rows = _rows(
        ("fine-1", "The fine is 1,000 rupees.", "The fine is 1,000 rupees."),
        ("broken-1", "Error: Invalid API Key", "The fine is 1,000 rupees."),
        ("broken-2", "Error code: 429 rate limit reached", "The fine is 1,000 rupees."),
    )
    project = Project(tmp_path, rows)
    run_id = project.run()
    storage, artifacts = project.storage()
    try:
        report = build_report(storage, artifacts, run_id)
    finally:
        storage.db.close()
    assert report["application"]["error_like_answers"] == {
        "count": 2,
        "case_ids": ["broken-1", "broken-2"],
    }
    facts = report_facts(report)
    assert facts["application"]["error_like_answers"]["count"] == 2

    console = Console(file=io.StringIO(), width=140, highlight=False)
    render.report(console, facts)
    shown = console.file.getvalue()  # type: ignore[attr-defined]
    assert "2 of 3 answers look like errors, not answers" in shown
    assert "broken-1, broken-2" in shown and "/case CASE_ID" in shown


def test_a_clean_run_has_no_warning(tmp_path: Path) -> None:
    project = Project(tmp_path, _rows(("a", "The fine is 1,000 rupees.", "x")))
    run_id = project.run()
    storage, artifacts = project.storage()
    try:
        facts = report_facts(build_report(storage, artifacts, run_id))
    finally:
        storage.db.close()
    assert facts["application"]["error_like_answers"] == {"count": 0, "case_ids": []}
    console = Console(file=io.StringIO(), width=140, highlight=False)
    render.report(console, facts)
    assert "look like errors" not in console.file.getvalue()  # type: ignore[attr-defined]
