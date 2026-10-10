"""Fault injection at dispatch, response and commit boundaries, then resume (06-G3).

`SimulatedCrash` is a BaseException, so nothing in the engine can catch it: it behaves like
the process dying at that exact point. Resume then runs on a fresh connection, as a new
process would. Invocation counts come from the application's own log."""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus, WorkItemState
from aibench.engine.engine import RunState
from aibench.security.policy import ExecutionPolicy
from aibench.services.reports import build_report
from aibench.storage.repositories import Storage
from tests.engine_support import Harness


class SimulatedCrash(BaseException):
    pass


def _crash_once(
    monkeypatch: pytest.MonkeyPatch, target: Any, name: str, when: Any
) -> dict[str, int]:
    """Make `target.name` raise SimulatedCrash the first time `when(*args, **kwargs)` holds."""
    original = getattr(target, name)
    fired = {"count": 0}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        if not fired["count"] and when(*args, **kwargs):
            fired["count"] += 1
            raise SimulatedCrash(name)
        return original(*args, **kwargs)

    monkeypatch.setattr(target, name, wrapper)
    return fired


def _no_duplicate_commits(h: Harness, run_id: str) -> None:
    storage, _ = h.storage()
    try:
        ok = Counter(
            (a.case_id, a.repetition_id)
            for a in storage.list_execution_attempts(run_id)
            if a.status is ExecutionStatus.OK
        )
        finals = storage.list_metric_results(run_id, scoring_id=f"engine-{run_id}")
        items = storage.list_work_items(run_id)
        states = Counter(w.state for w in items)
    finally:
        storage.db.close()
    assert all(n == 1 for n in ok.values()), ok  # one successful execution per item
    # Exactly one final result per evaluation item. (A second commit for the same item
    # would raise ConflictError, since the result key is deterministic; this checks none is
    # missing and none is extra.)
    evaluation_items = [w for w in items if w.kind == "evaluation"]
    assert len(finals) == len(evaluation_items)
    assert len({(r.case_id, r.repetition_id, r.binding_hash) for r in finals}) == len(finals)
    assert states.get(WorkItemState.RUNNING, 0) == 0 and states.get(WorkItemState.PENDING, 0) == 0


def _run_with_crash(h: Harness, run_id: str) -> None:
    with pytest.raises(SimulatedCrash):
        h.execute(run_id)
    storage, _ = h.storage()
    try:
        assert storage.get_run(run_id).status == "running"  # died mid-run
    finally:
        storage.db.close()


def test_crash_at_dispatch_boundary_redispatches_effect_free_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marked running, crashed before the application was invoked."""
    import aibench.engine.engine as engine_module

    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi", "b": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    original = engine_module.invoke_and_record
    calls = {"n": 0}

    async def crash_first(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SimulatedCrash("dispatch")
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine_module, "invoke_and_record", crash_first)
    _run_with_crash(h, run_id)
    monkeypatch.undo()
    assert h.count() <= 1  # the crashed dispatch never reached the application

    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    assert h.count("a") == 1 and h.count("b") == 1
    _no_duplicate_commits(h, run_id)


def test_case_ids_with_colons_survive_run_and_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Task-key delimiters must not truncate valid case IDs during resume."""
    import aibench.engine.engine as engine_module

    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"case:with:colons": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    original = engine_module.invoke_and_record
    calls = {"n": 0}

    async def crash_first(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SimulatedCrash("dispatch")
        return await original(*args, **kwargs)

    monkeypatch.setattr(engine_module, "invoke_and_record", crash_first)
    _run_with_crash(h, run_id)
    monkeypatch.undo()

    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    assert h.count("case:with:colons") == 1
    _no_duplicate_commits(h, run_id)


def test_crash_at_response_boundary_repeats_only_effect_free_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The application ran, but the process died before its result was committed."""
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    _crash_once(monkeypatch, Storage, "commit_execution_attempt", lambda self, result: True)
    _run_with_crash(h, run_id)
    monkeypatch.undo()
    assert h.count("a") == 1

    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    # The app declares no effects, so repeating is safe: at-least-once, and it is recorded.
    assert h.count("a") == 2
    storage, _ = h.storage()
    try:
        events = [e for e in storage.list_run_events(run_id) if e["event_type"] == "recovered"]
        attempts = storage.list_execution_attempts(run_id, "a")
    finally:
        storage.db.close()
    assert "re-dispatch (no declared effects)" in events[0]["payload"]["items"][0]
    assert [a.attempt_id for a in attempts] == [2]  # attempt 1 never committed; ids never reused
    _no_duplicate_commits(h, run_id)


def test_recovered_uncommitted_warmup_reports_unknown_cost_and_effect_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi"}),
        application=h.cli_app(effects="reversible", environment_digest="test-runtime-pin"),
        repetitions=1,
        warmup_repetitions=1,
    )
    run_id = h.create(plan, policy=ExecutionPolicy(max_effects="reversible"))
    _crash_once(monkeypatch, Storage, "commit_execution_attempt", lambda self, result: True)
    _run_with_crash(h, run_id)
    monkeypatch.undo()

    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    storage, artifacts = h.storage()
    try:
        report = build_report(storage, artifacts, run_id)
        warmup_item = next(
            item
            for item in storage.list_work_items(run_id)
            if item.kind == "execution" and item.warmup
        )
    finally:
        storage.db.close()

    assert warmup_item.state is WorkItemState.UNKNOWN_EFFECT
    assert report["application"]["warmup"]["dispatched_calls"] == 1
    assert report["application"]["warmup"]["uncommitted_dispatches"] == 1
    assert report["application"]["warmup"]["cost"]["calls_with_unknown_cost"] == 1
    assert report["application"]["warmup"]["work_item_states"] == {"unknown_effect": 1}


def test_crash_at_commit_boundary_settles_from_the_committed_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The result was committed, but the process died before the item moved on."""
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi", "b": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    _crash_once(
        monkeypatch,
        Storage,
        "transition_work_item",
        lambda self, run, key, **kw: (
            key.startswith("exec:") and kw["to_state"] is WorkItemState.SUCCEEDED
        ),
    )
    _run_with_crash(h, run_id)
    monkeypatch.undo()
    before = h.count()

    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    assert h.count() == 2  # the committed execution was not invoked again
    assert before >= 1
    _no_duplicate_commits(h, run_id)


