"""The DAG metric: a decision tree written in the plan as JSON, judged by the plan's judge.
The graph is checked before DeepEval sees it (see `aibench_deepeval.dag`); the scoring tests
run the REAL pinned DeepEval in its plugin environment with a deterministic judge."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import ExecutionStatus
from aibench.registry import BindingValidationError, EvaluatorRegistry
from tests.deepeval_support import JUDGES, PLUGIN_ENV, plugin_python, requires_plugin_env
from tests.scoring_support import Seeded, case, execution

pytestmark = requires_plugin_env

AGREEING = {"kind": "python_factory", "factory": "aibench_schema_judges:agreeing_judge"}
READS = ["input", "actual_output"]

GOOD: dict[str, Any] = {
    "nodes": {
        "extract": {
            "type": "TaskNode",
            "instructions": "List the amounts of money the answer states.",
            "output_label": "amounts",
            "evaluation_params": READS,
            "children": ["states_one"],
        },
        "states_one": {
            "type": "BinaryJudgementNode",
            "criteria": "Does the list contain exactly one amount?",
            "children": ["yes", "no"],
        },
        "yes": {"type": "VerdictNode", "verdict": True, "score": 10},
        "no": {
            "type": "VerdictNode",
            "verdict": False,
            "child": {
                "type": "geval",
                "name": "clarity",
                "criteria": "The answer is short and clear.",
                "evaluation_params": READS,
            },
        },
    }
}


def _problems(document: Any, allowed: list[str] | None = None) -> list[str]:
    allowed_fields = allowed or ["input", "actual_output"]
    return plugin_python(
        "import json;from aibench_deepeval.dag import validate_dag;"
        f"print(json.dumps(validate_dag({document!r}, tuple({allowed_fields!r}))))"
    )


def _with(**changes: Any) -> dict[str, Any]:
    document = copy.deepcopy(GOOD)
    for path, value in changes.items():
        node, _, key = path.partition("__")
        if value is None:
            document["nodes"][node].pop(key, None)
        else:
            document["nodes"][node][key] = value
    return document


def test_a_well_formed_graph_has_no_problems() -> None:
    assert _problems(GOOD) == []


def test_a_metric_child_is_refused_because_it_would_not_use_the_plans_judge() -> None:
    """Upstream builds a `metric` child with its own default model: a case would leave for a
    provider the plan never named."""
    bad = _with(
        no__child={
            "type": "metric",
            "metric_class": "AnswerRelevancyMetric",
            "kwargs": {"model": "gpt-4"},
        }
    )
    [problem] = _problems(bad)
    assert "not 'metric'" in problem and "default model" in problem


def test_a_judge_model_or_any_other_unknown_key_is_refused() -> None:
    geval = {**GOOD["nodes"]["no"]["child"], "model": "gpt-4"}
    assert any("geval key 'model'" in p for p in _problems(_with(no__child=geval)))
    assert any(
        "'model' is not allowed on a TaskNode" in p for p in _problems(_with(extract__model="x"))
    )
    assert any("not allowed on a VerdictNode" in p for p in _problems(_with(yes__code="import os")))


def test_the_graph_is_bounded_in_size_depth_and_text() -> None:
    wide = {
        "nodes": {f"n{i}": {"type": "VerdictNode", "verdict": True, "score": 1} for i in range(41)}
    }
    assert "at most 40" in _problems(wide)[0]
    chain: dict[str, Any] = {}
    for depth in range(10):  # ten nested task nodes
        node: dict[str, Any] = {
            "type": "TaskNode",
            "instructions": "x",
            "output_label": "y",
            "children": [f"t{depth + 1}"] if depth < 9 else [],
        }
        if depth == 0:
            node["evaluation_params"] = READS
        chain[f"t{depth}"] = node
    assert "levels deep" in _problems({"nodes": chain})[0]
    assert "characters" in _problems(_with(extract__instructions="x" * 4001))[0]


def test_structural_mistakes_are_named_before_deepeval_runs() -> None:
    assert "needs 'evaluation_params'" in _problems(_with(extract__evaluation_params=None))[0]
    assert any(
        "'ghost' is not a node" in p
        for p in _problems(_with(states_one__children=["yes", "ghost"]))
    )
    assert "whole number from 0 to 10" in _problems(_with(yes__score=11))[0]
    assert (
        "exactly one of 'score' and 'child'"
        in _problems(_with(yes__child=GOOD["nodes"]["no"]["child"]))[0]
    )
    cycle = _with(yes__score=None, yes__child={"type": "node", "ref": "states_one"})
    assert _problems(cycle)  # a cycle is never a valid graph
    assert any(
        "children must be verdict nodes" in p
        for p in _problems(_with(states_one__children=["extract", "yes"]))
    )


def test_a_yes_no_question_needs_both_answers_to_lead_somewhere() -> None:
    one_way = _with(states_one__children=["yes"])
    assert "exactly two verdict children" in _problems(one_way)[0]
    twice = _with(no__verdict=True)
    assert "exactly two verdict children" in _problems(twice)[0]


def test_fields_beyond_the_question_and_answer_must_be_named_by_the_plan() -> None:
    reads_expected = _with(extract__evaluation_params=["input", "expected_output"])
    assert "expected_output" in _problems(reads_expected)[0]
    assert _problems(reads_expected, ["input", "actual_output", "expected_output"]) == []


def _registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(PLUGIN_ENV, extra_paths=[JUDGES])
    return registry


def _score(tmp_path: Path, params: dict[str, Any], *, expected: str | None = None) -> Any:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1", expected)], [execution("c1", "Refunds cost 30 dollars.")])
    binding = {"metric": "deepeval.dag", "params": {"judge": AGREEING, **params}}
    [result] = seeded.score([binding], registry=_registry(), timeout_seconds=300).results
    return result


def test_a_graph_is_walked_by_the_judge_and_its_verdict_is_the_score(tmp_path: Path) -> None:
    """With the schema judge every question is answered yes: the graph ends at the 10-point
    verdict, reported as 1.0."""
    result = _score(tmp_path, {"dag": GOOD, "name": "one amount"})
    assert result.status is ExecutionStatus.OK, result.reason
    assert result.value.value == 1.0


def test_a_geval_child_grades_with_the_plans_judge_and_needs_no_openai_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upstream builds a G-Eval child on DeepEval's default OpenAI model: with no key that
    failed before the first case. Here it is built with the judge the plan configured."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    only_geval = _with(
        states_one__children=["no2"],
        yes=None,
        no=None,
    )
    only_geval["nodes"] = {
        "extract": GOOD["nodes"]["extract"] | {"children": ["judge"]},
        "judge": {
            "type": "BinaryJudgementNode",
            "criteria": "Is there an amount?",
            "children": ["graded", "none"],
        },
        "graded": {"type": "VerdictNode", "verdict": True, "child": GOOD["nodes"]["no"]["child"]},
        "none": {"type": "VerdictNode", "verdict": False, "score": 0},
    }
    result = _score(tmp_path, {"dag": only_geval})
    assert result.status is ExecutionStatus.OK, result.reason
    assert 0.0 <= result.value.value <= 1.0


def test_an_invalid_graph_stops_the_binding_before_any_case_is_judged(tmp_path: Path) -> None:
    bad = _with(no__child={"type": "metric", "metric_class": "AnswerRelevancyMetric", "kwargs": {}})
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "x")])
    binding = {"metric": "deepeval.dag", "params": {"judge": AGREEING, "dag": bad}}
    try:
        [result] = seeded.score([binding], registry=_registry(), timeout_seconds=300).results
    except BindingValidationError as exc:
        assert "dag" in str(exc)
        return
    assert result.status is ExecutionStatus.ERROR
    assert "default model" in (result.reason or "")


def test_a_graph_that_reads_the_expected_answer_needs_it_named_and_present(tmp_path: Path) -> None:
    reads_expected = _with(extract__evaluation_params=["input", "expected_output"])
    named = {"dag": reads_expected, "evaluation_params": ["expected_output"]}
    result = _score(tmp_path, named, expected="Refunds cost 30 dollars.")
    assert result.status is ExecutionStatus.OK, result.reason
    assert result.value.value == 1.0
    unnamed = _score(tmp_path / "unnamed", {"dag": reads_expected}, expected="x")
    assert unnamed.status is ExecutionStatus.ERROR  # the graph reads a field the plan never named
