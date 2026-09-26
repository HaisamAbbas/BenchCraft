"""DeepEval's agent-trace metrics (step efficiency, plan quality, plan adherence, agent loop
detection) against the REAL pinned DeepEval, fed the span tree of each execution's imported
OpenTelemetry trace. Skipped when the plugin environment is not installed."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus
from aibench.planning.catalog import CONCEPTS
from aibench.registry import EvaluatorRegistry
from tests.deepeval_support import JUDGES, PLUGIN_ENV, plugin_python, requires_plugin_env

pytestmark = requires_plugin_env

AGREEING = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
AGENTIC = ("step_efficiency", "plan_quality", "plan_adherence", "agent_loop_detection")

_SETUP = """
import asyncio, json, os
os.environ.update(DEEPEVAL_TELEMETRY_OPT_OUT="1", DEEPEVAL_DISABLE_DOTENV="1")
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval import EVALUATORS
from aibench_deepeval.metrics import deepeval_trace
BY_ID = {cls.manifest.evaluator_id: cls for cls in EVALUATORS}
CASE = BenchmarkCase(case_id="c", input="Where is order A17?")
EXECUTION = ExecutionResult(execution_id="e", run_id="r", case_id="c", status="ok",
    output="It ships tomorrow.")
def tree(calls):
    return {"format": "aibench-span-tree/1", "spans": [{
        "name": "agent.run", "kind": "agent", "input": "Where is order A17?",
        "output": "It ships tomorrow.", "children": [
            {"name": "chat", "kind": "llm", "model": "stub-1", "children": []},
            *[{"name": "lookup", "kind": "tool", "input": {"order": "A17"},
               "output": "ships tomorrow", "children": []} for _ in range(calls)]]}]}
async def outcome(metric_id, params, trace):
    evaluator = BY_ID[metric_id]()
    await evaluator.prepare(params)
    view = EvaluationView(case=CASE, execution=EXECUTION, trace=trace)
    return await evaluator.evaluate(view, EvaluatorContext(run_id="r", scoring_id="s"))
"""


def _params(name: str) -> dict[str, Any]:
    return {} if name == "agent_loop_detection" else {"judge": AGREEING}


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV, extra_paths=[JUDGES])
    assert [load.error for load in loads] == [None]
    return registry


def test_every_agent_metric_is_discovered_and_needs_the_trace(registry: EvaluatorRegistry) -> None:
    for name in AGENTIC:
        manifest, _ = registry.resolve(f"deepeval.{name}@1")
        assert "execution.trace" in {r.path for r in manifest.requires}, name
        assert manifest.concepts and set(manifest.concepts) <= set(CONCEPTS), name
        assert manifest.uses_models is (name != "agent_loop_detection"), name
    assert "deepeval" not in sys.modules


def test_the_span_tree_becomes_deepevals_trace() -> None:
    result = plugin_python(
        _SETUP
        + """
several = {"format": "aibench-span-tree/1", "spans": [
    {"name": "a", "kind": "other", "children": []},
    {"name": "b", "kind": "retriever", "error": True, "children": []}]}
print(json.dumps([deepeval_trace(tree(1)), deepeval_trace(several)]))
"""
    )
    one, several = result
    assert (one["name"], one["type"]) == ("agent.run", "agent")
    assert [(c["name"], c["type"]) for c in one["children"]] == [
        ("chat", "llm"),
        ("lookup", "tool"),
    ]
    assert one["children"][0]["model"] == "stub-1" and one["children"][1]["input"] == {
        "order": "A17"
    }
    assert several["type"] == "base" and [c["type"] for c in several["children"]] == [
        "base",
        "retriever",
    ]
    assert several["children"][1]["error"]


def test_agent_metrics_score_through_the_real_package_and_loops_are_detected() -> None:
    result = plugin_python(
        _SETUP
        + f"""
async def main():
    report = {{}}
    for name in {list(AGENTIC)!r}:
        params = {{}} if name == "agent_loop_detection" else {{"judge": {AGREEING!r}}}
        for calls in (1, 3):
            result = await outcome("deepeval." + name, params, tree(calls))
            report[f"{{name}}:{{calls}}"] = [result.status.value,
                result.value.value if result.value else result.reason]
    return report
