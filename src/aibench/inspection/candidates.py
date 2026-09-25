"""Bounded repository candidate validation and explicit dataset selection (Prompt 26-T2/3).

Repository paths are untrusted clues. Only JSONL files discovered in data-oriented paths
are schema-validated; test/evaluation files stay path-only candidates. Dataset values and
reference text never leave this service's local parser.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field

from aibench.core.errors import AibenchError, PolicyError
from aibench.core.models import FrozenModel, ObservationState, ReferenceStatus
from aibench.datasets.ingest import ingest_dataset
from aibench.inspection.dataset_summary import FieldCoverage, summarize_cases
from aibench.inspection.source import (
    CodebaseInspector,
    DiscoveryResult,
    SourceEvidence,
    SourceInspection,
    _is_link,
)
from aibench.security.policy import ExecutionPolicy


class CandidateValidationBudget(FrozenModel):
    """Additional strict caps for content validation of repository JSONL candidates."""

    max_candidates: int = Field(default=32, gt=0, le=100)
    max_file_bytes: int = Field(default=8 * 1024 * 1024, gt=0, le=32 * 1024 * 1024)
    max_total_bytes: int = Field(default=32 * 1024 * 1024, gt=0, le=128 * 1024 * 1024)


class RepositoryDatasetCandidate(FrozenModel):
    path: str
    state: Literal["compatible", "incompatible", "unknown"]
    provenance: ObservationState
    confidence: Literal["high", "medium", "low"]
    evidence: tuple[SourceEvidence, ...]
    content_hash: str | None = None
    case_count: int | None = None
    fields: tuple[FieldCoverage, ...] = ()
    reference_status_counts: dict[str, int] = Field(default_factory=dict)
    summary: str
    limitations: tuple[str, ...] = ()


class DatasetSelectionDecision(FrozenModel):
    state: Literal["selected", "ambiguous", "none"]
    selected_path: str | None = None
    equivalent_paths: tuple[str, ...] = ()
    content_hash: str | None = None
    question: str | None = None


class RepositoryCandidateInventory(FrozenModel):
    root: str
    budget: CandidateValidationBudget
    datasets: tuple[RepositoryDatasetCandidate, ...] = ()
    tests: tuple[DiscoveryResult, ...] = ()
    evaluators: tuple[DiscoveryResult, ...] = ()
    invocations: tuple[DiscoveryResult, ...] = ()
    skipped: dict[str, int] = Field(default_factory=dict)


_TEST_PARTS = {"test", "tests", "spec", "specs", "__tests__"}
_EVAL_PARTS = {"eval", "evals", "evaluation", "evaluations", "evaluator", "evaluators", "metrics"}


def _approved_root(root: Path, policy: ExecutionPolicy) -> Path:
    resolved = root.resolve()
    allowed = tuple(Path(item).resolve() for item in policy.inspection_roots)
    if not any(resolved == item or resolved.is_relative_to(item) for item in allowed):
        raise PolicyError(
            f"reading repository candidates under {resolved} is not approved "
            "(inspection_roots in the policy)"
        )
    if not resolved.is_dir():
        raise PolicyError(f"{resolved} is not a directory")
    return resolved


def _candidate_path(root: Path, relative: str) -> Path | None:
    pure = PurePosixPath(relative)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        return None
    candidate = root.joinpath(*pure.parts)
    try:
        for count in range(1, len(pure.parts) + 1):
            if _is_link(root.joinpath(*pure.parts[:count])):
                return None
        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
    except OSError:
        return None
    return resolved


def _inside_data_roots(path: Path, policy: ExecutionPolicy) -> bool:
    """Respect the run policy's optional dataset roots during automatic discovery."""
    roots = tuple(Path(item).resolve() for item in policy.data_roots)
    return not roots or any(path.is_relative_to(root) for root in roots)


