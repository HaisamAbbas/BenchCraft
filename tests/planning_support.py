"""Helpers for planning tests: app configs and datasets on disk, a scripted fake planner
provider (offline; it proves the harness loop, not any model's quality), and extra
evaluator manifests in a test-only `fixture.` namespace so eligibility rules can be
exercised without a plugin environment. These manifests are catalog entries only — they
are never executed and imitate no vendor package."""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
)
from aibench.core.plans import CaseSelection
from aibench.evaluators.worker_client import WorkerSpec
from aibench.inspection.dataset_summary import summarize_dataset
from aibench.inspection.profile import inspect_application
from aibench.planning.catalog import build_catalog
from aibench.planning.drafts import DraftContext
from aibench.planning.planner import ModelReply, PlannerError, PlanningInputs, ToolCall
from aibench.registry import EvaluatorRegistry
from aibench.security.policy import ExecutionPolicy

TRUSTED = ExecutionPolicy(allow_trusted_local=True)

GROUNDED = EvaluatorManifest(
    evaluator_id="fixture.grounded",
    version="1.0.0",
    plugin_id="fixture",
    plugin_version="1",
    description="Test catalog entry: answer supported by retrieved passages (model judge).",
    value_kind="scalar",
    direction=MetricDirection.HIGHER,
    aggregation="mean",
    requires=(
        FieldRequirement(path="case.input"),
        FieldRequirement(path="execution.output", non_empty=False),
        FieldRequirement(path="execution.retrieved_context"),
    ),
    default_rule=DecisionRule(comparator=">=", threshold=0.5),
    uses_models=True,
)
TOOL_NAMES = EvaluatorManifest(
    evaluator_id="fixture.tool_names",
    version="1.0.0",
    plugin_id="fixture",
    plugin_version="1",
    description="Test catalog entry: expected tool names were called.",
    value_kind="boolean",
    direction=MetricDirection.HIGHER,
    aggregation="rate",
    requires=(
        FieldRequirement(path="execution.tool_events"),
        FieldRequirement(path="case.reference.tools"),
    ),
    default_rule=DecisionRule(comparator="is_true"),
)


def write_app(
    root: Path,
    *,
    name: str = "app.json",
    application_id: str = "support-bot",
    runner: str = "http",
    output_binding: dict[str, Any] | None = None,
) -> Path:
    if runner == "http":
        transport: dict[str, Any] = {"kind": "http", "url": "http://127.0.0.1:9/answer"}
        target = "http://127.0.0.1:9/answer"
    else:
        transport = {"kind": "cli", "argv": ["python", "app.py"]}
        target = "app.py"
    config = {
        "application_id": application_id,
        "runner": runner,
        "target": target,
        "transport": transport,
        "output_binding": output_binding or {"output": "/output"},
    }
    path = root / name
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def write_dataset(root: Path, rows: Sequence[dict[str, Any]], name: str = "data.jsonl") -> Path:
    path = root / name
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def registry_with(*manifests: EvaluatorManifest) -> EvaluatorRegistry:
    """Native evaluators plus catalog-only fixture manifests. Their worker spec points at an
    interpreter that does not exist: planning and validation never start a worker, and an
    accidental start would fail loudly rather than run anything."""
    registry = EvaluatorRegistry.with_native()
    never = WorkerSpec(python=Path("fixture-interpreter-never-started"), target="fixture:none")
    for manifest in manifests:
        registry.register_external(manifest, worker=never)
    return registry


def planning_inputs(
    root: Path,
    app: Path,
    dataset: Path,
    objectives: Sequence[str],
    *,
    policy: ExecutionPolicy = TRUSTED,
    registry: EvaluatorRegistry | None = None,
    selection: CaseSelection | None = None,
    executions: Sequence[Any] = (),
) -> PlanningInputs:
    registry = registry or EvaluatorRegistry.with_native()
    profile = inspect_application(app, executions=executions)
    summary = summarize_dataset(dataset)
    context = DraftContext(
        plan_id="generated",
        out_dir=root,
        dataset=dataset,
        application=app,
        policy=policy,
        selection=selection,
        registry=registry,
        user_objectives=tuple(objectives),
    )
    catalog = build_catalog(registry, profile, summary, policy)
    return PlanningInputs(list(objectives), profile, summary, catalog, context)


Step = ModelReply | Exception | Callable[[list[dict[str, Any]]], ModelReply]


class FakeProvider:
    """Scripted planner model: replays `script` in order and records every request."""

    name = "fake"
    model = "scripted"

    def __init__(self, script: Sequence[Step]) -> None:
        self.script = list(script)
        self.requests: list[list[dict[str, Any]]] = []
        self.tools: list[list[dict[str, Any]]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        self.requests.append(copy.deepcopy(messages))
        self.tools.append(tools)
        if not self.script:
            raise PlannerError("script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages)
        return step

    def sent_text(self) -> str:
        return json.dumps(self.requests)


def call(name: str, arguments: dict[str, Any] | str, call_id: str = "c1") -> ModelReply:
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ModelReply(
        text=None,
        tool_calls=(ToolCall(call_id, name, text),),
        prompt_tokens=100,
        completion_tokens=40,
    )


def draft(
    metrics: Sequence[dict[str, Any]] = (),
    *,
    objectives: Sequence[dict[str, Any]] = (),
    gaps: Sequence[dict[str, Any]] = (),
    questions: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    return {
        "objectives": list(objectives),
        "metrics": list(metrics),
        "gaps": list(gaps),
        "questions": list(questions),
    }


def objective(oid: str, text: str, *concepts: str) -> dict[str, Any]:
    """A user objective as a planner must return it: verbatim text, source "user"."""
    return {"objective_id": oid, "text": text, "concepts": list(concepts), "source": "user"}


def metric(
    ref: str, *objective_ids: str, rationale: str = "because the evidence supports it"
) -> dict[str, Any]:
    return {"metric": ref, "objective_ids": list(objective_ids), "rationale": rationale}
