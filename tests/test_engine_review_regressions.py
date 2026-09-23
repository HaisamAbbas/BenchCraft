"""Regression tests for the Prompt 06 independent review (see reports/06.md §4).

Each finding was reproduced first; the reproductions asserted the defect and passed on the
pre-fix code. These tests assert the corrected behavior."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus, WorkItemState
from aibench.core.plans import BudgetLimits, ExecutablePlan, PluginEnvironmentRef
from aibench.engine.budget import BudgetLedger
from aibench.engine.compile import PolicyDenied, compile_plan
from aibench.engine.engine import RunController, RunState
from aibench.security.policy import ExecutionPolicy, plan_denials
from aibench.services.runs import execute_run
from aibench.storage.repositories import LeaseHeld, Storage
from tests.engine_support import Harness


class SimulatedCrash(BaseException):
    pass


def _states(h: Harness, run_id: str) -> dict[str, str]:
    storage, _ = h.storage()
    try:
        return {w.task_key: w.state.value for w in storage.list_work_items(run_id)}
    finally:
        storage.db.close()


def _events(h: Harness, run_id: str) -> list[dict[str, Any]]:
    storage, _ = h.storage()
    try:
        return storage.list_run_events(run_id)
    finally:
        storage.db.close()


# --------------------------------------------------------------------------- single session


def test_a_second_session_cannot_resume_a_live_run(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(dataset=h.dataset({"a": "slow 1.0", "b": "hi"}), application=h.cli_app())
    )
    refused: dict[str, Any] = {}

    async def during(ctl: RunController, harness: Harness) -> None:
        attempts = harness.log.with_suffix(".attempts")
        while not attempts.exists():
            await asyncio.sleep(0.02)
        storage, artifacts = harness.storage()
        try:
            with pytest.raises(LeaseHeld) as info:
                await execute_run(
                    run_id, storage=storage, artifacts=artifacts, controller=RunController()
                )
            refused["message"] = str(info.value)
        finally:
            storage.db.close()

    outcome = h.execute(run_id, during=during)
    assert outcome.state is RunState.COMPLETED
    assert h.count("a") == 1 and h.count("b") == 1  # the live session's work ran once
    assert "being run by another session" in refused["message"]


def test_a_dead_sessions_lease_is_taken_over_and_its_time_recorded(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    storage, _ = h.storage()
    try:
        now = time.time()
        storage.acquire_run_lease(
            run_id,
            owner="dead-session",
            host=socket.gethostname(),
            pid=2**22 + 7,  # most likely not a running process; the heartbeat is stale anyway
            now=now - 120,
            is_stale=lambda lease: True,
        )
        storage.heartbeat_run_lease(run_id, "dead-session", now - 90)
    finally:
        storage.db.close()
    assert h.execute(run_id).state is RunState.COMPLETED
    [lost] = [e for e in _events(h, run_id) if e["event_type"] == "run_session_lost"]
    assert lost["payload"]["session_elapsed_seconds"] == pytest.approx(30, abs=0.5)


# --------------------------------------------------------------------------- budgets on resume


def test_a_call_dispatched_before_a_crash_counts_against_the_hard_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi", "b": "hi"}),
            application=h.cli_app(),
            budgets={"max_application_calls": 2},
        )
    )

    def crash_before_commit(self: Storage, result: Any) -> Any:
        raise SimulatedCrash("before commit")

    monkeypatch.setattr(Storage, "commit_execution_attempt", crash_before_commit)
    with pytest.raises(SimulatedCrash):
        h.execute(run_id)
    monkeypatch.undo()
    outcome = h.execute(run_id)
    assert h.count() == 2  # never more than the hard limit
    assert outcome.state is RunState.BUDGET_EXHAUSTED
    assert "blocked" in outcome.counts["execution"]


def test_replayed_prior_spend_carries_tokens_known_cost_and_session_time() -> None:
    from aibench.services.runs import _replay_prior_spend

    class Recorded:
        def list_execution_attempts(self, run_id: str) -> list[Any]:
            return []

        def list_evaluation_attempts(self, run_id: str) -> list[Any]:
            resources = {"latency_ms": 1, "cost": 0.5, "tokens": {"input": 120}}
            return [SimpleNamespace(resources=resources)]

        def list_run_events(self, run_id: str) -> list[dict[str, Any]]:
            return [
                {"event_type": "run_session_ended", "payload": {"session_elapsed_seconds": 1.5}},
                {"event_type": "run_session_aborted", "payload": {"session_elapsed_seconds": 2}},
                {"event_type": "recovered", "payload": {"uncommitted_dispatches": 1}},
            ]

    ledger = BudgetLedger(BudgetLimits(max_judge_tokens=100, max_application_calls=1))
    _replay_prior_spend(Recorded(), "r", ledger)  # type: ignore[arg-type]
    assert ledger.reserve_evaluation() == "max_judge_tokens=100 reached"
    assert ledger.reserve_application() == "max_application_calls=1 reached"
    assert ledger.evaluator.known_cost == 0.5
    assert ledger.elapsed_before == 3.5  # per-session times, not cumulative totals re-summed


def test_session_time_is_not_double_counted_across_sessions(tmp_path: Path) -> None:
    from aibench.services.runs import _replay_prior_spend

    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(dataset=h.dataset({f"c{i}": "slow 0.5" for i in range(3)}), application=h.cli_app())
    )

    def interrupt_after(n: int) -> Any:
        async def during(ctl: RunController, harness: Harness) -> None:
            await harness.wait_for_invocations(n)
            ctl.interrupt()

        return during

    h.execute(run_id, during=interrupt_after(1))
    h.execute(run_id, during=interrupt_after(2))
    ends = [
        e["payload"]["budget"]["elapsed_seconds"]
        for e in _events(h, run_id)
        if e["event_type"] == "run_session_ended"
    ]
    storage, _ = h.storage()
    try:
        ledger = BudgetLedger(BudgetLimits())
        _replay_prior_spend(storage, run_id, ledger)
    finally:
        storage.db.close()
    assert ledger.elapsed_before == pytest.approx(ends[-1], abs=0.3)


# --------------------------------------------------------------------------- recovery


def test_recovery_never_retries_past_max_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "flaky 1"}),
            application=h.cli_app(timeout=1.0),
            retry={"max_attempts": 1},
        )
    )
    original = Storage.commit_execution_attempt

    def crash_after_commit(self: Storage, result: Any) -> Any:
        original(self, result)
        raise SimulatedCrash("after commit")

    monkeypatch.setattr(Storage, "commit_execution_attempt", crash_after_commit)
    with pytest.raises(SimulatedCrash):
        h.execute(run_id)
    monkeypatch.undo()
    h.execute(run_id)
    storage, _ = h.storage()
    try:
        attempts = [a.attempt_id for a in storage.list_execution_attempts(run_id)]
        item = storage.get_work_item_by_task_key(run_id, "exec:a:r0")
    finally:
        storage.db.close()
    assert attempts == [1]
    assert item is not None and item.state is WorkItemState.FAILED
    assert "retries exhausted after 1 attempts" in (item.last_error or "")


def test_editing_an_app_config_never_breaks_or_changes_existing_runs(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app(timeout=20)))
    second = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app(timeout=30)))
    storage, _ = h.storage()
    try:
        hashes = {r: storage.get_run(r).manifest.application_hash for r in (first, second)}  # type: ignore[union-attr]
    finally:
        storage.db.close()
    assert hashes[first] != hashes[second]
    # Each run resumes under its own frozen spec.
    assert h.execute(first).state is RunState.COMPLETED
    assert h.execute(second).state is RunState.COMPLETED


# --------------------------------------------------------------------------- run control


def test_cancel_during_retry_backoff_leaves_nothing_pending(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "flaky 1"}),
            application=h.cli_app(timeout=0.5),
            retry={
                "max_attempts": 3,
                "initial_backoff_seconds": 30,
                "max_backoff_seconds": 30,
                "jitter": 0,
            },
        )
    )

    async def during(ctl: RunController, harness: Harness) -> None:
        storage, _ = harness.storage()
        try:
            while not any(
                e["event_type"] == "retry_scheduled" for e in storage.list_run_events(run_id)
            ):
                await asyncio.sleep(0.05)
        finally:
            storage.db.close()
        ctl.cancel()

    started = time.monotonic()
    outcome = h.execute(run_id, during=during)
    assert time.monotonic() - started < 15  # did not sit out the 30 s backoff
    assert outcome.state is RunState.CANCELLED
    assert "pending" not in _states(h, run_id).values()
    assert outcome.counts["execution"] == {"cancelled": 1}


def test_a_second_interrupt_aborts_in_flight_work_but_keeps_the_run_resumable(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(dataset=h.dataset({"a": "slow 5", "b": "hi", "c": "hi"}), application=h.cli_app())
    )

    async def during(ctl: RunController, harness: Harness) -> None:
        attempts = harness.log.with_suffix(".attempts")
        while not attempts.exists():
            await asyncio.sleep(0.02)
        ctl.interrupt()
        ctl.interrupt()

    started = time.monotonic()
    first = h.execute(run_id, during=during)
    assert time.monotonic() - started < 4.5  # the 5 s call was aborted, not awaited
    assert first.state is RunState.INTERRUPTED
    assert first.counts["execution"] == {"pending": 3}  # aborted + unstarted: resumable
    second = h.execute(run_id)
    assert second.state is RunState.COMPLETED


def test_interrupt_requested_during_setup_is_honoured_before_any_dispatch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    ctl = RunController()
    ctl.request("interrupt")  # e.g. Ctrl+C while identities are verified
    outcome = h.execute(run_id, controller=ctl)
    assert outcome.state is RunState.INTERRUPTED
    assert h.count() == 0


def test_interrupt_returns_transient_evaluation_failures_to_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services.scoring import BindingScorer

    h = Harness(tmp_path)
    run_id = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    original = BindingScorer.score
    controllers: list[RunController] = []

    async def transient_while_interrupted(self: BindingScorer, execution: Any, cases: Any) -> Any:
        result = await original(self, execution, cases)
        controllers[0].interrupt()
        return result.model_copy(
            update={"status": ExecutionStatus.ERROR, "reason": "worker_failed:SIGINT"}
        )

    monkeypatch.setattr(BindingScorer, "score", transient_while_interrupted)
    ctl = RunController()
    controllers.append(ctl)
    outcome = h.execute(run_id, controller=ctl)
    monkeypatch.undo()
    assert outcome.state is RunState.INTERRUPTED
    assert outcome.counts["evaluation"] == {"pending": 1}  # not finalized as failed
    assert h.execute(run_id).counts["evaluation"] == {"succeeded": 1}


def test_evaluation_concurrency_cap_bounds_in_flight_evaluations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.services.scoring import BindingScorer

    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({f"c{i}": "hi" for i in range(6)}),
            application=h.cli_app(),
            concurrency={"application": 4, "evaluation": 2},
        )
    )
    original = BindingScorer.score
    live = {"now": 0, "max": 0}

    async def slow_score(self: BindingScorer, execution: Any, cases: Any) -> Any:
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        try:
            await asyncio.sleep(0.15)
            return await original(self, execution, cases)
        finally:
            live["now"] -= 1

    monkeypatch.setattr(BindingScorer, "score", slow_score)
    assert h.execute(run_id).state is RunState.COMPLETED
    assert live["max"] == 2


# --------------------------------------------------------------------------- policy


def test_plugin_import_paths_need_policy_approval_and_nothing_loads_when_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.registry import EvaluatorRegistry

    loads: list[Any] = []
    monkeypatch.setattr(
        EvaluatorRegistry, "load_plugin_environment", lambda self, *a, **k: loads.append(a)
    )
    python = sys.executable  # a real interpreter the policy allows
    policy = ExecutionPolicy(allowed_plugin_environments=(python,))
    plan = ExecutablePlan(
        plan_id="p",
        dataset="d",
        application="a",
        plugin_environments=(PluginEnvironmentRef(python=python, paths=("elsewhere/evil",)),),
    )
    assert plan_denials(policy, plan, tmp_path) == [
        "plugin path elsewhere/evil is not allowed by the policy"
    ]
    allowed = policy.model_copy(
        update={"allowed_plugin_paths": (str(tmp_path / "elsewhere/evil"),)}
    )
    assert plan_denials(allowed, plan, tmp_path) == []

    h = Harness(tmp_path)
    plan_path = h.plan(
        dataset=h.dataset({"a": "hi"}),
        application=h.cli_app(),
        plugin_environments=[{"python": python, "paths": ["elsewhere/evil"]}],
    )
    with pytest.raises(PolicyDenied, match="plugin path"):
        compile_plan(plan_path, policy=policy, trusted_local=True)
    assert loads == []  # the allowed interpreter never started


def test_data_roots_scope_the_plans_data(tmp_path: Path) -> None:
    from aibench.engine.compile import load_policy

    h = Harness(tmp_path / "project")
    plan = h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app())
    policy_file = tmp_path / "policy.json"
    policy_file.write_text(json.dumps({"data_roots": ["somewhere-else"]}), encoding="utf-8")
    with pytest.raises(PolicyDenied) as info:
        compile_plan(plan, policy=load_policy(policy_file), trusted_local=True)
    assert sorted(info.value.denials) == [
        "application config app.json is outside the policy's data_roots",
        "dataset data.jsonl is outside the policy's data_roots",
    ]
    policy_file.write_text(json.dumps({"data_roots": ["project"]}), encoding="utf-8")
    compile_plan(plan, policy=load_policy(policy_file), trusted_local=True)  # relative to file
