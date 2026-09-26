"""Dataset field coverage for planning (07-T1).

A summary carries *counts* only: which evaluation-view fields exist in how many cases.
Inputs, reference answers, expectations and metadata *values* never appear, so a planner
(and any model behind it) sees the dataset's shape, not its hidden labels (§6: "The planner
receives schema summaries ..., not unrestricted hidden labels").

Dataset hints are recorded as `inferred` claims with their limitation spelled out — e.g.
reference context suggests a retrieval task, but is not observed retrieval (§6, §8: "A RAG
label alone does not make faithfulness computable").
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import Path

from aibench.core.errors import AibenchError
from aibench.core.models import BenchmarkCase, FrozenModel, ObservationClaim, ObservationState
from aibench.datasets.ingest import ingest_dataset
from aibench.evaluators.protocol import EvaluationView

SUMMARY_SCHEMA_VERSION = "1.0.0"
_BASE_PATHS = (
    "case.input",
    "case.reference.answer",
    "case.reference.context",
    "case.reference.tools",
)


class FieldCoverage(FrozenModel):
    path: str
    present: int
    empty: int
    missing: int


class DatasetSummary(FrozenModel):
    schema_version: str = SUMMARY_SCHEMA_VERSION
    dataset_id: str
    content_hash: str
    case_count: int
    fields: tuple[FieldCoverage, ...]
    reference_status: dict[str, int]
    inferred: tuple[ObservationClaim, ...] = ()
    notes: tuple[str, ...] = ()

    def present(self, path: str) -> int:
        entry = next((f for f in self.fields if f.path == path), None)
        return entry.present if entry else 0

    def usable(self, path: str, *, non_empty: bool = True) -> int:
        entry = next((f for f in self.fields if f.path == path), None)
        if entry is None:
            return 0
        return entry.present + (0 if non_empty else entry.empty)


def _paths(cases: list[BenchmarkCase]) -> list[str]:
    extra: set[str] = set()
    for case in cases:
        extra.update(f"case.expectations.{key}" for key in case.expectations)
        extra.update(f"case.metadata.{key}" for key in case.metadata)
        extra.update(f"case.fixtures.{fixture.name}" for fixture in case.fixtures)
        if case.group_id is not None:
            extra.add("case.group_id")  # only datasets with episodes gain this field
    return [*_BASE_PATHS, *sorted(extra)]


def summarize_cases(
    cases: list[BenchmarkCase],
    *,
    dataset_id: str,
    content_hash: str,
    warnings: Iterable[str] = (),
) -> DatasetSummary:
    fields = []
    for path in _paths(cases):
        states = Counter(EvaluationView.case_state(case, path) for case in cases)
        fields.append(
            FieldCoverage(
                path=path,
                present=states["present"],
                empty=states["empty"],
                missing=states["missing"],
            )
        )
    statuses = Counter(
        (case.provenance.origin.value if case.provenance else "unspecified") for case in cases
    )
    summary = DatasetSummary(
        dataset_id=dataset_id,
        content_hash=content_hash,
        case_count=len(cases),
        fields=tuple(fields),
        reference_status=dict(sorted(statuses.items())),
        notes=tuple(dict.fromkeys(warnings)),
    )
    return summary.model_copy(update={"inferred": _inferred(summary)})


def _inferred(summary: DatasetSummary) -> tuple[ObservationClaim, ...]:
    claims = []
    context = summary.present("case.reference.context")
    if context:
        claims.append(
            ObservationClaim(
                observation_id=f"{summary.dataset_id}:retrieval_task",
                capability="retrieval_task",
                state=ObservationState.INFERRED,
                evidence_refs=("dataset:case.reference.context",),
                method="dataset_fields",
                scope=f"{context} of {summary.case_count} cases have reference context",
                limitations=(
                    "reference context is judge-only; it is not observed retrieval and cannot "
                    "stand in for what the application actually retrieved"
                ),
            )
        )
    tools = summary.present("case.reference.tools")
    if tools:
        claims.append(
            ObservationClaim(
                observation_id=f"{summary.dataset_id}:tool_use_task",
                capability="tool_use_task",
                state=ObservationState.INFERRED,
                evidence_refs=("dataset:case.reference.tools",),
                method="dataset_fields",
                scope=f"{tools} of {summary.case_count} cases list expected tools",
                limitations=(
                    "expected tools say what a reviewer expects, not what the application "
                    "exposes; tool checks need observed tool events"
                ),
            )
        )
    return tuple(claims)


def summarize_dataset(path: Path) -> DatasetSummary:
    """Validate and summarize a dataset file; raises `AibenchError` if it is invalid."""
    report = ingest_dataset(path)
    if not report.is_valid or report.manifest is None:
        errors = "; ".join(str(e) for e in report.errors[:5])
        raise AibenchError(f"dataset {path} is invalid: {errors}")
    return summarize_cases(
        report.cases,
        dataset_id=report.manifest.dataset_id,
        content_hash=report.manifest.content_hash,
        warnings=[str(w) for w in report.warnings],
    )
