"""A judge that scores the same answer 0.2 and then 1.0 makes one score untrustworthy. The
G-Eval metric scores each case several times; when the scores disagree its result says
`unstable:`, and the report counts those results so they are not read as solid numbers."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

from rich.console import Console

from aibench.core.models import Decision, EvaluationResult, ExecutionStatus, MetricValue
from aibench.reporting.render import render as render_report
from aibench.services.reports import build_report, report_facts, unstable_count
from aibench.tui import render
from tests.test_reports import Project, _rows


def _result(case: str, status: ExecutionStatus, reason: str | None) -> EvaluationResult:
    return EvaluationResult(
        result_id=f"r-{case}",
        run_id="run-1",
        case_id=case,
        metric_id="deepeval.g_eval@1.1.0",
        metric_version="1.1.0",
        status=status,
        decision=Decision.PASS if status is ExecutionStatus.OK else Decision.INDETERMINATE,
        value=MetricValue(kind="scalar", value=0.9) if status is ExecutionStatus.OK else None,
        reason=reason,
    )


def test_only_scored_results_that_say_unstable_are_counted() -> None:
    ok = ExecutionStatus.OK
    results = [
        _result("a", ok, "unstable: the judge's 3 scores disagree (0.20, 0.90, 1.00); median 0.90"),
        _result("b", ok, "median of 3 judge scores (1.00, 1.00, 1.00)"),
        _result("c", ok, None),
        _result("d", ExecutionStatus.ERROR, "unstable: but it errored"),
    ]
    assert unstable_count(results) == 1


def _report_with_unstable(tmp_path: Path, unstable: int) -> dict[str, Any]:
    """A real run's report, with the metric's unstable count set (the count comes from
    stored results; here the renderers are what is under test)."""
    project = Project(tmp_path, _rows(("a", "The fine is 1,000 rupees.", "x")))
    run_id = project.run()
    storage, artifacts = project.storage()
    try:
        report = build_report(storage, artifacts, run_id)
    finally:
        storage.db.close()
    for scoring in report["scoring_passes"]:
        for metric in scoring["metrics"]:
            assert metric["unstable_results"] == 0  # a native metric is never unstable
            metric["unstable_results"] = unstable
    return report


def test_every_report_form_names_unstable_scores_and_says_nothing_otherwise(
    tmp_path: Path,
) -> None:
    for unstable, label in ((2, "2 unstable"), (0, "")):
        report = _report_with_unstable(tmp_path / f"u{unstable}", unstable)
        console = Console(file=io.StringIO(), width=220, highlight=False)
        render.report(console, report_facts(report))
        terminal = " ".join(console.file.getvalue().split())  # type: ignore[attr-defined]
        files = [render_report(report, fmt) for fmt in ("markdown", "html")]
        if unstable:
            assert "2 unstable score(s)" in terminal
            assert all("2 unstable" in text for text in files)
        else:
            assert "unstable" not in terminal
            assert all("unstable" not in text for text in files)
