"""Planner fixtures and the static-template baseline (07-G3; §23 "Benchmark the planner").

Each fixture annotates what a correct plan does per concept — measure it, or report it as
an explicit gap — and which metrics must never be chosen. The template planner is run over
all of them and scored; its numbers are the baseline a model planner must beat (§23:
"Compare against a static template baseline ... the LLM must demonstrate incremental
value"). No live model was run here; see reports/07.md."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aibench.planning.benchmark import PlannerFixture, assess, score
from aibench.planning.planner import plan_with_template
from aibench.security.policy import ExecutionPolicy
from tests.planning_support import (
    GROUNDED,
    TOOL_NAMES,
    planning_inputs,
    registry_with,
    write_app,
    write_dataset,
)

JUDGES = ExecutionPolicy(
    allow_trusted_local=True,
    allowed_evaluators=("native.*", "fixture.*"),
    allow_model_evaluators=True,
)
NO_JUDGES = JUDGES.model_copy(update={"allow_model_evaluators": False})
REF = {"expected_output": "x"}
CTX = {"context": ["passage"]}
TOOLS = {"expected_tools": ["search"]}
RETRIEVAL = {"output": "/answer", "retrieved_context": "/ctx"}
TOOL_EVENTS = {"output": "/answer", "tool_events": "/tools"}


def _rows(n: int, **fields: Any) -> list[dict[str, Any]]:
    return [{"case_id": f"c{i}", "input": f"q{i}", **fields} for i in range(n)]


# name, category, objectives, app binding, rows, policy, select, gaps, forbidden
FIXTURES: list[tuple[PlannerFixture, dict[str, Any], list[dict[str, Any]], ExecutionPolicy]] = [
    (
        PlannerFixture(
            "chatbot_refs",
            "chatbot",
            ("catch wrong answers",),
            frozenset({"correctness"}),
            frozenset(),
        ),
        {"output": "/answer"},
        _rows(3, **REF),
        JUDGES,
    ),
    (
        PlannerFixture(
            "rag_declared",
            "rag",
            ("unsupported claims", "wrong answers"),
            frozenset({"groundedness", "correctness"}),
            frozenset(),
        ),
        RETRIEVAL,
        _rows(3, **REF, **CTX),
        JUDGES,
    ),
    (
        PlannerFixture(
            "rag_misleading_reference_context",
            "misleading",
            ("unsupported claims",),
            frozenset(),
            frozenset({"groundedness"}),
            frozenset({"fixture.grounded"}),
        ),
        {"output": "/answer"},
        _rows(3, **REF, **CTX),
        JUDGES,
    ),
    (
        PlannerFixture(
            "agent_declared",
            "agent",
            ("did it call the right tools",),
            frozenset({"tool_use"}),
            frozenset(),
        ),
        TOOL_EVENTS,
        _rows(2, **TOOLS),
        JUDGES,
    ),
    (
        PlannerFixture(
            "agent_blackbox",
            "agent",
            ("tool use",),
            frozenset(),
            frozenset({"tool_use"}),
            frozenset({"fixture.tool_names"}),
        ),
        {"output": "/answer"},
        _rows(2, **TOOLS),
        JUDGES,
    ),
    (
        PlannerFixture(
            "blackbox_no_references",
            "blackbox",
            ("correct answers", "latency"),
            frozenset(),
            frozenset({"correctness"}),
            frozenset({"native.exact_match"}),
        ),
        {"output": "/answer"},
        _rows(3),
        JUDGES,
    ),
    (
        PlannerFixture(
            "partial_references", "partial", ("accuracy",), frozenset({"correctness"}), frozenset()
        ),
        {"output": "/answer"},
        [*_rows(1, **REF), {"case_id": "x1", "input": "q"}, {"case_id": "x2", "input": "q"}],
        JUDGES,
    ),
    (
        PlannerFixture(
            "judges_not_permitted",
            "policy",
            ("unsupported claims",),
            frozenset(),
            frozenset({"groundedness"}),
            frozenset({"fixture.grounded"}),
        ),
        RETRIEVAL,
        _rows(2, **CTX),
        NO_JUDGES,
    ),
    (
        PlannerFixture(
            "format_without_schema",
            "format",
            ("valid JSON format",),
            frozenset(),
            frozenset({"format"}),
            frozenset({"native.json_schema"}),
        ),
        {"output": "/answer"},
        _rows(2),
        JUDGES,
    ),
    (
        PlannerFixture(
            "voice_turn_taking_unmapped",
            "unmapped",
            ("natural turn-taking in voice calls",),
            frozenset(),
            frozenset({"unmapped"}),
        ),
        {"output": "/answer"},
        _rows(2),
        JUDGES,
    ),
]


@pytest.mark.parametrize(
    ("fixture", "binding", "rows", "policy"), FIXTURES, ids=lambda f: getattr(f, "name", "")
)
def test_template_plans_each_fixture_with_justified_metrics_or_explicit_gaps(
    tmp_path: Path,
    fixture: PlannerFixture,
    binding: dict[str, Any],
    rows: list[dict[str, Any]],
    policy: ExecutionPolicy,
) -> None:
    app = write_app(tmp_path, output_binding=binding)
    dataset = write_dataset(tmp_path, rows)
    inputs = planning_inputs(
        tmp_path,
        app,
        dataset,
        list(fixture.objectives),
        policy=policy,
        registry=registry_with(GROUNDED, TOOL_NAMES),
    )
    outcome = plan_with_template(inputs)
    result = assess(
        fixture, outcome.proposal, inputs.catalog, executable=outcome.validation.executable
    )
    assert result.selected == set(fixture.select)
    assert result.gapped == set(fixture.gaps)
    assert not result.forbidden_selected
    assert outcome.validation.executable, outcome.validation.blocking_messages()
    for choice in outcome.proposal.metrics:
        assert choice.rationale and choice.objective_ids  # every metric is justified
    for gap in outcome.proposal.gaps:
        assert gap.reason  # every gap says why


def test_template_baseline_scores(tmp_path: Path) -> None:
    results = []
    for index, (fixture, binding, rows, policy) in enumerate(FIXTURES):
        root = tmp_path / str(index)
        root.mkdir()
        app = write_app(root, output_binding=binding)
        dataset = write_dataset(root, rows)
        inputs = planning_inputs(
            root,
            app,
            dataset,
            list(fixture.objectives),
            policy=policy,
            registry=registry_with(GROUNDED, TOOL_NAMES),
        )
        outcome = plan_with_template(inputs)
        results.append(
            assess(
                fixture, outcome.proposal, inputs.catalog, executable=outcome.validation.executable
            )
        )
    baseline = score(results).as_dict()
    assert baseline == {
        "fixtures": 10,
        "selection_precision": 1.0,
        "selection_recall": 1.0,
        "gap_precision": 1.0,
        "gap_recall": 1.0,
        "unnecessary_evaluator_rate": 0.0,
        "unsupported_selections": 0,
        "first_pass_valid": 1.0,
    }
