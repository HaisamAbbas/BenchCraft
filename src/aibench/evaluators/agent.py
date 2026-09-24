"""Agent outcome evaluators (15-T2): tool names, tool outcomes and the final world state are
three separate metrics, so a correct tool name can never stand in for a failed effect or
a wrong end state (15-G3).

- `native.tool_calls`: were the expected tools called, by name? Nothing about success.
- `native.tool_outcomes`: did each required call happen with arguments that satisfy the
  case's constraints, succeed and stay within the allowed tools?
- `native.final_state`: does the test world's state after the case satisfy the case's
  assertions?

Tool events are whatever the application reports (`output_binding.tool_events`). Events in
the OpenAI tool-call shape are *requests* by a model, never executed effects. Status
discipline is that of the native evaluators: a wrong outcome is `ok` with a failing value;
missing evidence is `not_applicable`; a malformed expectation is `error`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from aibench import __version__
from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
    ToolMatchMode,
)
from aibench.evaluators.native import NATIVE_PLUGIN_ID
from aibench.evaluators.protocol import (
    MISSING,
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.runners.bindings import MISSING as POINTER_MISSING
from aibench.runners.bindings import resolve_pointer

# --------------------------------------------------------------------------- tool events

_STATUS = {
    "ok": "ok", "success": "ok", "succeeded": "ok", "completed": "ok", "done": "ok",
    "error": "error", "failed": "error", "failure": "error", "exception": "error",
    "denied": "denied", "unauthorized": "denied", "forbidden": "denied", "rejected": "denied",
    "requested": "requested", "pending": "requested",
}  # fmt: skip


@dataclass(frozen=True)
class ToolAttempt:
    """One reported tool event, normalized. `status` is ok, error, denied, requested (asked
    for, not executed) or unknown (the event does not say)."""

    index: int
    name: str | None
    arguments: Any
    status: str
    result: Any = None


def parse_tool_events(events: Any) -> list[ToolAttempt]:
    """Read the shapes applications commonly report: `{name|tool, arguments|args|input,
    status, result, error}` and OpenAI tool calls `{type: function, function: {name,
    arguments}}` (a JSON string of arguments is decoded)."""
    attempts = []
    for index, event in enumerate(events if isinstance(events, list) else []):
        if not isinstance(event, dict):
            attempts.append(ToolAttempt(index, None, None, "unknown"))
            continue
        raw_function = event.get("function")
        function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
        name = event.get("name") or event.get("tool") or function.get("name")
        arguments = next(
            (event[k] for k in ("arguments", "args", "input") if k in event),
            function.get("arguments"),
        )
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        if "status" in event:
            status = _STATUS.get(str(event["status"]).lower(), "unknown")
        elif event.get("error"):
            status = "error"
        elif function:
            status = "requested"  # a model's tool-call request, not an executed effect
        elif "result" in event:
            status = "ok"
        else:
            status = "unknown"
        attempts.append(
            ToolAttempt(index, name if isinstance(name, str) else None, arguments, status,
                        event.get("result"))
        )  # fmt: skip
    return attempts


# --------------------------------------------------------------------------- constraints

_CONSTRAINT_KEYS = frozenset({"equals", "not_equals", "in", "matches", "min", "max", "length",
                              "present", "absent"})  # fmt: skip


class ExpectationError(ValueError):
    """A case expectation is malformed (an evaluator input problem, reported as error)."""


def _lookup(document: Any, path: str) -> Any:
    """The value at a JSON pointer or top-level key; the view's MISSING when absent."""
    if path.startswith("/") or path == "":
        found = resolve_pointer(document, path)
        return MISSING if found is POINTER_MISSING else found
    if isinstance(document, dict) and path in document:
        return document[path]
    return MISSING


