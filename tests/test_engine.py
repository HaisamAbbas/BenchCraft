"""Run engine end to end with a real instrumented application (06-T1..T4; gates 06-G1..G4).

Everything here drives real subprocesses and a real workspace database; invocation counts
come from the application's own log, not from the engine's bookkeeping."""

from __future__ import annotations

import asyncio
import random
import time
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus, WorkItem, WorkItemState
from aibench.engine.budget import BudgetLedger
from aibench.engine.compile import compile_plan
from aibench.engine.engine import (
    RunController,
    RunEngine,
    RunState,
    _Item,
    evaluation_key,
    execution_key,
)
from aibench.security.policy import ExecutionPolicy
from aibench.services.case_export import build_case_export
from aibench.services.reports import build_report
from aibench.services.runs import create_run, evaluate_run, run_status
from aibench.storage.repositories import WorkItemSettlement
from tests.engine_support import Harness, max_overlap


def _states(h: Harness, run_id: str) -> dict[str, dict[str, int]]:
    storage, _ = h.storage()
    try:
        return run_status(storage, run_id)["counts"]
    finally:
        storage.db.close()


# --------------------------------------------------------------------------- 06-G1


def test_run_engine_pages_pending_work_queues(tmp_path: Path) -> None:
    case_ids = [f"case-{index:04d}" for index in range(1207)]
    h = Harness(tmp_path)
    plan_path = h.plan(
        dataset=h.dataset({case_id: "hi" for case_id in case_ids}),
        application=h.cli_app(),
    )
    compiled = compile_plan(plan_path, policy=ExecutionPolicy(), trusted_local=True)
    storage, artifacts = h.storage()
    try:
        run_id = create_run(compiled, storage=storage, artifacts=artifacts, granted_by="test")
        engine = RunEngine(
            storage=storage,
            artifacts=artifacts,
            run_id=run_id,
            plan=compiled.plan,
            application=compiled.application.spec,
            runner=None,
            cases={case.case_id: case for case in compiled.cases},
            metrics=compiled.metrics,
            scoring_id=f"engine-{run_id}",
            ledger=BudgetLedger(compiled.plan.budgets),
            controller=RunController(),
            rng=random.Random(1),
            reset_mode="per_case",
        )
        engine._load()

        assert len(engine._exec_queue) == 500
        assert len(engine._warmup_queue) == 0
        seen_executions: list[str] = []
        while engine._exec_queue or engine._exec_source is not None:
            engine._refill_execution_queues()
            assert len(engine._exec_queue) <= 500
            seen_executions.extend(item.task_key for item in engine._exec_queue)
            engine._exec_queue.clear()
        assert seen_executions == [execution_key(case_id, 0) for case_id in case_ids]

        for start in range(0, len(case_ids), 500):
            settlements = [
                WorkItemSettlement(
                    task_key=execution_key(case_id, 0),
                    from_states=frozenset({WorkItemState.PENDING}),
                    to_state=WorkItemState.SUCCEEDED,
                )
                for case_id in case_ids[start : start + 500]
            ]
            storage.settle_work_items(
                run_id,
                settlements,
                event_type="test_queue_ready",
                payload={"count": len(settlements)},
            )

        [metric] = compiled.metrics
        engine._queued_evaluations.update(
            evaluation_key(case_id, 0, metric.binding_hash) for case_id in case_ids[:900]
        )
        engine._refill_evaluation_queue()
        assert len(engine._eval_queue) == 100
        assert len(engine._queued_evaluations) == 1000
        engine._eval_queue.clear()
        engine._queued_evaluations.clear()
        engine._eval_after_row_id = 0

        other_binding = "sha256:" + "b" * 64
        other_evaluation = WorkItem(
            work_item_id=f"{run_id}:eval:{case_ids[0]}:r0:{other_binding[7:23]}",
            run_id=run_id,
            task_key=evaluation_key(case_ids[0], 0, other_binding),
            kind="evaluation",
            dependency_keys=(execution_key(case_ids[0], 0),),
        )
        storage.commit_work_items([other_evaluation])
        engine._binding_by_key[other_binding[7:23]] = other_binding
        ready_page = storage.list_ready_evaluation_work_items(run_id, limit=500)
        delayed = ready_page[:245]
        held = ready_page[245:500]
        engine._queued_evaluations.update(item.task_key for _, item in delayed)
        engine._delayed = [
            (
                time.monotonic() + 3600,
                index,
                engine._item(work_item),
            )
            for index, (_, work_item) in enumerate(delayed)
        ]
        engine._eval_queue.extend(engine._item(item) for _, item in held)
        engine._queued_evaluations.update(item.task_key for _, item in held)
        cursor = 0
        while True:
            remaining = storage.list_ready_evaluation_work_items(
                run_id, after_row_id=cursor, limit=500
            )
            if not remaining:
                raise AssertionError("expected the independent evaluation to be ready")
            if remaining[-1][1].task_key == other_evaluation.task_key:
                engine._eval_after_row_id = remaining[-2][0]
                break
            cursor = remaining[-1][0]
        engine._refill_evaluation_queue()
        assert len(engine._eval_queue) == 500
        assert len(engine._queued_evaluations) == 745
        assert engine._eval_queue[255].task_key == other_evaluation.task_key
        assert engine._eval_queue[255].binding_hash == other_binding
        engine._eval_queue.clear()
        engine._queued_evaluations.clear()
        engine._delayed.clear()
        engine._eval_after_row_id = 0
        assert storage.transition_work_item(
            run_id,
            other_evaluation.task_key,
            from_states={WorkItemState.PENDING},
            to_state=WorkItemState.CANCELLED,
        )

        # Running evaluations retain reservations because retry completions can rejoin the
        # delay heap while held work and a fresh page already occupy the rest of the buffer.
        in_flight_page = storage.list_ready_evaluation_work_items(
            run_id, after_row_id=ready_page[-1][0], limit=218
        )
        scenario_rows = ready_page + in_flight_page
        delayed_rows = scenario_rows[:400]
        held_rows = scenario_rows[400:655]
        in_flight_rows = scenario_rows[655:718]
        engine._queued_evaluations.update(item.task_key for _, item in scenario_rows[:718])
        engine._eval_queue.extend(engine._item(item) for _, item in held_rows)
        deadline = time.monotonic() - 1
        engine._delayed = [
            (deadline, index, engine._item(item))
            for index, (_, item) in enumerate(delayed_rows)
        ]
        engine._eval_after_row_id = scenario_rows[717][0]
        engine._refill_evaluation_queue()
        assert len(engine._eval_queue) == 500
        assert len(engine._queued_evaluations) == 963
        engine._delayed.extend(
            (deadline, 400 + index, engine._item(item))
            for index, (_, item) in enumerate(in_flight_rows)
        )
        engine._release_due_retries()
        assert not engine._delayed
        assert len(engine._eval_queue) == 963
        assert len(engine._queued_evaluations) == 963
        engine._eval_queue.clear()
        engine._queued_evaluations.clear()
        engine._eval_after_row_id = 0

        seen_evaluations: list[str] = []
        while True:
            engine._refill_evaluation_queue()
            assert len(engine._eval_queue) <= 500
            if not engine._eval_queue:
                break
            while engine._eval_queue:
                item = engine._eval_queue.popleft()
                engine._queued_evaluations.remove(item.task_key)
                assert storage.transition_work_item(
                    run_id,
                    item.task_key,
                    from_states={WorkItemState.PENDING},
                    to_state=WorkItemState.RUNNING,
                    attempt=1,
                )
                seen_evaluations.append(item.task_key)

        assert seen_evaluations == [
            evaluation_key(case_id, 0, metric.binding_hash) for case_id in case_ids
        ]

        # Promoting due retries must make progress even when the prefetched page is full.
        engine._exec_queue.clear()
        engine._exec_queue.extend(
            _Item(f"queued-{index}", "execution", case_ids[0], 0, 1) for index in range(500)
        )
        deadline = time.monotonic() - 1
        engine._delayed = [
            (
                deadline,
                index,
                _Item(f"retry-{index}", "execution", case_ids[0], 0, 1),
            )
            for index in range(500)
        ]
        engine._release_due_retries()
        assert not engine._delayed
        assert len(engine._exec_queue) == 1000
    finally:
        storage.db.close()


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