def _dataset_candidate(
    root: Path,
    relative: str,
    budget: CandidateValidationBudget,
    *,
    remaining_bytes: int,
) -> tuple[RepositoryDatasetCandidate, int]:
    consumed = 0
    evidence = (
        SourceEvidence(
            path=relative,
            kind="dataset_candidate",
            detail="repository path candidate; contents are validated locally and not returned",
            context=ObservationState.OBSERVED.value,
        ),
    )
    path = _candidate_path(root, relative)
    if path is None:
        return (
            RepositoryDatasetCandidate(
                path=relative,
                state="unknown",
                provenance=ObservationState.UNKNOWN,
                confidence="low",
                evidence=evidence,
                summary="candidate could not be safely resolved inside the inspected root",
                limitations=("not read",),
            ),
            0,
        )
    if path.suffix.lower() != ".jsonl":
        return (
            RepositoryDatasetCandidate(
                path=relative,
                state="unknown",
                provenance=ObservationState.OBSERVED,
                confidence="low",
                evidence=evidence,
                summary="candidate format is not supported for dataset validation; JSONL only",
                limitations=("format was inventoried by path only; contents were not read",),
            ),
            0,
        )
    try:
        size = path.stat().st_size
    except OSError:
        size = -1
    if size < 0:
        reason = "candidate size could not be read"
        state: Literal["compatible", "incompatible", "unknown"] = "unknown"
    elif size > budget.max_file_bytes:
        reason = f"candidate exceeds the {budget.max_file_bytes}-byte validation limit"
        state = "unknown"
    elif size > remaining_bytes:
        reason = "candidate exceeds the remaining total validation budget"
        state = "unknown"
    else:
        consumed = size
        candidate_result: RepositoryDatasetCandidate | None = None
        try:
            report = ingest_dataset(path)
        except (AibenchError, OSError, UnicodeError):
            report = None
        if report is None:
            reason = "candidate could not be read or validated as JSONL"
            state = "incompatible"
        elif not report.is_valid or report.manifest is None:
            reason = f"JSONL case validation failed on {len(report.errors)} line(s)"
            state = "incompatible"
        elif report.manifest.case_count == 0:
            reason = "dataset contains no benchmark cases"
            state = "incompatible"
        else:
            unreviewed = sum(
                case.provenance.origin is ReferenceStatus.SYNTHETIC_UNVERIFIED
                or (
                    case.reference is not None
                    and case.reference.status is ReferenceStatus.SYNTHETIC_UNVERIFIED
                )
                for case in report.cases
            )
            if unreviewed:
                reason = (
                    f"contains {unreviewed} unreviewed generated case(s); use the explicit "
                    "candidate review and promotion workflow"
                )
                state = "incompatible"
            else:
                summary = summarize_cases(
                    report.cases,
                    dataset_id=path.stem,
                    content_hash=report.manifest.content_hash,
                )
                candidate_result = RepositoryDatasetCandidate(
                    path=relative,
                    state="compatible",
                    provenance=ObservationState.OBSERVED,
                    confidence="high",
                    evidence=evidence,
                    content_hash=summary.content_hash,
                    case_count=summary.case_count,
                    fields=summary.fields,
                    reference_status_counts=summary.reference_status,
                    summary="valid non-empty JSONL benchmark dataset; field counts only",
                    limitations=(
                        "schema validity does not verify reference correctness or objective fit",
                        "dataset references remain judge-only and are not included in this result",
                    ),
                )
        if candidate_result is not None:
            return candidate_result, consumed
    return (
        RepositoryDatasetCandidate(
            path=relative,
            state=state,
            provenance=ObservationState.OBSERVED
            if state != "unknown"
            else ObservationState.UNKNOWN,
            confidence="medium" if state == "incompatible" else "low",
            evidence=evidence,
            summary=reason,
            limitations=("candidate content was not returned",),
        ),
        consumed,
    )