def test_crash_at_evaluation_commit_boundary_does_not_duplicate_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
            metrics=[
                {"metric": "native.exact_match"},
                {"metric": "native.json_schema", "params": {"schema": {}}},
            ],
        )
    )
    _crash_once(
        monkeypatch,
        Storage,
        "transition_work_item",
        lambda self, run, key, **kw: (
            key.startswith("eval:") and kw["to_state"] is WorkItemState.SUCCEEDED
        ),
    )
    _run_with_crash(h, run_id)
    monkeypatch.undo()
    outcome = h.execute(run_id)
    assert outcome.state is RunState.COMPLETED
    assert h.count("a") == 1
    _no_duplicate_commits(h, run_id)


def test_ambiguous_effectful_crash_is_unknown_effect_and_never_auto_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An effectful app performed its effect, then the process died before the result was
    committed. Resume must not book it again."""
    from tests.runner_support import load_example, serving

    effect_app = load_example("effect_counter_app")
    server = effect_app.make_server(port=0)
    h = Harness(tmp_path)
    with serving(server) as base:
        (tmp_path / "effect.json").write_text(
            '{"application_id": "booking", "runner": "http", "target": "b", "revision": "fixture-v1", "effects": "irreversible",'
            f'"transport": {{"kind": "http", "url": "{base}/book"}},'
            '"input_binding": {"fields": {"/destination": "/input"}}}',
            encoding="utf-8",
        )
        run_id = h.create(
            h.plan(dataset=h.dataset({"trip": "Dubai"}), application="effect.json"),
            policy=ExecutionPolicy(max_effects="irreversible"),
        )
        _crash_once(monkeypatch, Storage, "commit_execution_attempt", lambda self, result: True)
        _run_with_crash(h, run_id)
        monkeypatch.undo()
        assert server.count == 1

        outcome = h.execute(run_id)
        time.sleep(0.2)
    assert server.count == 1 and len(server.received) == 1  # never booked twice
    assert outcome.counts["execution"] == {"unknown_effect": 1}
    storage, _ = h.storage()
    try:
        [item] = [w for w in storage.list_work_items(run_id) if w.kind == "execution"]
    finally:
        storage.db.close()
    assert "reconcile the application state" in (item.last_error or "")


def test_resume_verifies_frozen_identities(tmp_path: Path) -> None:
    from aibench.services.runs import RunError

    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    assert h.execute(run_id).state is RunState.COMPLETED
    with pytest.raises(RunError, match="only interrupted or unfinished runs resume"):
        h.execute(run_id)

    tampered = h.create(h.plan(dataset=h.dataset({"a": "hi"}), application=h.cli_app()))
    storage, _ = h.storage()
    try:
        record = storage.get_run(tampered)
        ref = storage.get_artifact(record.manifest.parameters["plan_artifact_id"])
    finally:
        storage.db.close()
    Path(ref.uri).write_bytes(b'{"plan_id": "evil"}')
    with pytest.raises(RunError, match="frozen plan"):
        h.execute(tampered)
    assert h.count() == 1  # the tampered run dispatched nothing


def test_a_crash_during_recovery_never_loses_the_uncommitted_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery settles in-flight items and records them in one transaction. A crash while
    recording must leave the items `running`, so the next recovery still counts the call
    that may have reached the application, in the ledger and the report (13-T1)."""
    from aibench.services.reports import build_report

    h = Harness(tmp_path)
    run_id = h.create(
        h.plan(
            dataset=h.dataset({"a": "hi"}),
            application=h.cli_app(environment_digest="test-runtime-pin"),
        )
    )
    _crash_once(monkeypatch, Storage, "commit_execution_attempt", lambda self, result: True)
    _run_with_crash(h, run_id)
    monkeypatch.undo()
    assert h.count("a") == 1  # the app ran; its result was never committed

    _crash_once(monkeypatch, Storage, "_insert_run_event", lambda self, run, kind, payload: True)
    with pytest.raises(SimulatedCrash):
        h.execute(run_id)  # dies while recording recovery
    monkeypatch.undo()
    storage, _ = h.storage()
    try:
        [item] = [w for w in storage.list_work_items(run_id) if w.kind == "execution"]
        assert item.state is WorkItemState.RUNNING  # nothing settled without its record
        assert not [e for e in storage.list_run_events(run_id) if e["event_type"] == "recovered"]
    finally:
        storage.db.close()

    assert h.execute(run_id).state is RunState.COMPLETED
    assert h.count("a") == 2
    storage, artifacts = h.storage()
    try:
        [event] = [e for e in storage.list_run_events(run_id) if e["event_type"] == "recovered"]
        report = build_report(storage, artifacts, run_id)
    finally:
        storage.db.close()
    assert event["payload"]["uncommitted_dispatches"] == 1
    assert report["application"]["uncommitted_dispatches"] == 1
    _no_duplicate_commits(h, run_id)
