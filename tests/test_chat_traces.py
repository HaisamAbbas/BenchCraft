"""`/traces` and `/rescore` in the chat: attach an OpenTelemetry export to the session's
run, then score the draft's trace metrics on it, without leaving the conversation and
without calling the application again."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from aibench.core.plans import PluginEnvironmentRef
from aibench.core.sessions import PlanPatch
from aibench.tui.commands import Commands
from tests.deepeval_support import PLUGIN_ENV, requires_plugin_env
from tests.runner_support import load_example
from tests.session_support import SessionHarness

LOOPS = "the agent does not loop"


def _export(correlations: dict[str, str], path: Path) -> None:
    """One OTLP/JSON document per execution, spans built as the traced fixture app builds
    them (root, agent, two model calls, one tool), keyed by the execution's correlation ID."""
    spans_for = load_example("traced_app").spans_for
    lines = [
        json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans_for(q, cid)}]}]})
        for q, cid in correlations.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _session(tmp_path: Path, **policy: object):
    h = SessionHarness(tmp_path)
    ctl = h.open_session(
        {"a": "answer a", "b": "answer b"},
        objectives=("catch wrong answers",),
        policy={"data_roots": [str(tmp_path)], **policy},
    )

    async def run() -> str:
        started = await ctl.start_run(action_id="run-1", expected_revision=1)
        done = await ctl.wait_for_run(started.run_id)
        assert done is not None and done.state.value == "completed"
        return started.run_id

    run_id = asyncio.run(run())
    attempts = ctl.storage.list_execution_attempts(run_id)
    correlations = {f"question {a.case_id}": a.correlation_id for a in attempts}
    assert all(correlations.values())
    return h, ctl, run_id, correlations


def test_traces_import_rejects_paths_outside_the_project_and_data_roots(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _, ctl, _, correlations = _session(project)
    outside = tmp_path / "elsewhere.jsonl"
    _export(correlations, outside)
    try:
        commands = Commands(ctl)
        refused = asyncio.run(commands.run(f"/traces import {outside}"))
        assert not refused.ok and "outside the project" in refused.data["error"]
        missing = asyncio.run(commands.run("/traces import nothing-here.jsonl"))
        assert not missing.ok and "no trace file" in missing.data["error"]
        usage = asyncio.run(commands.run("/traces import"))
        assert not usage.ok and "usage" in usage.data["error"]
    finally:
        ctl.storage.db.close()


def test_traces_import_attaches_the_export_and_traces_shows_it(tmp_path: Path) -> None:
    h, ctl, run_id, correlations = _session(tmp_path)
    try:
        commands = Commands(ctl)
        before = asyncio.run(commands.run("/traces"))
        assert before.kind == "traces" and before.data["available"] is False
        calls = h.count()
        _export(correlations, tmp_path / "traces.jsonl")
        imported = asyncio.run(commands.run("/traces import traces.jsonl"))
        assert imported.ok, imported.data
        assert imported.data["run_id"] == run_id
        assert (imported.data["traces"], imported.data["matched"]) == (2, 2)
        again = asyncio.run(commands.run("/traces import traces.jsonl"))
        assert again.ok and again.data["added"] == 0  # the same file adds nothing
        shown = asyncio.run(commands.run(f"/traces {run_id}"))
        summary = shown.data["trace_summary"]
        assert summary["matched_to_executions"] == 2 and summary["tool_spans"] == 2
        assert h.count() == calls  # the application was not called again
    finally:
        ctl.storage.db.close()


@requires_plugin_env
def test_run_then_import_then_rescore_scores_agent_loops_in_the_chat(tmp_path: Path) -> None:
    """The whole flow in one session: a run, `/traces import`, then `/rescore` scores
    DeepEval loop detection (no judge) from the imported traces in the real worker."""
    h, ctl, run_id, correlations = _session(
        tmp_path,
        allowed_evaluators=["native.*", "deepeval.*"],
        allowed_plugin_environments=[str(PLUGIN_ENV)],
    )
    try:
        loaded = ctl.use_plugin_environments((PluginEnvironmentRef(python=str(PLUGIN_ENV)),), {})
        assert loaded.status == "applied", loaded.problems
        patched = ctl.apply_patch(
            PlanPatch(add_objectives=(LOOPS,), objective_concepts={LOOPS: ("agent_loops",)}),
            expected_revision=ctl.session.revision,
            source="user",
        )
        assert patched.status == "applied", patched.problems
        metrics = {m["metric"].split("@")[0] for m in ctl.state()["draft"]["metrics"]}
        assert "deepeval.agent_loop_detection" in metrics, metrics

        commands = Commands(ctl)
        calls = h.count()
        without = asyncio.run(commands.run("/rescore"))
        assert without.ok, without.data
        by_metric = {s["metric_id"]: s for s in without.data["summaries"]}
        loops = by_metric["deepeval.agent_loop_detection"]
        assert loops["not_applicable"] == 2 and loops["completed"] == 0  # no traces yet

        _export(correlations, tmp_path / "traces.jsonl")
        assert asyncio.run(commands.run("/traces import traces.jsonl")).ok
        rescored = asyncio.run(commands.run("/rescore"))
        assert rescored.ok, rescored.data
        assert rescored.data["run_id"] == run_id and rescored.data["application_invoked"] is False
        loops = {s["metric_id"]: s for s in rescored.data["summaries"]}[
            "deepeval.agent_loop_detection"
        ]
        assert loops["completed"] == 2, loops
        assert loops["value_summary"]["mean"] == pytest.approx(1.0)  # one call per tool
        assert h.count() == calls
    finally:
        ctl.storage.db.close()
