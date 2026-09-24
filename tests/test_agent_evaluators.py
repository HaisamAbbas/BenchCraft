"""Agent outcome evaluators (15-T2): the edges the end-to-end world does not reach."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from aibench.core.models import BenchmarkCase, ExecutionResult, ExecutionStatus
from aibench.evaluators.agent import FinalState, ToolCalls, ToolOutcomes, check, parse_tool_events
from aibench.evaluators.protocol import MISSING, EvaluationView, EvaluatorContext
from aibench.registry import EvaluatorRegistry


def _view(
    events: Any = None, *, world: Any = None, expectations: Any = None, tools: Any = None
) -> EvaluationView:
    completeness = {}
    if events is not None:
        completeness["tool_events"] = {"state": "observed", "detail": "present"}
    case = BenchmarkCase(
        case_id="c",
        input="book",
        reference={"tools": tools} if tools else None,
        expectations=expectations or {},
    )
    execution = ExecutionResult(
        execution_id="e",
        run_id="r",
        case_id="c",
        status=ExecutionStatus.OK,
        output="Booked.",
        tool_events=tuple(events or ()),
        world_state=world,
        observation_completeness=completeness,
    )
    return EvaluationView(case=case, execution=execution)


def _evaluate(factory: Any, view: EvaluationView) -> Any:
    async def go() -> Any:
        return await factory().evaluate(view, EvaluatorContext(run_id="r", scoring_id="s"))

    return asyncio.run(go())


def test_openai_tool_calls_are_requests_not_successes() -> None:
    [attempt] = parse_tool_events(
        [{"id": "1", "type": "function",
          "function": {"name": "book_flight", "arguments": '{"flight": "BA117"}'}}]
    )  # fmt: skip
    assert (attempt.name, attempt.arguments, attempt.status) == (
        "book_flight",
        {"flight": "BA117"},
        "requested",
    )
    outcome = _evaluate(
        ToolOutcomes,
        _view(
            [{"type": "function", "function": {"name": "book_flight", "arguments": "{}"}}],
            expectations={"tool_calls": [{"tool": "book_flight"}]},
        ),
    )
    assert outcome.value.value == "not_executed"


@pytest.mark.parametrize(
    ("event", "status"),
    [
        ({"tool": "t", "args": {}, "status": "SUCCESS"}, "ok"),
        ({"name": "t", "error": "boom"}, "error"),
        ({"name": "t", "status": "forbidden"}, "denied"),
        ({"name": "t", "result": 1}, "ok"),
        ({"name": "t"}, "unknown"),
        ("not an object", "unknown"),
    ],
)
def test_tool_event_shapes_are_normalized(event: Any, status: str) -> None:
    [attempt] = parse_tool_events([event])
    assert attempt.status == status


def test_tool_names_follow_the_match_mode() -> None:
    events = [{"name": "search"}, {"name": "book"}, {"name": "email"}]
    for mode, expected, verdict in (
        ("contains_all", ["book", "search"], True),
        ("exact", ["search", "book"], False),
        ("ordered_subsequence", ["search", "email"], True),
        ("ordered_subsequence", ["email", "search"], False),
    ):
        outcome = _evaluate(
            ToolCalls, _view(events, tools={"tool_names": expected, "match_mode": mode})
        )
        assert outcome.value.value is verdict, (mode, expected)


def test_denied_and_missing_calls_have_their_own_categories() -> None:
    expectations = {"tool_calls": [{"tool": "pay", "arguments": {"amount": {"max": 100}}}]}
    denied = _evaluate(
        ToolOutcomes,
        _view([{"name": "pay", "arguments": {"amount": 50}, "status": "denied"}],
              expectations=expectations),
    )  # fmt: skip
    assert denied.value.value == "denied"
    over = _evaluate(
        ToolOutcomes,
        _view([{"name": "pay", "arguments": {"amount": 500}, "status": "ok"}],
              expectations=expectations),
    )  # fmt: skip
    assert over.value.value == "argument_violation"
    assert "amount is 500, above 100" in over.raw["calls"][0]["problems"]
    nothing = _evaluate(ToolOutcomes, _view([], expectations=expectations))
    assert nothing.value.value == "not_called"


def test_an_unobserved_world_or_tool_log_is_missing_evidence_not_a_failure() -> None:
    registry = EvaluatorRegistry.with_native()
    manifest, _ = registry.resolve("native.final_state")
    assert [r.path for r in manifest.requires] == [
        "execution.world_state",
        "case.expectations.final_state",
    ]
    # With no world state observed the view reports it missing; the scorer then records
    # not_applicable (missing:execution.world_state) before the evaluator runs.
    assert _view(world=None).get("execution.world_state") is MISSING
    assert _view(None).get("execution.tool_events") is MISSING


def test_malformed_expectations_are_evaluator_errors() -> None:
    bad_final = _evaluate(
        FinalState, _view(world={"a": 1}, expectations={"final_state": [{"path": "/a"}]})
    )
    assert bad_final.status is ExecutionStatus.ERROR and "states nothing" in bad_final.reason
    bad_key = _evaluate(
        FinalState,
        _view(world={"a": 1}, expectations={"final_state": [{"path": "/a", "around": 1}]}),
    )
    assert bad_key.status is ExecutionStatus.ERROR
    bad_calls = _evaluate(ToolOutcomes, _view([], expectations={"tool_calls": "book"}))
    assert bad_calls.status is ExecutionStatus.ERROR


def test_final_state_reports_every_assertion() -> None:
    world = {"bookings": [{"flight": "BA117"}], "seats": 1, "outbox": []}
    outcome = _evaluate(
        FinalState,
        _view(
            world=world,
            expectations={
                "final_state": [
                    {"path": "/bookings", "length": 1},
                    {"path": "/bookings/0/flight", "matches": "BA[0-9]+"},
                    {"path": "/seats", "min": 2},
                    {"path": "/refunds", "absent": True},
                ]
            },
        ),
    )
    assert outcome.value.value is False
    assert [a["ok"] for a in outcome.raw["assertions"]] == [True, True, False, True]
    assert outcome.raw["assertions"][2]["reason"] == "is 1, below 2"


@pytest.mark.parametrize(
    ("actual", "constraint", "ok"),
    [
        ("BA117", "BA117", True),
        ("BA117", {"in": ["AF22", "BA117"]}, True),
        (3, {"min": 1, "max": 3}, True),
        (True, {"min": 0}, False),  # a boolean is not a number
        (MISSING, {"present": True}, False),
        (MISSING, {"absent": True}, True),
        ([1, 2], {"length": 2}, True),
        ("x", {"not_equals": "x"}, False),
    ],
)
def test_constraints(actual: Any, constraint: Any, ok: bool) -> None:
    assert (check(actual, constraint) is None) is ok
