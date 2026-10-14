"""Scoring is concurrent. A rescore of 90 judged evaluations on LightRAG took 56 minutes, as
long as all of them end to end: it scored one metric after another and one case after
another whatever the plan allowed, and a metric's single worker process took one case at a
time even in a run. A rescore now scores its metrics and their cases at once within the
plan's evaluation limit, and a metric starts more workers within a budget shared by the
pass, taking over the idle extra workers of metrics that have finished."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, ClassVar

from aibench.core.models import (
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricBinding,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from aibench.services.scoring import BindingScorer, WorkerBudget
from tests.scoring_support import RUN_ID, Seeded, case, execution


def _manifest(evaluator_id: str) -> EvaluatorManifest:
    return EvaluatorManifest.model_validate(
        {
            "evaluator_id": evaluator_id,
            "version": "1.0.0",
            "plugin_id": "tests",
            "plugin_version": "0",
            "description": "a stand-in for a worker process",
            "value_kind": "scalar",
            "direction": MetricDirection.HIGHER,
            "aggregation": "mean",
            "requires": (FieldRequirement(path="execution.output", non_empty=False),),
            "default_rule": {"comparator": ">=", "threshold": 0.5},
        }
    )


class ProcessLike(Evaluator):
    """Like a worker process: one request at a time per instance (a second one at once is
    a failure), and each instance is a process that was started."""

    one_at_a_time: ClassVar[bool] = True
    manifest = _manifest("tests.process_like")
    seconds: ClassVar[float] = 0.3
    started: ClassVar[int] = 0
    closed: ClassVar[int] = 0
    running: ClassVar[int] = 0
    most_at_once: ClassVar[int] = 0

    def __init__(self) -> None:
        super().__init__()
        self.busy = False

    async def prepare(self, params: Mapping[str, Any]) -> None:
        # Started, as a worker process is: the registry also constructs one to validate.
        await super().prepare(params)
        type(self).started += 1

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        if self.busy:
            return EvaluationOutcome.error("two requests at once in one worker")
        self.busy = True
        cls = type(self)
        cls.running += 1
        cls.most_at_once = max(cls.most_at_once, cls.running)
        try:
            await asyncio.sleep(cls.seconds)
        finally:
            cls.running -= 1
            self.busy = False
        return EvaluationOutcome.ok("scalar", 0.9)

    async def close(self) -> None:
        type(self).closed += 1


class Quick(ProcessLike):
    manifest = _manifest("tests.quick")
    seconds = 0.05


class Slow(ProcessLike):
    manifest = _manifest("tests.slow")
    seconds = 0.3


def _reset(*classes: type[ProcessLike]) -> None:
    for cls in classes:
        cls.started = cls.closed = cls.running = cls.most_at_once = 0


def _seeded(tmp_path: Path, n: int) -> Seeded:
    seeded = Seeded(tmp_path)
    names = [f"c{i}" for i in range(n)]
    seeded.seed([case(name, "a") for name in names], [execution(name, "x") for name in names])
    return seeded


def _registry(*classes: type[ProcessLike]) -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    for cls in classes:
        registry.register(cls)
    return registry


def test_a_rescore_scores_a_metrics_cases_at_once_with_a_worker_each(tmp_path: Path) -> None:
    _reset(ProcessLike)
    seeded = _seeded(tmp_path, 6)
    started = time.perf_counter()
    report = seeded.score(
        [{"metric": "tests.process_like"}],
        registry=_registry(ProcessLike),
        evaluation_concurrency=3,
    )
    took = time.perf_counter() - started
    assert [r.status for r in report.results] == [ExecutionStatus.OK] * 6, [
        r.reason for r in report.results
    ]
    assert [r.case_id for r in report.results] == [f"c{i}" for i in range(6)]  # dataset order
    assert ProcessLike.most_at_once == 3  # the plan's limit, not one at a time
    assert ProcessLike.started == 3  # a worker for each case in flight, no more
    assert ProcessLike.closed == 3  # and every one is closed with the pass
    assert took < 6 * ProcessLike.seconds * 0.8, took  # two rounds of three, not six turns


def test_one_at_a_time_without_a_limit_set_by_the_plan(tmp_path: Path) -> None:
    _reset(ProcessLike)
    seeded = _seeded(tmp_path, 3)
    report = seeded.score(
        [{"metric": "tests.process_like"}],
        registry=_registry(ProcessLike),
        evaluation_concurrency=1,
    )
    assert [r.status for r in report.results] == [ExecutionStatus.OK] * 3
    assert ProcessLike.most_at_once == 1 and ProcessLike.started == 1


def test_metrics_are_scored_at_once_and_a_busy_one_takes_over_idle_workers(
    tmp_path: Path,
) -> None:
    """Quick finishes its cases early; its idle extra worker's slot goes to Slow, whose cases
    are still waiting. The number of processes never exceeds the pass's limit."""
    _reset(Quick, Slow)
    seeded = _seeded(tmp_path, 8)
    report = seeded.score(
        [{"metric": "tests.quick"}, {"metric": "tests.slow"}],
        registry=_registry(Quick, Slow),
        evaluation_concurrency=4,
    )
    assert all(r.status is ExecutionStatus.OK for r in report.results), [
        r.reason for r in report.results
    ]
    assert [s.metric_id for s in report.summaries] == ["tests.quick", "tests.slow"]
    assert Quick.most_at_once + Slow.most_at_once > 2  # both metrics ran side by side
    assert Slow.most_at_once >= 3  # it got the workers Quick no longer needed
    # Each metric's first worker plus three extra at most (the limit of four in flight).
    alive_at_most = 2 + 3
    assert Quick.started + Slow.started - Quick.closed - Slow.closed == 0
    assert max(Quick.started, Slow.started) <= alive_at_most


