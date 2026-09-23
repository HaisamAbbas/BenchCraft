"""Run engine end to end with a real instrumented application (06-T1..T4; gates 06-G1..G4).

Everything here drives real subprocesses and a real workspace database; invocation counts
come from the application's own log, not from the engine's bookkeeping."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus, WorkItemState
from aibench.engine.engine import RunController, RunState
from aibench.security.policy import ExecutionPolicy
from aibench.services.runs import evaluate_run, run_status
from tests.engine_support import Harness, max_overlap


def _states(h: Harness, run_id: str) -> dict[str, dict[str, int]]:
    storage, _ = h.storage()
    try:
        return run_status(storage, run_id)["counts"]
    finally:
        storage.db.close()


# --------------------------------------------------------------------------- 06-G1


def test_manual_plan_runs_end_to_end_and_saved_executions_rescore(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi", "b": "hi", "c": "crash"}),
        application=h.cli_app(),
        repetitions=2,
    )
    run_id = h.create(plan)
    outcome = h.execute(run_id)

    assert outcome.state is RunState.COMPLETED
    assert outcome.counts == {
        "execution": {"succeeded": 4, "failed": 2},  # "crash" exits 3: not retryable
        "evaluation": {"succeeded": 6},  # failed executions are recorded as skipped
    }
    assert h.count() == 6  # 3 cases x 2 repetitions, no retries of a non-retryable failure
    storage, artifacts = h.storage()
    try:
        results = storage.list_metric_results(run_id, scoring_id=f"engine-{run_id}")
        assert sorted((r.case_id, r.repetition_id, r.status.value) for r in results) == [
            ("a", 0, "ok"),
            ("a", 1, "ok"),
            ("b", 0, "ok"),
            ("b", 1, "ok"),
            ("c", 0, "skipped"),
            ("c", 1, "skipped"),
        ]
        events = storage.list_run_events(run_id)
        assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
        assert (
            events[0]["event_type"] == "run_created"
            and events[-1]["event_type"] == "run_session_ended"
        )
        report = asyncio.run(
            evaluate_run(
                run_id, plan, storage=storage, artifacts=artifacts, policy=ExecutionPolicy()
            )
        )
    finally:
        storage.db.close()
    assert h.count() == 6  # rescoring never invoked the application
    [summary] = report.summaries
    assert (summary.selected, summary.completed, summary.decisions["pass"]) == (6, 4, 4)


# --------------------------------------------------------------------------- 06-T3 retries


def test_transient_timeouts_are_retried_and_every_attempt_is_recorded(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"once": "flaky 1", "always": "flaky 9"}),
        application=h.cli_app(timeout=1.5),
        retry={"max_attempts": 3, "initial_backoff_seconds": 0.05, "max_backoff_seconds": 0.1},
    )
    run_id = h.create(plan)
    outcome = h.execute(run_id)
    storage, _ = h.storage()
    try:
        attempts = {c: storage.list_execution_attempts(run_id, c) for c in ("once", "always")}
        items = {w.task_key: w for w in storage.list_work_items(run_id)}
    finally:
        storage.db.close()
    assert [
        a.error_kind.value if a.error_kind else "ok"
        for a in sorted(attempts["once"], key=lambda a: a.attempt_id)
    ] == ["timeout", "ok"]
    assert len(attempts["always"]) == 3  # bounded by max_attempts, each attempt kept
    assert items["exec:once:r0"].state is WorkItemState.SUCCEEDED
    assert items["exec:always:r0"].state is WorkItemState.FAILED
    assert "retries exhausted after 3 attempts" in (items["exec:always:r0"].last_error or "")
    assert outcome.state is RunState.COMPLETED


def test_ambiguous_effectful_timeout_is_unknown_effect_and_never_repeated(tmp_path: Path) -> None:
    from tests.runner_support import load_example, serving

    effect_app = load_example("effect_counter_app")
    server = effect_app.make_server(port=0, respond_delay=1.5)
    h = Harness(tmp_path)
    with serving(server) as base:
        (tmp_path / "effect.json").write_text(
            '{"application_id": "booking", "runner": "http", "target": "b", "effects": "reversible",'
            f'"transport": {{"kind": "http", "url": "{base}/book", "timeout_seconds": 0.5}},'
            '"input_binding": {"fields": {"/destination": "/input"}}}',
            encoding="utf-8",
        )
        plan = h.plan(
            dataset=h.dataset({"trip": "Dubai"}),
            application="effect.json",
            retry={"max_attempts": 5, "initial_backoff_seconds": 0.01, "max_backoff_seconds": 0.01},
        )
        run_id = h.create(plan, policy=ExecutionPolicy(max_effects="reversible"))
        outcome = h.execute(run_id)
        import time

        time.sleep(1.6)
    assert server.count == 1 and len(server.received) == 1  # dispatched once, never repeated
    assert outcome.counts["execution"] == {"unknown_effect": 1}
    storage, _ = h.storage()
    try:
        [item] = [w for w in storage.list_work_items(run_id) if w.kind == "execution"]
    finally:
        storage.db.close()
    assert "reconcile before retrying" in (item.last_error or "")


# --------------------------------------------------------------------------- 06-G4 caps


@pytest.mark.parametrize("cap", [1, 3])
def test_application_concurrency_cap_bounds_in_flight_calls(tmp_path: Path, cap: int) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "slow 0.6" for i in range(6)}),
        application=h.cli_app(),
        concurrency={"application": cap, "evaluation": 1},
    )
    h.execute(h.create(plan))
    overlap = max_overlap(h.invocations())
    assert overlap <= cap
    if cap > 1:
        assert overlap > 1  # the cap is used, not just respected


def test_hard_call_budget_blocks_remaining_work_with_lost_coverage(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "hi" for i in range(4)}),
        application=h.cli_app(),
        budgets={"max_application_calls": 2},
    )
    run_id = h.create(plan)
    outcome = h.execute(run_id)
    assert h.count() == 2
    assert outcome.state is RunState.BUDGET_EXHAUSTED
    assert outcome.counts["execution"] == {"succeeded": 2, "blocked": 2}
    assert outcome.budget["application"]["calls"] == 2
    storage, _ = h.storage()
    try:
        results = storage.list_metric_results(run_id, scoring_id=f"engine-{run_id}")
    finally:
        storage.db.close()
    skipped = [r for r in results if r.status is ExecutionStatus.SKIPPED]
    assert len(results) == 4 and len(skipped) == 2  # blocked cases stay in the denominator
    assert all((r.reason or "").startswith("not_executed:blocked") for r in skipped)


def test_evaluator_budget_and_soft_cost_estimate(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "hi" for i in range(4)}),
        application=h.cli_app(),
        budgets={"max_evaluator_calls": 1},
    )
    outcome = h.execute(h.create(plan))
    assert outcome.budget["evaluator"]["calls"] == 1
    assert outcome.state is RunState.BUDGET_EXHAUSTED

    h2 = Harness(tmp_path / "soft")
    plan = h2.plan(
        dataset=h2.dataset({f"c{i}": "hi" for i in range(5)}),
        application=h2.cli_app(),
        budgets={"max_cost_usd": 2.5, "estimated_cost_per_application_call_usd": 1.0},
    )
    outcome = h2.execute(h2.create(plan))
    assert h2.count() == 2  # third call would project $3 > $2.5
    assert outcome.budget["limits"]["soft"] == {"max_cost_usd": 2.5}
    assert "soft estimate" in (outcome.stop_reason or "")


# --------------------------------------------------------------------------- 06-T4 control


def test_pause_stops_new_dispatch_until_resumed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "slow 0.3" for i in range(4)}), application=h.cli_app()
    )
    seen: dict[str, Any] = {}

    async def during(ctl: RunController, harness: Harness) -> None:
        await harness.wait_for_invocations(1)
        ctl.pause()
        while ctl.state is not RunState.PAUSED:
            await asyncio.sleep(0.02)
        seen["at_pause"] = harness.count()
        await asyncio.sleep(1.0)
        seen["after_wait"] = harness.count()
        ctl.resume()

    outcome = h.execute(h.create(plan), during=during)
    assert seen["at_pause"] == seen["after_wait"]  # nothing dispatched while paused
    assert outcome.state is RunState.COMPLETED and h.count() == 4


def test_cancel_stops_dispatch_and_cancels_in_flight_work(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "slow 5" if i else "hi" for i in range(4)}),
        application=h.cli_app(),
    )

    async def during(ctl: RunController, harness: Harness) -> None:
        await harness.wait_for_invocations(1)
        await asyncio.sleep(0.5)  # c1 (slow) is now in flight
        ctl.cancel()

    run_id = h.create(plan)
    outcome = h.execute(run_id, during=during)
    assert outcome.state is RunState.CANCELLED
    assert outcome.counts["execution"] == {"succeeded": 1, "cancelled": 3}
    storage, _ = h.storage()
    try:
        in_flight = storage.list_execution_attempts(run_id, "c1")
    finally:
        storage.db.close()
    assert [a.status for a in in_flight] == [ExecutionStatus.CANCELLED]  # recorded, not lost


def test_interrupt_then_resume_completes_without_duplicates(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({f"c{i}": "slow 0.2" for i in range(5)}), application=h.cli_app()
    )

    async def during(ctl: RunController, harness: Harness) -> None:
        await harness.wait_for_invocations(2)
        ctl.interrupt()

    run_id = h.create(plan)
    first = h.execute(run_id, during=during)
    assert first.state is RunState.INTERRUPTED
    done_first = h.count()
    assert 2 <= done_first < 5 and first.counts["execution"].get("pending", 0) == 5 - done_first

    second = h.execute(run_id)  # a fresh "process"
    assert second.state is RunState.COMPLETED
    assert h.count() == 5  # every case invoked exactly once across both sessions
    storage, _ = h.storage()
    try:
        results = storage.list_metric_results(run_id, scoring_id=f"engine-{run_id}")
        events = [e["event_type"] for e in storage.list_run_events(run_id)]
    finally:
        storage.db.close()
    assert sorted(r.case_id for r in results) == [f"c{i}" for i in range(5)]
    assert events.count("run_session_started") == 2
