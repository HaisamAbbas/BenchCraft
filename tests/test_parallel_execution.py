"""Provider-aware quotas, backpressure and bounded parallelism (16-T4, 16-G4), measured
against a controlled rate-limited HTTP server (`examples/apps/rate_limited_app.py`) that
records what it actually received. Terminal responsiveness is measured as event-loop lag:
the chat runs on the same loop as the engine."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aibench.engine.engine import RunController, RunState
from tests.engine_support import Harness


@pytest.fixture
def serve() -> Iterator[Any]:
    from tests.runner_support import load_example

    started = []

    def start(rate: float, **kwargs: Any) -> Any:
        server = load_example("rate_limited_app").make_server(rate, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return server

    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def _project(tmp_path: Path, server: Any, cases: int, **plan: Any) -> tuple[Harness, str]:
    h = Harness(tmp_path)
    (tmp_path / "app.json").write_text(
        json.dumps(
            {
                "application_id": "limited",
                "runner": "http",
                "target": "limited",
                "transport": {"kind": "http", "url": f"http://127.0.0.1:{server.server_port}/"},
                "input_binding": {"fields": {"/input": "/input"}},
            }
        ),
        encoding="utf-8",
    )
    dataset = h.dataset({f"c{i:03d}": f"q{i}" for i in range(cases)})
    return h, h.create(h.plan(dataset=dataset, application="app.json", **plan))


def _attempts(h: Harness, run_id: str) -> list[Any]:
    storage, _ = h.storage()
    try:
        return storage.list_execution_attempts(run_id)
    finally:
        storage.db.close()


class LoopProbe:
    """Samples event-loop lag and the number of asyncio tasks while a run executes."""

    def __init__(self) -> None:
        self.max_lag = 0.0
        self.max_tasks = 0

    async def __call__(self, ctl: RunController, harness: Any) -> None:
        while ctl.state not in (RunState.COMPLETED, RunState.CANCELLED, RunState.INTERRUPTED):
            before = time.perf_counter()
            await asyncio.sleep(0.01)
            self.max_lag = max(self.max_lag, time.perf_counter() - before - 0.01)
            self.max_tasks = max(self.max_tasks, len(asyncio.all_tasks()))
            if ctl.state is RunState.CREATED and time.perf_counter() - before > 60:
                return


def _max_in_window(times: list[float], window: float = 1.0) -> int:
    times = sorted(times)
    best, start = 0, 0
    for end, t in enumerate(times):
        while t - times[start] > window:
            start += 1
        best = max(best, end - start + 1)
    return best


def test_a_quota_bounds_rate_and_concurrency_without_unbounded_tasks(
    tmp_path: Path, serve: Any
) -> None:
    server = serve(rate=100.0, burst=100, delay=0.05)  # generous: the quota must do the work
    quota = {"name": "provider", "applies_to": "application", "max_in_flight": 3,
             "requests_per_second": 15, "burst": 2}  # fmt: skip
    h, run_id = _project(
        tmp_path, server, 45, concurrency={"application": 16, "evaluation": 4}, quotas=[quota]
    )
    probe = LoopProbe()
    started = time.perf_counter()
    outcome = h.execute(run_id, during=probe)
    elapsed = time.perf_counter() - started
    assert outcome.state is RunState.COMPLETED
    assert outcome.counts["execution"] == {"succeeded": 45}
    assert len(server.arrivals) == 45 and not server.rejections
    assert server.peak_active <= 3  # max_in_flight, although application concurrency is 16
    assert _max_in_window(server.arrivals) <= 15 + 2  # rate plus the burst
    assert elapsed >= (45 - 2) / 15 * 0.9  # the rate really held the run back
    # Throttled work waits in the queue, not as coroutines: tasks stay near the caps.
    assert probe.max_tasks <= 3 + 4 + 8
    assert probe.max_lag < 1.0, probe.max_lag  # the loop (and a chat on it) stays responsive


def test_a_providers_slow_down_pauses_all_work_under_its_quota(tmp_path: Path, serve: Any) -> None:
    server = serve(rate=8.0, burst=4, delay=0.0, retry_after=1.0)
    quota = {"name": "provider", "applies_to": "application", "max_in_flight": 8}
    h, run_id = _project(
        tmp_path,
        server,
        24,
        concurrency={"application": 8, "evaluation": 4},
        quotas=[quota],
        retry={"max_attempts": 6, "initial_backoff_seconds": 0.05, "max_backoff_seconds": 2},
    )
    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    assert outcome.counts["execution"] == {"succeeded": 24}  # retried after backing off
    assert server.rejections  # the provider did say "slow down"
    storage, _ = h.storage()
    try:
        events = storage.list_run_events(run_id)
    finally:
        storage.db.close()
    pauses = [e["payload"] for e in events if e["event_type"] == "backpressure"]
    assert pauses and {p["quota"] for p in pauses} == {"provider"}
    assert all(p["pause_seconds"] == 1.0 for p in pauses)  # the server's Retry-After
    ended = next(e for e in reversed(events) if e["event_type"] == "run_session_ended")
    [summary] = ended["payload"]["quotas"]
    assert summary["backpressure_events"] == len(pauses) and summary["max_in_flight_seen"] <= 8
    # Once the engine registered a pause, no attempt started until it ended. (Requests
    # already sent before the 429 arrived cannot be recalled.)
    from datetime import datetime

    starts = [
        datetime.fromisoformat(a.timing["started_at"]).timestamp() for a in _attempts(h, run_id)
    ]
    for event in (e for e in events if e["event_type"] == "backpressure"):
        paused_at = datetime.fromisoformat(event["created_at"]).timestamp()
        early = [t for t in starts if paused_at + 0.02 < t < paused_at + 0.95]
        assert not early, (paused_at, early)


def test_throttled_work_can_still_be_paused_and_cancelled_promptly(
    tmp_path: Path, serve: Any
) -> None:
    server = serve(rate=1000.0, burst=1000)
    quota = {"name": "slow", "applies_to": "application", "requests_per_second": 1.0}
    h, run_id = _project(tmp_path, server, 30, quotas=[quota])

    async def cancel_while_throttled(ctl: RunController, harness: Any) -> None:
        await asyncio.sleep(1.5)
        ctl.request("cancel")

    started = time.perf_counter()
    outcome = h.execute(run_id, during=cancel_while_throttled)
    assert outcome.state is RunState.CANCELLED
    assert time.perf_counter() - started < 5  # not the 30 s the quota would take
    assert len(server.arrivals) <= 3


def test_an_evaluator_quota_caps_evaluations_it_matches(tmp_path: Path, serve: Any) -> None:
    server = serve(rate=1000.0, burst=1000)
    quotas = [
        {"name": "judges", "applies_to": "evaluator:native.*", "max_in_flight": 1},
        {"name": "unrelated", "applies_to": "evaluator:deepeval.*", "max_in_flight": 1},
    ]
    h, run_id = _project(
        tmp_path, server, 12, concurrency={"application": 4, "evaluation": 4}, quotas=quotas
    )
    assert h.execute(run_id).state is RunState.COMPLETED
    storage, _ = h.storage()
    try:
        events = storage.list_run_events(run_id)
    finally:
        storage.db.close()
    ended = next(e for e in reversed(events) if e["event_type"] == "run_session_ended")
    summaries = {q["quota"]: q for q in ended["payload"]["quotas"]}
    assert summaries["judges"]["started"] == 12 and summaries["judges"]["max_in_flight_seen"] == 1
    assert summaries["unrelated"]["started"] == 0


def test_cancelling_under_a_throttling_evaluator_quota_never_stalls_the_loop(
    tmp_path: Path, serve: Any
) -> None:
    """Cancel while an evaluator quota holds work back: the queued evaluations are recorded
    as cancelled at once. Before the fix the loop spun without awaiting until the bucket
    refilled, freezing a chat on the same loop (review finding)."""
    server = serve(rate=1000.0, burst=1000)
    quota = {"name": "judge", "applies_to": "evaluator:native.*", "requests_per_second": 0.2}
    h, run_id = _project(tmp_path, server, 8, quotas=[quota])
    probe = LoopProbe()

    async def cancel_when_executed(ctl: RunController, harness: Any) -> None:
        while len(server.arrivals) < 8:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.5)
        ctl.request("cancel")
        await probe(ctl, harness)

    started = time.perf_counter()
    outcome = h.execute(run_id, during=cancel_when_executed)
    assert outcome.state is RunState.CANCELLED
    assert time.perf_counter() - started < 10  # not the 40 s the quota would take
    assert probe.max_lag < 1.0, probe.max_lag


def test_one_throttled_evaluator_does_not_hold_back_the_others(tmp_path: Path, serve: Any) -> None:
    server = serve(rate=1000.0, burst=1000)
    quota = {"name": "slow-judge", "applies_to": "evaluator:native.exact_match",
             "requests_per_second": 0.2}  # fmt: skip
    metrics = [
        {"metric": "native.exact_match"},
        {"metric": "native.json_schema",
         "params": {"schema": {"type": "string"}, "parse_text": False}},
    ]  # fmt: skip
    h, run_id = _project(tmp_path, server, 6, quotas=[quota], metrics=metrics)
    seen: dict[str, int] = {}

    async def watch_then_cancel(ctl: RunController, harness: Any) -> None:
        deadline = time.perf_counter() + 20
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.2)
            storage, _ = harness.storage()
            try:
                results = storage.list_metric_results(run_id)
            finally:
                storage.db.close()
            seen.clear()
            for r in results:
                seen[r.metric_id] = seen.get(r.metric_id, 0) + 1
            if seen.get("native.json_schema") == 6:
                break
        ctl.request("cancel")

    h.execute(run_id, during=watch_then_cancel)
    assert seen.get("native.json_schema") == 6  # finished while the other was throttled
    assert seen.get("native.exact_match", 0) < 6


def test_a_providers_retry_after_is_capped_by_the_quota(tmp_path: Path, serve: Any) -> None:
    server = serve(rate=4.0, burst=2, retry_after=99999.0)  # an absurd Retry-After
    quota = {"name": "provider", "applies_to": "application", "max_backpressure_seconds": 0.5}
    h, run_id = _project(
        tmp_path,
        server,
        8,
        concurrency={"application": 8, "evaluation": 4},
        quotas=[quota],
        retry={"max_attempts": 8, "initial_backoff_seconds": 0.05, "max_backoff_seconds": 0.5},
    )
    started = time.perf_counter()
    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    assert time.perf_counter() - started < 30
    storage, _ = h.storage()
    try:
        events = storage.list_run_events(run_id)
    finally:
        storage.db.close()
    pauses = [e["payload"]["pause_seconds"] for e in events if e["event_type"] == "backpressure"]
    assert pauses and set(pauses) == {0.5}
