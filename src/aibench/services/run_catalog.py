"""Validated annotations and approved named-baseline operations for run history."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aibench.core.errors import AibenchError
from aibench.security.redaction import sanitize
from aibench.services.reports import build_report
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import RunBaseline, Storage

_LABEL_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_MAX_NOTE_CHARS = 2_000


class RunCatalogError(AibenchError):
    """A run annotation or baseline action is invalid or not allowed."""


def normalize_tag(tag: str) -> str:
    value = tag.strip().casefold()
    if not _LABEL_RE.fullmatch(value):
        raise RunCatalogError("tag must start with a letter or digit and contain only letters, digits, '.', '_' or '-' (max 64 characters)")
    return value


def normalize_baseline_alias(alias: str) -> str:
    value = alias.strip().casefold()
    if not _LABEL_RE.fullmatch(value):
        raise RunCatalogError("baseline alias must start with a letter or digit and contain only letters, digits, '.', '_' or '-' (max 64 characters)")
    return value


def normalize_note(note: str) -> str:
    value = sanitize(note).strip()
    if not value:
        raise RunCatalogError("run note cannot be empty; use --clear to remove it")
    if len(value) > _MAX_NOTE_CHARS:
        raise RunCatalogError(f"run note must be at most {_MAX_NOTE_CHARS} characters")
    return value


def _approver(value: str) -> str:
    approver = sanitize(value).strip()
    if not approver or len(approver) > 200:
        raise RunCatalogError("--approved-by must be between 1 and 200 characters")
    return approver


@dataclass(frozen=True)
class PromotionResult:
    baseline: RunBaseline
    changed: bool
    outcome: dict[str, Any]


def promote_approved_baseline(
    storage: Storage,
    artifacts: ArtifactStore,
    alias: str,
    run_id: str,
    *,
    approved_by: str,
) -> PromotionResult:
    """Promote only a completed, healthy run with every declared gate passing.

    The caller supplies the human approver identity explicitly. Existing execution-policy
    approvals authorize dispatch; they are not treated as quality approval for a baseline.
    """
    alias = normalize_baseline_alias(alias)
    approver = _approver(approved_by)
    record = storage.get_run(run_id)
    if record is None:
        raise RunCatalogError(f"no run committed with run_id={run_id!r}")
    if record.status != "completed":
        raise RunCatalogError(
            f"run {run_id!r} is {record.status}; only completed runs can become baselines"
        )
    report = build_report(storage, artifacts, run_id, include_content=False)
    outcome = report["outcome"]
    if outcome["complete"] is not True:
        raise RunCatalogError("run has incomplete or unhealthy work and cannot be promoted")
    nonpassing = [
        str(gate.get("gate_id", "unknown"))
        for gate in report["gates"]
        if gate.get("status") != "pass"
    ]
    if nonpassing:
        raise RunCatalogError(
            "run has release gates that are failed or undecided: " + ", ".join(nonpassing)
        )
    baseline, changed = storage.promote_baseline(alias, run_id, approver)
    return PromotionResult(baseline=baseline, changed=changed, outcome=outcome)


__all__ = [
    "PromotionResult",
    "RunCatalogError",
    "normalize_baseline_alias",
    "normalize_note",
    "normalize_tag",
    "promote_approved_baseline",
]
