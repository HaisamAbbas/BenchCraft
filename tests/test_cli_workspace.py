"""Workspace maintenance commands default to inspection and preserve referenced data."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import zipfile
from contextlib import contextmanager
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


def test_windows_extended_path_prefix_preserves_extended_and_unc_paths() -> None:
    from aibench.storage._windows_handles import _extended_path

    assert _extended_path(r"D:\BenchCraft") == r"\\?\D:\BenchCraft"
    assert _extended_path(r"\\server\share\BenchCraft") == (r"\\?\UNC\server\share\BenchCraft")
    assert _extended_path(r"\\?\D:\BenchCraft") == r"\\?\D:\BenchCraft"
    assert _extended_path(r"\\?\UNC\server\share\BenchCraft") == (
        r"\\?\UNC\server\share\BenchCraft"
    )


def test_workspace_retention_setting_is_persisted_and_controls_gc_default(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    store = ArtifactStore(workspace.artifacts_dir)
    orphan = store.write_bytes(b"kept by policy", mime_type="text/plain")
    _old(Path(orphan.uri))

    saved = cli.invoke(
        app,
        [
            "workspace",
            "retention",
            "--workspace",
            str(project),
            "--grace-seconds",
            "86400",
            "--json",
        ],
    )
    assert saved.exit_code == 0, saved.output
    assert json.loads(saved.stdout)["artifact_orphan_grace_seconds"] == 86400
    policy = workspace.root / "retention.json"
    assert json.loads(policy.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "artifact_orphan_grace_seconds": 86400.0,
    }

    shown = cli.invoke(app, ["workspace", "retention", "--workspace", str(project), "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["configured"] is True

    preview = cli.invoke(app, ["workspace", "gc", "--workspace", str(project), "--json"])
    assert preview.exit_code == 0, preview.output
    assert json.loads(preview.stdout)["grace_seconds"] == 86400
    assert json.loads(preview.stdout)["candidate_count"] == 0
    assert Path(orphan.uri).is_file()

    undercut = cli.invoke(
        app,
        [
            "workspace",
            "gc",
            "--workspace",
            str(project),
            "--grace-seconds",
            "3600",
            "--apply",
            "--json",
        ],
    )
    assert undercut.exit_code == 2, undercut.output
    assert "shorter than the workspace retention policy" in json.loads(undercut.stdout)["message"]
    assert Path(orphan.uri).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory fsync behavior")
def test_workspace_retention_save_fsyncs_directory_after_atomic_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_retention

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def observe_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            events.append("directory_fsync")
        real_fsync(descriptor)

    def observe_replace(*args: object, **kwargs: object) -> None:
        real_replace(*args, **kwargs)
        events.append("publish")

    monkeypatch.setattr(workspace_retention.os, "fsync", observe_fsync)
    monkeypatch.setattr(workspace_retention.os, "replace", observe_replace)

    workspace_retention.save_retention_policy(workspace.root, 86400)

    assert events[-2:] == ["publish", "directory_fsync"]


def test_gc_refuses_a_workspace_swap_between_preflight_and_policy_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import aibench.storage.workspace_retention as retention_module

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    save_retention = cli.invoke(
        app,
        [
            "workspace",
            "retention",
            "--workspace",
            str(project),
            "--grace-seconds",
            "86400",
            "--json",
        ],
    )
    assert save_retention.exit_code == 0, save_retention.output
    orphan = ArtifactStore(workspace.artifacts_dir).write_bytes(
        b"protected", mime_type="text/plain"
    )
    _old(Path(orphan.uri))

    moved = workspace.root.with_name(".aibench-original")
    replacement = workspace.root.with_name(".aibench-replacement")
    swapped = False

    def swap_workspace_directory() -> None:
        workspace.root.rename(moved)
        workspace.root.mkdir()
        (workspace.root / "retention.json").write_text(
            json.dumps({"schema_version": 1, "artifact_orphan_grace_seconds": 3600}),
            encoding="utf-8",
        )

    def restore_workspace_directory() -> None:
        workspace.root.rename(replacement)
        moved.rename(workspace.root)

    if os.name == "nt":
        from aibench.storage import _windows_handles

        real_open_relative = _windows_handles.open_relative

        def swap_then_open(directory_handle: int, name: str, **kwargs: object):
            nonlocal swapped
            if name == ".aibench" and not swapped:
                swapped = True
                swap_workspace_directory()
                try:
                    return real_open_relative(directory_handle, name, **kwargs)
                finally:
                    restore_workspace_directory()
            return real_open_relative(directory_handle, name, **kwargs)

        monkeypatch.setattr(_windows_handles, "open_relative", swap_then_open)
    else:
        real_open = os.open

        def swap_then_open(path: str | os.PathLike[str], *args: object, **kwargs: object):
            nonlocal swapped
            if path == ".aibench" and not swapped:
                swapped = True
                swap_workspace_directory()
                try:
                    return real_open(path, *args, **kwargs)
                finally:
                    restore_workspace_directory()
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(retention_module.os, "open", swap_then_open)

    result = cli.invoke(
        app,
        ["workspace", "gc", "--workspace", str(project), "--apply", "--json"],
    )

    assert swapped
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert Path(orphan.uri).is_file()


def test_workspace_retention_show_uses_default_without_writing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database = Database.open_workspace(Workspace.at(project))
    database.close()
    policy = project / ".aibench" / "retention.json"

    result = cli.invoke(app, ["workspace", "retention", "--workspace", str(project), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["artifact_orphan_grace_seconds"] == 3600
    assert payload["configured"] is False
    assert not policy.exists()
    assert not list(policy.parent.glob(".retention.*.tmp"))

    invalid = cli.invoke(
        app,
        [
            "workspace",
            "retention",
            "--workspace",
            str(project),
            "--grace-seconds",
            "-1",
            "--json",
        ],
    )
    assert invalid.exit_code == 2, invalid.output
    assert json.loads(invalid.stdout)["status"] == "error"


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-relative retention policy I/O")
def test_retention_policy_read_stays_bound_to_the_open_workspace_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import _windows_handles
    from aibench.storage.workspace_retention import load_retention_policy

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    policy = workspace.root / "retention.json"
    policy.write_text(
        json.dumps({"schema_version": 1, "artifact_orphan_grace_seconds": 86400}),
        encoding="utf-8",
    )
    moved = workspace.root.with_name(".aibench-original")
    replacement = workspace.root.with_name(".aibench-replacement")
    real_open_relative = _windows_handles.open_relative
    swapped = False

    def swap_before_policy_open(directory_handle: int, name: str, **kwargs: object):
        nonlocal swapped
        if name == "retention.json" and not swapped:
            swapped = True
            workspace.root.rename(moved)
            workspace.root.mkdir()
            (workspace.root / "retention.json").write_text(
                json.dumps({"schema_version": 1, "artifact_orphan_grace_seconds": 3600}),
                encoding="utf-8",
            )
            return real_open_relative(directory_handle, name, **kwargs)
        return real_open_relative(directory_handle, name, **kwargs)

    monkeypatch.setattr(_windows_handles, "open_relative", swap_before_policy_open)

    value, persisted = load_retention_policy(workspace.root)
    workspace.root.rename(replacement)
    moved.rename(workspace.root)

    assert swapped
    assert persisted is True
    assert value == 86400
    assert (
        json.loads((replacement / "retention.json").read_text(encoding="utf-8"))[
            "artifact_orphan_grace_seconds"
        ]
        == 3600
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-relative retention policy I/O")
def test_retention_policy_write_publishes_through_the_pinned_workspace_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import _windows_handles
    from aibench.storage.workspace_retention import load_retention_policy, save_retention_policy

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    policy = workspace.root / "retention.json"
    policy.write_text(
        json.dumps({"schema_version": 1, "artifact_orphan_grace_seconds": 7200}),
        encoding="utf-8",
    )
    real_rename_relative = _windows_handles.rename_relative
    published = False

    def observe_pinned_publish(
        file_handle: int, parent_handle: int, name: str, *, replace: bool
    ) -> None:
        nonlocal published
        if name == "retention.json" and not published:
            assert (
                _windows_handles.handle_info(parent_handle).file_id == workspace.root.stat().st_ino
            )
            published = True
        real_rename_relative(file_handle, parent_handle, name, replace=replace)

    monkeypatch.setattr(_windows_handles, "rename_relative", observe_pinned_publish)

    saved = save_retention_policy(workspace.root, 86400)
    original_value, persisted = load_retention_policy(workspace.root)

    assert published
    assert saved["artifact_orphan_grace_seconds"] == 86400
    assert persisted is True
    assert original_value == 86400


def test_workspace_retention_refuses_malformed_or_linked_policy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    policy = workspace.root / "retention.json"
    policy.write_text('{"schema_version":1,"artifact_orphan_grace_seconds":NaN}', encoding="utf-8")

    malformed = cli.invoke(app, ["workspace", "retention", "--workspace", str(project), "--json"])
    assert malformed.exit_code == 2, malformed.output
    assert json.loads(malformed.stdout)["status"] == "error"

    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"schema_version":1,"artifact_orphan_grace_seconds":3600}', encoding="utf-8"
    )
    policy.unlink()
    try:
        policy.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    linked = cli.invoke(app, ["workspace", "retention", "--workspace", str(project), "--json"])
    assert linked.exit_code == 2, linked.output
    assert json.loads(linked.stdout)["status"] == "error"

    policy.unlink()
    policy.write_text(
        '{"schema_version":1,"artifact_orphan_grace_seconds":86400,'
        '"artifact_orphan_grace_seconds":3600}',
        encoding="utf-8",
    )
    duplicate = cli.invoke(app, ["workspace", "retention", "--workspace", str(project), "--json"])
    assert duplicate.exit_code == 2, duplicate.output
    assert json.loads(duplicate.stdout)["status"] == "error"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="named pipes are unavailable")
def test_workspace_retention_fifo_policy_fails_without_blocking(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    os.mkfifo(workspace.root / "retention.json")

    program = """\
