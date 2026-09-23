"""Helpers for scoring tests: seed a real storage with a run, Golden cases and recorded
executions, exactly as a smoke or engine run would leave them."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from aibench.core.hashes import content_hash
from aibench.core.models import (
    ApplicationSpec,
    BenchmarkCase,
    DatasetManifest,
    ExecutionResult,
    ExecutionStatus,
    MetricBinding,
    ReferenceAnswer,
    RunManifest,
)
from aibench.registry import EvaluatorRegistry
from aibench.services.scoring import ScoringReport, score_recorded_run
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database
from aibench.storage.repositories import Storage
from tests.runner_support import run

RUN_ID = "run-1"


def case(case_id: str, answer: str | None = None, **fields: Any) -> BenchmarkCase:
    reference = ReferenceAnswer(answer=answer) if answer is not None else None
    return BenchmarkCase(
        case_id=case_id, input=f"question {case_id}", reference=reference, **fields
    )


def execution(
    case_id: str,
    output: Any = "x",
    *,
    status: ExecutionStatus = ExecutionStatus.OK,
    attempt_id: int = 0,
    repetition_id: int = 0,
    **fields: Any,
) -> ExecutionResult:
    return ExecutionResult(
        execution_id=ExecutionResult.build_id(RUN_ID, case_id, repetition_id, attempt_id),
        run_id=RUN_ID,
        case_id=case_id,
        repetition_id=repetition_id,
        attempt_id=attempt_id,
        status=status,
        output=output if status is ExecutionStatus.OK else None,
        **fields,
    )


class Seeded:
    def __init__(self, tmp_path: Path) -> None:
        self.storage = Storage(Database.open_in_memory())
        self.artifacts = ArtifactStore(tmp_path / "artifacts")

    def seed(
        self,
        cases: Sequence[BenchmarkCase],
        executions: Sequence[ExecutionResult],
        *,
        application: ApplicationSpec | None = None,
    ) -> None:
        dataset_hash = content_hash([c.case_id for c in cases])
        self.storage.commit_dataset(
            DatasetManifest(dataset_id="d", content_hash=dataset_hash, case_count=len(cases))
        )
        self.storage.commit_cases(dataset_hash, cases)
        if application is not None:
            self.storage.commit_application(application)
        self.storage.commit_run(
            RunManifest(
                run_id=RUN_ID,
                dataset_hash=dataset_hash,
                application_hash="app",
                plan_hash="plan",
                application_id=application.application_id if application else None,
            )
        )
        for item in executions:
            self.storage.commit_execution_attempt(item)

    def score(
        self,
        bindings: Sequence[MetricBinding | dict[str, Any]],
        *,
        registry: EvaluatorRegistry | None = None,
        **kwargs: Any,
    ) -> ScoringReport:
        return run(
            score_recorded_run(
                storage=self.storage,
                artifacts=self.artifacts,
                registry=registry or EvaluatorRegistry.with_native(),
                run_id=RUN_ID,
                bindings=[
                    b if isinstance(b, MetricBinding) else MetricBinding.model_validate(b)
                    for b in bindings
                ],
                **kwargs,
            )
        )
