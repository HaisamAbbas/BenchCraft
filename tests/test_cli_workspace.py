"""Workspace maintenance commands default to inspection and preserve referenced data."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ValidationError
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

cli = CliRunner()


def _old(path: Path) -> None:
    old = path.stat().st_mtime - 10_000
    os.utime(path, (old, old))


def test_workspace_gc_previews_then_removes_only_stale_orphans(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    storage = Storage(database)
    store = ArtifactStore(workspace.artifacts_dir)
    referenced = store.write_bytes(b"live", mime_type="text/plain")
    commit_verified_artifact(store, storage, referenced)
    orphan = store.write_bytes(b"orphan", mime_type="text/plain")
    _old(Path(orphan.uri))
    database.close()

    preview = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--grace-seconds", "0", "--json"],
    )
    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.stdout)
    assert preview_payload["mode"] == "dry_run"
    assert preview_payload["candidate_count"] == 1
    assert preview_payload["candidate_bytes"] == len(b"orphan")
    assert preview_payload["removed_count"] == 0
    assert Path(orphan.uri).is_file()
    assert Path(referenced.uri).is_file()
    assert list(workspace.root.glob(".aibench-gc-*")) == []

    applied = cli.invoke(
        app,
        [
            "workspace",
            "gc",
            "--workspace",
            str(project),
            "--apply",
            "--json",
        ],
    )
    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["mode"] == "applied"
    assert applied_payload["removed_count"] == 1
    assert applied_payload["removed_bytes"] == len(b"orphan")
    assert not Path(orphan.uri).exists()
    assert Path(referenced.uri).is_file()
    assert list(workspace.root.glob(".aibench-gc-*")) == []


def test_workspace_gc_requires_existing_catalog_without_creating_it(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = cli.invoke(app, ["workspace", "gc", "--workspace", str(project), "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not (project / ".aibench").exists()


def test_workspace_gc_refuses_a_symlinked_metadata_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external_project = tmp_path / "external"
    external_project.mkdir()
    external_workspace = Workspace.at(external_project)
    database = Database.open_workspace(external_workspace)
    database.close()
    store = ArtifactStore(external_workspace.artifacts_dir)
    orphan = store.write_bytes(b"outside workspace", mime_type="text/plain")
    _old(Path(orphan.uri))
    try:
        (project / ".aibench").symlink_to(external_workspace.root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    result = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--apply", "--json"],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "must not be a link" in payload["message"]
    assert Path(orphan.uri).is_file()


def test_workspace_gc_detects_metadata_redirect_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    original_store = ArtifactStore(workspace.artifacts_dir)
    original_orphan = original_store.write_bytes(b"original project", mime_type="text/plain")
    _old(Path(original_orphan.uri))

    external_project = tmp_path / "external"
    external_project.mkdir()
    external_workspace = Workspace.at(external_project)
    database = Database.open_workspace(external_workspace)
    database.close()
    external_store = ArtifactStore(external_workspace.artifacts_dir)
    external_orphan = external_store.write_bytes(b"redirect target", mime_type="text/plain")
    _old(Path(external_orphan.uri))

    metadata_dir = project / ".aibench"
    moved_metadata = project / ".aibench-original"
    original_open = Database.open_readonly

    def redirect_then_open(db_path: Path) -> Database:
        metadata_dir.rename(moved_metadata)
        metadata_dir.symlink_to(external_workspace.root, target_is_directory=True)
        return original_open(db_path)

    try:
        monkeypatch.setattr(Database, "open_readonly", redirect_then_open)
        result = cli.invoke(
            app,
            ["workspace", "gc", "--workspace", str(project), "--apply", "--json"],
        )
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    original_relative_path = Path(original_orphan.uri).relative_to(workspace.artifacts_dir)
    assert (moved_metadata / "artifacts" / original_relative_path).is_file()
    assert Path(external_orphan.uri).is_file()


def test_workspace_gc_refuses_a_symlinked_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    store = ArtifactStore(workspace.artifacts_dir)
    referenced = store.write_bytes(b"still catalogued", mime_type="text/plain")
    commit_verified_artifact(store, Storage(database), referenced)
    database.close()
    _old(Path(referenced.uri))

    external_project = tmp_path / "external"
    external_project.mkdir()
    external_workspace = Workspace.at(external_project)
    Database.open_workspace(external_workspace).close()

    original_database = workspace.db_path
    moved_database = workspace.root / "aibench-original.db"
    original_database.rename(moved_database)
    try:
        original_database.symlink_to(external_workspace.db_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    result = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--apply", "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert "workspace database must be a regular file" in json.loads(result.stdout)["message"]
    assert moved_database.is_file()
    assert Path(referenced.uri).is_file()


def test_workspace_gc_uses_pinned_database_if_catalog_is_swapped_during_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    store = ArtifactStore(workspace.artifacts_dir)
    referenced = store.write_bytes(b"still catalogued", mime_type="text/plain")
    commit_verified_artifact(store, Storage(database), referenced)
    database.close()
    _old(Path(referenced.uri))

    external_project = tmp_path / "external"
    external_project.mkdir()
    external_workspace = Workspace.at(external_project)
    Database.open_workspace(external_workspace).close()

    moved_database = workspace.root / "aibench-original.db"
    original_open = Database.open_readonly

    def swap_catalog_then_restore(db_path: Path) -> Database:
        workspace.db_path.rename(moved_database)
        shutil.copyfile(external_workspace.db_path, workspace.db_path)
        try:
            opened = original_open(db_path)
        finally:
            workspace.db_path.unlink()
            moved_database.rename(workspace.db_path)
        return opened

    monkeypatch.setattr(Database, "open_readonly", swap_catalog_then_restore)
    result = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--apply", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed_count"] == 0
    assert workspace.db_path.is_file()
    assert Path(referenced.uri).is_file()


def test_workspace_gc_accepts_an_abandoned_snapshot_hard_link(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    store = ArtifactStore(workspace.artifacts_dir)
    orphan = store.write_bytes(b"stale orphan", mime_type="text/plain")
    _old(Path(orphan.uri))

    abandoned_snapshot = workspace.root / ".aibench-gc-abandoned"
    abandoned_snapshot.mkdir()
    snapshot_database = abandoned_snapshot / "aibench.db"
    try:
        os.link(workspace.db_path, snapshot_database, follow_symlinks=False)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    result = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--apply", "--json"],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["removed_count"] == 1
    assert not Path(orphan.uri).exists()
    assert snapshot_database.is_file()


def test_workspace_gc_reads_references_from_a_live_wal_snapshot(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    try:
        storage = Storage(database)
        store = ArtifactStore(workspace.artifacts_dir)
        referenced = store.write_bytes(b"committed in wal", mime_type="text/plain")
        commit_verified_artifact(store, storage, referenced)
        orphan = store.write_bytes(b"stale orphan", mime_type="text/plain")
        _old(Path(orphan.uri))
        wal_path = Path(f"{workspace.db_path}-wal")
        assert wal_path.is_file()

        result = cli.invoke(
            app,
            ["workspace", "gc", "--workspace", str(project), "--apply", "--json"],
        )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["removed_count"] == 1
        assert Path(referenced.uri).is_file()
        assert not Path(orphan.uri).exists()
    finally:
        database.close()


def test_workspace_gc_returns_json_error_for_corrupt_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    metadata = project / ".aibench"
    metadata.mkdir(parents=True)
    (metadata / "aibench.db").write_bytes(b"not a sqlite database")

    result = cli.invoke(app, ["workspace", "gc", "--workspace", str(project), "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert "could not read workspace artifact references" in json.loads(result.stdout)["message"]


def test_workspace_gc_bounds_json_path_preview(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    shard = workspace.artifacts_dir / "aa"
    shard.mkdir()
    for number in range(120):
        artifact = shard / f"aa{number:062x}"
        artifact.write_bytes(b"orphan")
        _old(artifact)

    result = cli.invoke(
        app,
        [
            "workspace",
            "gc",
            "--workspace",
            str(project),
            "--grace-seconds",
            "0",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["candidate_count"] == 120
    assert len(payload["candidate_paths"]) == 100
    assert payload["candidate_paths_truncated"] is True
    assert not payload["removed_paths"]


def test_workspace_gc_skips_symlinked_shards_and_unknown_files(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_object = outside / ("aa" + "0" * 62)
    outside_object.write_bytes(b"outside data")
    _old(outside_object)
    try:
        (workspace.artifacts_dir / "aa").symlink_to(outside, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks are unavailable: {exc}")
    unrelated_dir = workspace.artifacts_dir / "bb"
    unrelated_dir.mkdir()
    unrelated_file = unrelated_dir / "notes.txt"
    unrelated_file.write_text("keep this file", encoding="utf-8")
    _old(unrelated_file)

    result = cli.invoke(
        app,
        [
            "workspace",
            "gc",
            "--workspace",
            str(project),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["skipped_unsafe"] == 1
    assert payload["candidate_count"] == 0
    assert outside_object.is_file()
    assert unrelated_file.is_file()


def test_workspace_gc_refuses_short_grace_when_applying(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database = Database.open_workspace(Workspace.at(project))
    database.close()

    result = cli.invoke(
        app,
        [
            "workspace",
            "gc",
            "--workspace",
            str(project),
            "--grace-seconds",
            "0",
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "at least 3600 seconds" in payload["message"]


def test_workspace_gc_negative_grace_keeps_json_error_contract(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database = Database.open_workspace(Workspace.at(project))
    database.close()

    result = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--grace-seconds", "-1", "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"


def test_workspace_gc_human_output_reports_only_successful_removals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aibench.storage.artifacts as artifacts_module

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    store = ArtifactStore(workspace.artifacts_dir)
    orphan = store.write_bytes(b"concurrent replacement", mime_type="text/plain")
    _old(Path(orphan.uri))
    monkeypatch.setattr(artifacts_module, "_unlink_if_still_regular", lambda *args: False)

    result = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--apply"],
    )

    assert result.exit_code == 0, result.output
    assert "removed 0 artifact(s)" in result.output
    assert "skipped 1 unsafe path(s)" in result.output
    assert Path(orphan.uri).is_file()


def test_artifact_gc_rejects_invalid_grace_period(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    for grace in (-1.0, float("nan"), float("inf")):
        with pytest.raises(ValidationError, match="finite and non-negative"):
            store.garbage_collect(set(), grace_seconds=grace)
