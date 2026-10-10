"""Named dataset suites are immutable workspace snapshots pinned by NAME@VERSION."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.datasets import suites as suites_service
from aibench.datasets.suites import DatasetSuiteError, resolve_dataset_suite
from aibench.storage.db import Workspace
from aibench.storage.repositories import Storage
from tests.engine_support import Harness

cli = CliRunner()


def _write_cases(path: Path, cases: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(case, separators=(",", ":")) + "\n" for case in cases),
        encoding="utf-8",
    )


def _suite_registration(
    source: Path, project: Path, *, name: str = "support", version: str = "1.0.0"
):
    return cli.invoke(
        app,
        [
            "dataset",
            "suites",
            "register",
            name,
            version,
            str(source),
            "--workspace",
            str(project),
            "--json",
        ],
    )


def test_suite_register_is_immutable_idempotent_and_listable(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.jsonl"
    original = '{"case_id":"case-a","input":"original"}\n'
    source.write_text(original, encoding="utf-8")

    first = _suite_registration(source, project)
    assert first.exit_code == 0, first.output
    first_payload = json.loads(first.stdout)
    assert first_payload["created"] is True
    assert first_payload["suite"]["reference"] == "support@1.0.0"
    assert first_payload["suite"]["case_count"] == 1
    snapshot = Path(first_payload["snapshot"])
    assert snapshot.read_text(encoding="utf-8") == original

    duplicate = _suite_registration(source, project)
    assert duplicate.exit_code == 0, duplicate.output
    assert json.loads(duplicate.stdout)["created"] is False

    source.write_text('{"case_id":"case-a","input":"changed"}\n', encoding="utf-8")
    listed = cli.invoke(
        app,
        ["dataset", "suites", "list", "--workspace", str(project), "--json"],
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)["count"] == 1
    assert snapshot.read_text(encoding="utf-8") == original

    conflict = _suite_registration(source, project)
    assert conflict.exit_code == 2, conflict.output
    assert "already has a different snapshot" in json.loads(conflict.stdout)["message"]
    assert snapshot.read_text(encoding="utf-8") == original


def test_suite_show_verifies_snapshot_and_rejects_tampering(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.jsonl"
    _write_cases(source, [{"case_id": "one", "input": "safe"}])
    registered = _suite_registration(source, project)
    assert registered.exit_code == 0, registered.output
    payload = json.loads(registered.stdout)
    expected_hash = "sha256:" + hashlib.sha256(b'{"case_id":"one","input":"safe"}').hexdigest()
    assert payload["suite"]["content_hash"] == expected_hash

    shown = cli.invoke(
        app,
        ["dataset", "suites", "show", "support@1.0.0", "--workspace", str(project), "--json"],
    )
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["available"] is True

    Path(payload["snapshot"]).write_text('{"case_id":"one","input":"tampered"}\n', encoding="utf-8")
    tampered = cli.invoke(
        app,
        ["dataset", "suites", "show", "support@1.0.0", "--workspace", str(project), "--json"],
    )
    assert tampered.exit_code == 2, tampered.output
    assert "has changed" in json.loads(tampered.stdout)["message"]


def test_suite_show_returns_json_error_for_corrupt_workspace_database(tmp_path: Path) -> None:
    project = tmp_path / "project"
    metadata = project / ".aibench"
    metadata.mkdir(parents=True)
    (metadata / "aibench.db").write_bytes(b"not a SQLite database")

    result = cli.invoke(
        app,
        ["dataset", "suites", "show", "support@1.0.0", "--workspace", str(project), "--json"],
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "could not read dataset suite catalog" in payload["message"]


def test_suite_registration_requires_unique_explicit_ids(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    duplicate_source = tmp_path / "duplicate.jsonl"
    _write_cases(
        duplicate_source,
        [{"case_id": "same", "input": "one"}, {"case_id": "same", "input": "two"}],
    )
    duplicate = _suite_registration(duplicate_source, project)
    assert duplicate.exit_code == 2, duplicate.output
    assert "duplicate case ID" in json.loads(duplicate.stdout)["message"]

    missing_id_source = tmp_path / "missing-id.jsonl"
    _write_cases(missing_id_source, [{"input": "no stable identity"}])
    missing_id = _suite_registration(missing_id_source, project, name="missing")
    assert missing_id.exit_code == 2, missing_id.output
    assert "explicit case_id" in json.loads(missing_id.stdout)["message"]

    listed = cli.invoke(
        app,
        ["dataset", "suites", "list", "--workspace", str(project), "--json"],
    )
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)["count"] == 0


def test_suite_versions_that_differ_only_by_case_have_distinct_snapshots(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source_upper = tmp_path / "upper.jsonl"
    source_lower = tmp_path / "lower.jsonl"
    _write_cases(source_upper, [{"case_id": "upper", "input": "one"}])
    _write_cases(source_lower, [{"case_id": "lower", "input": "two"}])

    upper = _suite_registration(source_upper, project, version="RC")
    lower = _suite_registration(source_lower, project, version="rc")
    reserved_device_stem = _suite_registration(source_upper, project, version="CON")

    assert upper.exit_code == 0, upper.output
    assert lower.exit_code == 0, lower.output
    assert reserved_device_stem.exit_code == 0, reserved_device_stem.output
    upper_snapshot = Path(json.loads(upper.stdout)["snapshot"])
    lower_snapshot = Path(json.loads(lower.stdout)["snapshot"])
    assert upper_snapshot != lower_snapshot
    assert upper_snapshot.read_text(encoding="utf-8") != lower_snapshot.read_text(encoding="utf-8")

    listed = cli.invoke(
        app,
        ["dataset", "suites", "list", "--name", "support", "--workspace", str(project), "--json"],
    )
    assert listed.exit_code == 0, listed.output
    references = {row["reference"] for row in json.loads(listed.stdout)["suites"]}
    assert references == {"support@RC", "support@rc", "support@CON"}


def test_failed_registration_cleanup_cannot_remove_concurrent_winner(
    tmp_path: Path, monkeypatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.jsonl"
    _write_cases(source, [{"case_id": "stable", "input": "same"}])
    first_entered_storage = threading.Event()
    second_waiting_for_lock = threading.Event()
    first_cleanup_finished = threading.Event()
    outcomes: dict[str, object] = {}

    original_acquire = suites_service._SuiteRegistrationLock.acquire

    def observed_acquire(lock) -> None:
        if threading.current_thread().name == "suite-register-b":
            second_waiting_for_lock.set()
        original_acquire(lock)

    monkeypatch.setattr(suites_service._SuiteRegistrationLock, "acquire", observed_acquire)

    original_register = Storage.register_dataset_suite

    def fail_first_registration(storage, **kwargs):
        if threading.current_thread().name == "suite-register-a":
            first_entered_storage.set()
            if not second_waiting_for_lock.wait(5):
                raise AssertionError("second registration did not contend on the version lock")
            raise sqlite3.OperationalError("injected catalog failure")
        if not first_cleanup_finished.wait(5):
            raise AssertionError("first registration cleanup did not finish")
        return original_register(storage, **kwargs)

    monkeypatch.setattr(Storage, "register_dataset_suite", fail_first_registration)

    original_remove = suites_service._remove_snapshot_if_unreferenced

    def signal_cleanup(*args):
        result = original_remove(*args)
        if threading.current_thread().name == "suite-register-a":
            first_cleanup_finished.set()
        return result

    monkeypatch.setattr(suites_service, "_remove_snapshot_if_unreferenced", signal_cleanup)

    def register_worker(label: str) -> None:
        try:
            outcomes[label] = suites_service.register_dataset_suite(
                project, "support", "1.0.0", source
            )
        except Exception as exc:  # noqa: BLE001 - capture worker failures for assertions
            outcomes[label] = exc

    first = threading.Thread(target=register_worker, args=("a",), name="suite-register-a")
    second = threading.Thread(target=register_worker, args=("b",), name="suite-register-b")
    first.start()
    assert first_entered_storage.wait(5)
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive() and not second.is_alive()
    assert isinstance(outcomes["a"], DatasetSuiteError)
    assert isinstance(outcomes["b"], dict)
    assert outcomes["b"]["created"] is True
    snapshot, record = resolve_dataset_suite(project, "support@1.0.0")
    assert record.case_count == 1
    assert snapshot.is_file()


def test_registration_replaces_unreferenced_snapshot_left_by_interrupted_writer(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    original_source = tmp_path / "original.jsonl"
    replacement_source = tmp_path / "replacement.jsonl"
    _write_cases(original_source, [{"case_id": "stable", "input": "original"}])
    _write_cases(replacement_source, [{"case_id": "stable", "input": "replacement"}])

    original = _suite_registration(original_source, project)
    assert original.exit_code == 0, original.output
    snapshot = Path(json.loads(original.stdout)["snapshot"])

    # Model a process stopping after its atomic snapshot publication but before its
    # catalog insert became durable: the file remains, but its catalog row is absent.
    database = sqlite3.connect(project / ".aibench" / "aibench.db")
    try:
        database.execute("DELETE FROM dataset_suites WHERE suite_name = ?", ("support",))
        database.commit()
    finally:
        database.close()

    replacement = _suite_registration(replacement_source, project)

    assert replacement.exit_code == 0, replacement.output
    assert json.loads(replacement.stdout)["created"] is True
    assert snapshot.read_text(encoding="utf-8") == replacement_source.read_text(encoding="utf-8")
    resolved, record = resolve_dataset_suite(project, "support@1.0.0")
    assert resolved == snapshot
    assert record.dataset_content_hash == json.loads(replacement.stdout)["suite"]["content_hash"]


def test_registration_lock_rejects_symlink_without_touching_external_target(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.jsonl"
    _write_cases(source, [{"case_id": "one", "input": "safe"}])
    suite_directory = Workspace.at(project).root / "dataset-suites" / "support"
    suite_directory.mkdir(parents=True)
    lock_path = suite_directory / f".register-{'1.0.0'.encode('ascii').hex()}.lock"
    outside_target = tmp_path / "outside.lock"
    outside_target.write_bytes(b"")
    try:
        lock_path.symlink_to(outside_target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"file symlinks are unavailable: {exc}")

    with pytest.raises(DatasetSuiteError):
        suites_service.register_dataset_suite(project, "support", "1.0.0", source)

    assert outside_target.read_bytes() == b""


def test_suite_listing_sanitizes_every_database_text_field(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.jsonl"
    _write_cases(source, [{"case_id": "one", "input": "safe"}])
    registered = _suite_registration(source, project)
    assert registered.exit_code == 0, registered.output

    injected = (
        "\x1b]0;forged title\x07\x1b[2J",
        "\x1b[31mhash\x1b[0m",
        "\x1b]8;;https://example.invalid\x07link\x1b]8;;\x07",
    )
    database = sqlite3.connect(project / ".aibench" / "aibench.db")
    try:
        database.execute(
            "UPDATE dataset_suites SET suite_name = ?, suite_version = ?, "
            "dataset_content_hash = ?, description = ?",
            (injected[0], injected[1], injected[2], injected[2]),
        )
        database.commit()
    finally:
        database.close()

    listed = cli.invoke(app, ["dataset", "suites", "list", "--workspace", str(project)])

    assert listed.exit_code == 0, listed.output
    assert "forged title" not in listed.stdout
    assert "https://example.invalid" not in listed.stdout
    assert "hash" in listed.stdout and "link" in listed.stdout
    assert "\x1b" not in listed.stdout and "\x07" not in listed.stdout


def test_suite_listing_renders_user_description_as_literal_text(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    source = tmp_path / "source.jsonl"
    _write_cases(source, [{"case_id": "one", "input": "safe"}])
    description = "\x1b]0;forged title\x07[bold]literal[/bold]\x1b[2J"
    registered = cli.invoke(
        app,
        [
            "dataset",
            "suites",
            "register",
            "support",
            "1.0.0",
            str(source),
            "--description",
            description,
            "--workspace",
            str(project),
        ],
    )
    assert registered.exit_code == 0, registered.output
    listed = cli.invoke(app, ["dataset", "suites", "list", "--workspace", str(project)])
    assert listed.exit_code == 0, listed.output
    assert "[bold]literal[/bold]" in listed.stdout
    assert "forged title" not in listed.stdout
    assert "\x1b" not in listed.stdout and "\x07" not in listed.stdout

    shown = cli.invoke(
        app,
        ["dataset", "suites", "show", "support@1.0.0", "--workspace", str(project)],
    )
    assert shown.exit_code == 0, shown.output
    assert "[bold]literal[/bold]" in shown.stdout
    assert "forged title" not in shown.stdout
    assert "\x1b" not in shown.stdout and "\x07" not in shown.stdout


def test_run_dry_run_uses_registered_suite_snapshot(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    base_dataset = harness.dataset({"plan-case": "base"})
    plan = harness.plan(dataset=base_dataset, application=harness.cli_app())
    suite_source = tmp_path / "suite.jsonl"
    _write_cases(
        suite_source,
        [
            {"case_id": "suite-a", "input": "selected", "expected_output": "yes"},
            {"case_id": "suite-b", "input": "selected", "expected_output": "yes"},
        ],
    )
    project = harness.workspace.root.parent
    registered = _suite_registration(suite_source, project)
    assert registered.exit_code == 0, registered.output
    registered_payload = json.loads(registered.stdout)

    preview = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--dataset-suite",
            "support@1.0.0",
            "--workspace",
            str(project),
            "--trust-local-app",
            "--dry-run",
            "--json",
        ],
    )
    assert preview.exit_code == 0, preview.output
    result = json.loads(preview.stdout)
    assert result["scope"]["case_ids"] == ["suite-a", "suite-b"]
    assert harness.count() == 0

    Path(registered_payload["snapshot"]).write_text(
        '{"case_id":"tampered","input":"changed"}\n', encoding="utf-8"
    )
    rejected = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--dataset-suite",
            "support@1.0.0",
            "--workspace",
            str(project),
            "--trust-local-app",
            "--json",
        ],
    )
    assert rejected.exit_code == 2, rejected.output
    assert "has changed" in json.loads(rejected.stdout)["message"]
    assert harness.count() == 0


def test_run_rejects_positional_dataset_with_suite_reference(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.dataset({"one": "hi"})
    dataset = tmp_path / "data.jsonl"
    result = cli.invoke(
        app,
        [
            "run",
            str(dataset),
            "--dataset-suite",
            "support@1.0.0",
            "--json",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "cannot be combined" in json.loads(result.stdout)["message"]
    assert harness.count() == 0