print(json.dumps(asyncio.run(main())))
"""
    )
    for key, (status, value) in result.items():
        assert status == "ok", (key, value)
        assert 0.0 <= value <= 1.0, key
    # Deterministic, no judge: one lookup is fine; the same lookup three times is a loop.
    assert result["agent_loop_detection:1"][1] == 1.0
    assert result["agent_loop_detection:3"][1] < 1.0


def test_an_empty_trace_is_not_applicable() -> None:
    result = plugin_python(
        _SETUP
        + """
result = asyncio.run(outcome("deepeval.agent_loop_detection", {},
    {"format": "aibench-span-tree/1", "spans": []}))
print(json.dumps([result.status.value, result.reason]))
"""
    )
    assert result == ["not_applicable", "empty:execution.trace"]


def test_run_import_traces_then_score_agent_metrics_end_to_end(tmp_path: Path) -> None:
    """The product path: `aibench run` against an app that exports OpenTelemetry spans,
    `aibench traces import`, then `aibench score` with DeepEval agent metrics in the real
    worker. Complete traces are scored; the unsampled and rootless ones are not applicable."""
    import threading

    from typer.testing import CliRunner

    from aibench.cli.main import app
    from tests.runner_support import load_example

    trace_file = tmp_path / "traces.jsonl"
    server = load_example("traced_app").make_server(trace_file, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        (tmp_path / "app.json").write_text(
            json.dumps(
                {
                    "application_id": "traced-app",
                    "runner": "http",
                    "target": f"{base}/answer",
                    "transport": {"kind": "http", "url": f"{base}/answer"},
                    "input_binding": {"fields": {"/input": "/input"}},
                    "output_binding": {"output": "/answer"},
                }
            ),
            encoding="utf-8",
        )
        questions = ["refund policy", "shipping", "sampled out please", "lost root please"]
        (tmp_path / "data.jsonl").write_text(
            "".join(
                json.dumps({"case_id": f"c{i}", "input": q}) + "\n" for i, q in enumerate(questions)
            ),
            encoding="utf-8",
        )
        (tmp_path / "plan.json").write_text(
            json.dumps(
                {
                    "plan_id": "traced",
                    "dataset": "data.jsonl",
                    "application": "app.json",
                    "metrics": [
                        {
                            "metric": "native.json_schema",
                            "params": {"schema": {"type": "string"}, "parse_text": False},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        cli = CliRunner()
        ws = ["--workspace", str(tmp_path)]
        run = cli.invoke(app, ["run", "--plan", str(tmp_path / "plan.json"), "--json", *ws])
        assert run.exit_code == 0, run.output
        run_id = json.loads(run.stdout)["run_id"]
        imported = cli.invoke(app, ["traces", "import", run_id, str(trace_file), "--json", *ws])
        assert imported.exit_code == 0, imported.output
        (tmp_path / "metrics.json").write_text(
            json.dumps(
                {
                    "metrics": [
                        {"metric": "deepeval.agent_loop_detection"},
                        {"metric": "deepeval.step_efficiency", "params": {"judge": AGREEING}},
                    ]
                }
            ),
            encoding="utf-8",
        )
        scored = cli.invoke(
            app,
            [
                "score",
                run_id,
                "--metrics",
                str(tmp_path / "metrics.json"),
                "--plugin-env",
                str(PLUGIN_ENV),
                "--plugin-path",
                str(JUDGES),
                "--json",
                *ws,
            ],
        )
        assert scored.exit_code == 0, scored.output
        from aibench.storage.db import Database, Workspace
        from aibench.storage.repositories import Storage

        storage = Storage(Database.open_workspace(Workspace.at(tmp_path)))
        try:
            results = {
                (r.metric_id, r.case_id): r
                for r in storage.list_metric_results(run_id)
                if r.metric_id.startswith("deepeval.")
            }
        finally:
            storage.db.close()
        for metric in ("deepeval.agent_loop_detection", "deepeval.step_efficiency"):
            for case_id in ("c0", "c1"):
                item = results[(metric, case_id)]
                assert item.status is ExecutionStatus.OK, (metric, case_id, item.reason)
                assert 0.0 <= item.value.value <= 1.0
            for case_id in ("c2", "c3"):  # unsampled, and missing its root span
                item = results[(metric, case_id)]
                assert item.status is ExecutionStatus.NOT_APPLICABLE, item.reason
                assert (item.reason or "").startswith("trace_partial:"), item.reason
        # The fixture calls each tool once: no loop.
        assert results[("deepeval.agent_loop_detection", "c0")].value.value == 1.0
    finally:
        server.shutdown()
        server.server_close()