def discover_repository_candidates(
    root: Path,
    policy: ExecutionPolicy,
    *,
    inspection: SourceInspection | None = None,
    budget: CandidateValidationBudget | None = None,
) -> RepositoryCandidateInventory:
    """Validate bounded JSONL candidates and return path-only test/evaluator references."""
    root = _approved_root(root, policy)
    if inspection is None:
        inspection = CodebaseInspector(policy).inspect(root)
    elif Path(inspection.root).resolve() != root:
        raise PolicyError("repository inventory root does not match the approved scan root")
    budget = budget or CandidateValidationBudget()

    discoveries = inspection.discoveries
    dataset_paths = sorted(
        {
            item.subject
            for item in discoveries
            if item.kind == "dataset_candidate"
            and not (
                {part.lower() for part in PurePosixPath(item.subject).parts[:-1]}
                & (_TEST_PARTS | _EVAL_PARTS)
            )
        }
    )
    test_paths = tuple(
        sorted(
            (item for item in discoveries if item.kind == "test_candidate"),
            key=lambda item: item.subject,
        )
    )
    evaluator_paths = tuple(
        sorted(
            (item for item in discoveries if item.kind == "evaluator_candidate"),
            key=lambda item: item.subject,
        )
    )
    invocation_paths = tuple(
        sorted(
            (item for item in discoveries if item.kind == "invocation_candidate"),
            key=lambda item: item.subject,
        )
    )

    skipped: dict[str, int] = dict(inspection.skipped)
    candidates: list[RepositoryDatasetCandidate] = []
    remaining = budget.max_total_bytes
    validated = 0
    for relative in dataset_paths:
        if validated >= budget.max_candidates:
            skipped["candidate_count_limit"] = skipped.get("candidate_count_limit", 0) + 1
            continue
        candidate_path = _candidate_path(root, relative)
        if candidate_path is not None and not _inside_data_roots(candidate_path, policy):
            candidates.append(
                RepositoryDatasetCandidate(
                    path=relative,
                    state="incompatible",
                    provenance=ObservationState.OBSERVED,
                    confidence="high",
                    evidence=(
                        SourceEvidence(
                            path=relative,
                            kind="dataset_candidate",
                            detail="repository candidate is outside policy data_roots",
                            context=ObservationState.OBSERVED.value,
                        ),
                    ),
                    summary="candidate is outside policy data_roots; content was not read",
                    limitations=("not eligible for evaluation under the current policy",),
                )
            )
            continue
        item, read_bytes = _dataset_candidate(root, relative, budget, remaining_bytes=remaining)
        candidates.append(item)
        if read_bytes:
            validated += 1
            remaining -= read_bytes
        elif item.state != "unknown" and item.path.endswith(".jsonl"):
            validated += 1
    return RepositoryCandidateInventory(
        root=str(root),
        budget=budget,
        datasets=tuple(candidates),
        tests=test_paths,
        evaluators=evaluator_paths,
        invocations=invocation_paths,
        skipped=dict(sorted(skipped.items())),
    )


def compatible_dataset_groups(
    inventory: RepositoryCandidateInventory,
) -> tuple[tuple[RepositoryDatasetCandidate, ...], ...]:
    """Group duplicate paths by exact content identity for selection prompts."""
    groups: dict[str, list[RepositoryDatasetCandidate]] = defaultdict(list)
    for item in inventory.datasets:
        if item.state == "compatible" and item.content_hash is not None:
            groups[item.content_hash].append(item)
    return tuple(
        tuple(sorted(items, key=lambda item: item.path))
        for _digest, items in sorted(groups.items())
    )


def select_unique_dataset(inventory: RepositoryCandidateInventory) -> DatasetSelectionDecision:
    """Select only a sole compatible content identity; materially different data stays ambiguous."""
    groups = compatible_dataset_groups(inventory)
    if not groups:
        return DatasetSelectionDecision(
            state="none",
            question=(
                "No compatible JSONL dataset was found in the policy-approved inspection root. "
                "Which dataset path should be used?"
            ),
        )
    if len(groups) > 1:
        distinct_paths = sorted(item.path for items in groups for item in items)
        return DatasetSelectionDecision(
            state="ambiguous",
            question=(
                "Which dataset should be used? These compatible candidates have different "
                "content: " + ", ".join(distinct_paths) + ". Choose one with --dataset."
            ),
        )
    equivalent = groups[0]
    content_hash = equivalent[0].content_hash
    assert content_hash is not None
    equivalent_paths = tuple(sorted(item.path for item in equivalent))
    return DatasetSelectionDecision(
        state="selected",
        selected_path=equivalent_paths[0],
        equivalent_paths=equivalent_paths,
        content_hash=content_hash,
    )
