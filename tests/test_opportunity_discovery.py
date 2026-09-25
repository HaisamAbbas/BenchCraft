"""Prompt 26-T1: objective opportunities remain tied to observed evaluator inputs."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.planning.opportunities import discover_opportunities
from aibench.security.policy import ExecutionPolicy
from tests.planning_support import (
    GROUNDED,
    planning_inputs,
    registry_with,
    write_app,
    write_dataset,
)

_RAG_POLICY = ExecutionPolicy(
    allow_trusted_local=True,
    allowed_evaluators=("native.*", "fixture.*"),
    allow_model_evaluators=True,
)
runner = CliRunner()


def test_rag_opportunity_uses_application_retrieval_and_reports_case_coverage(
    tmp_path: Path,
) -> None:
    app = write_app(tmp_path, output_binding={"output": "/answer", "retrieved_context": "/ctx"})
    dataset = write_dataset(
        tmp_path,
        [
            {
                "case_id": "with-reference-context",
                "input": "private question",
                "reference": {"answer": "private reference", "context": ["private passage"]},
            },
            {"case_id": "missing-reference-context", "input": "another question"},
        ],
    )
    inputs = planning_inputs(
        tmp_path,
        app,
        dataset,
        ["check groundedness"],
        policy=_RAG_POLICY,
        registry=registry_with(GROUNDED),
    )

    report = discover_opportunities(inputs)
    [objective] = report.objectives
    [concept] = objective.concepts
    [metric] = concept.metrics
    assert (objective.state, concept.state, metric.state, metric.recommended) == (
        "available",
        "available",
        "available",
        True,
    )
    assert metric.usable_cases == 2
    requirements = {item.path: item.state for item in metric.requirements}
    assert requirements == {
        "case.input": "available",
        "execution.output": "available",
        "execution.retrieved_context": "available",
    }
    serialized = report.model_dump_json()
    assert "private question" not in serialized
    assert "private reference" not in serialized
    assert "private passage" not in serialized


def test_reference_context_does_not_make_unobserved_retrieval_available(tmp_path: Path) -> None:
    app = write_app(tmp_path)
    dataset = write_dataset(
        tmp_path,
        [
            {
                "case_id": "rag-labeled",
                "input": "private input",
                "reference": {"answer": "private answer", "context": ["judge-only context"]},
            }
        ],
    )
    inputs = planning_inputs(
        tmp_path,
        app,
        dataset,
        ["check groundedness"],
        policy=_RAG_POLICY,
        registry=registry_with(GROUNDED),
    )

    report = discover_opportunities(inputs)
    [concept] = report.objectives[0].concepts
    [metric] = concept.metrics
    assert concept.state == metric.state == "unavailable"
    assert not metric.recommended
    assert "execution.retrieved_context" in (concept.gap or "")
    retrieval = next(
        item for item in metric.requirements if item.path == "execution.retrieved_context"
    )
    assert retrieval.state == "missing"
    assert "does not declare or expose" in retrieval.detail
    assert "judge-only context" not in report.model_dump_json()


def test_native_tool_call_opportunity_requires_declared_events(tmp_path: Path) -> None:
    data = write_dataset(
        tmp_path,
        [
            {
                "case_id": "tool-case",
                "input": "private task",
                "reference": {"tools": {"tool_names": ["private_tool"]}},
            }
        ],
    )
    objective = ["check expected tool calls"]

    declared_app = write_app(
        tmp_path,
        name="declared-tools.json",
        output_binding={"output": "/answer", "tool_events": "/events"},
    )
    declared = planning_inputs(
        tmp_path,
        declared_app,
        data,
        objective,
        policy=_RAG_POLICY,
    )
    available = discover_opportunities(declared)
    [available_concept] = available.objectives[0].concepts
    available_metric = next(
        metric for metric in available_concept.metrics if metric.evaluator_id == "native.tool_calls"
    )
    assert available_concept.state == available_metric.state == "available"
    assert available_metric.recommended

    uninstrumented_app = write_app(tmp_path, name="unobserved-tools.json")
    unobserved = planning_inputs(
        tmp_path,
        uninstrumented_app,
        data,
        objective,
        policy=_RAG_POLICY,
    )
    unavailable = discover_opportunities(unobserved)
    [missing_concept] = unavailable.objectives[0].concepts
    missing_metric = next(
        metric for metric in missing_concept.metrics if metric.evaluator_id == "native.tool_calls"
    )
    assert missing_concept.state == missing_metric.state == "unavailable"
    assert not missing_metric.recommended
    event_evidence = next(
        item for item in missing_metric.requirements if item.path == "execution.tool_events"
    )
    assert event_evidence.state == "missing"
    assert "private_tool" not in json.dumps(unavailable.model_dump(mode="json"))


def test_unrecognized_objective_remains_unknown_and_requests_clarification(tmp_path: Path) -> None:
    app = write_app(tmp_path)
    dataset = write_dataset(tmp_path, [{"case_id": "one", "input": "q"}])
    inputs = planning_inputs(tmp_path, app, dataset, ["private domain-specific objective"])

    report = discover_opportunities(inputs)
    [objective] = report.objectives
    assert objective.state == "unknown"
    assert not objective.concepts
    assert objective.questions and "Which concept" in objective.questions[0]


def test_opportunities_cli_emits_json_without_writing_a_plan_or_running_app(tmp_path: Path) -> None:
    app_file = write_app(tmp_path)
    dataset = write_dataset(
        tmp_path,
        [{"case_id": "one", "input": "private input", "reference": {"answer": "private answer"}}],
    )
    result = runner.invoke(
        app,
        [
            "plan",
            "opportunities",
            "--app",
            str(app_file),
            "--dataset",
            str(dataset),
            "--objective",
            "check correctness",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    [objective] = payload["objectives"]
    [concept] = objective["concepts"]
    assert (objective["state"], concept["concept"], concept["state"]) == (
        "available",
        "correctness",
        "available",
    )
    assert concept["recommended_metric"].startswith("native.exact_match@")
    assert "private input" not in result.stdout and "private answer" not in result.stdout
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        app_file.name,
        dataset.name,
    ]