def test_warmups_run_before_measurement_and_are_excluded_from_scoring_and_latency(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi", "b": "hi"}),
        application=h.cli_app(),
        repetitions=2,
        warmup_repetitions=1,
        concurrency={"application": 1, "evaluation": 1},
    )
    run_id = h.create(plan)
    outcome = h.execute(run_id)

    # The first two calls are the warmup phase (one per case); all four measurements follow.
    assert [call["case"] for call in h.invocations()] == ["a", "b", "a", "a", "b", "b"]
    assert outcome.state is RunState.COMPLETED
    assert outcome.counts == {
        "execution": {"succeeded": 6},
        "evaluation": {"succeeded": 4},
    }
    storage, artifacts = h.storage()
    try:
        attempts = storage.list_execution_attempts(run_id)
        assert sum(attempt.warmup for attempt in attempts) == 2
        items = storage.list_work_items(run_id)
        assert sum(item.kind == "execution" and item.warmup for item in items) == 2
        assert sum(item.kind == "evaluation" for item in items) == 4
        report = build_report(storage, artifacts, run_id)
        case_export = build_case_export(storage, artifacts, run_id)
    finally:
        storage.db.close()

    assert report["application"]["latency"]["successful_requests"] == 4
    assert report["application"]["measurement"] == {
        "planned": 4,
        "recorded": 4,
        "not_recorded": 0,
        "completed": 4,
        "failed": 0,
    }
    assert report["application"]["warmup"]["planned"] == 2
    assert report["application"]["warmup"]["recorded"] == 2
    assert report["application"]["warmup"]["dispatched_calls"] == 2
    assert report["application"]["warmup"]["cost"]["calls_with_unknown_cost"] == 2
    assert len(case_export["rows"]) == 4
    assert all(row["repetition"] < 2 for row in case_export["rows"])


