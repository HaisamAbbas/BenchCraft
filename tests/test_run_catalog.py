"""Approval rules and identity normalization for run catalog operations."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from aibench.core.errors import AibenchError
from aibench.core.models import RunManifest
from aibench.services import run_catalog
from aibench.services.run_catalog import (
    RunCatalogError,
    normalize_baseline_alias,
    normalize_note,
    normalize_tag,
    promote_approved_baseline,
)
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database
from aibench.storage.repositories import Storage


@pytest.fixture
def storage() -> Storage:
    database = Database.open_in_memory()
    value = Storage(database)
    value.commit_run(
        RunManifest(
            run_id="run-1",
            dataset_hash="sha256:dataset",
            application_hash="sha256:application",
            plan_hash="sha256:plan",
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        ),
        status="completed",
    )
    yield value
    database.close()


def test_promote_requires_complete_run_and_passing_declared_gates(
    storage: Storage, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = {
        "outcome": {"complete": True, "status": "healthy"},
        "gates": [{"gate_id": "quality", "status": "pass"}],
    }
    monkeypatch.setattr(run_catalog, "build_report", lambda *_args, **_kwargs: report)

    result = promote_approved_baseline(
        storage,
        ArtifactStore(tmp_path),
        "Production",
        "run-1",
        approved_by="Release manager",
    )

    assert result.baseline.alias == "production"
    assert result.baseline.run_id == "run-1"
    assert result.baseline.approved_by == "Release manager"
    assert result.changed is True
    assert result.outcome == report["outcome"]


@pytest.mark.parametrize(
    ("outcome", "gates", "message"),
    [
        ({"complete": False}, [{"gate_id": "quality", "status": "pass"}], "incomplete"),
        ({"complete": True}, [{"gate_id": "quality", "status": "fail"}], "quality"),
        ({"complete": True}, [{"gate_id": "policy", "status": "undecided"}], "undecided"),
    ],
)
def test_promote_rejects_unhealthy_or_nonpassing_run(
    storage: Storage,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: dict[str, object],
    gates: list[dict[str, str]],
    message: str,
) -> None:
    monkeypatch.setattr(
        run_catalog,
        "build_report",
        lambda *_args, **_kwargs: {"outcome": outcome, "gates": gates},
    )

    with pytest.raises(RunCatalogError, match=message):
        promote_approved_baseline(
            storage,
            ArtifactStore(tmp_path),
            "production",
            "run-1",
            approved_by="qa",
        )
    assert storage.get_baseline("production") is None


def test_catalog_labels_notes_and_approver_are_validated(
    storage: Storage, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert normalize_tag("  Release.Candidate ") == "release.candidate"
    assert normalize_baseline_alias(" Production ") == "production"
    assert "[redacted]" in normalize_note("token=secret-value")
    with pytest.raises(RunCatalogError, match="tag must start"):
        normalize_tag("../release")
    with pytest.raises(RunCatalogError, match="at most"):
        normalize_note("x" * 2001)

    monkeypatch.setattr(
        run_catalog,
        "build_report",
        lambda *_args, **_kwargs: {"outcome": {"complete": True}, "gates": []},
    )
    with pytest.raises(RunCatalogError, match="approved-by"):
        promote_approved_baseline(
            storage, ArtifactStore(tmp_path), "production", "run-1", approved_by="  "
        )
    with pytest.raises(AibenchError, match="only completed runs"):
        # State checks happen before report construction or persistence.
        storage.update_run_status("run-1", "running")
        promote_approved_baseline(
            storage, ArtifactStore(tmp_path), "production", "run-1", approved_by="qa"
        )