def check(actual: Any, constraint: Any) -> str | None:
    """None when `actual` satisfies `constraint`, else why not. A non-object constraint
    means equality. `actual` is MISSING when the path does not exist."""
    if not isinstance(constraint, dict) or not constraint.keys() & _CONSTRAINT_KEYS:
        constraint = {"equals": constraint}
    unknown = set(constraint) - _CONSTRAINT_KEYS
    if unknown:
        raise ExpectationError(f"unknown constraint keys {sorted(unknown)}")
    for key in ("absent", "present"):
        if key in constraint and not isinstance(constraint[key], bool):
            raise ExpectationError(f"'{key}' needs true or false")
    must_be_absent = constraint.get("absent") is True or constraint.get("present") is False
    if must_be_absent:
        return None if actual is MISSING else "present, expected absent"
    if actual is MISSING:
        return "missing"
    if "equals" in constraint and not same(actual, constraint["equals"]):
        return f"is {_short(actual)}, expected {_short(constraint['equals'])}"
    if "not_equals" in constraint and same(actual, constraint["not_equals"]):
        return f"is {_short(actual)}, which is not allowed"
    if "in" in constraint:
        options = constraint["in"]
        if not isinstance(options, list):
            raise ExpectationError("'in' needs a list")
        if not any(same(actual, option) for option in options):
            return f"is {_short(actual)}, expected one of {_short(options)}"
    if "matches" in constraint:
        try:
            pattern = re.compile(str(constraint["matches"]))
        except re.error as exc:
            raise ExpectationError(f"invalid 'matches' pattern: {exc}") from exc
        if not isinstance(actual, str) or not pattern.fullmatch(actual):
            return f"is {_short(actual)}, does not match {constraint['matches']!r}"
    for key, fails in (("min", lambda a, b: a < b), ("max", lambda a, b: a > b)):
        if key in constraint:
            bound = constraint[key]
            if isinstance(bound, bool) or not isinstance(bound, int | float):
                raise ExpectationError(f"'{key}' needs a number")
            if isinstance(actual, bool) or not isinstance(actual, int | float):
                return f"is {_short(actual)}, not a number"
            if fails(actual, bound):
                return f"is {actual}, {'below' if key == 'min' else 'above'} {bound}"
    if "length" in constraint:
        if not isinstance(actual, list | dict | str):
            return f"is {_short(actual)}, which has no length"
        if len(actual) != constraint["length"]:
            return f"has length {len(actual)}, expected {constraint['length']}"
    return None


