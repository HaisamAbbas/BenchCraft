"""F07: immediate and reconstructed summaries retain missing selected work."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import ExecutionStatus, WorkItem, WorkItemState
from aibench.services.reports import build_report
from tests.engine_support import Harness
from tests.scoring_support import RUN_ID, Seeded, case, execution
from tests.session_support import SessionHarness


@pytest.mark.parametrize("recorded", [0, 1, 2])
@pytest.mark.parametrize(
    "state", [WorkItemState.BLOCKED, WorkItemState.PENDING, WorkItemState.CANCELLED]
)
def test_missing_execution_items_stay_selected_and_reports_agree(
    tmp_path: Path, recorded: int, state: WorkItemState
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case(c, "yes") for c in ["a:r2:x", "b", "unselected"]],
        [execution(c, "yes") for c in ["a:r2:x", "b"][:recorded]],
    )
    try:
        for case_id in ["a:r2:x", "b"]:
            seeded.storage.commit_work_item(
                WorkItem(
                    work_item_id=f"work-{case_id}",
                    run_id=RUN_ID,
                    task_key=f"exec:{case_id}:r0",
                    kind="execution",
                    state=state,
                )
            )
        scored = seeded.score([{"metric": "native.exact_match"}])
        summary = scored.summaries[0].as_dict()
        assert summary["selected"] == 2
        assert summary["completed"] == recorded
        assert summary["unavailable"] == 2 - recorded
        assert summary["pending"] == 0
        assert summary["completed_coverage"] == recorded / 2
        if recorded < 2:
            assert summary["reasons"] == {"not_executed": 2 - recorded}
        assert scored.budget["evaluator"]["calls"] == recorded
        document = build_report(seeded.storage, seeded.artifacts, RUN_ID)
        [scoring] = document["scoring_passes"]
        assert scoring["metrics"][0]["summary"] == summary
    finally:
        seeded.storage.db.close()


def test_legacy_selection_uses_final_executions_including_repetitions(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("a", "yes"), case("unselected", "yes")],
        [
            execution("a", "no", attempt_id=0),
            execution("a", "yes", attempt_id=1),
            execution("a", "yes", repetition_id=1),
        ],
    )
    try:
        scored = seeded.score([{"metric": "native.exact_match"}])
        assert scored.summaries[0].selected == 2
        assert scored.summaries[0].completed_coverage == 1.0
        document = build_report(seeded.storage, seeded.artifacts, RUN_ID)
        assert (
            document["scoring_passes"][0]["metrics"][0]["summary"] == scored.summaries[0].as_dict()
        )
    finally:
        seeded.storage.db.close()


def test_unplanned_remote_style_pass_uses_its_uploaded_results_denominator(tmp_path: Path) -> None:
    """A remote job can report one successful uploaded output from a two-execution run.

    It has no engine work graph or rescore pass event; its own uploaded result set defines
    selection. F07's frozen denominator rule must only affect recorded-output rescoring.
    """
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("uploaded", "yes"), case("failed_or_nontext", "yes")],
        [
            execution("uploaded", "yes"),
            execution("failed_or_nontext", status=ExecutionStatus.ERROR),
        ],
    )
    try:
        scored = seeded.score([{"metric": "native.exact_match"}])
        seeded.storage.conn.execute(
            "DELETE FROM metric_results WHERE run_id = ? AND case_id = ?",
            (RUN_ID, "failed_or_nontext"),
        )
        seeded.storage.conn.execute(
            "DELETE FROM run_events WHERE run_id = ? AND event_type = ?",
            (RUN_ID, "scoring_pass"),
        )
        seeded.storage.conn.commit()
        document = build_report(seeded.storage, seeded.artifacts, RUN_ID)
        [scoring] = document["scoring_passes"]
        assert scoring["scoring_id"] == scored.scoring_id
        assert scoring["metrics"][0]["summary"]["selected"] == 1
        assert scoring["metrics"][0]["summary"]["completed_coverage"] == 1.0
    finally:
        seeded.storage.db.close()


def test_cli_partial_selected_repetitions_keep_frozen_denominator(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi", "b": "hi", "unselected": "hi"}),
        application=h.cli_app(),
        selection={"limit": 2},
        repetitions=2,
        budgets={"max_application_calls": 1},
    )
    run_id = h.create(plan)
    h.execute(run_id)
    assert h.count() == 1

    # A changed metric binding still scores the original selected work, not plan selection.
    rescore_plan = h.plan(
        dataset="data.jsonl",
        application="app.json",
        selection={"limit": 1},
        metrics=[{"metric": "native.exact_match", "params": {"case_sensitive": False}}],
    )
    result = CliRunner().invoke(
        app,
        [
            "evaluate",
            run_id,
            "--plan",
            str(rescore_plan),
            "--workspace",
            str(h.workspace.root.parent),
            "--json",
        ],
    )
    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    [summary] = payload["summaries"]
    assert (summary["selected"], summary["completed"], summary["unavailable"]) == (4, 1, 3)
    assert summary["completed_coverage"] == 0.25
    storage, artifacts = h.storage()
    try:
        document = build_report(storage, artifacts, run_id)
        scoring = next(
            p for p in document["scoring_passes"] if p["scoring_id"] == payload["scoring_id"]
        )
        assert scoring["metrics"][0]["summary"] == summary
    finally:
        storage.db.close()
    assert h.count() == 1


def test_session_rescore_keeps_the_same_missing_work(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    controller = h.open_session(
        {"a": "hi", "b": "hi"},
        objectives=("catch wrong answers",),
        policy={"ceilings": {"max_application_calls": 1}},
    )

    async def go():  # type: ignore[no-untyped-def]
        started = await controller.start_run(action_id="selected-budget", expected_revision=1)
        await controller.wait_for_run(started.run_id)
        rescored = await controller.rescore(started.run_id)
        stored = controller.report(started.run_id)
        return rescored, stored

    try:
        rescored, stored = asyncio.run(go())
        [summary] = rescored["summaries"]
        assert (summary["selected"], summary["completed"], summary["unavailable"]) == (2, 1, 1)
        scoring = next(
            p for p in stored["scoring_passes"] if p["scoring_id"] == rescored["scoring_id"]
        )
        assert scoring["metrics"][0]["summary"] == summary
        assert rescored["budget"]["evaluator"]["calls"] == 0  # completed result was carried
        assert h.count() == 1
    finally:
        controller.storage.db.close()
