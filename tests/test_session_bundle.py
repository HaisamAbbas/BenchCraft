"""Portable conversation bundles preserve review history while rebasing project paths."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
import zlib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ConflictError, ValidationError
from aibench.core.sessions import (
    ActionKind,
    ActionRequest,
    ActionState,
    ConversationTurn,
    PendingQuestion,
    PlanPatch,
)
from aibench.sessions.store import SessionStore
from aibench.storage import session_bundle
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.storage.session_bundle import export_session_bundle, import_session_bundle
from tests.session_support import SessionHarness

runner = CliRunner()


def _session(tmp_path: Path):
    harness = SessionHarness(tmp_path)
    harness.workspace = Workspace.at(harness.root)
    harness.workspace.ensure_directories()
    controller = harness.open_session({"case-a": "hello"}, objectives=("answers are correct",))
    user, inserted = controller.store.append_turn(
        ConversationTurn(
            turn_id="turn-user",
            session_id=controller.session_id,
            sequence=1,
            role="user",
            kind="message",
            content="Please compare the latest benchmark.",
            message_id="message-1",
        )
    )
    assert inserted
    changed = controller.apply_patch(
        PlanPatch(repetitions=2), expected_revision=1, source_turn_id=user.turn_id
    )
    assert changed.status == "applied"
    decision = controller.current_decision()
    controller.store.ask(
        controller.session_id,
        [
            PendingQuestion(
                question_id="portable-question",
                prompt="Should this draft use a subset?",
                required_fields=("selection",),
                choices=("all", "sample"),
                blocking_scope="selection",
                draft_revision=2,
            )
        ],
    )
    action, created = controller.store.record_action(
        ActionRequest(
            action_id="action-run-1",
            session_id=controller.session_id,
            source_turn_id=user.turn_id,
            source="user",
            kind=ActionKind.START_RUN,
            expected_revision=2,
            run_id="run-linked-1",
            state=ActionState.DONE,
        )
    )
    assert created
    assistant, inserted = controller.store.append_turn(
        ConversationTurn(
            turn_id="turn-assistant",
            session_id=controller.session_id,
            sequence=2,
            role="assistant",
            kind="reply",
            content="I have prepared the reviewed draft.",
            replies_to=user.turn_id,
            decision_refs=(decision.decision_id,),
            action_refs=(action.action_id,),
        )
    )
    assert inserted and assistant.sequence == 2
    return harness, controller


def _matching_project(source: Path, destination: Path) -> None:
    destination.mkdir()
    for name in ("app.json", "app.py", "data.jsonl"):
        shutil.copy2(source / name, destination / name)
    config = json.loads((destination / "app.json").read_text(encoding="utf-8"))
    config["transport"]["env"]["APP_LOG"] = str(destination / "invocations.jsonl")
    (destination / "app.json").write_text(json.dumps(config), encoding="utf-8")


def _with_windows_source_paths(bundle: Path, destination: Path) -> None:
    with zipfile.ZipFile(bundle) as original:
        entries = {info.filename: original.read(info.filename) for info in original.infolist()}
    manifest = json.loads(entries["manifest.json"])
    data = json.loads(entries["session/data.json"])
    source_root = r"C:\Origin\project"
    data["source_project_root"] = source_root
    session_record = data["tables"]["sessions"][0]
    session = json.loads(session_record["data"])
    session["project_root"] = source_root
    session_record["data"] = json.dumps(session, sort_keys=True, separators=(",", ":"))
    for row in data["tables"]["decision_records"]:
        decision = json.loads(row["data"])
        decision["choices"]["application"] = source_root + r"\app.json"
        decision["choices"]["dataset"] = source_root + r"\data.jsonl"
        row["data"] = json.dumps(decision, sort_keys=True, separators=(",", ":"))
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    entries["session/data.json"] = encoded
    for record in manifest["files"]:
        if record["path"] == "session/data.json":
            record["size_bytes"] = len(encoded)
            record["sha256"] = hashlib.sha256(encoded).hexdigest()
    entries["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)


def _with_mismatched_row_session_id(bundle: Path, destination: Path, table: str) -> None:
    _with_catalog_value(bundle, destination, table, "session_id", "other-session")


def _with_catalog_value(
    bundle: Path, destination: Path, table: str, column: str, value: object
) -> None:
    with zipfile.ZipFile(bundle) as original:
        entries = {info.filename: original.read(info.filename) for info in original.infolist()}
    manifest = json.loads(entries["manifest.json"])
    data = json.loads(entries["session/data.json"])
    data["tables"][table][0][column] = value
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    entries["session/data.json"] = encoded
    for record in manifest["files"]:
        if record["path"] == "session/data.json":
            record["size_bytes"] = len(encoded)
            record["sha256"] = hashlib.sha256(encoded).hexdigest()
    entries["manifest.json"] = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)


def test_session_bundle_round_trip_rebases_paths_and_is_idempotent(tmp_path: Path) -> None:
    harness, controller = _session(tmp_path / "source")
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)
    bundle = tmp_path / "session.zip"

    exported = export_session_bundle(harness.root, controller.session_id, bundle)
    assert exported["revision_count"] == 2
    assert exported["turn_count"] == 2
    assert exported["referenced_run_ids"] == ["run-linked-1"]

    imported = import_session_bundle(bundle, destination)
    assert imported["imported"] is True
    assert imported["referenced_run_ids"] == ["run-linked-1"]
    again = import_session_bundle(bundle, destination)
    assert again["already_present"] is True

    database = Database.open_workspace(Workspace.at(destination))
    try:
        store = SessionStore(Storage(database))
        session = store.get_session(controller.session_id)
        assert session is not None
        assert session.project_root == str(destination.resolve())
        assert session.trusted_local is False
        assert [item.revision for item in store.list_decisions(session.session_id)] == [1, 2]
        assert store.turns(session.session_id)[1].replies_to == "turn-user"
        assert store.list_actions(session.session_id)[0].run_id == "run-linked-1"
        assert [q.question_id for q in store.questions(session.session_id)] == ["portable-question"]
        for decision in store.list_decisions(session.session_id):
            plan = (
                Workspace.at(destination).root
                / "sessions"
                / session.session_id
                / decision.plan_file
            )
            assert plan.is_file()
        store.update_session(session.session_id, presented_revision=2)
        with pytest.raises(ConflictError, match="sessions already contains different data"):
            import_session_bundle(bundle, destination)
        assert len(store.list_decisions(session.session_id)) == 2
        assert store.get_session(session.session_id).presented_revision == 2
    finally:
        database.close()
    controller.storage.db.close()


def test_session_import_rejects_missing_project_dependencies_before_workspace_creation(
    tmp_path: Path,
) -> None:
    harness, controller = _session(tmp_path / "source")
    bundle = tmp_path / "session.zip"
    export_session_bundle(harness.root, controller.session_id, bundle)
    destination = tmp_path / "incomplete"
    destination.mkdir()

    with pytest.raises(ValidationError, match="application config is missing"):
        import_session_bundle(bundle, destination)

    assert not Workspace.at(destination).root.exists()
    controller.storage.db.close()


def test_session_import_rebases_windows_paths_from_an_archive_on_any_host(tmp_path: Path) -> None:
    harness, controller = _session(tmp_path / "source")
    original = tmp_path / "session.zip"
    portable = tmp_path / "windows-origin.zip"
    export_session_bundle(harness.root, controller.session_id, original)
    _with_windows_source_paths(original, portable)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)

    result = import_session_bundle(portable, destination)

    assert result["imported"] is True
    database = Database.open_workspace(Workspace.at(destination))
    try:
        session = SessionStore(Storage(database)).get_session(controller.session_id)
        assert session is not None
        decision = SessionStore(Storage(database)).get_decision(session.decision_id or "")
        assert decision is not None
        assert Path(decision.choices.application) == destination / "app.json"
        assert Path(decision.choices.dataset) == destination / "data.jsonl"
    finally:
        database.close()
        controller.storage.db.close()


def test_session_bundle_detects_tampered_plan_and_cli_exposes_transfer_commands(
    tmp_path: Path,
) -> None:
    harness, controller = _session(tmp_path / "source")
    bundle = tmp_path / "session.zip"
    export_session_bundle(harness.root, controller.session_id, bundle)
    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as original, zipfile.ZipFile(tampered, "w") as changed:
        for info in original.infolist():
            body = original.read(info.filename)
            if info.filename.startswith("session/plans/"):
                body = bytes((body[0] ^ 1,)) + body[1:]
            changed.writestr(info.filename, body)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)
    with pytest.raises(ValidationError, match="integrity check"):
        import_session_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()

    exported = runner.invoke(
        app,
        [
            "sessions",
            "export",
            controller.session_id,
            str(tmp_path / "cli-session.zip"),
            "--workspace",
            str(harness.root),
            "--json",
        ],
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout)["session_id"] == controller.session_id
    imported = runner.invoke(
        app,
        [
            "sessions",
            "import",
            str(tmp_path / "cli-session.zip"),
            "--workspace",
            str(destination),
            "--json",
        ],
    )
    assert imported.exit_code == 0, imported.output
    assert json.loads(imported.stdout)["imported"] is True
    controller.storage.db.close()


@pytest.mark.parametrize(
    ("table", "error"),
    [
        ("conversation_turns", "conversation turn is inconsistent"),
        ("action_requests", "action is inconsistent"),
    ],
)
def test_session_import_rejects_catalog_rows_linked_to_another_session(
    tmp_path: Path, table: str, error: str
) -> None:
    harness, controller = _session(tmp_path / "source")
    original = tmp_path / "session.zip"
    malformed = tmp_path / f"{table}.zip"
    export_session_bundle(harness.root, controller.session_id, original)
    _with_mismatched_row_session_id(original, malformed, table)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)

    with pytest.raises(ValidationError, match=error):
        import_session_bundle(malformed, destination)

    assert not Workspace.at(destination).root.exists()
    controller.storage.db.close()


def test_session_import_rejects_sql_column_types_that_sqlite_would_coerce(
    tmp_path: Path,
) -> None:
    harness, controller = _session(tmp_path / "source")
    original = tmp_path / "session.zip"
    malformed = tmp_path / "coerced-column.zip"
    export_session_bundle(harness.root, controller.session_id, original)
    _with_catalog_value(original, malformed, "sessions", "created_at", 7)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)

    with pytest.raises(ValidationError, match="sessions contains an invalid row"):
        import_session_bundle(malformed, destination)

    assert not Workspace.at(destination).root.exists()
    controller.storage.db.close()


def test_session_import_reports_zip_decompression_errors_as_validation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, controller = _session(tmp_path / "source")
    bundle = tmp_path / "session.zip"
    export_session_bundle(harness.root, controller.session_id, bundle)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)

    def fail_decompression(*_args: object, **_kwargs: object) -> None:
        raise zlib.error("invalid compressed stream")

    monkeypatch.setattr(zipfile.ZipFile, "open", fail_decompression)
    with pytest.raises(ValidationError, match="could not import session bundle"):
        import_session_bundle(bundle, destination)

    assert not Workspace.at(destination).root.exists()
    controller.storage.db.close()


@pytest.mark.parametrize(
    ("limit_name", "limit", "error"),
    [
        ("_MAX_SESSION_ARCHIVE_BYTES", 1, "unsupported size"),
        ("_MAX_SESSION_ENTRIES", 2, "invalid number of entries"),
    ],
)
def test_session_import_bounds_archive_size_and_entry_count_before_parsing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    limit: int,
    error: str,
) -> None:
    harness, controller = _session(tmp_path / "source")
    bundle = tmp_path / "session.zip"
    export_session_bundle(harness.root, controller.session_id, bundle)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)
    monkeypatch.setattr(session_bundle, limit_name, limit)

    with pytest.raises(ValidationError, match=error):
        import_session_bundle(bundle, destination)

    assert not Workspace.at(destination).root.exists()
    controller.storage.db.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd publication is exercised here")
def test_session_import_rolls_back_when_session_directory_is_swapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, controller = _session(tmp_path / "source")
    bundle = tmp_path / "session.zip"
    export_session_bundle(harness.root, controller.session_id, bundle)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)
    external = tmp_path / "outside"
    external.mkdir()
    original_link = session_bundle._link_session_plan
    swapped = False

    def swap_then_link(directory: Path, staging: str, name: str, directory_fd: int | None) -> None:
        nonlocal swapped
        if not swapped:
            moved = directory.with_name(f"{directory.name}-pinned")
            directory.rename(moved)
            directory.symlink_to(external, target_is_directory=True)
            swapped = True
        original_link(directory, staging, name, directory_fd)

    monkeypatch.setattr(session_bundle, "_link_session_plan", swap_then_link)
    with pytest.raises(ValidationError, match="session directory changed while being pinned"):
        import_session_bundle(bundle, destination)

    workspace = Workspace.at(destination)
    session_directory = workspace.root / "sessions" / controller.session_id
    assert session_directory.is_symlink()
    assert list(external.iterdir()) == []
    assert list(session_directory.with_name(f"{controller.session_id}-pinned").iterdir()) == []
    database = Database.open_workspace(workspace)
    try:
        assert SessionStore(Storage(database)).get_session(controller.session_id) is None
    finally:
        database.close()
        controller.storage.db.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd publication is exercised here")
def test_session_import_rolls_back_when_sessions_parent_is_moved_outside_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, controller = _session(tmp_path / "source")
    bundle = tmp_path / "session.zip"
    export_session_bundle(harness.root, controller.session_id, bundle)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)
    external = tmp_path / "outside"
    external.mkdir()
    original_link = session_bundle._link_session_plan
    swapped = False

    def move_parent_then_link(
        directory: Path, staging: str, name: str, directory_fd: int | None
    ) -> None:
        nonlocal swapped
        if not swapped:
            moved_sessions = external / "sessions"
            directory.parent.rename(moved_sessions)
            directory.parent.symlink_to(moved_sessions, target_is_directory=True)
            swapped = True
        original_link(directory, staging, name, directory_fd)

    monkeypatch.setattr(session_bundle, "_link_session_plan", move_parent_then_link)
    with pytest.raises(ValidationError, match="session directory changed while being pinned"):
        import_session_bundle(bundle, destination)

    workspace = Workspace.at(destination)
    assert (workspace.root / "sessions").is_symlink()
    assert list((external / "sessions" / controller.session_id).iterdir()) == []
    database = Database.open_workspace(workspace)
    try:
        assert SessionStore(Storage(database)).get_session(controller.session_id) is None
    finally:
        database.close()
        controller.storage.db.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX dir_fd publication is exercised here")
def test_session_import_rolls_back_when_project_root_is_moved_and_symlinked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, controller = _session(tmp_path / "source")
    bundle = tmp_path / "session.zip"
    export_session_bundle(harness.root, controller.session_id, bundle)
    destination = tmp_path / "destination"
    _matching_project(harness.root, destination)
    external = tmp_path / "outside"
    external.mkdir()
    moved_project = external / "destination"
    original_link = session_bundle._link_session_plan
    moved = False

    def move_project_then_link(
        directory: Path, staging: str, name: str, directory_fd: int | None
    ) -> None:
        nonlocal moved
        if not moved:
            destination.rename(moved_project)
            destination.symlink_to(moved_project, target_is_directory=True)
            moved = True
        original_link(directory, staging, name, directory_fd)

    monkeypatch.setattr(session_bundle, "_link_session_plan", move_project_then_link)
    with pytest.raises(ValidationError, match="session directory changed while being pinned"):
        import_session_bundle(bundle, destination)

    session_directory = moved_project / ".aibench" / "sessions" / controller.session_id
    assert destination.is_symlink()
    assert list(session_directory.iterdir()) == []
    database = Database.open_workspace(Workspace.at(moved_project))
    try:
        assert SessionStore(Storage(database)).get_session(controller.session_id) is None
    finally:
        database.close()
        controller.storage.db.close()


def test_active_session_cannot_be_exported(tmp_path: Path) -> None:
    harness, controller = _session(tmp_path / "source")
    controller.store.update_session(controller.session_id, active_run_id="run-live")
    with pytest.raises(ValidationError, match="active run"):
        export_session_bundle(harness.root, controller.session_id, tmp_path / "session.zip")
    controller.storage.db.close()


def test_session_export_caps_aggregate_frozen_plan_bytes_before_retaining_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, controller = _session(tmp_path / "source")
    decisions = controller.store.list_decisions(controller.session_id)
    plan_directory = Workspace.at(harness.root).root / "sessions" / controller.session_id
    plan_sizes = [(plan_directory / decision.plan_file).stat().st_size for decision in decisions]
    assert len(plan_sizes) == 2
    assert max(plan_sizes) < sum(plan_sizes)
    monkeypatch.setattr(session_bundle, "_MAX_PLAN_BYTES", max(plan_sizes))
    destination = tmp_path / "session.zip"

    with pytest.raises(ValidationError, match="stable, bounded regular file"):
        export_session_bundle(harness.root, controller.session_id, destination)

    assert not destination.exists()
    controller.storage.db.close()


def test_session_export_bounds_all_database_columns_before_fetching_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness, controller = _session(tmp_path / "source")
    monkeypatch.setattr(session_bundle, "_MAX_DATA_BYTES", 1024)
    controller.storage.db.connection.execute(
        "UPDATE sessions SET updated_at = ? WHERE session_id = ?",
        ("x" * 2048, controller.session_id),
    )

    with pytest.raises(ValidationError, match="session exceeds the supported bundle size"):
        export_session_bundle(harness.root, controller.session_id, tmp_path / "session.zip")

    controller.storage.db.close()


def test_existing_plan_comparison_refuses_oversized_target_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "existing-plan.json"
    target.write_bytes(b"x" * 2048)
    monkeypatch.setattr(session_bundle, "_MAX_PLAN_BYTES", 1024)

    assert not session_bundle._existing_plan_matches(target, b"x" * 2048)


@pytest.mark.parametrize("session_id", ["existing-id.", "CON", "con.json", "COM1", "LPT³"])
def test_session_bundle_rejects_windows_path_aliases(session_id: str) -> None:
    assert not session_bundle._is_portable_session_id(session_id)
