"""DeepEval's upstream MCP evaluators use recorded tool events, not a live MCP server."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from aibench.core.models import ExecutionStatus
from aibench.planning.catalog import CONCEPTS
from aibench.registry import EvaluatorRegistry
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env
from tests.scoring_support import Seeded, case, execution

pytestmark = requires_plugin_env

JUDGE = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
SERVERS = [
    {
        "server_name": "orders",
        "transport": "stdio",
        "available_tools": [
            {
                "name": "lookup_order",
                "description": "Look up one order.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            }
        ],
    }
]
PARAMS = {"judge": JUDGE, "mcp_servers": SERVERS}
EVENT = {
    "type": "mcp_tool_call",
    "name": "lookup_order",
    "arguments": {"order_id": "A17"},
    "result": {"status": "refundable"},
    "status": "success",
}

_SETUP = """
import asyncio, json, os
os.environ.update(DEEPEVAL_TELEMETRY_OPT_OUT="1", DEEPEVAL_DISABLE_DOTENV="1")
from aibench.core.models import BenchmarkCase, ExecutionResult, ReferenceAnswer
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval import EVALUATORS
BY_ID = {cls.manifest.evaluator_id: cls for cls in EVALUATORS}
CASE = BenchmarkCase(case_id="t2", input="Can I refund A17?", group_id="ep",
    reference=ReferenceAnswer(answer="Order A17 is refundable."))
EXECUTION = ExecutionResult(execution_id="e", run_id="r", case_id="t2", status="ok",
    output="Order A17 is refundable.", tool_events=[{"type":"mcp_tool_call","name":"lookup_order",
    "arguments":{"order_id":"A17"},"result":{"status":"refundable"},"status":"success"}],
    observation_completeness={"tool_events":{"state":"observed"}})
TURNS = [
    {"case_id":"t1", "input":"Can I refund A17?", "output":"Checking the order.",
     "retrieved_context":[], "tool_events":[{"type":"mcp_tool_call","name":"lookup_order",
     "arguments":{"order_id":"A17"},"result":{"status":"refundable"},"status":"success"}]},
    {"case_id":"t2", "input":"Thanks.", "output":"Order A17 is refundable.",
     "retrieved_context":[], "tool_events":[]},
]
async def outcome(metric_id, params, turns=None, events=None):
    evaluator = BY_ID[metric_id]()
    await evaluator.prepare(params)
    execution = EXECUTION
    if events is not None:
        execution = execution.model_copy(update={"tool_events": events})
    view = EvaluationView(case=CASE, execution=execution, episode=tuple(TURNS if turns is None else turns))
    return await evaluator.evaluate(view, EvaluatorContext(run_id="r", scoring_id="s"))
"""


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV, extra_paths=[Path(__file__).parent / "fixtures" / "deepeval_judges"]
    )
    assert [load.error for load in loads] == [None]
    return registry


def test_mcp_evaluators_are_discovered_with_correct_scope_and_concepts(
    registry: EvaluatorRegistry,
) -> None:
    expected = {
        "deepeval.mcp_use": {"case.input", "execution.output", "execution.tool_events"},
        "deepeval.mcp_task_completion": {
            "case.group_id",
            "episode.turns",
            "execution.output",
            "execution.tool_events",
        },
        "deepeval.multi_turn_mcp_use": {
            "case.group_id",
            "episode.turns",
            "execution.output",
            "execution.tool_events",
        },
    }
    for evaluator_id, required in expected.items():
        manifest, _ = registry.resolve(f"{evaluator_id}@1")
        assert required <= {item.path for item in manifest.requires}
        assert manifest.uses_models and manifest.requires_worker
        assert manifest.concepts and set(manifest.concepts) <= set(CONCEPTS)
        assert "never connects to or discovers a live MCP server" in " ".join(manifest.limitations)
        assert manifest.plugin_version == "0.2.0rc24"
    expected_resources = {
        "deepeval.mcp_use": (8, 1),
        "deepeval.mcp_task_completion": (204, 50),
        "deepeval.multi_turn_mcp_use": (404, 50),
    }
    for evaluator_id, (retries, concurrency) in expected_resources.items():
        manifest, _ = registry.resolve(f"{evaluator_id}@1")
        assert manifest.internal_retries == retries
        assert manifest.internal_concurrency == concurrency
    assert "deepeval" not in sys.modules


def test_all_three_mcp_metrics_score_with_the_pinned_deepeval_package() -> None:
    metrics = (
        "deepeval.mcp_use",
        "deepeval.mcp_task_completion",
        "deepeval.multi_turn_mcp_use",
    )
    result = plugin_python(
        _SETUP
        + f"""
