"""Bounded planning loop with an injected fake provider (07-T3; gates 07-G1, 07-G3, 07-G4).

The fake provider is scripted: these tests prove the harness side of planning — narrow
tools, validation outside the model, bounded repairs and spend, fallback, and what data
leaves the harness — not any real model's planning quality."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from aibench.planning.planner import (
    TOOL_NAMES,
    ModelReply,
    PlannerError,
    PlannerLimits,
    plan_with_model,
    plan_with_template,
    tool_specs,
)
from aibench.security.policy import ExecutionPolicy
from tests.planning_support import (
    GROUNDED,
    FakeProvider,
    call,
    draft,
    metric,
    objective,
    planning_inputs,
    registry_with,
    write_app,
    write_dataset,
)

ROWS = [
    {"case_id": "a", "input": "SECRET-Q1", "expected_output": "SECRET-A1", "context": ["SECRET-C"]},
    {"case_id": "b", "input": "SECRET-Q2", "expected_output": "SECRET-A2"},
]
JUDGES = ExecutionPolicy(
    allow_trusted_local=True,
    allowed_evaluators=("native.*", "fixture.*"),
    allow_model_evaluators=True,
)
CORRECT = objective("o1", "catch wrong answers", "correctness")
VALID = draft([metric("native.exact_match@1.0.0", "o1")], objectives=[CORRECT])


def _inputs(
    tmp_path: Path,
    *,
    retrieval: bool = False,
    objectives: tuple[str, ...] = ("catch wrong answers",),
):  # type: ignore[no-untyped-def]
    binding = {"output": "/answer", **({"retrieved_context": "/ctx"} if retrieval else {})}
    app = write_app(tmp_path, output_binding=binding)
    dataset = write_dataset(tmp_path, ROWS)
    return planning_inputs(
        tmp_path, app, dataset, list(objectives), policy=JUDGES, registry=registry_with(GROUNDED)
    )


def test_a_valid_draft_is_accepted_through_write_plan_draft(tmp_path: Path) -> None:
    fake = FakeProvider([call("list_evaluators", {}), call("write_plan_draft", VALID, "c2")])
    outcome = plan_with_model(_inputs(tmp_path), fake)
    assert outcome.validation.executable
    assert [m.metric for m in outcome.proposal.metrics] == ["native.exact_match@1.0.0"]
    p = outcome.provenance
    assert (p.kind, p.model_calls, p.tool_calls, p.repairs, p.fallback_reason) == (
        "model",
        2,
        2,
        0,
        None,
    )
    assert (p.prompt_tokens, p.completion_tokens) == (200, 80)
    # Tool results go back as role=tool messages after the assistant's tool call.
    assert fake.requests[1][-2]["tool_calls"][0]["function"]["name"] == "list_evaluators"
    assert fake.requests[1][-1]["role"] == "tool" and fake.requests[1][-1]["tool_call_id"] == "c1"


def test_unknown_evaluator_ids_are_rejected_outside_the_model_then_repaired(tmp_path: Path) -> None:
    invented = draft([metric("deepeval.hallucination_magic", "o1")], objectives=[CORRECT])
    fake = FakeProvider([call("write_plan_draft", invented), call("write_plan_draft", VALID, "c2")])
    outcome = plan_with_model(_inputs(tmp_path), fake)
    feedback = json.loads(fake.requests[1][-1]["content"])
    assert feedback["accepted"] is False
    assert any(
        "unknown evaluator 'deepeval.hallucination_magic'" in m
        for m in feedback["blocking_findings"]
    )
    assert outcome.provenance.repairs == 1 and outcome.validation.executable
    assert "deepeval" not in json.dumps([m.metric for m in outcome.proposal.metrics])


def test_misleading_rag_evidence_becomes_an_explicit_gap_not_a_metric(tmp_path: Path) -> None:
    """The dataset has reference context, but the app exposes no retrieval: groundedness
    cannot be measured, whatever the model first proposes (07-G3)."""
    grounded = objective("o1", "unsupported claims", "groundedness")
    wrong = draft([metric("fixture.grounded@1.0.0", "o1")], objectives=[grounded])
    honest = draft(
        objectives=[grounded],
        gaps=[{"subject": "o1", "reason": "the application does not expose retrieved context"}],
    )
    fake = FakeProvider([call("write_plan_draft", wrong), call("write_plan_draft", honest, "c2")])
    outcome = plan_with_model(_inputs(tmp_path, objectives=("unsupported claims",)), fake)
    feedback = json.loads(fake.requests[1][-1]["content"])
    assert any("retrieved_context" in m for m in feedback["blocking_findings"])
    assert outcome.validation.executable and not outcome.proposal.metrics
    assert outcome.proposal.gaps[0].subject == "o1"


def test_when_retrieval_is_declared_the_judge_metric_is_eligible(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, retrieval=True, objectives=("unsupported claims",))
    option = next(o for o in inputs.catalog if o.evaluator_id == "fixture.grounded")
    assert option.eligible and option.concepts == ("groundedness",)
    template = plan_with_template(inputs)
    assert [m.metric for m in template.proposal.metrics] == ["fixture.grounded@1.0.0"]
    assert template.validation.estimate is not None
    assert template.validation.estimate.model_evaluations == 2


def test_no_planner_tool_gives_terminal_file_or_network_access(tmp_path: Path) -> None:
    assert TOOL_NAMES == {
        "read_profile",
        "summarize_dataset",
        "list_evaluators",
        "describe_evaluator",
        "validate_plan",
        "estimate_cost",
        "write_plan_draft",
    }
    fake = FakeProvider(
        [
            call("run_shell", {"command": "rm -rf /"}),
            call("write_file", {"path": "/etc/passwd", "content": "x"}, "c2"),
            call("write_plan_draft", VALID, "c3"),
        ]
    )
    outcome = plan_with_model(_inputs(tmp_path), fake)
    assert outcome.provenance.rejected_tool_calls == ("run_shell", "write_file")
    assert "unknown tool 'run_shell'" in fake.requests[1][-1]["content"]
    assert [s["function"]["name"] for s in fake.tools[0]] == [
        s["function"]["name"] for s in tool_specs()
    ]


def test_planning_code_imports_no_process_or_shell_facilities() -> None:
    """Static guard: the planner package cannot spawn processes or run shell commands."""
    root = Path(__file__).resolve().parents[1] / "src" / "aibench" / "planning"
    banned = {"subprocess", "shutil", "pty", "socket", "multiprocessing"}
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom):
                names = {(node.module or "").split(".")[0]}
            elif isinstance(node, ast.Attribute) and node.attr in ("system", "popen", "spawnv"):
                pytest.fail(f"{path.name} uses os.{node.attr}")
            else:
                continue
            assert not names & banned, f"{path.name} imports {names & banned}"


def test_the_model_never_receives_inputs_or_reference_labels(tmp_path: Path) -> None:
    fake = FakeProvider(
        [
            call("summarize_dataset", {}),
            call("read_profile", {}, "c2"),
            call("write_plan_draft", VALID, "c3"),
        ]
    )
    plan_with_model(_inputs(tmp_path), fake)
    sent = fake.sent_text()
    assert "SECRET" not in sent
    assert "case.reference.answer" in sent  # the shape is shared, the values are not


def test_the_model_cannot_set_paths_budgets_or_policy(tmp_path: Path) -> None:
    sneaky = {**VALID, "dataset": "/etc/shadow", "budgets": {"max_application_calls": 10**6}}
    fake = FakeProvider([call("write_plan_draft", sneaky), call("write_plan_draft", VALID, "c2")])
    outcome = plan_with_model(_inputs(tmp_path), fake)
    errors = json.loads(fake.requests[1][-1]["content"])["schema_errors"]
    assert any("dataset" in e for e in errors) and any("budgets" in e for e in errors)
    plan = outcome.validation.plan
    assert (
        plan is not None
        and plan.dataset == "data.jsonl"
        and plan.budgets.max_application_calls is None
    )


@pytest.mark.parametrize(
    ("script", "limits", "reason"),
    [
        (
            [ModelReply(text="I think exact match is good.")] * 5,
            PlannerLimits(),
            "repair limit reached (2)",
        ),
        (
            [call("write_plan_draft", draft([metric("nope.nothing", "o1")], objectives=[CORRECT]))]
            * 5,
            PlannerLimits(),
            "repair limit reached (2)",
        ),
        (
            [PlannerError("HTTP 503: overloaded")],
            PlannerLimits(),
            "provider failed: HTTP 503: overloaded",
        ),
        (
            [
                ModelReply(
                    text=None,
                    tool_calls=call("list_evaluators", {}).tool_calls,
                    prompt_tokens=90_000,
                )
            ],
            PlannerLimits(),
            "planner token limit reached (60000)",
        ),
        (
            [call("describe_evaluator", {"metric": "native.exact_match"})] * 20,
            PlannerLimits(max_tool_calls=3),
            "tool call limit reached (3)",
        ),
        (
            [call("list_evaluators", {})] * 20,
            PlannerLimits(max_model_calls=4),
            "model call limit reached (4)",
        ),
    ],
    ids=["no-tool-call", "always-invalid", "provider-error", "tokens", "tool-calls", "model-calls"],
)
def test_every_bound_falls_back_to_the_deterministic_template(
    tmp_path: Path, script: list[object], limits: PlannerLimits, reason: str
) -> None:
    inputs = _inputs(tmp_path)
    outcome = plan_with_model(inputs, FakeProvider(script), limits)  # type: ignore[arg-type]
    assert outcome.provenance.fallback_reason == reason
    assert outcome.provenance.kind == "model"
    assert outcome.proposal == plan_with_template(inputs).proposal
    assert outcome.validation.executable


def test_malformed_tool_arguments_are_reported_back_not_crashing(tmp_path: Path) -> None:
    fake = FakeProvider(
        [call("write_plan_draft", "{not json"), call("write_plan_draft", VALID, "c2")]
    )
    outcome = plan_with_model(_inputs(tmp_path), fake)
    assert "not valid JSON" in fake.requests[1][-1]["content"]
    assert outcome.validation.executable


def test_deeply_nested_tool_arguments_are_reported_back_not_crashing(tmp_path: Path) -> None:
    nested = "[" * 10_000 + "0" + "]" * 10_000
    fake = FakeProvider([call("read_profile", nested), call("write_plan_draft", VALID, "c2")])

    outcome = plan_with_model(_inputs(tmp_path), fake)

    assert "could not be parsed safely" in fake.requests[1][-1]["content"]
    assert outcome.provenance.fallback_reason is None
    assert outcome.validation.executable


def test_only_the_user_selects_cases(tmp_path: Path) -> None:
    """A model-proposed selection is a schema error (selection predicates would leak label
    values); the user's selection is what runs."""
    from aibench.core.plans import CaseSelection

    app = write_app(tmp_path)
    dataset = write_dataset(tmp_path, ROWS)
    inputs = planning_inputs(
        tmp_path, app, dataset, ["wrong answers"], selection=CaseSelection(case_ids=("a",))
    )
    greedy = {**VALID, "selection": {"case_ids": ["a", "b"]}}
    fake = FakeProvider([call("write_plan_draft", greedy), call("write_plan_draft", VALID, "c2")])
    outcome = plan_with_model(inputs, fake)
    errors = json.loads(fake.requests[1][-1]["content"])["schema_errors"]
    assert any(e.startswith("selection") for e in errors)
    assert outcome.validation.plan is not None
    assert outcome.validation.plan.selection.case_ids == ("a",)
