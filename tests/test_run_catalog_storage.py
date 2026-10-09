"""Storage support for searchable run annotations and named baselines."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from aibench.core.models import RunManifest
from aibench.storage.db import Database
from aibench.storage.repositories import Storage


def _manifest(run_id: str) -> RunManifest:
    return RunManifest(
        run_id=run_id,
        dataset_hash="sha256:dataset",
        application_hash="sha256:app",
        plan_hash="sha256:plan",
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )


@pytest.fixture
def storage():
    database = Database.open_in_memory()
    value = Storage(database)
    yield value
    database.close()


def _seed(storage: Storage, run_id: str, *, status: str = "created") -> None:
    storage.commit_run(_manifest(run_id), status=status)


def test_search_runs_filters_metadata_and_pages_stably(storage: Storage) -> None:
    for run_id in ("run-a", "run-b", "run-c"):
        _seed(storage, run_id)
    storage.add_run_tag("run-c", "release")
    storage.set_run_note("run-c", "candidate for quarterly Ångström quality review")
    storage.promote_baseline("production", "run-c", "release-manager")
    storage.update_run_status("run-b", "completed")

    assert [r.manifest.run_id for r in storage.search_runs(limit=1)] == ["run-c"]
    assert [r.manifest.run_id for r in storage.search_runs(limit=1, offset=1)] == ["run-b"]
    assert [r.manifest.run_id for r in storage.search_runs(tag="release")] == ["run-c"]
    assert [r.manifest.run_id for r in storage.search_runs(baseline="production")] == ["run-c"]
    assert [r.manifest.run_id for r in storage.search_runs(status="completed")] == ["run-b"]
    assert [r.manifest.run_id for r in storage.search_runs(query="quarterly")] == ["run-c"]
    assert [r.manifest.run_id for r in storage.search_runs(query="ångström")] == ["run-c"]
    assert [r.manifest.run_id for r in storage.search_runs(query="production")] == ["run-c"]
    assert storage.search_runs(query="%") == []  # user wildcards remain literal text

    metadata = storage.list_run_metadata(["run-c"])["run-c"]
    assert metadata == {
        "tags": ["release"],
        "note": "candidate for quarterly Ångström quality review",
        "baselines": ["production"],
    }


def test_tags_notes_and_baseline_promotions_are_idempotent_and_audited(storage: Storage) -> None:
    _seed(storage, "run-a")
    _seed(storage, "run-b")
    assert storage.add_run_tag("run-a", "release") is True
    assert storage.add_run_tag("run-a", "release") is False
    assert storage.remove_run_tag("run-a", "missing") is False
    assert storage.remove_run_tag("run-a", "release") is True

    assert storage.set_run_note("run-a", "first note") is True
    assert storage.set_run_note("run-a", "first note") is False
    assert storage.set_run_note("run-a", "updated note") is True
    assert storage.set_run_note("run-a", None) is True
    assert storage.set_run_note("run-a", None) is False

    first, changed = storage.promote_baseline("stable", "run-a", "approver")
    assert changed and first.run_id == "run-a"
    same, changed = storage.promote_baseline("stable", "run-a", "approver")
    assert not changed and same == first
    second, changed = storage.promote_baseline("stable", "run-b", "new-approver")
    assert changed and second.run_id == "run-b"
    assert storage.get_baseline("stable") == second
    latest, initial = storage.list_baseline_promotions("stable")
    assert latest.run_id == "run-b"
    assert latest.previous_run_id == "run-a"
    assert latest.approved_by == "new-approver"
    assert initial.run_id == "run-a"
    assert initial.previous_run_id is None


@pytest.mark.parametrize("limit", [0, -1, 1001])
def test_search_runs_rejects_unbounded_or_empty_pages(storage: Storage, limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be"):
        storage.search_runs(limit=limit)


def test_search_runs_rejects_negative_offsets(storage: Storage) -> None:
    with pytest.raises(ValueError, match="offset must be"):
        storage.search_runs(offset=-1)


def test_search_runs_huge_offset_returns_empty_page(storage: Storage) -> None:
    assert storage.search_runs(offset=2**100) == []


def test_run_metadata_batching_respects_sqlite_bind_limit(storage: Storage) -> None:
    storage.conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)
    run_ids = [f"run-{index}" for index in range(1001)]

    metadata = storage.list_run_metadata(run_ids)

    assert len(metadata) == 1001
    assert metadata["run-1000"] == {"tags": [], "note": None, "baselines": []}