async def main():
    report = {{}}
    for metric in {metrics!r}:
        outcome_value = await outcome(metric, {PARAMS!r})
        report[metric] = {{"status": outcome_value.status.value,
            "value": outcome_value.value.value if outcome_value.value else None,
            "reason": outcome_value.reason,
            "metric": (outcome_value.raw or {{}}).get("metric"),
            "calls": (outcome_value.raw or {{}}).get("successful_mcp_tool_calls")}}
    return report
print(json.dumps(asyncio.run(main())))
"""
    )
    assert set(result) == set(metrics)
    for evaluator_id, output in result.items():
        assert output["status"] == "ok", (evaluator_id, output)
        assert output["value"] == 1.0, evaluator_id
        assert output["calls"] == 1, evaluator_id


def test_model_requested_but_unexecuted_tool_call_is_not_applicable() -> None:
    request_only = [{"type": "function", "function": {"name": "lookup_order", "arguments": "{}"}}]
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_use", {PARAMS!r}, events={request_only!r}))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "no_successful_mcp_tool_calls"]


def test_mixed_tool_events_require_inventory_match_or_explicit_mcp_type() -> None:
    events = [
        {
            "type": "function",
            "name": "lookup_order",
            "arguments": {},
            "result": "ok",
            "status": "success",
        },
        {
            "type": "not_mcp",
            "name": "another_tool",
            "arguments": {},
            "result": "ok",
            "status": "success",
        },
        {
            "type": "mcp_tool_call",
            "name": "undeclared_tool",
            "arguments": {},
            "result": "ok",
            "status": "success",
        },
    ]
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_use", {PARAMS!r}, events={events!r}))
print(json.dumps([value.status.value, (value.raw or {{}}).get("successful_mcp_tool_calls")]))
"""
    )
    assert result == ["ok", 1]


def test_untyped_matching_name_is_not_assumed_to_be_an_mcp_call() -> None:
    # Even a matching tool name cannot distinguish an untyped native call from an MCP call.
    local_call = [{"name": "lookup_order", "arguments": {}, "result": "ok", "status": "success"}]
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_use", {PARAMS!r}, events={local_call!r}))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "no_successful_mcp_tool_calls"]


def test_malformed_mcp_arguments_are_not_rewritten_as_a_valid_object() -> None:
    malformed = [
        {
            "type": "mcp_tool_call",
            "name": "lookup_order",
            "arguments": "not-json",
            "result": "ok",
            "status": "success",
        }
    ]
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_use", {PARAMS!r}, events={malformed!r}))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "oversized_or_invalid:mcp_evidence"]


def test_oversized_all_failed_event_list_is_rejected_before_call_parsing() -> None:
    result = plugin_python(
        _SETUP
        + f"""
failed_events = [{{"type":"mcp_tool_call", "name":"lookup_order", "arguments":{{}},
    "result":"failed", "status":"failed"}}] * 513
value = asyncio.run(outcome("deepeval.mcp_use", {PARAMS!r}, events=failed_events))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "oversized_or_invalid:mcp_evidence"]


def test_episode_total_event_limit_covers_failed_events_across_turns() -> None:
    result = plugin_python(
        _SETUP
        + f"""
failed_events = [{{"type":"mcp_tool_call", "name":"lookup_order", "arguments":{{}},
    "result":"failed", "status":"failed"}}] * 256
turns = [{{"case_id":f"t{{index}}", "input":"Question",
    "output":"No action succeeded.", "tool_events":failed_events}} for index in range(5)]
value = asyncio.run(outcome("deepeval.mcp_task_completion", {PARAMS!r}, turns=turns))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "oversized_or_invalid:mcp_evidence"]


