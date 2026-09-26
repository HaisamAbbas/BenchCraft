"""Agent traces for evaluators: the span tree built from an imported OpenTelemetry trace
(`observations.otel.span_tree`), and a metric requiring `execution.trace` receiving the tree
of its own execution's trace. No plugin is involved; the DeepEval agent metrics built on this
are in tests/test_deepeval_agentic.py."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.observations.otel import parse_otlp, span_tree
from aibench.registry import EvaluatorRegistry
from aibench.services.traces import import_traces
from tests.scoring_support import Seeded, case, execution


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def span(
    span_id: str,
    name: str,
    parent: str | None = None,
    *,
    trace_id: str = "t1",
    start: int = 0,
    error: bool = False,
    **attributes: Any,
) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "startTimeUnixNano": str(start),
        "endTimeUnixNano": str(start + 1),
        "attributes": [_attr(k.replace("__", "."), v) for k, v in attributes.items()],
    }
    if parent:
        raw["parentSpanId"] = parent
    if error:
        raw["status"] = {"code": 2}
    return raw


def otlp(*spans: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "resourceSpans": [
                {"resource": {"attributes": []}, "scopeSpans": [{"spans": list(spans)}]}
            ]
        }
    ).encode()


def _tree(*spans: dict[str, Any], **kwargs: Any) -> list[dict[str, Any]]:
    [trace] = parse_otlp(otlp(*spans))
    return span_tree(trace, **kwargs)


# --------------------------------------------------------------------------- span tree


def test_span_kinds_come_from_the_attributes_the_spans_carry() -> None:
    [root] = _tree(
        span("a", "agent.run", gen_ai__agent__name="support-agent"),
        span("b", "chat stub", "a", start=1, gen_ai__request__model="stub-1"),
        span("c", "execute_tool lookup", "a", start=2, gen_ai__operation__name="execute_tool",
             gen_ai__tool__name="lookup"),
        span("d", "search", "a", start=3, openinference__span__kind="RETRIEVER"),
        span("e", "misc", "a", start=4),
    )  # fmt: skip
    assert (root["name"], root["kind"]) == ("support-agent", "agent")
    assert [(c["name"], c["kind"]) for c in root["children"]] == [
        ("chat stub", "llm"),
        ("lookup", "tool"),
        ("search", "retriever"),
        ("misc", "other"),
    ]
    assert root["children"][0]["model"] == "stub-1"


def test_inputs_and_outputs_are_read_decoded_and_cut() -> None:
    [root] = _tree(
        span("a", "chat", gen_ai__operation__name="chat",
             gen_ai__input__messages=json.dumps([{"role": "user", "content": "hi"}]),
             gen_ai__output__messages="plain text"),
        span("b", "tool", "a", start=1, gen_ai__tool__name="lookup",
             gen_ai__tool__call__arguments='{"order": "A17"}', gen_ai__tool__call__result="x" * 50),
        span("c", "oi", "a", start=2, openinference__span__kind="LLM", input__value="question"),
        text_limit=40,
    )  # fmt: skip
    assert root["input"] == [{"role": "user", "content": "hi"}]
    assert root["output"] == "plain text"
    tool, oi = root["children"]
    assert tool["input"] == {"order": "A17"}
    assert tool["output"].startswith("x" * 40) and "[cut: 10 more characters]" in tool["output"]
    assert oi["input"] == "question" and "output" not in oi  # nothing invented


def test_children_follow_start_order_and_errors_and_several_roots_are_kept() -> None:
    roots = _tree(
        span("a", "first", start=5),
        span("b", "second child", "a", start=9, error=True),
        span("c", "first child", "a", start=7),
        span("z", "another root", start=1),
    )
    assert [r["name"] for r in roots] == ["another root", "first"]
    children = roots[1]["children"]
    assert [c["name"] for c in children] == ["first child", "second child"]
    assert children[1]["error"] is True and "error" not in children[0]


# --------------------------------------------------------------------------- scoring


class TraceRecorder(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="test.trace",
        version="1.0.0",
        plugin_id="test",
        plugin_version="1",
        description="records the trace it is given",
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=(FieldRequirement(path="execution.trace"),),
        default_rule=DecisionRule(comparator=">=", threshold=0.0),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        tree = view.get("execution.trace")
        return EvaluationOutcome.ok("scalar", float(len(tree["spans"])), raw=tree)


@pytest.fixture
def scored(tmp_path: Path) -> tuple[Seeded, dict[str, Any]]:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("traced"), case("partial"), case("untraced")],
        [
            execution("traced", "ok", correlation_id="req-1"),
            execution("partial", "ok", correlation_id="req-2"),
            execution("untraced", "ok", correlation_id="req-3"),
        ],
    )
    export = tmp_path / "traces.json"
    export.write_bytes(
        otlp(
            span("a", "agent.run", aibench__correlation_id="req-1"),
            span("b", "execute_tool lookup", "a", start=1, gen_ai__tool__name="lookup"),
            # A trace whose root was not exported: partial.
            span("x", "chat", "missing-root", trace_id="t2", aibench__correlation_id="req-2"),
        )
    )
    summary = import_traces(seeded.storage, seeded.artifacts, "run-1", export)
    assert summary["matched"] == 2 and summary["partial"] == 1
    registry = EvaluatorRegistry.with_native()
    registry.register(TraceRecorder)
    report = seeded.score([{"metric": "test.trace"}], registry=registry)
    return seeded, {r.case_id: r for r in report.results}


def test_a_metric_gets_the_span_tree_of_its_own_executions_trace(
    scored: tuple[Seeded, dict[str, Any]],
) -> None:
    seeded, results = scored
    traced = results["traced"]
    assert traced.status is ExecutionStatus.OK
    tree = json.loads(
        seeded.artifacts.read_bytes(seeded.storage.get_artifact(traced.raw_artifact_ref))
    )
    assert tree["format"] == "aibench-span-tree/1"
    [root] = tree["spans"]
    assert root["name"] == "agent.run" and [c["name"] for c in root["children"]] == ["lookup"]


def test_missing_and_partial_traces_are_not_applicable(
    scored: tuple[Seeded, dict[str, Any]],
) -> None:
    _, results = scored
    untraced, partial = results["untraced"], results["partial"]
    assert (untraced.status, untraced.reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "missing:execution.trace",
    )
    assert partial.status is ExecutionStatus.NOT_APPLICABLE
    assert (partial.reason or "").startswith("trace_partial:missing_parent")


# --------------------------------------------------------------------------- time limits


def _slow(evaluator_id: str, *, uses_models: bool) -> type[Evaluator]:
    import asyncio

    class Slow(Evaluator):
        manifest = EvaluatorManifest(
            evaluator_id=evaluator_id,
            version="1.0.0",
            plugin_id="test",
            plugin_version="1",
            description="takes a moment",
            value_kind="scalar",
            direction=MetricDirection.HIGHER,
            aggregation="mean",
            uses_models=uses_models,
            default_rule=DecisionRule(comparator=">=", threshold=0.0),
        )

        async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
            await asyncio.sleep(0.3)
            ctx.report_usage(provider="p", calls=1)
            return EvaluationOutcome.ok("scalar", 1.0)

    return Slow


def test_model_judged_metrics_get_their_own_longer_time_limit(tmp_path: Path) -> None:
    """A judge model takes far longer than a local check (GLM-4.6 took about a minute per
    case): model-backed metrics get `model_timeout_seconds`, others `timeout_seconds`."""
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "ok")])
    registry = EvaluatorRegistry.with_native()
    registry.register(_slow("test.slow_local", uses_models=False))
    registry.register(_slow("test.slow_judge", uses_models=True))
    report = seeded.score(
        [{"metric": "test.slow_local"}, {"metric": "test.slow_judge"}],
        registry=registry,
        timeout_seconds=0.1,
        model_timeout_seconds=5,
    )
    by_metric = {r.metric_id: r for r in report.results}
    assert by_metric["test.slow_judge"].status is ExecutionStatus.OK
    local = by_metric["test.slow_local"]
    assert local.status is ExecutionStatus.ERROR and (local.reason or "").startswith("timeout:")


def test_plans_give_judged_metrics_five_minutes_by_default() -> None:
    from aibench.core.plans import ExecutablePlan

    plan = ExecutablePlan.model_validate(
        {"plan_id": "p", "dataset": "d.jsonl", "application": "a.json"}
    )
    assert (plan.evaluation_timeout_seconds, plan.model_evaluation_timeout_seconds) == (60.0, 300.0)