def test_warmups_consume_application_budget_before_measurements(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi", "b": "hi"}),
        application=h.cli_app(),
        repetitions=1,
        warmup_repetitions=1,
        budgets={"max_application_calls": 1},
        concurrency={"application": 1, "evaluation": 1},
    )
    run_id = h.create(plan)
    outcome = h.execute(run_id)
    assert outcome.state is RunState.BUDGET_EXHAUSTED
    assert h.count() == 1
    assert outcome.budget["application"]["calls"] == 1
    storage, _ = h.storage()
    try:
        attempts = storage.list_execution_attempts(run_id)
        assert len(attempts) == 1 and attempts[0].warmup
        assert sum(item.kind == "execution" and item.warmup for item in storage.list_work_items(run_id)) == 2
    finally:
        storage.db.close()


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


def test_a_pause_while_the_run_is_starting_is_committed_as_pausing_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 22-T3 regression: a pause during start-up (runner preparation, evaluator start-up)
    # dispatched nothing, but the committed status stayed "running" until start-up ended,
    # so /status contradicted the pause (section 15; seen as an intermittent acceptance
    # failure on a loaded machine).
    from aibench.runners.base import BaseRunner

    original = BaseRunner.prepare

    async def slow_prepare(self: BaseRunner) -> None:
        await asyncio.sleep(1.5)
        await original(self)

    monkeypatch.setattr(BaseRunner, "prepare", slow_prepare)
    h = Harness(tmp_path)
    plan = h.plan(dataset=h.dataset({"c0": "hi", "c1": "hi"}), application=h.cli_app())
    run_id = h.create(plan)
    seen: dict[str, Any] = {}

    def committed() -> str:
        storage, _ = h.storage()
        try:
            return str(run_status(storage, run_id)["status"])
        finally:
            storage.db.close()

    async def during(ctl: RunController, harness: Harness) -> None:
        while ctl.state is not RunState.RUNNING:
            await asyncio.sleep(0.01)
        ctl.pause()  # while the runner is still being prepared
        await asyncio.sleep(0.3)
        seen["while_starting"] = (committed(), harness.count())
        while ctl.state is not RunState.PAUSED:
            await asyncio.sleep(0.02)
        seen["paused"] = (committed(), harness.count())
        ctl.resume()

    outcome = h.execute(run_id, during=during)
    assert seen["while_starting"] == ("pausing", 0)
    assert seen["paused"] == ("paused", 0)  # nothing was dispatched while paused
    assert outcome.state is RunState.COMPLETED and h.count() == 2


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
        dataset=h.dataset({f"c{i}": "slow 0.2" for i in range(5)}),
        application=h.cli_app(environment_digest="test-runtime-pin"),
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
