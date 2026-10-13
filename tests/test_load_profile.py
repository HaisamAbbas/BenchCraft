"""Arrival schedules stay deterministic while engine dispatch remains bounded."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path

import pytest

from aibench.core.plans import ExecutablePlan, OpenLoopLoadProfile, RampLoadProfile
from aibench.engine.compile import freeze_plan
from aibench.engine.engine import RunController, RunEngine, RunState, _Item
from aibench.engine.load_profile import arrival_offset_seconds
from aibench.reporting.render import render
from aibench.services.reports import build_report
from tests.engine_support import Harness


def _profile_plan(**fields: object) -> ExecutablePlan:
    return ExecutablePlan.model_validate(
        {
            "plan_id": "load-profile",
            "dataset": "cases.jsonl",
            "application": "app.json",
            "metrics": [{"metric": "native.exact_match"}],
            **fields,
        }
    )


def test_open_loop_offsets_are_even_and_closed_loop_has_no_schedule() -> None:
    profile = OpenLoopLoadProfile(requests_per_second=4.0)
    assert [arrival_offset_seconds(profile, index) for index in range(5)] == [
        0.0,
        0.25,
        0.5,
        0.75,
        1.0,
    ]
    assert arrival_offset_seconds(_profile_plan().load_profile, 0) is None


def test_ramp_offsets_invert_integrated_rate_and_hold_target_rate() -> None:
    profile = RampLoadProfile(
        start_requests_per_second=1.0,
        end_requests_per_second=3.0,
        ramp_duration_seconds=2.0,
    )
    offsets = [arrival_offset_seconds(profile, index) for index in range(7)]
    assert offsets == pytest.approx(
        [
            0.0,
            2 / (1 + math.sqrt(3)),
            4 / (1 + math.sqrt(5)),
            6 / (1 + math.sqrt(7)),
            2,
            7 / 3,
            8 / 3,
        ]
    )
    assert offsets == sorted(offsets)

    with pytest.raises(ValueError, match="must exceed"):
        RampLoadProfile(
            start_requests_per_second=3.0,
            end_requests_per_second=3.0,
            ramp_duration_seconds=2.0,
        )


def test_retries_do_not_emit_another_offered_arrival() -> None:
    engine = object.__new__(RunEngine)
    engine._profile_started_at = 1.0
    engine._profile_pause_started_at = None
    engine._profile_paused_seconds = 0.0
    retry = _Item(
        "exec:case:r0",
        "execution",
        "case",
        0,
        2,
        arrival_ordinal=0,
        arrival_offset_seconds=0.0,
    )
    assert engine._arrival_dispatch_details(retry) is None


def test_open_loop_profile_cannot_use_execution_cache_and_changes_frozen_hash() -> None:
    closed = _profile_plan()
    frozen_closed, hash_closed = freeze_plan(closed)
    legacy = closed.model_dump(mode="json")
    legacy.pop("warmup_repetitions")
    legacy.pop("load_profile")
    assert frozen_closed == json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()

    open_loop = _profile_plan(
        load_profile={"mode": "open_loop", "requests_per_second": 8.0}
    )
    assert freeze_plan(open_loop)[1] != hash_closed
    assert b'"mode":"open_loop"' in freeze_plan(open_loop)[0]

    with pytest.raises(ValueError, match="cannot use the execution cache"):
        _profile_plan(
            load_profile={"mode": "open_loop", "requests_per_second": 8.0},
            cache={"executions": True},
        )


def test_open_loop_records_first_attempt_dispatch_lag_and_warmup_barrier(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plan = harness.plan(
        dataset=harness.dataset({"one": "slow 0.12", "two": "slow 0.12", "three": "hi"}),
        application=harness.cli_app(),
        warmup_repetitions=1,
        concurrency={"application": 1},
        load_profile={"mode": "open_loop", "requests_per_second": 20.0},
    )
    run_id = harness.create(plan)
    outcome = harness.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    storage, artifacts = harness.storage()
    try:
        events = storage.list_run_events(run_id)
        arrivals = [
            event["payload"]
            for event in events
            if event["event_type"] == "load_profile_dispatch"
        ]
        report = build_report(storage, artifacts, run_id)
    finally:
        storage.db.close()

    assert [event["arrival_ordinal"] for event in arrivals] == [0, 1, 2]
    assert [event["scheduled_arrival_offset_seconds"] for event in arrivals] == [0, 0.05, 0.1]
    assert all(event["dispatch_lag_seconds"] >= 0 for event in arrivals)
    assert arrivals[-1]["dispatch_lag_seconds"] > arrivals[0]["dispatch_lag_seconds"] + 0.04
    offered = report["application"]["latency"]["offered_load"]
    assert offered["mode"] == "open_loop"
    assert offered["planned_first_attempts"] == 3
    assert offered["observed_dispatches"] == 3
    assert offered["dispatch_lag_ms"]["p95_ms"] is not None
    assert "Offered load" in render(report, "markdown")
    warmup_running = [
        event["payload"]
        for event in events
        if event["event_type"] == "item_state"
        and event["payload"].get("state") == "running"
        and ":r1" in event["payload"].get("task_key", "")
    ]
    assert len(warmup_running) == 3
    assert len(arrivals) == 3  # warmups did not consume any measured arrival slots


def test_pause_freezes_profile_clock_between_first_attempts(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    plan = harness.plan(
        dataset=harness.dataset({"one": "slow 0.05", "two": "hi"}),
        application=harness.cli_app(),
        concurrency={"application": 1},
        load_profile={"mode": "open_loop", "requests_per_second": 2.0},
    )

    async def pause_after_first(ctl: RunController, h: Harness) -> None:
        await h.wait_for_attempts(1)
        ctl.pause()
        while ctl.state is not RunState.PAUSED:
            await asyncio.sleep(0.01)
        await asyncio.sleep(1.0)
        ctl.resume()

    run_id = harness.create(plan)
    outcome = harness.execute(run_id, during=pause_after_first)
    assert outcome.state is RunState.COMPLETED
    storage, _ = harness.storage()
    try:
        arrivals = [
            event["payload"]
            for event in storage.list_run_events(run_id)
            if event["event_type"] == "load_profile_dispatch"
        ]
    finally:
        storage.db.close()
    assert [event["arrival_ordinal"] for event in arrivals] == [0, 1]
    active_interval = (
        arrivals[1]["dispatch_active_seconds"] - arrivals[0]["dispatch_active_seconds"]
    )
    assert 0.3 < active_interval < 1.2
