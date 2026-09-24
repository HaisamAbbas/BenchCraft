"""Shared candidate review and promotion operations used by the CLI."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from uuid import uuid4

from aibench.core.errors import ConflictError, ValidationError
from aibench.core.models import (
    CandidateEvent,
    CandidateEventKind,
    CandidateStatus,
    CandidateVerification,
    DatasetCandidate,
    ReferenceStatus,
)
from aibench.datasets.candidates import (
    apply_verification,
    candidate_for_promotion,
    source_text_for_span,
    verify_source_quote,
)
from aibench.storage.repositories import CandidateTransition, Storage


def _event(
    candidate: DatasetCandidate,
    kind: CandidateEventKind,
    actor: str,
    details: object,
) -> CandidateEvent:
    return CandidateEvent(
        event_id=str(uuid4()),
        candidate_id=candidate.candidate_id,
        kind=kind,
        actor=actor,
        details=details,
    )


def _need_candidate(storage: Storage, candidate_id: str) -> DatasetCandidate:
    candidate = storage.get_candidate(candidate_id)
    if candidate is None:
        raise ValidationError(f"no candidate with candidate_id={candidate_id!r}")
    return candidate


def record_candidate_review(
    storage: Storage,
    candidate_id: str,
    *,
    reviewer: str,
    decision: str,
    note: str,
) -> DatasetCandidate:
    """Record an explicit human acceptance or rejection; never auto-promote."""
    reviewer = reviewer.strip()
    note = note.strip()
    if not reviewer or len(reviewer) > 200:
        raise ValidationError("reviewer identity must contain 1 to 200 characters")
    if not note or len(note) > 2000:
        raise ValidationError("review note must contain 1 to 2,000 characters")
    candidate = _need_candidate(storage, candidate_id)
    if candidate.status is not CandidateStatus.CANDIDATE:
        raise ConflictError(f"candidate is already {candidate.status.value}")

    if decision == "reject":
        verification = CandidateVerification(
            method="human",
            outcome="failed",
            verifier_id="human_review",
            actor=reviewer,
            detail=note,
        )
        updated = candidate.model_copy(
            update={
                "status": CandidateStatus.REJECTED,
                "verifications": (*candidate.verifications, verification),
            }
        )
        event = _event(candidate, "review_rejected", reviewer, {"note": note})
    elif decision in ("source_verified", "human_reviewed"):
        for span in candidate.source_spans:
            source_text_for_span(span)  # reviewers may not approve stale source evidence
        status = ReferenceStatus(decision)
        verification = CandidateVerification(
            method="human",
            outcome="passed",
            verifier_id=decision,
            actor=reviewer,
            detail=note,
        )
        updated = apply_verification(candidate, verification, review_status=status)
        event_kind: CandidateEventKind = (
            "reviewed_source" if decision == "source_verified" else "reviewed_human"
        )
        event = _event(candidate, event_kind, reviewer, {"decision": decision, "note": note})
    else:
        raise ValidationError("decision must be source_verified, human_reviewed, or reject")

    storage.transition_candidates([CandidateTransition(updated, candidate.status, event)])
    return updated


def record_candidate_executable_check(
    storage: Storage, candidate_id: str, *, actor: str
) -> DatasetCandidate:
    actor = actor.strip()
    if not actor or len(actor) > 200:
        raise ValidationError("actor identity must contain 1 to 200 characters")
    candidate = _need_candidate(storage, candidate_id)
    verification = verify_source_quote(candidate)
    updated = apply_verification(candidate, verification)
    event_kind: CandidateEventKind = (
        "executable_check_passed" if verification.outcome == "passed" else "executable_check_failed"
    )
    event = _event(
        candidate,
        event_kind,
        actor,
        {
            "verifier_id": verification.verifier_id,
            "outcome": verification.outcome,
            "detail": verification.detail,
        },
    )
    storage.transition_candidates([CandidateTransition(updated, candidate.status, event)])
    return updated


def _write_cases(path: Path, candidates: tuple[DatasetCandidate, ...]) -> tuple[Path, bytes]:
    if path.suffix.lower() != ".jsonl":
        raise ValidationError("promoted datasets must use a .jsonl output path")
    if path.exists():
        raise ConflictError(f"output already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "".join(
        json.dumps(item.case.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
        + "\n"
        for item in candidates
    ).encode("utf-8")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        # A same-directory hard link publishes atomically and refuses to replace a path
        # created by someone else while this command was preparing the output.
        os.link(temp, path)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp, content


def promote_candidates(
    storage: Storage,
    pool_id: str,
    candidate_ids: tuple[str, ...],
    output: Path,
    *,
    actor: str,
) -> tuple[Path, tuple[DatasetCandidate, ...]]:
    """Export only explicitly selected, reviewed candidates as ordinary JSONL cases."""
    actor = actor.strip()
    if not actor or len(actor) > 200:
        raise ValidationError("actor identity must contain 1 to 200 characters")
    if not candidate_ids:
        raise ValidationError("select at least one candidate ID to promote")
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValidationError("candidate IDs must be unique")
    pool = storage.get_candidate_pool(pool_id)
    if pool is None:
        raise ValidationError(f"no candidate pool with pool_id={pool_id!r}")
    originals = []
    for candidate_id in candidate_ids:
        candidate = _need_candidate(storage, candidate_id)
        if candidate.pool_id != pool_id:
            raise ValidationError(f"candidate {candidate_id!r} does not belong to pool {pool_id!r}")
        for span in candidate.source_spans:
            source_text_for_span(span)
        originals.append(candidate)
    candidates = tuple(candidate_for_promotion(candidate) for candidate in originals)
    temp, _ = _write_cases(output, candidates)
    transitions = [
        CandidateTransition(
            promoted,
            candidate.status,
            _event(
                candidate,
                "promoted",
                actor,
                {"pool_id": pool_id, "output": str(output.resolve())},
            ),
        )
        for candidate, promoted in zip(originals, candidates, strict=True)
    ]
    try:
        storage.transition_candidates(transitions)
    except BaseException:
        output.unlink(missing_ok=True)
        raise
    finally:
        temp.unlink(missing_ok=True)
    return output, candidates
