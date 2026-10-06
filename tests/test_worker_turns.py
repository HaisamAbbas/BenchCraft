"""Cases that share a metric's worker take turns. Found on a real run: three faithfulness cases
were evaluated at once through one worker process; the first hit its time limit, the worker was
killed, and the two cases still in line failed with "worker is not running". Each case's time
limit also counted the minutes it spent waiting in line."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import ClassVar

from aibench.core.models import (
    BenchmarkCase,
    EvaluatorManifest,
    ExecutionResult,
    ExecutionStatus,
    FieldRequirement,
    MetricBinding,
    MetricDirection,
    ReferenceAnswer,
    RunManifest,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from aibench.services.scoring import BindingScorer
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage


class FakeWorker(Evaluator):
    """Behaves like a worker process: one request at a time, killed by a cancelled call, and
    restarted by `ensure_ready`."""

    one_at_a_time: ClassVar[bool] = True
    manifest = EvaluatorManifest.model_validate(
        {
            "evaluator_id": "tests.fake_worker",
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
    slow: ClassVar[set[str]] = set()
    running = 0
    most_at_once = 0
    restarts = 0
    alive = True

    async def ensure_ready(self) -> None:
        if not FakeWorker.alive:
            FakeWorker.alive = True
            FakeWorker.restarts += 1

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        if not FakeWorker.alive:
            raise RuntimeError("worker is not running")
        FakeWorker.running += 1
        FakeWorker.most_at_once = max(FakeWorker.most_at_once, FakeWorker.running)
        try:
            await asyncio.sleep(10 if view.case.case_id in FakeWorker.slow else 0.15)
        except asyncio.CancelledError:
            FakeWorker.alive = False  # a call interrupted by its time limit kills the worker
            raise
        finally:
            FakeWorker.running -= 1
        return EvaluationOutcome.ok("scalar", 0.9)


def _reset() -> None:
    FakeWorker.slow = set()
    FakeWorker.running = FakeWorker.most_at_once = FakeWorker.restarts = 0
    FakeWorker.alive = True


def _score_three(tmp_path: Path, timeout: float) -> dict[str, ExecutionResult | object]:
    registry = EvaluatorRegistry.with_native()
    registry.register(FakeWorker)
    workspace = Workspace.at(tmp_path)
    workspace.ensure_directories()
    storage = Storage(Database.open_workspace(workspace))
    artifacts = ArtifactStore(workspace.artifacts_dir)
    storage.commit_run(
        RunManifest(run_id="turns", dataset_hash="d", application_hash="a", plan_hash="p")
    )

    async def go() -> dict[str, object]:
        scorer = BindingScorer(
            storage,
            artifacts,
            "turns",
            registry.resolve_binding(MetricBinding(metric="tests.fake_worker")),
            timeout,
            None,
        )
        await scorer.open()
        cases = [
            BenchmarkCase(case_id=name, input="q", reference=ReferenceAnswer(answer="a"))
            for name in ("a", "b", "c")
        ]
        executions = [
            ExecutionResult(
                execution_id=f"e-{case.case_id}",
                run_id="turns",
                case_id=case.case_id,
                status=ExecutionStatus.OK,
                output="x",
            )
            for case in cases
        ]
        results = await asyncio.gather(
            *(
                scorer._score_one(scorer._evaluator, execution, [case])
                for execution, case in zip(executions, cases, strict=True)
            )
        )
        await scorer.close([])
        return {r.case_id: r for r in results}

    try:
        return asyncio.run(go())  # type: ignore[return-value]
    finally:
        storage.db.close()


def test_a_timeout_does_not_fail_the_cases_waiting_for_the_same_worker(tmp_path: Path) -> None:
    _reset()
    FakeWorker.slow = {"a"}  # the first in line never finishes inside its limit
    by_case = _score_three(tmp_path, timeout=0.4)
    assert by_case["a"].status is ExecutionStatus.ERROR  # type: ignore[attr-defined]
    assert "timeout" in (by_case["a"].reason or "")  # type: ignore[attr-defined]
    for name in ("b", "c"):  # restarted worker, their own turn: scored, not "not running"
        result = by_case[name]
        assert result.status is ExecutionStatus.OK, result  # type: ignore[attr-defined]
        assert result.value.value == 0.9  # type: ignore[attr-defined]
    assert FakeWorker.restarts >= 1
    assert FakeWorker.most_at_once == 1  # one request at a time, as a worker serves them


def test_a_cases_time_limit_counts_its_own_turn_not_the_line(tmp_path: Path) -> None:
    """Three calls of 0.15 s each take 0.45 s in turn: every case fits a 0.3 s limit alone,
    but the last would have waited out the limit in line."""
    _reset()
    by_case = _score_three(tmp_path, timeout=0.3)
    assert {name: r.status for name, r in by_case.items()} == {  # type: ignore[attr-defined]
        "a": ExecutionStatus.OK,
        "b": ExecutionStatus.OK,
        "c": ExecutionStatus.OK,
    }
    assert FakeWorker.most_at_once == 1
