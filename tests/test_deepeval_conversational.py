"""DeepEval's conversational metrics against the REAL pinned DeepEval in its plugin
environment, with deterministic `DeepEvalBaseLLM` judges (no DeepEval code is mocked).
Skipped when the plugin environment is not installed (see plugins/deepeval/README.md)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus, ReferenceAnswer
from aibench.planning.catalog import CONCEPTS
from aibench.registry import EvaluatorRegistry
from tests.deepeval_support import JUDGES, PLUGIN_ENV, plugin_python, requires_plugin_env
from tests.scoring_support import Seeded, case, execution

pytestmark = requires_plugin_env

AGREEING = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
CONVERSATIONAL = (
    "conversation_completeness",
    "knowledge_retention",
    "role_adherence",
    "goal_accuracy",
    "topic_adherence",
    "tool_use",
    "turn_relevancy",
    "turn_faithfulness",
    "turn_contextual_precision",
    "turn_contextual_recall",
    "turn_contextual_relevancy",
    "conversational_g_eval",
)
EXTRA: dict[str, dict[str, Any]] = {
    "deepeval.role_adherence": {"chatbot_role": "a support agent"},
    "deepeval.topic_adherence": {"relevant_topics": ["refunds"]},
    "deepeval.tool_use": {"available_tools": ["lookup"]},
    "deepeval.conversational_g_eval": {"name": "polite", "criteria": "Is the assistant polite?"},
}

# Two turns of one episode, as the harness assembles them for the second turn.
_SETUP = """
import asyncio, json, os
os.environ.update(DEEPEVAL_TELEMETRY_OPT_OUT="1", DEEPEVAL_DISABLE_DOTENV="1")
from aibench.core.models import BenchmarkCase, ExecutionResult, ReferenceAnswer
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval import EVALUATORS
BY_ID = {cls.manifest.evaluator_id: cls for cls in EVALUATORS}
CASE = BenchmarkCase(case_id="t2", input="It was two days ago.", group_id="ep",
    reference=ReferenceAnswer(answer="Order A17 is refundable."))
EXECUTION = ExecutionResult(execution_id="e", run_id="r", case_id="t2", status="ok",
    output="Order A17 is refundable.")
TURNS = [
    {"case_id": "t1", "input": "I need a refund for A17.", "output": "When did you buy it?",
     "retrieved_context": ["Refunds within 30 days."],
     "tool_events": [{"name": "lookup", "arguments": {"order": "A17"}, "result": "ok"}]},
    {"case_id": "t2", "input": "It was two days ago.", "output": "Order A17 is refundable.",
     "retrieved_context": ["Refunds within 30 days."], "tool_events": []},
]
async def outcome(metric_id, params, turns=None, case=CASE):
    evaluator = BY_ID[metric_id]()
    await evaluator.prepare(params)
    view = EvaluationView(case=case, execution=EXECUTION, episode=tuple(turns or TURNS))
    return await evaluator.evaluate(view, EvaluatorContext(run_id="r", scoring_id="s"))
"""


def _params(metric_id: str) -> dict[str, Any]:
    return {"judge": AGREEING, **EXTRA.get(metric_id, {})}


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV, extra_paths=[JUDGES])
    assert [load.error for load in loads] == [None]
    return registry


def test_every_conversational_metric_is_discovered(registry: EvaluatorRegistry) -> None:
    for name in CONVERSATIONAL:
        manifest, _ = registry.resolve(f"deepeval.{name}@1")
        paths = {r.path for r in manifest.requires}
        assert {"case.group_id", "episode.turns", "execution.output"} <= paths, name
        assert manifest.uses_models and manifest.requires_worker
        assert manifest.concepts and set(manifest.concepts) <= set(CONCEPTS), name
        assert "conversation up to each turn" in manifest.description, name
    assert "deepeval" not in sys.modules


def test_every_conversational_metric_scores_through_the_real_package() -> None:
    cases = {f"deepeval.{name}": _params(f"deepeval.{name}") for name in CONVERSATIONAL}
    result = plugin_python(
        _SETUP
        + f"""
async def main():
    report = {{}}
    for metric, params in {cases!r}.items():
        result = await outcome(metric, params)
        report[metric] = [result.status.value, result.value.value if result.value else result.reason,
                          (result.raw or {{}}).get("turns")]
    return report