def test_untyped_calls_can_be_declared_mcp_only_for_an_exclusive_event_stream() -> None:
    untyped_call = [{"name": "lookup_order", "arguments": {}, "result": "ok", "status": "success"}]
    params = {**PARAMS, "tool_events_are_mcp_only": True}
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_use", {params!r}, events={untyped_call!r}))
print(json.dumps([value.status.value, (value.raw or {{}}).get("successful_mcp_tool_calls")]))
"""
    )
    assert result == ["ok", 1]


def test_mcp_only_opt_in_does_not_accept_typed_resources_prompts_or_native_tools() -> None:
    typed_non_tool_events = [
        {
            "type": event_type,
            "name": "lookup_order",
            "arguments": {},
            "result": "ok",
            "status": "success",
        }
        for event_type in ("mcp_resource_read", "mcp_prompt_get", "native_tool")
    ]
    params = {**PARAMS, "tool_events_are_mcp_only": True}
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_use", {params!r}, events={typed_non_tool_events!r}))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "no_successful_mcp_tool_calls"]


def test_unknown_episode_tool_telemetry_makes_the_mcp_metric_not_applicable() -> None:
    incomplete = [
        {"case_id": "t1", "input": "Question", "output": "Checking.", "tool_events": None},
        {"case_id": "t2", "input": "Thanks", "output": "Done.", "tool_events": [EVENT]},
    ]
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_task_completion", {PARAMS!r}, turns={incomplete!r}))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "missing:execution.tool_events:t1"]


def test_malformed_episode_tool_telemetry_makes_the_mcp_metric_not_applicable() -> None:
    malformed = [
        {"case_id": "t1", "input": "Question", "output": "Checking.", "tool_events": [EVENT]},
        {
            "case_id": "t2",
            "input": "Thanks",
            "output": "Done.",
            "tool_events": {"unexpected": EVENT},
        },
    ]
    result = plugin_python(
        _SETUP
        + f"""
value = asyncio.run(outcome("deepeval.mcp_task_completion", {PARAMS!r}, turns={malformed!r}))
print(json.dumps([value.status.value, value.reason]))
"""
    )
    assert result == ["not_applicable", "oversized_or_invalid:mcp_evidence"]


def test_mcp_use_scores_through_the_isolated_worker(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("mcp-case")],
        [
            execution(
                "mcp-case",
                "Order A17 is refundable.",
                tool_events=[EVENT],
                observation_completeness={"tool_events": {"state": "observed"}},
            )
        ],
    )
    report = seeded.score(
        [{"metric": "deepeval.mcp_use", "params": PARAMS}],
        registry=registry,
        timeout_seconds=180,
    )
    assert len(report.results) == 1
    assert report.results[0].status is ExecutionStatus.OK, report.results[0].reason
    assert report.results[0].value.value == 1.0


def test_pending_judge_calls_settle_before_usage_is_reported() -> None:
    result = plugin_python(
        _SETUP
        + """
import types
import aibench_deepeval.mcp_metrics as module

class FakeJudge:
    settled = False
    async def settle_pending_calls(self):
        self.settled = True

class FakeMetric:
    async def a_measure(self, test_case, _show_indicator=False):
        raise RuntimeError("judge failure")

judge = FakeJudge()
metric = FakeMetric()
class BrokenMCPUse(module.MCPUse):
    def _new_metric(self, _judge):
        return metric

evaluator = BrokenMCPUse()
evaluator.params = {"judge": {}}
module.build_judge = lambda _params: judge
reported = types.SimpleNamespace(value=False)
def report(_ctx, _metric, active_judge):
    assert active_judge.settled
    reported.value = True
module.report_judge_usage = report
try:
    asyncio.run(evaluator._measure(object(), object(), call_count=1, turn_count=1))
except RuntimeError as exc:
    print(json.dumps([str(exc), judge.settled, reported.value]))
"""
    )
    assert result == ["judge failure", True, True]