def same(a: Any, b: Any) -> bool:
    """JSON equality: `true` is not `1`, `false` is not `0`, but `1` equals `1.0`."""
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(same(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return bool(a == b)


def _short(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= 80 else text[:77] + "..."


def _arguments_problems(arguments: Any, constraints: Any) -> list[str]:
    if constraints in (None, {}):
        return []
    if not isinstance(constraints, dict):
        raise ExpectationError("tool call 'arguments' must be an object of constraints")
    problems = []
    for path, constraint in constraints.items():
        reason = check(_lookup(arguments, path), constraint)
        if reason is not None:
            problems.append(f"{path} {reason}")
    return problems


# --------------------------------------------------------------------------- evaluators

_TOOL_EVENTS = FieldRequirement(path="execution.tool_events", non_empty=False)


class ToolCalls(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="native.tool_calls",
        version="1.0.0",
        plugin_id=NATIVE_PLUGIN_ID,
        plugin_version=__version__,
        description=(
            "The expected tools were called, by name (reference.tools with contains_all, "
            "exact or ordered_subsequence)."
        ),
        limitations=(
            (
                "Names only: says nothing about arguments, success, authorization or the end "
                "state. Pair it with native.tool_outcomes and native.final_state."
            ),
            "Tool events are as reported by the application.",
        ),
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(_TOOL_EVENTS, FieldRequirement(path="case.reference.tools")),
        default_rule=DecisionRule(comparator="is_true"),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        attempts = parse_tool_events(view.get("execution.tool_events"))
        expected_spec = view.get("case.reference.tools")
        expected = list(expected_spec.get("tool_names") or [])
        mode = ToolMatchMode(expected_spec.get("match_mode", ToolMatchMode.CONTAINS_ALL.value))
        called = [a.name for a in attempts]
        if mode is ToolMatchMode.EXACT:
            matched = called == expected
        elif mode is ToolMatchMode.ORDERED_SUBSEQUENCE:
            remaining = iter(called)
            matched = all(any(name == want for name in remaining) for want in expected)
        else:
            matched = set(expected) <= set(called)
        return EvaluationOutcome.ok(
            "boolean",
            matched,
            evidence=("execution.tool_events", "case.reference.tools"),
            raw={"called": called, "expected": expected, "match_mode": mode.value},
        )


class ToolOutcomes(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="native.tool_outcomes",
        version="1.0.0",
        plugin_id=NATIVE_PLUGIN_ID,
        plugin_version=__version__,
        description=(
            "Each required tool call (expectations.tool_calls) happened with arguments that "
            "satisfy its constraints and succeeded, and no tool outside "
            "expectations.allowed_tools was attempted."
        ),
        limitations=(
            (
                "Success is as reported in the tool events; the world's state is checked by "
                "native.final_state."
            ),
            "A model's tool-call request is 'not_executed', never a success.",
        ),
        value_kind="category",
        direction=MetricDirection.NONE,
        aggregation="category_counts",
        requires=(_TOOL_EVENTS, FieldRequirement(path="case.expectations.tool_calls")),
        default_rule=DecisionRule(comparator="in", categories=("succeeded",)),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        attempts = parse_tool_events(view.get("execution.tool_events"))
        required = view.get("case.expectations.tool_calls")
        allowed = view.get("case.expectations.allowed_tools")
        evidence = ("execution.tool_events", "case.expectations.tool_calls")
        try:
            if not isinstance(required, list):
                raise ExpectationError("expectations.tool_calls must be a list")
            unauthorized = []
            if allowed is not MISSING:
                if not isinstance(allowed, list):
                    raise ExpectationError("expectations.allowed_tools must be a list")
                unauthorized = [a.name for a in attempts if a.name not in allowed]
            calls = [self._judge(attempts, spec) for spec in required]
        except ExpectationError as exc:
            return EvaluationOutcome.error(f"invalid_expectation: {exc}")
        verdicts = [c["verdict"] for c in calls]
        category = (
            "unauthorized_attempt"
            if unauthorized
            else next((v for v in verdicts if v != "succeeded"), "succeeded")
        )
        return EvaluationOutcome.ok(
            "category",
            category,
            evidence=evidence,
            raw={"calls": calls, "unauthorized": unauthorized},
        )

    def _judge(self, attempts: list[ToolAttempt], spec: Any) -> dict[str, Any]:
        if not isinstance(spec, dict) or not isinstance(spec.get("tool"), str):
            raise ExpectationError("each expected tool call needs a 'tool' name")
        tool = spec["tool"]
        candidates = [a for a in attempts if a.name == tool]
        if not candidates:
            return {"tool": tool, "verdict": "not_called"}
        problems = {
            a.index: _arguments_problems(a.arguments, spec.get("arguments")) for a in candidates
        }
        matching = [a for a in candidates if not problems[a.index]]
        if not matching:
            return {
                "tool": tool,
                "verdict": "argument_violation",
                "problems": [p for a in candidates for p in problems[a.index]],
            }
        statuses = {a.status for a in matching}
        if "ok" in statuses:
            verdict = "succeeded"
        elif statuses == {"requested"}:
            verdict = "not_executed"
        elif "denied" in statuses:
            verdict = "denied"
        else:
            verdict = "failed"
        return {"tool": tool, "verdict": verdict, "statuses": sorted(statuses)}


class FinalState(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="native.final_state",
        version="1.0.0",
        plugin_id=NATIVE_PLUGIN_ID,
        plugin_version=__version__,
        description=(
            "The test world's state after the case satisfies every assertion in "
            "expectations.final_state ({path, equals|not_equals|in|matches|min|max|length|"
            "present|absent})."
        ),
        limitations=(
            (
                "The world state is as the application or its test world reports it "
                "(output_binding.world_state)."
            ),
            "Checks the declared assertions only, not every field of the state.",
        ),
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(
            FieldRequirement(path="execution.world_state", non_empty=False),
            FieldRequirement(path="case.expectations.final_state"),
        ),
        default_rule=DecisionRule(comparator="is_true"),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        state = view.get("execution.world_state")
        assertions = view.get("case.expectations.final_state")
        if not isinstance(assertions, list) or not assertions:
            return EvaluationOutcome.error(
                "invalid_expectation: expectations.final_state must be a non-empty list"
            )
        results = []
        try:
            for assertion in assertions:
                if not isinstance(assertion, dict) or not isinstance(assertion.get("path"), str):
                    raise ExpectationError("each assertion needs a 'path'")
                constraint = {k: v for k, v in assertion.items() if k != "path"}
                if not constraint:
                    raise ExpectationError(f"assertion on {assertion['path']!r} states nothing")
                unknown = set(constraint) - _CONSTRAINT_KEYS
                if unknown:
                    raise ExpectationError(f"unknown assertion keys {sorted(unknown)}")
                reason = check(_lookup(state, assertion["path"]), constraint)
                results.append({"path": assertion["path"], "ok": reason is None, "reason": reason})
        except ExpectationError as exc:
            return EvaluationOutcome.error(f"invalid_expectation: {exc}")
        return EvaluationOutcome.ok(
            "boolean",
            all(r["ok"] for r in results),
            evidence=("execution.world_state", "case.expectations.final_state"),
            raw={"assertions": results},
        )


AGENT_EVALUATORS: tuple[type[Evaluator], ...] = (ToolCalls, ToolOutcomes, FinalState)