print(json.dumps(asyncio.run(main())))
"""
    )
    for metric, (status, value, turns) in result.items():
        assert status == "ok", (metric, value)
        assert 0.0 <= value <= 1.0, metric
        assert turns == 2, metric  # both turns of the conversation reached DeepEval


@pytest.mark.parametrize(
    ("metric", "change", "reason"),
    [
        ("deepeval.knowledge_retention", "TURNS[0]['output'] = '  '", "unscorable_output:t1"),
        (
            "deepeval.turn_faithfulness",
            "for t in TURNS: t['retrieved_context'] = []",
            "empty:execution.retrieved_context",
        ),
        ("deepeval.tool_use", "TURNS[0]['tool_events'] = []", "no_tool_calls"),
        (
            "deepeval.turn_contextual_recall",
            "CASE = CASE.model_copy(update={'reference': None})",
            "missing:case.reference.answer",
        ),
    ],
)
def test_what_a_conversation_lacks_is_not_applicable(metric: str, change: str, reason: str) -> None:
    result = plugin_python(
        _SETUP
        + f"""
{change}
result = asyncio.run(outcome({metric!r}, {_params(metric)!r}, TURNS, CASE))
print(json.dumps([result.status.value, result.reason]))
"""
    )
    assert result == ["not_applicable", reason]


def test_conversations_are_scored_per_turn_through_the_real_worker(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    """End to end in the harness: each turn of an episode is scored by the real DeepEval
    worker on the conversation so far; a case outside any episode is not applicable."""
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("t1", group_id="ep"), case("t2", group_id="ep"), case("solo")],
        [
            execution("t1", "When did you buy it?"),
            execution("t2", "Order A17 is refundable."),
            execution("solo", "Hello."),
        ],
    )
    binding = {"metric": "deepeval.knowledge_retention", "params": {"judge": AGREEING}}
    report = seeded.score([binding], registry=registry, timeout_seconds=180)
    by_case = {r.case_id: r for r in report.results}
    for turn, expected in (("t1", 1), ("t2", 2)):
        result = by_case[turn]
        assert result.status is ExecutionStatus.OK, result.reason
        raw = json.loads(
            seeded.artifacts.read_bytes(seeded.storage.get_artifact(result.raw_artifact_ref))
        )
        assert raw["turns"] == expected and raw["metric"] == "KnowledgeRetentionMetric"
    assert (by_case["solo"].status, by_case["solo"].reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "missing:case.group_id",
    )


def test_conversational_g_eval_reads_the_fields_it_is_given(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    """G-Eval over the conversation, told to use the expected outcome: through the worker,
    a turn with a reference answer is scored and one without it is not applicable."""
    seeded = Seeded(tmp_path)
    golden = case("t2", group_id="ep").model_copy(
        update={"reference": ReferenceAnswer(answer="Order A17 is refundable.")}
    )
    seeded.seed(
        [case("t1", group_id="ep"), golden],
        [execution("t1", "When did you buy it?"), execution("t2", "Order A17 is refundable.")],
    )
    binding = {
        "metric": "deepeval.conversational_g_eval",
        "params": {
            "judge": AGREEING,
            "name": "reaches the outcome",
            "criteria": "Does the conversation reach the expected outcome?",
            "evaluation_params": ["role", "content", "expected_outcome"],
        },
    }
    report = seeded.score([binding], registry=registry, timeout_seconds=180)
    by_case = {r.case_id: r for r in report.results}
    assert by_case["t2"].status is ExecutionStatus.OK, by_case["t2"].reason
    assert (by_case["t1"].status, by_case["t1"].reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "missing:case.reference.answer",
    )


def test_a_real_multi_turn_run_is_scored_turn_by_turn_by_deepeval(tmp_path: Path) -> None:
    """End to end through `aibench run`: the stateful multi-turn fixture app runs two
    episodes, and DeepEval knowledge retention scores each turn in the real worker, on the
    conversation the run actually recorded."""
    import threading

    from typer.testing import CliRunner

    from aibench.cli.main import app
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage
    from tests.runner_support import load_example
    from tests.test_episode_contract import _copied_example

    server = load_example("multi_turn_support").make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        project = _copied_example(tmp_path, server)
        plan_path = project / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["metrics"].append(
            {"metric": "deepeval.knowledge_retention", "params": {"judge": AGREEING}}
        )
        plan["plugin_environments"] = [{"python": str(PLUGIN_ENV), "paths": [str(JUDGES)]}]
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        policy_path = project / "policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy.update(
            allowed_plugin_environments=[str(PLUGIN_ENV)],
            allowed_plugin_paths=[str(JUDGES)],
            allowed_evaluators=[*policy.get("allowed_evaluators", ["native.*"]), "deepeval.*"],
            allow_model_evaluators=True,
        )
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        result = CliRunner().invoke(
            app,
            [
                "run",
                "--plan",
                str(plan_path),
                "--policy",
                str(policy_path),
                "--workspace",
                str(project),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        run_id = json.loads(result.stdout)["run_id"]
        storage = Storage(Database.open_workspace(Workspace.at(project)))
        try:
            retention = {
                r.case_id: r
                for r in storage.list_metric_results(run_id)
                if r.metric_id == "deepeval.knowledge_retention"
            }
            assert set(retention) == {
                "refund-turn-1",
                "refund-turn-2",
                "exchange-turn-1",
                "exchange-turn-2",
            }
            for case_id, result_ in retention.items():
                assert result_.status is ExecutionStatus.OK, (case_id, result_.reason)
            from aibench.storage.artifacts import ArtifactStore

            artifacts = ArtifactStore(Workspace.at(project).artifacts_dir)
            turns = {
                case_id: json.loads(artifacts.read_bytes(storage.get_artifact(r.raw_artifact_ref)))[
                    "turns"
                ]
                for case_id, r in retention.items()
            }
            assert turns == {
                "refund-turn-1": 1,
                "refund-turn-2": 2,
                "exchange-turn-1": 1,
                "exchange-turn-2": 2,
            }
        finally:
            storage.db.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