import sys
from pathlib import Path
from aibench.core.errors import ValidationError
from aibench.storage.workspace_retention import load_retention_policy
try:
    load_retention_policy(Path(sys.argv[1]))
except ValidationError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    check = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(workspace.root),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=3,
    )

    assert check.returncode == 0, check.stderr


def test_workspace_usage_opens_workspace_relative_to_pinned_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    (workspace.root / "inside.txt").write_text("inside", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    external_workspace = Workspace.at(external)
    Database.open_workspace(external_workspace).close()
    (external_workspace.root / "sensitive.txt").write_text("outside", encoding="utf-8")

    moved_project = tmp_path / "project-original"
    swapped = False

    def swap_project_path() -> None:
        project.rename(moved_project)
        try:
            external.rename(project)
        except BaseException:
            moved_project.rename(project)
            raise

    def restore_project_path() -> None:
        project.rename(external)
        moved_project.rename(project)

    if os.name == "nt":
        from aibench.storage import _windows_handles

        real_open_relative = _windows_handles.open_relative
        pinned_workspace_id: int | None = None

        def swap_before_workspace_open(directory_handle: int, name: str, **kwargs: object):
            nonlocal pinned_workspace_id, swapped
            if name == ".aibench" and not swapped:
                swapped = True
                assert (
                    _windows_handles.handle_info(directory_handle).file_id == project.stat().st_ino
                )
                swap_project_path()
                workspace_handle = real_open_relative(directory_handle, name, **kwargs)
                assert workspace_handle is not None
                pinned_workspace_id = _windows_handles.handle_info(workspace_handle).file_id
                assert pinned_workspace_id == (moved_project / ".aibench").stat().st_ino
                return workspace_handle
            return real_open_relative(directory_handle, name, **kwargs)

        monkeypatch.setattr(_windows_handles, "open_relative", swap_before_workspace_open)
    else:
        import aibench.storage.workspace_retention as retention_module

        real_open = os.open

        def swap_before_workspace_open(
            path: str | os.PathLike[str], *args: object, **kwargs: object
        ):
            nonlocal swapped
            if path == ".aibench" and not swapped:
                swapped = True
                swap_project_path()
                try:
                    return real_open(path, *args, **kwargs)
                finally:
                    restore_project_path()
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(retention_module.os, "open", swap_before_workspace_open)

    try:
        result = cli.invoke(app, ["workspace", "usage", "--workspace", str(project), "--json"])
    finally:
        if os.name == "nt" and moved_project.exists():
            restore_project_path()

    assert swapped, result.output
    if os.name == "nt":
        assert pinned_workspace_id == workspace.root.stat().st_ino
        assert result.exit_code == 2, result.output
        assert json.loads(result.stdout)["status"] == "error"
        assert (external_workspace.root / "sensitive.txt").read_text(encoding="utf-8") == "outside"
        return
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["categories"]["other"] == {"files": 1, "bytes": len("inside")}
    assert not moved_project.exists()
    assert (external_workspace.root / "sensitive.txt").read_text(encoding="utf-8") == "outside"


def test_workspace_retention_setting_stays_with_pinned_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    Database.open_workspace(workspace).close()
    original_policy = workspace.root / "retention.json"
    original_policy.write_text(
        json.dumps({"schema_version": 1, "artifact_orphan_grace_seconds": 7200}),
        encoding="utf-8",
    )
    external = tmp_path / "external"
    external.mkdir()
    external_workspace = Workspace.at(external)
    Database.open_workspace(external_workspace).close()
    external_policy = external_workspace.root / "retention.json"
    external_policy.write_text(
        json.dumps({"schema_version": 1, "artifact_orphan_grace_seconds": 3600}),
        encoding="utf-8",
    )
    moved_project = tmp_path / "project-original"
    swapped = False

    def swap_project_path() -> None:
        project.rename(moved_project)
        external.rename(project)

    def restore_project_path() -> None:
        project.rename(external)
        moved_project.rename(project)

    if os.name == "nt":
        from aibench.storage import _windows_handles

        real_open_relative = _windows_handles.open_relative

        def swap_before_workspace_open(directory_handle: int, name: str, **kwargs: object):
            nonlocal swapped
            if name == ".aibench" and not swapped:
                swapped = True
                swap_project_path()
            return real_open_relative(directory_handle, name, **kwargs)

        monkeypatch.setattr(_windows_handles, "open_relative", swap_before_workspace_open)
    else:
        import aibench.storage.workspace_retention as retention_module

        real_open = os.open

        def swap_before_workspace_open(
            path: str | os.PathLike[str], *args: object, **kwargs: object
        ):
            nonlocal swapped
            if path == ".aibench" and not swapped:
                swapped = True
                swap_project_path()
                try:
                    return real_open(path, *args, **kwargs)
                finally:
                    restore_project_path()
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(retention_module.os, "open", swap_before_workspace_open)

    try:
        result = cli.invoke(
            app,
            [
                "workspace",
                "retention",
                "--workspace",
                str(project),
                "--grace-seconds",
                "86400",
                "--json",
            ],
        )
    finally:
        if os.name == "nt" and moved_project.exists():
            restore_project_path()

    assert swapped, result.output
    if os.name == "nt":
        assert result.exit_code == 2, result.output
        assert json.loads(result.stdout)["status"] == "error"
        assert (
            json.loads(original_policy.read_text(encoding="utf-8"))["artifact_orphan_grace_seconds"]
            == 7200
        )
    else:
        assert result.exit_code == 0, result.output
        assert (
            json.loads(original_policy.read_text(encoding="utf-8"))["artifact_orphan_grace_seconds"]
            == 86400
        )
    assert (
        json.loads(external_policy.read_text(encoding="utf-8"))["artifact_orphan_grace_seconds"]
        == 3600
    )


@pytest.mark.parametrize(
    "policy_text",
    [
        '{"schema_version":1,"artifact_orphan_grace_seconds":' + ("9" * 400) + "}",
        "[" * 3000 + "0" + "]" * 3000,
    ],
    ids=["oversized-number", "deeply-nested-json"],
)
def test_workspace_retention_pathological_json_uses_cli_error_envelope(
    tmp_path: Path, policy_text: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    (workspace.root / "retention.json").write_text(policy_text, encoding="utf-8")

    result = cli.invoke(app, ["workspace", "retention", "--workspace", str(project), "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"


def test_workspace_usage_rejects_directory_replacement_during_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    target = workspace.root / "sessions" / "session-001"
    target.mkdir(parents=True)
    (target / "plan.json").write_text("original", encoding="utf-8")
    moved = target.with_name("session-001-original")
    replacement = target.with_name("session-001-replacement")
    swapped = False
    if os.name == "nt":
        from aibench.storage import _windows_handles

        real_iter_directory = _windows_handles.iter_directory

        def replace_directory(handle: int):
            nonlocal swapped
            if not swapped and _windows_handles.handle_info(handle).file_id == target.stat().st_ino:
                swapped = True
                target.rename(moved)
                target.mkdir()
                (target / "external-data.bin").write_bytes(b"outside")
            yield from real_iter_directory(handle)

        monkeypatch.setattr(_windows_handles, "iter_directory", replace_directory)
    else:
        import aibench.storage.workspace_retention as retention_module

        real_scandir = os.scandir
        expected_target = target.stat()

        @contextmanager
        def replace_directory(path: str | os.PathLike[str] | int):
            nonlocal swapped
            if (
                not swapped
                and isinstance(path, int)
                and (os.fstat(path).st_dev, os.fstat(path).st_ino)
                == (expected_target.st_dev, expected_target.st_ino)
            ):
                swapped = True
                target.rename(moved)
                target.mkdir()
                (target / "external-data.bin").write_bytes(b"outside")
                try:
                    with real_scandir(path) as entries:
                        yield entries
                finally:
                    target.rename(replacement)
                    moved.rename(target)
                return
            with real_scandir(path) as entries:
                yield entries

        monkeypatch.setattr(retention_module.os, "scandir", replace_directory)

    result = cli.invoke(app, ["workspace", "usage", "--workspace", str(project), "--json"])

    assert swapped, result.output
    if os.name == "nt":
        assert result.exit_code == 2, result.output
        assert json.loads(result.stdout)["status"] == "error"
        assert (target / "external-data.bin").is_file()
    else:
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["categories"]["sessions"] == {"files": 1, "bytes": len("original")}
        assert not (target / "external-data.bin").exists()
        assert replacement.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows handle-relative directory enumeration")
def test_workspace_usage_enumerates_the_pinned_windows_directory_after_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import _windows_handles

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    target = workspace.root / "sessions" / "session-001"
    target.mkdir(parents=True)
    (target / "plan.json").write_text("original", encoding="utf-8")
    moved = target.with_name("session-001-original")
    replacement = target.with_name("session-001-replacement")
    real_iter_directory = _windows_handles.iter_directory
    swapped = False

    def swap_and_restore(handle: int):
        nonlocal swapped
        if _windows_handles.handle_info(handle).file_id == target.stat().st_ino:
            swapped = True
            target.rename(moved)
            target.mkdir()
            (target / "external-data.bin").write_bytes(b"outside")
            try:
                yield from real_iter_directory(handle)
            finally:
                target.rename(replacement)
                moved.rename(target)
            return
        yield from real_iter_directory(handle)

    monkeypatch.setattr(_windows_handles, "iter_directory", swap_and_restore)

    result = cli.invoke(app, ["workspace", "usage", "--workspace", str(project), "--json"])

    assert swapped
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["categories"]["sessions"] == {"files": 1, "bytes": len("original")}
    assert not (target / "external-data.bin").exists()
    assert replacement.exists()


def test_workspace_usage_is_read_only_and_breaks_down_storage_areas(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    artifact = workspace.artifacts_dir / "ab" / ("ab" + "c" * 62)
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"artifact")
    session_file = workspace.root / "sessions" / "session-001" / "plan.json"
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(b"plan-data")
    log = workspace.root / "logs" / "run.log"
    log.parent.mkdir()
    log.write_bytes(b"log")
    unsafe = workspace.root / "external-link"
    has_unsafe_link = True
    try:
        unsafe.symlink_to(tmp_path, target_is_directory=True)
    except (NotImplementedError, OSError):
        has_unsafe_link = False
    before = sorted(
        path.relative_to(workspace.root).as_posix() for path in workspace.root.rglob("*")
    )

    result = cli.invoke(app, ["workspace", "usage", "--workspace", str(project), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["read_only"] is True
    assert payload["categories"]["artifacts"] == {"files": 1, "bytes": len(b"artifact")}
    assert payload["categories"]["sessions"] == {"files": 1, "bytes": len(b"plan-data")}
    assert payload["categories"]["logs"] == {"files": 1, "bytes": len(b"log")}
    assert payload["categories"]["database"]["files"] >= 1
    assert payload["skipped_unsafe"] == (1 if has_unsafe_link else 0)
    after = sorted(
        path.relative_to(workspace.root).as_posix() for path in workspace.root.rglob("*")
    )
    assert before == after


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


def test_workspace_backup_restores_database_artifacts_and_auxiliary_files(
    tmp_path: Path,
) -> None:
    source_project = tmp_path / "source"
    source_project.mkdir()
    source_workspace = Workspace.at(source_project)
    database = Database.open_workspace(source_workspace)
    storage = Storage(database)
    artifacts = ArtifactStore(source_workspace.artifacts_dir)
    referenced = artifacts.write_bytes(b"backup payload", mime_type="text/plain")
    commit_verified_artifact(artifacts, storage, referenced)
    orphan = artifacts.write_bytes(b"orphan omitted from backup", mime_type="text/plain")
    log = source_workspace.root / "logs" / "run-a.log"
    log.parent.mkdir()
    log.write_text("durable event log\n", encoding="utf-8")
    retention_policy = source_workspace.root / "retention.json"
    retention_policy.write_text(
        json.dumps({"schema_version": 1, "artifact_orphan_grace_seconds": 86400}) + "\n",
        encoding="utf-8",
    )

    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source_project), "--json"],
    )

    assert backup.exit_code == 0, backup.output
    backup_payload = json.loads(backup.stdout)
    assert backup_payload["artifact_count"] == 1
    assert archive.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    database.close()

    restored_project = tmp_path / "restored"
    restored_project.mkdir()
    restore = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(restored_project), "--json"],
    )

    assert restore.exit_code == 0, restore.output
    restore_payload = json.loads(restore.stdout)
    assert restore_payload["artifact_count"] == 1
    restored_workspace = Workspace.at(restored_project)
    if os.name != "nt":
        assert stat.S_IMODE(restored_workspace.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(restored_workspace.db_path.stat().st_mode) == 0o600
    assert (restored_workspace.root / "logs" / "run-a.log").read_text(encoding="utf-8") == (
        "durable event log\n"
    )
    assert (
        json.loads((restored_workspace.root / "retention.json").read_text(encoding="utf-8"))[
            "artifact_orphan_grace_seconds"
        ]
        == 86400
    )
    restored_database = Database.open_workspace(restored_workspace)
    try:
        restored_storage = Storage(restored_database)
        restored_ref = restored_storage.get_artifact(referenced.artifact_id)
        assert restored_ref is not None
        assert Path(restored_ref.uri).is_relative_to(restored_workspace.artifacts_dir)
        if os.name != "nt":
            assert stat.S_IMODE(Path(restored_ref.uri).stat().st_mode) == 0o600
        restored_store = ArtifactStore(restored_workspace.artifacts_dir, create=False)
        assert restored_store.read_bytes(restored_ref) == b"backup payload"
        assert commit_verified_artifact(restored_store, restored_storage, restored_ref) is False
    finally:
        restored_database.close()
    orphan_hex = Path(orphan.uri).name
    assert not (restored_workspace.artifacts_dir / orphan_hex[:2] / orphan_hex).exists()


def test_workspace_repair_previews_then_recovers_and_retains_previous_workspace(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_workspace = Workspace.at(source)
    source_database = Database.open_workspace(source_workspace)
    source_store = ArtifactStore(source_workspace.artifacts_dir)
    reference = source_store.write_bytes(b"verified recovery", mime_type="text/plain")
    commit_verified_artifact(source_store, Storage(source_database), reference)
    source_database.close()
    archive = tmp_path / "backup.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    project = tmp_path / "project"
    project.mkdir()
    previous_workspace = Workspace.at(project)
    previous_database = Database.open_workspace(previous_workspace)
    previous_database.close()
    previous_marker = previous_workspace.root / "operator-note.txt"
    previous_marker.write_text("preserve this workspace", encoding="utf-8")

    preview = cli.invoke(
        app,
        ["workspace", "repair", str(archive), "--workspace", str(project), "--json"],
    )

    assert preview.exit_code == 0, preview.output
    preview_payload = json.loads(preview.stdout)
    assert preview_payload["mode"] == "dry_run"
    assert preview_payload["verified"] is True
    assert preview_payload["existing_workspace"] == "will_be_retained"
    assert previous_marker.read_text(encoding="utf-8") == "preserve this workspace"
    assert not list(project.glob(".aibench-repair-*"))

    applied = cli.invoke(
        app,
        [
            "workspace",
            "repair",
            str(archive),
            "--workspace",
            str(project),
            "--apply",
            "--json",
        ],
    )

    assert applied.exit_code == 0, applied.output
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["mode"] == "applied"
    assert applied_payload["existing_workspace"] == "replaced_and_retained"
    retained = Path(applied_payload["retained_workspace"])
    assert retained == project / retained.name
    assert (retained / "operator-note.txt").read_text(encoding="utf-8") == (
        "preserve this workspace"
    )
    from aibench.storage.workspace_integrity import check_workspace

    assert check_workspace(project)["ok"] is True
    restored_database = Database.open_workspace(Workspace.at(project))
    try:
        restored_ref = Storage(restored_database).get_artifact(reference.artifact_id)
        assert restored_ref is not None
        assert Path(restored_ref.uri).is_relative_to(Workspace.at(project).artifacts_dir)
        assert (
            ArtifactStore(Workspace.at(project).artifacts_dir, create=False).read_bytes(
                restored_ref
            )
            == b"verified recovery"
        )
    finally:
        restored_database.close()
    assert not list(project.glob(".aibench-repair-*"))


def test_workspace_repair_rejects_an_unverified_backup_without_changing_workspace(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    marker = workspace.root / "keep.txt"
    marker.write_text("original", encoding="utf-8")
    archive = tmp_path / "invalid.zip"
    archive.write_bytes(b"not a workspace backup")

    result = cli.invoke(
        app,
        [
            "workspace",
            "repair",
            str(archive),
            "--workspace",
            str(project),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert marker.read_text(encoding="utf-8") == "original"
    assert workspace.db_path.is_file()
    assert not list(project.glob(".aibench-recovery-*"))


def test_workspace_repair_rolls_back_if_the_published_workspace_fails_its_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    source_database = Database.open_workspace(Workspace.at(source))
    source_database.close()
    archive = tmp_path / "backup.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    marker = workspace.root / "keep.txt"
    marker.write_text("original", encoding="utf-8")

    from aibench.storage import workspace_integrity

    real_check = workspace_integrity.check_workspace
    checks = 0

    def fail_final_check(root: Path) -> dict[str, object]:
        nonlocal checks
        checks += 1
        report = real_check(root)
        if checks == 2:
            return {**report, "ok": False, "issue_count": 1}
        return report

    monkeypatch.setattr(workspace_integrity, "check_workspace", fail_final_check)
    result = cli.invoke(
        app,
        [
            "workspace",
            "repair",
            str(archive),
            "--workspace",
            str(project),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    assert "failed its final integrity check" in json.loads(result.stdout)["message"]
    assert marker.read_text(encoding="utf-8") == "original"
    assert workspace.db_path.is_file()
    assert list(project.glob(".aibench-repair-rejected-*"))


def test_workspace_repair_preserves_a_workspace_that_wins_the_publication_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    source_database = Database.open_workspace(Workspace.at(source))
    source_database.close()
    archive = tmp_path / "backup.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    project = tmp_path / "project"
    project.mkdir()
    old_workspace = Workspace.at(project)
    old_database = Database.open_workspace(old_workspace)
    old_database.close()
    old_marker = old_workspace.root / "owner.txt"
    old_marker.write_text("original workspace", encoding="utf-8")

    real_restore = workspace_backup.restore_workspace_backup
    concurrent_marker = project / ".aibench" / "concurrent-owner.txt"

    def race_publication(
        archive_path: Path,
        project_root: Path,
        **kwargs: object,
    ) -> dict[str, object]:
        if project_root.resolve() == project.resolve():
            concurrent_workspace = Workspace.at(project)
            concurrent_database = Database.open_workspace(concurrent_workspace)
            concurrent_database.close()
            concurrent_marker.write_text("concurrent workspace", encoding="utf-8")
        return real_restore(archive_path, project_root, **kwargs)

    monkeypatch.setattr(workspace_backup, "restore_workspace_backup", race_publication)
    result = cli.invoke(
        app,
        [
            "workspace",
            "repair",
            str(archive),
            "--workspace",
            str(project),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    message = json.loads(result.stdout)["message"]
    assert "automatic rollback was incomplete" in message
    retained = next(project.glob(".aibench-recovery-*"))
    assert str(retained) in message
    assert (retained / "owner.txt").read_text(encoding="utf-8") == "original workspace"
    assert concurrent_marker.read_text(encoding="utf-8") == "concurrent workspace"
    assert not list(project.glob(".aibench-repair-rejected-*"))


def test_workspace_repair_can_recover_an_absent_workspace(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "backup.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output
    project = tmp_path / "project"
    project.mkdir()

    result = cli.invoke(
        app,
        [
            "workspace",
            "repair",
            str(archive),
            "--workspace",
            str(project),
            "--apply",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["mode"] == "applied"
    assert payload["existing_workspace"] == "absent"
    assert payload["retained_workspace"] is None
    assert (project / ".aibench" / "aibench.db").is_file()
    assert not list(project.glob(".aibench-repair-*"))


def test_workspace_backup_rejects_casefolded_sqlite_sidecar_alias(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    alias = workspace.root / "AIBENCH.DB-WAL"
    alias.write_bytes(b"must not become a restored SQLite WAL")
    archive = tmp_path / "workspace.zip"

    result = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(project), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "collides with reserved storage" in json.loads(result.stdout)["message"]
    assert not archive.exists()


def test_workspace_backup_enforces_size_limit_before_copying_auxiliary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_backup

    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    (workspace.root / "oversized.log").write_bytes(b"x" * 4096)
    monkeypatch.setattr(
        workspace_backup, "_MAX_TOTAL_BYTES", workspace.db_path.stat().st_size + 1024
    )

    def unexpected_copy(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("auxiliary file was copied before its size limit was enforced")

    monkeypatch.setattr(workspace_backup, "_copy_regular_file", unexpected_copy)
    archive = tmp_path / "workspace.zip"

    result = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(project), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "too large" in json.loads(result.stdout)["message"]
    assert not archive.exists()


def test_workspace_backup_bounds_sqlite_snapshot_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_backup

    project = tmp_path / "project"
    project.mkdir()
    database = Database.open_workspace(Workspace.at(project))
    database.close()
    monkeypatch.setattr(workspace_backup, "_MAX_TOTAL_BYTES", 4096)
    archive = tmp_path / "workspace.zip"

    result = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(project), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "database exceeds" in json.loads(result.stdout)["message"]
    assert not archive.exists()


def test_workspace_restore_publishes_under_the_pinned_project_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("directory-handle-relative publication is a POSIX restore check")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    selected_parent = tmp_path / "selected-parent"
    selected_parent.mkdir()
    selected = selected_parent / "project"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    displaced = tmp_path / "displaced-project-parent"
    real_publish = workspace_backup._publish_directory

    def redirect_during_publish(*args: object, **kwargs: object) -> None:
        selected_parent.rename(displaced)
        selected_parent.symlink_to(outside, target_is_directory=True)
        try:
            real_publish(*args, **kwargs)
        finally:
            selected_parent.unlink()
            displaced.rename(selected_parent)

    monkeypatch.setattr(workspace_backup, "_publish_directory", redirect_during_publish)
    restore = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert restore.exit_code == 0, restore.output
    assert (selected / ".aibench" / "aibench.db").is_file()
    assert not (outside / "project" / ".aibench").exists()


def test_workspace_restore_rejects_replaced_staging_child_without_writing_outside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("Windows directory handles prevent renaming an open staging child")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    (Workspace.at(source).root / "notes.txt").write_text("private backup data", encoding="utf-8")
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    displaced = tmp_path / "displaced-stage-workspace"
    real_extract = workspace_backup._extract_verified_files

    def replace_staging_child(*args: object, **kwargs: object) -> None:
        payload = args[3]
        assert isinstance(payload, Path)
        staged_path = payload.resolve(strict=True)
        staged_path.rename(displaced)
        staged_path.symlink_to(outside, target_is_directory=True)
        real_extract(*args, **kwargs)

    monkeypatch.setattr(workspace_backup, "_extract_verified_files", replace_staging_child)
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not (selected / ".aibench").exists()
    assert list(outside.iterdir()) == []
    assert not displaced.exists() or not any(displaced.iterdir())


def test_workspace_restore_rejects_staging_replacement_at_publish_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX publication uses directory-handle-relative rename")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    displaced: Path | None = None
    real_publish = workspace_backup._publish_directory

    def replace_at_publish(*args: object, **kwargs: object) -> None:
        nonlocal displaced
        payload = args[0]
        assert isinstance(payload, Path)
        staged_path = payload.resolve(strict=True)
        displaced = staged_path.parent / "displaced-workspace"
        staged_path.rename(displaced)
        staged_path.symlink_to(outside, target_is_directory=True)
        real_publish(*args, **kwargs)

    monkeypatch.setattr(workspace_backup, "_publish_directory", replace_at_publish)
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert (selected / ".aibench").is_symlink()
    assert (selected / ".aibench").resolve() == outside
    assert list(outside.iterdir()) == []
    assert displaced is not None
    assert not displaced.exists() or not any(displaced.iterdir())


def test_workspace_restore_preserves_real_directory_replaced_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX publication uses directory-handle-relative rename")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    selected = tmp_path / "selected"
    selected.mkdir()
    displaced: Path | None = None
    real_rename = workspace_backup._rename_directory_without_replacement

    def replace_after_rename(*args: object, **kwargs: object) -> None:
        nonlocal displaced
        real_rename(*args, **kwargs)
        if kwargs.get("root_dir_fd") is not None and kwargs.get("source_name") == "workspace":
            target = args[1]
            assert isinstance(target, Path)
            displaced = target.parent / "displaced-workspace"
            target.rename(displaced)
            target.mkdir()
            (target / "unrelated.txt").write_text("preserve this", encoding="utf-8")

    monkeypatch.setattr(
        workspace_backup, "_rename_directory_without_replacement", replace_after_rename
    )
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "changed during publication" in json.loads(result.stdout)["message"]
    assert (selected / ".aibench" / "unrelated.txt").read_text(encoding="utf-8") == (
        "preserve this"
    )
    assert displaced is not None
    assert not displaced.exists() or not any(displaced.iterdir())
    assert not list(selected.glob(".aibench-restore-*"))


def test_workspace_restore_preflights_zip_entry_count_before_materializing_central_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_backup

    archive = tmp_path / "oversized-directory.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr("manifest.json", b"{}")
        zipped.writestr("workspace/aibench.db", b"database")
        zipped.writestr("workspace/extra", b"extra")
    monkeypatch.setattr(workspace_backup, "_MAX_ENTRIES", 1)

    def must_not_materialize(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("ZipFile was opened before checking the central-directory count")

    monkeypatch.setattr(workspace_backup.zipfile, "ZipFile", must_not_materialize)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "invalid number of entries" in json.loads(result.stdout)["message"]
    assert not (selected / ".aibench").exists()


def test_workspace_restore_uses_private_archive_snapshot_after_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("Windows archive handles prevent in-place mutation")
    import struct

    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    (Workspace.at(source).root / "notes.txt").write_text("saved bytes", encoding="utf-8")
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    with zipfile.ZipFile(archive) as zipped:
        info = zipped.getinfo("workspace/notes.txt")
    archive_bytes = bytearray(archive.read_bytes())
    name_length, extra_length = struct.unpack_from("<HH", archive_bytes, info.header_offset + 26)
    data_offset = info.header_offset + 30 + name_length + extra_length
    original_preflight = workspace_backup._preflight_zip_archive

    def mutate_original_after_snapshot(snapshot: object) -> None:
        original_preflight(snapshot)
        changed = bytearray(archive.read_bytes())
        changed[data_offset] ^= 0xFF
        archive.write_bytes(changed)

    monkeypatch.setattr(workspace_backup, "_preflight_zip_archive", mutate_original_after_snapshot)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert (selected / ".aibench" / "notes.txt").read_text(encoding="utf-8") == "saved bytes"


def test_workspace_restore_normalizes_deeply_nested_manifest_json(tmp_path: Path) -> None:
    archive = tmp_path / "deep-manifest.zip"
    nested = b"[" * 5_000 + b"0" + b"]" * 5_000
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        zipped.writestr("manifest.json", nested)
        zipped.writestr("workspace/aibench.db", b"database")
    selected = tmp_path / "selected"
    selected.mkdir()

    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "manifest is malformed" in json.loads(result.stdout)["message"]
    assert not (selected / ".aibench").exists()


def test_workspace_restore_detects_database_replacement_before_sqlite_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("Windows sharing handles prevent the staged path replacement")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    external_database = tmp_path / "external.db"
    external = sqlite3.connect(external_database)
    external.execute("CREATE TABLE protected_data (value TEXT)")
    external.execute("INSERT INTO protected_data VALUES ('untouched')")
    external.commit()
    external.close()
    external_bytes = external_database.read_bytes()
    real_connect = workspace_backup.sqlite3.connect

    def replace_before_connect(database_path: object, *args: object, **kwargs: object):
        path = Path(database_path) if isinstance(database_path, (str, Path)) else None
        if path is not None and path.name == "aibench.db" and ".aibench-restore-" in str(path):
            path.unlink()
            os.link(external_database, path)
        return real_connect(database_path, *args, **kwargs)

    monkeypatch.setattr(workspace_backup.sqlite3, "connect", replace_before_connect)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "changed while it was being opened" in json.loads(result.stdout)["message"]
    assert external_database.read_bytes() == external_bytes
    check = sqlite3.connect(external_database)
    try:
        assert check.execute("SELECT value FROM protected_data").fetchone() == ("untouched",)
    finally:
        check.close()
    assert not (selected / ".aibench").exists()


def test_workspace_restore_rejects_database_hardlinked_before_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    source_db = Workspace.at(source).db_path
    original_bytes = source_db.read_bytes()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    real_extract = workspace_backup._extract_verified_files

    def hardlink_before_validation(*args: object, **kwargs: object) -> None:
        real_extract(*args, **kwargs)
        payload = args[3]
        assert isinstance(payload, Path)
        restored_db = payload / "aibench.db"
        restored_db.unlink()
        os.link(source_db, restored_db)

    monkeypatch.setattr(workspace_backup, "_extract_verified_files", hardlink_before_validation)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "changed while it was being opened" in json.loads(result.stdout)["message"]
    assert source_db.read_bytes() == original_bytes
    assert not (selected / ".aibench").exists()


def test_workspace_restore_caps_aggregate_path_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aibench.storage import workspace_backup

    monkeypatch.setattr(workspace_backup, "_MAX_PATH_COMPONENTS", 3)
    with pytest.raises(ValidationError, match="too many path components"):
        workspace_backup._validate_portable_file_paths(["workspace/a/b", "workspace/c"])


def test_workspace_restore_pins_database_against_windows_path_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows file handles pin restored SQLite files during open")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    real_connect = workspace_backup.sqlite3.connect
    blocked = False

    def connect_while_pinned(database_path: object, *args: object, **kwargs: object):
        nonlocal blocked
        path = Path(database_path) if isinstance(database_path, (str, Path)) else None
        if path is not None and path.name == "aibench.db" and ".aibench-restore-" in str(path):
            with pytest.raises(OSError):
                path.unlink()
            blocked = True
        return real_connect(database_path, *args, **kwargs)

    monkeypatch.setattr(workspace_backup.sqlite3, "connect", connect_while_pinned)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert blocked


def test_workspace_restore_handles_malformed_deflate_without_publishing(
    tmp_path: Path,
) -> None:
    import struct

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    (Workspace.at(source).root / "notes.txt").write_text("A" * 200_000, encoding="utf-8")
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    with zipfile.ZipFile(archive) as zipped:
        info = zipped.getinfo("workspace/notes.txt")
        assert info.compress_type == zipfile.ZIP_DEFLATED
        name_length, extra_length = struct.unpack_from(
            "<HH", archive.read_bytes(), info.header_offset + 26
        )
        data_offset = info.header_offset + 30 + name_length + extra_length
        corrupted = bytearray(archive.read_bytes())
        corrupted[data_offset : data_offset + min(info.compress_size, 8)] = b"\xff" * min(
            info.compress_size, 8
        )
    malformed = tmp_path / "malformed.zip"
    malformed.write_bytes(corrupted)
    selected = tmp_path / "selected"
    selected.mkdir()

    result = cli.invoke(
        app,
        ["workspace", "restore", str(malformed), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not (selected / ".aibench").exists()


def test_workspace_restore_bounds_catalog_rows_before_materializing_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    workspace = Workspace.at(source)
    database = Database.open_workspace(workspace)
    storage = Storage(database)
    artifacts = ArtifactStore(workspace.artifacts_dir)
    for payload in (b"first catalog object", b"second catalog object"):
        reference = artifacts.write_bytes(payload, mime_type="text/plain")
        commit_verified_artifact(artifacts, storage, reference)
    database.close()

    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    monkeypatch.setattr(workspace_backup, "_MAX_CATALOG_ROWS", 1)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "too many artifact records" in json.loads(result.stdout)["message"]
    assert not (selected / ".aibench").exists()


def test_workspace_restore_rejects_copied_database_swapped_after_extraction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    alternate_database = tmp_path / "copied-valid-database.db"
    real_extract = workspace_backup._extract_verified_files

    def replace_with_copied_database(*args: object, **kwargs: object) -> None:
        payload = args[3]
        assert isinstance(payload, Path)
        real_extract(*args, **kwargs)
        staged_database = payload / "aibench.db"
        shutil.copyfile(staged_database, alternate_database)
        connection = sqlite3.connect(alternate_database)
        (version, applied_at) = connection.execute(
            "SELECT version, applied_at FROM schema_migrations ORDER BY version LIMIT 1"
        ).fetchone()
        replacement_timestamp = "9" * len(applied_at)
        assert replacement_timestamp != applied_at
        connection.execute(
            "UPDATE schema_migrations SET applied_at = ? WHERE version = ?",
            (replacement_timestamp, version),
        )
        connection.commit()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        connection.close()
        assert alternate_database.stat().st_size == staged_database.stat().st_size
        assert workspace_backup._hash_file(alternate_database) != workspace_backup._hash_file(
            staged_database
        )
        shutil.copyfile(alternate_database, staged_database)

    monkeypatch.setattr(workspace_backup, "_extract_verified_files", replace_with_copied_database)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "does not match its archive manifest" in json.loads(result.stdout)["message"]
    assert not (selected / ".aibench").exists()


def _workspace_payload_with_artifact(tmp_path: Path) -> Workspace:
    payload = tmp_path / "payload"
    workspace = Workspace(root=payload)
    database = Database.open_workspace(workspace)
    storage = Storage(database)
    artifacts = ArtifactStore(workspace.artifacts_dir)
    reference = artifacts.write_bytes(b"catalog value", mime_type="text/plain")
    commit_verified_artifact(artifacts, storage, reference)
    database.close()
    return workspace


def test_workspace_restore_rejects_oversized_catalog_field_before_fetching_rows(
    tmp_path: Path,
) -> None:
    from aibench.storage import workspace_backup

    workspace = _workspace_payload_with_artifact(tmp_path)
    connection = sqlite3.connect(workspace.db_path)
    connection.execute("UPDATE artifacts SET uri = ?", ("x" * (300 * 1024),))
    connection.commit()
    connection.close()

    expected_size, expected_sha256 = workspace_backup._hash_file(workspace.db_path)
    with pytest.raises(ValidationError, match="invalid or oversized artifact metadata"):
        workspace_backup._relocate_and_verify_database(
            workspace.root,
            tmp_path / "restored",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )


def test_workspace_restore_rejects_non_text_catalog_digest_without_traceback(
    tmp_path: Path,
) -> None:
    from aibench.storage import workspace_backup

    workspace = _workspace_payload_with_artifact(tmp_path)
    connection = sqlite3.connect(workspace.db_path)
    connection.execute("UPDATE artifacts SET digest = ?", (sqlite3.Binary(b"sha256:bad"),))
    connection.commit()
    connection.close()

    expected_size, expected_sha256 = workspace_backup._hash_file(workspace.db_path)
    with pytest.raises(ValidationError, match="invalid or oversized artifact metadata"):
        workspace_backup._relocate_and_verify_database(
            workspace.root,
            tmp_path / "restored",
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )


def test_windows_restore_pins_project_ancestors(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("Windows directory handles protect against ancestor replacement")
    from aibench.storage import workspace_backup

    parent = tmp_path / "selected-parent"
    root = parent / "project"
    root.mkdir(parents=True)
    info = root.stat()
    moved = tmp_path / "moved-parent"

    with (
        workspace_backup._pin_project_root(root, (info.st_dev, info.st_ino)),
        pytest.raises(OSError),
    ):
        parent.rename(moved)

    assert parent.is_dir()
    assert not moved.exists()


def test_windows_restore_rejects_stage_replacement_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows stage identity is checked against its opened directory handle")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    selected = tmp_path / "selected"
    selected.mkdir()
    displaced = tmp_path / "displaced-restore-stage"
    marker: Path | None = None
    real_open = workspace_backup._open_windows_directory_fd

    def replace_before_open(path: Path, **kwargs: object) -> int:
        nonlocal marker
        if path.name.startswith(".aibench-restore-"):
            path.rename(displaced)
            path.mkdir()
            marker = path / "unrelated.txt"
            marker.write_text("preserve this", encoding="utf-8")
            return real_open(path, **kwargs)
        return real_open(path, **kwargs)

    monkeypatch.setattr(workspace_backup, "_open_windows_directory_fd", replace_before_open)
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "changed while it was opened" in json.loads(result.stdout)["message"]
    assert not (selected / ".aibench").exists()
    assert marker is not None
    assert marker.read_text(encoding="utf-8") == "preserve this"
    assert displaced.is_dir()


def test_windows_restore_pins_staging_child_through_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows directory handles protect staging entries during publication")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    selected = tmp_path / "selected"
    selected.mkdir()
    moved = tmp_path / "attacker-selected-payload"
    real_publish = workspace_backup._publish_directory

    def replace_before_publish(*args: object, **kwargs: object) -> None:
        source_path = args[0]
        assert isinstance(source_path, Path)
        with pytest.raises(OSError):
            source_path.rename(moved)
        assert not moved.exists()
        real_publish(*args, **kwargs)

    monkeypatch.setattr(workspace_backup, "_publish_directory", replace_before_publish)
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 0, result.output
    assert (selected / ".aibench" / "aibench.db").is_file()
    assert not moved.exists()


def test_windows_restore_cleanup_keeps_the_pinned_stage_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name != "nt":
        pytest.skip("Windows cleanup uses handle-relative deletion")
    from aibench.storage import workspace_backup

    source = tmp_path / "source"
    source.mkdir()
    database = Database.open_workspace(Workspace.at(source))
    database.close()
    archive = tmp_path / "workspace.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    real_extract = workspace_backup._extract_verified_files
    real_cleanup = workspace_backup._cleanup_windows_restore_stage
    moved = tmp_path / "attacker-stage"

    def fail_after_extract(*args: object, **kwargs: object) -> None:
        real_extract(*args, **kwargs)
        raise ValidationError("force restore cleanup")

    def check_stage_pin(
        stage: Path | None,
        stage_fd: int | None,
        payload: Path | None,
        payload_fd: int | None,
        *,
        published: bool,
    ) -> None:
        assert stage is not None
        with pytest.raises(OSError):
            stage.rename(moved)
        real_cleanup(stage, stage_fd, payload, payload_fd, published=published)

    monkeypatch.setattr(workspace_backup, "_extract_verified_files", fail_after_extract)
    monkeypatch.setattr(workspace_backup, "_cleanup_windows_restore_stage", check_stage_pin)
    selected = tmp_path / "selected"
    selected.mkdir()
    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(selected), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "force restore cleanup" in json.loads(result.stdout)["message"]
    assert not moved.exists()
    assert not list(selected.glob(".aibench-restore-*"))


def test_project_root_resolution_rejects_a_symlink_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("Windows root handles prevent renaming during path resolution")
    from aibench.storage import workspace_backup

    selected = tmp_path / "selected"
    selected.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    displaced = tmp_path / "displaced-selected"
    original_resolve = Path.resolve

    def swap_during_resolution(path: Path, *, strict: bool = False) -> Path:
        if path == selected:
            selected.rename(displaced)
            selected.symlink_to(outside, target_is_directory=True)
            try:
                resolved = original_resolve(path, strict=strict)
            finally:
                selected.unlink()
                displaced.rename(selected)
            return resolved
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", swap_during_resolution)

    with pytest.raises(ValidationError, match="changed while it was being resolved"):
        workspace_backup._project_root(selected)

    assert selected.is_dir()
    assert not selected.is_symlink()


def test_workspace_backup_refuses_existing_or_inside_destination(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database = Database.open_workspace(Workspace.at(project))
    database.close()
    inside = project / ".aibench" / "backup.zip"
    existing = tmp_path / "existing.zip"
    existing.write_bytes(b"keep")

    inside_result = cli.invoke(
        app,
        ["workspace", "backup", str(inside), "--workspace", str(project), "--json"],
    )
    existing_result = cli.invoke(
        app,
        ["workspace", "backup", str(existing), "--workspace", str(project), "--json"],
    )

    assert inside_result.exit_code == 2, inside_result.output
    assert json.loads(inside_result.stdout)["status"] == "error"
    assert existing_result.exit_code == 2, existing_result.output
    assert json.loads(existing_result.stdout)["status"] == "error"
    assert not inside.exists()
    assert existing.read_bytes() == b"keep"


def test_workspace_restore_never_replaces_existing_workspace(tmp_path: Path) -> None:
    archive = tmp_path / "invalid.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "aibench-workspace-backup",
                    "format_version": 1,
                    "files": [],
                }
            ),
        )
    project = tmp_path / "project"
    project.mkdir()
    workspace = Workspace.at(project)
    database = Database.open_workspace(workspace)
    database.close()
    marker = workspace.root / "keep.txt"
    marker.write_text("preserve", encoding="utf-8")

    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(project), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert marker.read_text(encoding="utf-8") == "preserve"


def test_workspace_restore_rejects_traversal_before_creating_workspace(tmp_path: Path) -> None:
    archive = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive, "w") as zipped:
        zipped.writestr(
            "manifest.json",
            json.dumps(
                {
                    "format": "aibench-workspace-backup",
                    "format_version": 1,
                    "files": [
                        {
                            "path": "workspace/../outside.txt",
                            "size_bytes": 0,
                            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                        }
                    ],
                }
            ),
        )
        zipped.writestr("workspace/../outside.txt", b"")
    project = tmp_path / "project"
    project.mkdir()

    result = cli.invoke(
        app,
        ["workspace", "restore", str(archive), "--workspace", str(project), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not (project / ".aibench").exists()
    assert not (tmp_path / "outside.txt").exists()


def test_workspace_restore_rejects_tampered_payload_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    workspace = Workspace.at(source)
    database = Database.open_workspace(workspace)
    database.close()
    (workspace.root / "notes.txt").write_text("original", encoding="utf-8")
    archive = tmp_path / "backup.zip"
    backup = cli.invoke(
        app,
        ["workspace", "backup", str(archive), "--workspace", str(source), "--json"],
    )
    assert backup.exit_code == 0, backup.output

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(archive, "r") as original, zipfile.ZipFile(tampered, "w") as altered:
        for info in original.infolist():
            data = original.read(info.filename)
            if info.filename == "workspace/notes.txt":
                data = b"changed"
            altered.writestr(info.filename, data)
    target = tmp_path / "target"
    target.mkdir()

    result = cli.invoke(
        app,
        ["workspace", "restore", str(tampered), "--workspace", str(target), "--json"],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not (target / ".aibench").exists()


def test_workspace_backup_requires_an_existing_workspace(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = cli.invoke(
        app,
        [
            "workspace",
            "backup",
            str(tmp_path / "backup.zip"),
            "--workspace",
            str(project),
            "--json",
        ],
    )

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not (project / ".aibench").exists()
    assert not (tmp_path / "backup.zip").exists()