def test_the_budget_lends_an_idle_extra_worker_to_another_metric() -> None:
    class Owner:
        def __init__(self) -> None:
            self.retired: list[object] = []

        def _retire(self, evaluator: object) -> None:
            self.retired.append(evaluator)

    budget = WorkerBudget(1)
    first, second = Owner(), Owner()
    assert budget.take(first)  # type: ignore[arg-type]
    assert not budget.take(second)  # type: ignore[arg-type]  # nothing free, nothing idle
    worker = object()
    budget.idle(worker, first)  # type: ignore[arg-type]
    assert not budget.take(first)  # type: ignore[arg-type]  # its own idle worker is not lent
    assert budget.take(second)  # type: ignore[arg-type]
    assert first.retired == [worker]
    budget.give()
    assert budget.take(second)  # type: ignore[arg-type]


def test_a_run_scores_a_metrics_cases_with_more_than_one_worker(tmp_path: Path) -> None:
    """The engine's scorers share a budget too: three cases in flight for one metric use
    three workers, never one worker twice at once."""
    _reset(ProcessLike)
    seeded = _seeded(tmp_path, 3)
    registry = _registry(ProcessLike)
    metric = registry.resolve_binding(MetricBinding(metric="tests.process_like"))

    async def go() -> list[ExecutionStatus]:
        scorer = BindingScorer(
            seeded.storage,
            seeded.artifacts,
            "pass",
            metric,
            5.0,
            None,
            workers=WorkerBudget(2),
        )
        await scorer.open()
        executions = [execution(f"c{i}", "x") for i in range(3)]
        results = await asyncio.gather(
            *(
                scorer._score_one(scorer._evaluator, item, [case(item.case_id, "a")])
                for item in executions
            )
        )
        await scorer.close([])
        return [r.status for r in results]

    assert asyncio.run(go()) == [ExecutionStatus.OK] * 3
    assert ProcessLike.most_at_once == 3 and ProcessLike.started == 3
    assert ProcessLike.closed == 3
    assert RUN_ID  # the seeded run the executions belong to
