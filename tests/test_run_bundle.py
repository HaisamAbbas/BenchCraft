from __future__ import annotations

import hashlib
import json
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ConflictError, ValidationError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    ApplicationSpec,
    Approval,
    ArtifactRef,
    BenchmarkCase,
    DatasetManifest,
    EvaluationPlan,
    ExecutionResult,
    ExecutionStatus,
    ObservationClaim,
    ObservationState,
    RunManifest,
    deep_unfreeze,
)
from aibench.core.plans import ExecutablePlan
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage, _hash_of
from aibench.storage.run_bundle import export_run_bundle, import_run_bundle

runner = CliRunner()


def _seed_terminal_run(
    project: Path,
    *,
    run_id: str = "portable-run",
    seed_bytes: bytes = b'{"world":"portable-seed"}',
) -> str:
    workspace = Workspace.at(project)
    storage = Storage(Database.open_workspace(workspace))
    try:
        dataset_hash = "sha256:" + "1" * 64
        dataset = DatasetManifest(dataset_id="portable", content_hash=dataset_hash, case_count=1)
        storage.commit_dataset(dataset)
        storage.commit_cases(dataset_hash, [BenchmarkCase(case_id="case-1", input="question")])

        application = ApplicationSpec(application_id="portable-app", runner="cli", target="app.py")
        storage.commit_application(application)
        storage.commit_observation(
            ObservationClaim(
                observation_id="portable-profile",
                capability="text-generation",
                state=ObservationState.OBSERVED,
                evidence_refs=("case:case-1#input",),
            ),
            application_id=application.application_id,
        )
        application_hash = content_hash(application.model_dump(mode="json"))
        plan = EvaluationPlan(plan_id="portable-plan", objectives=("correctness",))
        storage.commit_plan(plan)
        plan_hash = content_hash(plan.model_dump(mode="json"))
        artifacts = ArtifactStore(workspace.artifacts_dir)
        application_ref = artifacts.write_bytes(
            application.model_dump_json().encode("utf-8"),
            mime_type="application/json",
            artifact_id="portable-application-spec",
        )
        commit_verified_artifact(artifacts, storage, application_ref)
        seed_ref = artifacts.write_bytes(
            seed_bytes,
            mime_type="application/json",
            artifact_id="portable-world-seed",
        )
        commit_verified_artifact(artifacts, storage, seed_ref)
        storage.commit_run(
            RunManifest(
                run_id=run_id,
                dataset_hash=dataset_hash,
                application_hash=application_hash,
                application_id=application.application_id,
                plan_hash=plan_hash,
                parameters={
                    "application_artifact_id": application_ref.artifact_id,
                    "test_world": {
                        "world_id": "portable-world",
                        "seed_hash": seed_ref.digest,
                        "seed_artifact_id": seed_ref.artifact_id,
                    },
                },
            ),
            status="succeeded",
        )
        storage.commit_approval(
            Approval(
                approval_id=f"{run_id}:approval",
                scope_hash="portable-approval-scope",
                granted_by="cli:aibench run",
                allowed_actions=("run",),
            )
        )

        ref = artifacts.write_bytes(
            b"portable output", mime_type="text/plain", run_id=run_id, artifact_id="artifact-1"
        )
        commit_verified_artifact(artifacts, storage, ref)
        storage.commit_execution_attempt(
            ExecutionResult(
                execution_id=f"{run_id}-execution-1",
                run_id=run_id,
                case_id="case-1",
                status=ExecutionStatus.OK,
                output={"artifact": ref.model_dump(mode="json")},
            )
        )
        storage.append_run_event(run_id, "run_completed", {"ok": True})
        storage.add_run_tag(run_id, "portable")
        return ref.digest
    finally:
        storage.db.close()


def _rewrite_bundle_data(
    source: Path, destination: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    with zipfile.ZipFile(source) as original:
        payloads = {member.filename: original.read(member) for member in original.infolist()}
    manifest = json.loads(payloads["manifest.json"])
    data = json.loads(payloads["bundle/data.json"])
    mutate(data)
    data_bytes = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payloads["bundle/data.json"] = data_bytes
    record = next(item for item in manifest["files"] if item["path"] == "bundle/data.json")
    record["size_bytes"] = len(data_bytes)
    record["sha256"] = hashlib.sha256(data_bytes).hexdigest()
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as rewritten:
        for name, payload in payloads.items():
            rewritten.writestr(name, manifest_bytes if name == "manifest.json" else payload)


def _rewrite_bundle_plan_artifact(
    source: Path, destination: Path, *, artifact_id: str, raw: bytes
) -> None:
    with zipfile.ZipFile(source) as original:
        payloads = {member.filename: original.read(member) for member in original.infolist()}
    manifest = json.loads(payloads["manifest.json"])
    data = json.loads(payloads["bundle/data.json"])
    tables = data["tables"]
    artifacts = tables["artifacts"]
    artifact_row = next(row for row in artifacts if row["artifact_id"] == artifact_id)
    old_digest = artifact_row["digest"].split(":", 1)[1]
    digest = hashlib.sha256(raw).hexdigest()
    ref = ArtifactRef.model_validate(
        {name: artifact_row[name] for name in ArtifactRef.model_fields}
    ).model_copy(
        update={
            "digest": f"sha256:{digest}",
            "uri": f"artifacts/{digest[:2]}/{digest}",
            "size_bytes": len(raw),
        }
    )
    artifact_row.update(ref.model_dump(mode="json"))
    artifact_row["content_hash"] = content_hash(ref.model_dump_json())

    run_row = tables["runs"][0]
    run_manifest = json.loads(run_row["data"])
    run_manifest["plan_hash"] = ref.digest
    run_row["plan_hash"] = ref.digest
    run_row["data"] = json.dumps(run_manifest, sort_keys=True, separators=(",", ":"))
    run_row["content_hash"] = content_hash(run_row["data"])

    payloads.pop(f"bundle/artifacts/{old_digest}")
    payloads[f"bundle/artifacts/{digest}"] = raw
    data_bytes = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payloads["bundle/data.json"] = data_bytes
    manifest["files"] = [
        {
            "path": name,
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
        for name, payload in sorted(payloads.items())
        if name != "manifest.json"
    ]
    manifest_bytes = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as rewritten:
        for name, payload in payloads.items():
            rewritten.writestr(name, manifest_bytes if name == "manifest.json" else payload)


def test_run_bundle_round_trips_artifacts_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    digest = _seed_terminal_run(source)
    source_uri = (
        Workspace.at(source).artifacts_dir / digest.split(":", 1)[1][:2] / digest.split(":", 1)[1]
    ).as_posix()
    bundle = tmp_path / "run.zip"

    exported = export_run_bundle(source, "portable-run", bundle)
    assert exported["artifact_count"] == 3
    first = import_run_bundle(bundle, destination)
    second = import_run_bundle(bundle, destination)
    assert first["imported"] is True
    assert second["already_present"] is True
    destination_db = Database.open(Workspace.at(destination).db_path)
    try:
        imported_approval = Storage(destination_db).get_approval("portable-run:approval")
        assert imported_approval is not None
        assert imported_approval.granted_by == "cli:aibench run"
    finally:
        destination_db.close()

    workspace = Workspace.at(destination)
    db = Database.open(workspace.db_path)
    storage = Storage(db)
    try:
        record = storage.get_run("portable-run")
        assert record is not None and record.status == "succeeded"
        [execution] = storage.list_execution_attempts("portable-run")
        reference = execution.output["artifact"]
        assert reference["uri"].startswith(workspace.artifacts_dir.as_posix())
        assert not reference["uri"].startswith(source_uri)
        assert storage.list_observations("portable-app")[0].evidence_refs == ("case:case-1#input",)
        ref = storage.get_artifact("artifact-1")
        assert ref is not None
        assert ArtifactStore(workspace.artifacts_dir).read_bytes(ref) == b"portable output"
        assert [case.case_id for case in storage.list_cases(record.manifest.dataset_hash)] == [
            "case-1"
        ]
        assert len(storage.list_run_events("portable-run")) == 1
        assert storage.list_run_metadata(["portable-run"])["portable-run"]["tags"] == ["portable"]
    finally:
        db.close()


def test_run_bundle_rejects_active_run_and_does_not_publish_archive(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed_terminal_run(source)
    db = Database.open(Workspace.at(source).db_path)
    Storage(db).update_run_status("portable-run", "running")
    db.close()

    with pytest.raises(ValidationError, match="terminal runs"):
        export_run_bundle(source, "portable-run", tmp_path / "active.zip")
    assert not (tmp_path / "active.zip").exists()


def test_run_bundle_detects_tampered_artifact_before_database_import(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)

    tampered = tmp_path / "tampered.zip"
    with zipfile.ZipFile(bundle) as original, zipfile.ZipFile(tampered, "w") as changed:
        for member in original.infolist():
            payload = original.read(member)
            if member.filename.startswith("bundle/artifacts/"):
                payload = b"x" * len(payload)
            changed.writestr(member.filename, payload)

    with pytest.raises(ValidationError, match="integrity check"):
        import_run_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()


@pytest.mark.parametrize(("calls", "calls_without_tokens"), [(-1, 0), (1, 2)])
def test_run_bundle_rejects_impossible_usage_event_counts(
    tmp_path: Path, calls: int, calls_without_tokens: int
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    tampered = tmp_path / "tampered.zip"

    def add_invalid_usage(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        event = {
            "usage_event_id": "planner-invalid-count",
            "run_id": "portable-run",
            "role": "planner",
            "provider": None,
            "tokens": {},
            "calls": calls,
            "calls_without_tokens": calls_without_tokens,
            "cost": None,
        }
        raw = json.dumps(event, sort_keys=True, separators=(",", ":"))
        tables["usage_events"].append(
            {
                "usage_event_id": event["usage_event_id"],
                "run_id": event["run_id"],
                "role": event["role"],
                "provider": event["provider"],
                "cost": event["cost"],
                "content_hash": _hash_of(raw),
                "data": raw,
            }
        )

    _rewrite_bundle_data(bundle, tampered, add_invalid_usage)
    with pytest.raises(ValidationError, match="usage_events data is invalid"):
        import_run_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_rejects_rehashed_execution_ref_to_missing_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    tampered = tmp_path / "missing-artifact-ref.zip"

    def inject_missing_artifact_ref(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        artifacts = tables["artifacts"]
        executions = tables["execution_attempts"]
        assert isinstance(artifacts, list) and isinstance(executions, list) and executions
        artifact_row = next(row for row in artifacts if row["artifact_id"] == "artifact-1")
        ref = ArtifactRef.model_validate(
            {name: artifact_row[name] for name in ArtifactRef.model_fields}
        ).model_copy(update={"artifact_id": "missing-output-artifact"})
        execution = executions[0]
        execution_data = json.loads(execution["data"])
        execution_data["output"] = {"artifact": ref.model_dump(mode="json")}
        execution["data"] = json.dumps(execution_data, sort_keys=True, separators=(",", ":"))
        execution["content_hash"] = content_hash(execution["data"])

    _rewrite_bundle_data(bundle, tampered, inject_missing_artifact_ref)
    with pytest.raises(ValidationError, match="refers to a missing artifact catalog entry"):
        import_run_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_conflict_rolls_back_all_catalog_rows(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    import_run_bundle(bundle, destination)

    workspace = Workspace.at(destination)
    db = Database.open(workspace.db_path)
    db.connection.execute("DELETE FROM run_tags WHERE run_id = ?", ("portable-run",))
    db.connection.execute(
        "UPDATE run_events SET payload = ? WHERE run_id = ? AND sequence = ?",
        ('{"tampered":true}', "portable-run", 1),
    )
    db.close()

    with pytest.raises(ConflictError, match="run_events already contains different data"):
        import_run_bundle(bundle, destination)
    db = Database.open(workspace.db_path)
    try:
        assert (
            db.connection.execute(
                "SELECT count(*) FROM run_tags WHERE run_id = ?", ("portable-run",)
            ).fetchone()[0]
            == 0
        )
        assert (
            db.connection.execute(
                "SELECT payload FROM run_events WHERE run_id = ?", ("portable-run",)
            ).fetchone()[0]
            == '{"tampered":true}'
        )
    finally:
        db.close()


def test_run_bundle_rejects_rehashed_invalid_model_data_before_workspace_creation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    malformed = tmp_path / "malformed.zip"

    def make_application_invalid(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        applications = tables["applications"]
        assert isinstance(applications, list) and applications
        row = applications[0]
        assert isinstance(row, dict)
        row["data"] = "{}"
        row["content_hash"] = content_hash("{}")

    _rewrite_bundle_data(bundle, malformed, make_application_invalid)
    with pytest.raises(ValidationError, match="applications data is invalid"):
        import_run_bundle(malformed, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_rejects_unrelated_frozen_application_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    tampered = tmp_path / "unrelated-application.zip"

    def point_application_at_output(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        runs = tables["runs"]
        assert isinstance(runs, list) and len(runs) == 1
        row = runs[0]
        assert isinstance(row, dict)
        manifest = json.loads(row["data"])
        manifest["parameters"]["application_artifact_id"] = "artifact-1"
        row["data"] = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        row["content_hash"] = content_hash(row["data"])

    _rewrite_bundle_data(bundle, tampered, point_application_at_output)
    with pytest.raises(ValidationError, match="frozen application artifact"):
        import_run_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_rejects_seed_hash_that_disagrees_with_seed_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    tampered = tmp_path / "mismatched-seed.zip"

    def change_seed_hash(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        runs = tables["runs"]
        assert isinstance(runs, list) and len(runs) == 1
        row = runs[0]
        assert isinstance(row, dict)
        manifest = json.loads(row["data"])
        manifest["parameters"]["test_world"]["seed_hash"] = "sha256:" + "f" * 64
        row["data"] = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        row["content_hash"] = content_hash(row["data"])

    _rewrite_bundle_data(bundle, tampered, change_seed_hash)
    with pytest.raises(ValidationError, match="seed does not match its artifact"):
        import_run_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_rejects_frozen_world_without_world_id(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    tampered = tmp_path / "missing-world-id.zip"

    def remove_world_id(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        runs = tables["runs"]
        assert isinstance(runs, list) and len(runs) == 1
        row = runs[0]
        assert isinstance(row, dict)
        manifest = json.loads(row["data"])
        del manifest["parameters"]["test_world"]["world_id"]
        row["data"] = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        row["content_hash"] = content_hash(row["data"])

    _rewrite_bundle_data(bundle, tampered, remove_world_id)
    with pytest.raises(ValidationError, match="seed does not match its artifact"):
        import_run_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()


@pytest.mark.parametrize("seed_bytes", [b"{", b'"scalar"'])
def test_run_bundle_rejects_invalid_frozen_seed_payload(tmp_path: Path, seed_bytes: bytes) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _seed_terminal_run(source, seed_bytes=seed_bytes)
    bundle = tmp_path / "run.zip"

    with pytest.raises(ValidationError, match="frozen test-world seed"):
        export_run_bundle(source, "portable-run", bundle)
    assert not bundle.exists()


def test_run_bundle_rejects_columns_that_disagree_with_frozen_run_manifest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    mismatched = tmp_path / "mismatched.zip"

    def change_run_dataset_column(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        runs = tables["runs"]
        assert isinstance(runs, list) and len(runs) == 1
        run = runs[0]
        assert isinstance(run, dict)
        run["dataset_hash"] = "sha256:" + "f" * 64

    _rewrite_bundle_data(bundle, mismatched, change_run_dataset_column)
    with pytest.raises(ValidationError, match="run record fails its integrity check"):
        import_run_bundle(mismatched, destination)
    assert not Workspace.at(destination).root.exists()


def test_import_relocates_artifact_refs_without_mutating_other_equal_strings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    digest = _seed_terminal_run(source)
    source_uri = (
        Workspace.at(source).artifacts_dir / digest.split(":", 1)[1][:2] / digest.split(":", 1)[1]
    ).as_posix()
    db = Database.open(Workspace.at(source).db_path)
    try:
        row = db.connection.execute(
            "SELECT data FROM cases WHERE case_id = ?", ("case-1",)
        ).fetchone()
        case = json.loads(row["data"])
        case["metadata"] = {"literal_source_uri": source_uri}
        case_json = json.dumps(case, ensure_ascii=False, separators=(",", ":"))
        db.connection.execute(
            "UPDATE cases SET data = ?, content_hash = ? WHERE case_id = ?",
            (case_json, content_hash(case_json), "case-1"),
        )
    finally:
        db.close()
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    import_run_bundle(bundle, destination)

    db = Database.open(Workspace.at(destination).db_path)
    try:
        cases = Storage(db).list_cases("sha256:" + "1" * 64)
        assert cases[0].metadata["literal_source_uri"] == source_uri
        execution = Storage(db).list_execution_attempts("portable-run")[0]
        assert execution.output["artifact"]["uri"] != source_uri
    finally:
        db.close()


@pytest.mark.parametrize("table", ["applications", "plans"])
@pytest.mark.parametrize("extra", [False, True])
def test_run_bundle_requires_exactly_its_model_dependencies(
    tmp_path: Path, table: str, extra: bool
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    changed = tmp_path / f"{table}-{extra}.zip"

    def change_dependency(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        dependencies = tables[table]
        assert isinstance(dependencies, list) and dependencies
        if not extra:
            dependencies.clear()
            return
        original = dependencies[0]
        assert isinstance(original, dict)
        added = dict(original)
        if table == "applications":
            application = ApplicationSpec(
                application_id="unrelated-app", runner="cli", target="other.py"
            )
            model_json = application.model_dump_json()
            added.update(
                application_id=application.application_id,
                content_hash=content_hash(model_json),
                runner=application.runner.value,
                target=application.target,
                data=model_json,
            )
        else:
            plan = EvaluationPlan(plan_id="unrelated-plan", objectives=("other",))
            model_json = plan.model_dump_json()
            added.update(
                plan_id=plan.plan_id,
                content_hash=content_hash(model_json),
                policy_hash=plan.policy_hash,
                data=model_json,
            )
        dependencies.append(added)

    _rewrite_bundle_data(bundle, changed, change_dependency)
    expected = "application dependency" if table == "applications" else "plan"
    with pytest.raises(ValidationError, match=expected):
        import_run_bundle(changed, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_rejects_trace_observations_for_another_run(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    injected = tmp_path / "trace-injection.zip"

    def inject_foreign_trace(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        traces = tables["trace_observations"]
        assert isinstance(traces, list)
        traces.append(
            {
                "import_id": "injected-import",
                "run_id": "other-run",
                "trace_id": "injected-trace",
                "execution_id": None,
                "complete": 1,
                "data": json.dumps({"partial_reasons": []}),
                "created_at": "2026-10-10T00:00:00+00:00",
            }
        )

    _rewrite_bundle_data(bundle, injected, inject_foreign_trace)
    db = Database.open_workspace(Workspace.at(destination))
    try:
        Storage(db).commit_run(
            RunManifest(
                run_id="other-run",
                dataset_hash="sha256:" + "2" * 64,
                application_hash="sha256:" + "3" * 64,
                plan_hash="sha256:" + "4" * 64,
            ),
            status="completed",
        )
    finally:
        db.close()

    with pytest.raises(ValidationError, match="unrelated trace_observations"):
        import_run_bundle(injected, destination)
    db = Database.open(Workspace.at(destination).db_path)
    try:
        assert (
            db.connection.execute(
                "SELECT count(*) FROM trace_observations WHERE run_id = ?", ("other-run",)
            ).fetchone()[0]
            == 0
        )
    finally:
        db.close()


def test_run_bundle_normalizes_detached_embedded_artifact_ownership(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    db = Database.open(Workspace.at(source).db_path)
    try:
        storage = Storage(db)
        run = storage.get_run("portable-run")
        assert run is not None
        storage.commit_run(
            run.manifest.model_copy(update={"run_id": "owner-run"}), status="completed"
        )
        artifact_row = db.connection.execute(
            "SELECT * FROM artifacts WHERE artifact_id = ?", ("artifact-1",)
        ).fetchone()
        ref = ArtifactRef(
            artifact_id=artifact_row["artifact_id"],
            digest=artifact_row["digest"],
            uri=artifact_row["uri"],
            mime_type=artifact_row["mime_type"],
            size_bytes=artifact_row["size_bytes"],
            redaction=artifact_row["redaction"],
            run_id="owner-run",
        )
        db.connection.execute(
            "UPDATE artifacts SET run_id = ?, content_hash = ? WHERE artifact_id = ?",
            ("owner-run", content_hash(ref.model_dump_json()), ref.artifact_id),
        )
        execution_row = db.connection.execute(
            "SELECT data FROM execution_attempts WHERE execution_id = ?",
            ("portable-run-execution-1",),
        ).fetchone()
        execution_data = json.loads(execution_row["data"])
        execution_data["output"]["artifact"]["run_id"] = "owner-run"
        execution_json = json.dumps(execution_data, separators=(",", ":"))
        db.connection.execute(
            "UPDATE execution_attempts SET data = ?, content_hash = ? WHERE execution_id = ?",
            (execution_json, content_hash(execution_json), "portable-run-execution-1"),
        )
    finally:
        db.close()
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    import_run_bundle(bundle, destination)

    db = Database.open(Workspace.at(destination).db_path)
    try:
        imported_ref = Storage(db).get_artifact("artifact-1")
        imported_execution = Storage(db).list_execution_attempts("portable-run")[0]
        assert imported_ref is not None and imported_ref.run_id is None
        assert imported_execution.output["artifact"]["run_id"] is None
    finally:
        db.close()


def test_run_bundle_rejects_unknown_run_status(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    invalid = tmp_path / "unknown-status.zip"

    def set_unknown_status(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        runs = tables["runs"]
        assert isinstance(runs, list) and len(runs) == 1
        run = runs[0]
        assert isinstance(run, dict)
        run["status"] = "banana"

    _rewrite_bundle_data(bundle, invalid, set_unknown_status)
    with pytest.raises(ValidationError, match="unsupported run status"):
        import_run_bundle(invalid, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_rejects_unknown_catalog_columns_before_workspace_creation(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "run.zip"
    export_run_bundle(source, "portable-run", bundle)
    incompatible = tmp_path / "incompatible.zip"

    def add_unknown_column(data: dict[str, Any]) -> None:
        tables = data["tables"]
        assert isinstance(tables, dict)
        runs = tables["runs"]
        assert isinstance(runs, list) and len(runs) == 1
        run = runs[0]
        assert isinstance(run, dict)
        run["unexpected"] = "unsupported-column"

    _rewrite_bundle_data(bundle, incompatible, add_unknown_column)
    with pytest.raises(ValidationError, match="runs schema does not match"):
        import_run_bundle(incompatible, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_preserves_frozen_manual_plan_artifact_dependency(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    workspace = Workspace.at(source)
    db = Database.open(workspace.db_path)
    try:
        storage = Storage(db)
        old = storage.get_run("portable-run")
        assert old is not None
        frozen_plan = (
            ExecutablePlan(
                plan_id="portable-frozen-plan", dataset="dataset.jsonl", application="app.yaml"
            )
            .model_dump_json()
            .encode("utf-8")
        )
        ref = ArtifactStore(workspace.artifacts_dir).write_bytes(
            frozen_plan,
            mime_type="application/json",
            run_id="portable-run",
            artifact_id="portable-frozen-plan",
        )
        commit_verified_artifact(ArtifactStore(workspace.artifacts_dir), storage, ref)
        manifest = old.manifest.model_copy(
            update={
                "plan_hash": ref.digest,
                "parameters": {
                    **deep_unfreeze(old.manifest.parameters),
                    "mode": "manual_plan",
                    "plan_artifact_id": ref.artifact_id,
                },
            }
        )
        manifest_json = manifest.model_dump_json()
        db.connection.execute(
            "UPDATE runs SET plan_hash = ?, data = ?, content_hash = ? WHERE run_id = ?",
            (manifest.plan_hash, manifest_json, content_hash(manifest_json), "portable-run"),
        )
    finally:
        db.close()

    bundle = tmp_path / "manual-plan.zip"
    export_run_bundle(source, "portable-run", bundle)
    import_run_bundle(bundle, destination)
    imported_workspace = Workspace.at(destination)
    imported_db = Database.open(imported_workspace.db_path)
    try:
        imported_storage = Storage(imported_db)
        imported_run = imported_storage.get_run("portable-run")
        assert imported_run is not None
        plan_ref = imported_storage.get_artifact("portable-frozen-plan")
        assert plan_ref is not None
        assert ArtifactStore(imported_workspace.artifacts_dir).read_bytes(plan_ref) == frozen_plan
        assert imported_run.manifest.plan_hash == plan_ref.digest
        assert ExecutablePlan.model_validate_json(frozen_plan).plan_id == "portable-frozen-plan"
    finally:
        imported_db.close()


def test_run_bundle_rejects_rehashed_invalid_frozen_plan_artifact(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    workspace = Workspace.at(source)
    db = Database.open(workspace.db_path)
    try:
        storage = Storage(db)
        run = storage.get_run("portable-run")
        assert run is not None
        frozen_plan = (
            ExecutablePlan(
                plan_id="portable-frozen-plan", dataset="dataset.jsonl", application="app.yaml"
            )
            .model_dump_json()
            .encode("utf-8")
        )
        artifacts = ArtifactStore(workspace.artifacts_dir)
        ref = artifacts.write_bytes(
            frozen_plan,
            mime_type="application/json",
            run_id="portable-run",
            artifact_id="portable-frozen-plan",
        )
        commit_verified_artifact(artifacts, storage, ref)
        manifest = run.manifest.model_copy(
            update={
                "plan_hash": ref.digest,
                "parameters": {
                    **deep_unfreeze(run.manifest.parameters),
                    "mode": "manual_plan",
                    "plan_artifact_id": ref.artifact_id,
                },
            }
        )
        manifest_json = manifest.model_dump_json()
        db.connection.execute(
            "UPDATE runs SET plan_hash = ?, data = ?, content_hash = ? WHERE run_id = ?",
            (manifest.plan_hash, manifest_json, content_hash(manifest_json), "portable-run"),
        )
    finally:
        db.close()

    bundle = tmp_path / "manual-plan.zip"
    export_run_bundle(source, "portable-run", bundle)
    tampered = tmp_path / "invalid-plan.zip"
    _rewrite_bundle_plan_artifact(
        bundle, tampered, artifact_id="portable-frozen-plan", raw=b'{"compiled_plan":"portable"}'
    )
    with pytest.raises(ValidationError, match="frozen plan artifact is invalid"):
        import_run_bundle(tampered, destination)
    assert not Workspace.at(destination).root.exists()


def test_run_bundle_preserves_case_metadata_that_names_external_artifact_id(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    source_db = Database.open(Workspace.at(source).db_path)
    try:
        row = source_db.connection.execute(
            "SELECT data FROM cases WHERE dataset_content_hash = ? AND case_id = ?",
            ("sha256:" + "1" * 64, "case-1"),
        ).fetchone()
        assert row is not None
        case = json.loads(row["data"])
        case["metadata"] = {"external_artifact_id": "foreign-system-object"}
        case_data = json.dumps(case, sort_keys=True, separators=(",", ":"))
        source_db.connection.execute(
            "UPDATE cases SET data = ?, content_hash = ? WHERE dataset_content_hash = ? "
            "AND case_id = ?",
            (
                case_data,
                content_hash(case_data),
                "sha256:" + "1" * 64,
                "case-1",
            ),
        )
    finally:
        source_db.close()

    bundle = tmp_path / "external-case-metadata.zip"
    export_run_bundle(source, "portable-run", bundle)
    import_run_bundle(bundle, destination)
    destination_db = Database.open(Workspace.at(destination).db_path)
    try:
        stored = destination_db.connection.execute(
            "SELECT data FROM cases WHERE dataset_content_hash = ? AND case_id = ?",
            ("sha256:" + "1" * 64, "case-1"),
        ).fetchone()
        assert stored is not None
        assert json.loads(stored["data"])["metadata"] == {
            "external_artifact_id": "foreign-system-object"
        }
    finally:
        destination_db.close()


def test_run_bundle_validates_delegated_suite_plan_identity(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    db = Database.open(Workspace.at(source).db_path)
    try:
        run = Storage(db).get_run("portable-run")
        assert run is not None
        suite = {"plugin": "portable-plugin", "eval_type": "custom"}
        manifest = run.manifest.model_copy(
            update={
                "plan_hash": content_hash({"kind": "delegated_suite", **suite}),
                "parameters": {
                    **deep_unfreeze(run.manifest.parameters),
                    "mode": "delegated_suite",
                    "suite": suite,
                },
            }
        )
        manifest_json = manifest.model_dump_json()
        db.connection.execute(
            "UPDATE runs SET plan_hash = ?, data = ?, content_hash = ? WHERE run_id = ?",
            (manifest.plan_hash, manifest_json, content_hash(manifest_json), "portable-run"),
        )
    finally:
        db.close()

    bundle = tmp_path / "delegated-suite.zip"
    export_run_bundle(source, "portable-run", bundle)
    result = import_run_bundle(bundle, destination)
    assert result["imported"] is True


@pytest.mark.parametrize("prepopulate_unselected_case", [False, True])
def test_run_bundle_exports_smoke_subset_when_only_selected_cases_are_attached(
    tmp_path: Path, prepopulate_unselected_case: bool
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    db = Database.open(Workspace.at(source).db_path)
    try:
        storage = Storage(db)
        dataset = storage.get_dataset("sha256:" + "1" * 64)
        run = storage.get_run("portable-run")
        assert dataset is not None and run is not None
        expanded_dataset = dataset.model_copy(update={"case_count": 2})
        dataset_json = expanded_dataset.model_dump_json()
        db.connection.execute(
            "UPDATE datasets SET case_count = ?, manifest_hash = ? WHERE content_hash = ?",
            (2, content_hash(dataset_json), dataset.content_hash),
        )
        if prepopulate_unselected_case:
            storage.commit_cases(
                dataset.content_hash,
                [BenchmarkCase(case_id="case-2", input="not selected", source_line=2)],
            )
        manifest = run.manifest.model_copy(
            update={
                "plan_hash": content_hash({"kind": "developer_smoke", "case_ids": ["case-1"]}),
                "parameters": {**deep_unfreeze(run.manifest.parameters), "mode": "developer_smoke"},
            }
        )
        manifest_json = manifest.model_dump_json()
        db.connection.execute(
            "UPDATE runs SET plan_hash = ?, data = ?, content_hash = ? WHERE run_id = ?",
            (manifest.plan_hash, manifest_json, content_hash(manifest_json), "portable-run"),
        )
    finally:
        db.close()

    bundle = tmp_path / "smoke.zip"
    export_run_bundle(source, "portable-run", bundle)
    import_run_bundle(bundle, destination)
    db = Database.open(Workspace.at(destination).db_path)
    try:
        cases = Storage(db).list_cases("sha256:" + "1" * 64)
        assert [case.case_id for case in cases] == ["case-1"]
    finally:
        db.close()


def test_run_bundle_smoke_preserves_nonlexical_execution_order(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    db = Database.open(Workspace.at(source).db_path)
    try:
        storage = Storage(db)
        dataset = storage.get_dataset("sha256:" + "1" * 64)
        run = storage.get_run("portable-run")
        assert dataset is not None and run is not None
        expanded_dataset = dataset.model_copy(update={"case_count": 3})
        dataset_json = expanded_dataset.model_dump_json()
        db.connection.execute(
            "UPDATE datasets SET case_count = ?, manifest_hash = ? WHERE content_hash = ?",
            (3, content_hash(dataset_json), dataset.content_hash),
        )
        storage.commit_cases(
            dataset.content_hash,
            [
                BenchmarkCase(case_id="case-z", input="first selected", source_line=2),
                BenchmarkCase(case_id="case-a", input="second selected", source_line=3),
            ],
        )
        db.connection.execute(
            "DELETE FROM execution_attempts WHERE run_id = ?", (run.manifest.run_id,)
        )
        for case_id in ("case-z", "case-a"):
            storage.commit_execution_attempt(
                ExecutionResult(
                    execution_id=f"smoke-{case_id}",
                    run_id=run.manifest.run_id,
                    case_id=case_id,
                    status=ExecutionStatus.OK,
                )
            )
        case_ids = ["case-z", "case-a"]
        manifest = run.manifest.model_copy(
            update={
                "plan_hash": content_hash({"kind": "developer_smoke", "case_ids": case_ids}),
                "parameters": {**deep_unfreeze(run.manifest.parameters), "mode": "developer_smoke"},
            }
        )
        manifest_json = manifest.model_dump_json()
        db.connection.execute(
            "UPDATE runs SET plan_hash = ?, data = ?, content_hash = ? WHERE run_id = ?",
            (manifest.plan_hash, manifest_json, content_hash(manifest_json), run.manifest.run_id),
        )
    finally:
        db.close()

    bundle = tmp_path / "smoke-order.zip"
    export_run_bundle(source, "portable-run", bundle)
    import_run_bundle(bundle, destination)
    db = Database.open(Workspace.at(destination).db_path)
    try:
        cases = Storage(db).list_cases("sha256:" + "1" * 64)
        assert [case.case_id for case in cases] == ["case-z", "case-a"]
        execution_rows = db.connection.execute(
            "SELECT case_id FROM execution_attempts WHERE run_id = ? ORDER BY rowid",
            ("portable-run",),
        ).fetchall()
        assert [row["case_id"] for row in execution_rows] == ["case-z", "case-a"]
    finally:
        db.close()


def test_runs_cli_exports_and_imports_bundles(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    _seed_terminal_run(source)
    bundle = tmp_path / "cli-run.zip"

    exported = runner.invoke(
        app,
        ["runs", "export", "portable-run", str(bundle), "--workspace", str(source), "--json"],
    )
    assert exported.exit_code == 0, exported.output
    assert json.loads(exported.stdout)["run_id"] == "portable-run"
    imported = runner.invoke(
        app, ["runs", "import", str(bundle), "--workspace", str(destination), "--json"]
    )
    assert imported.exit_code == 0, imported.output
    assert json.loads(imported.stdout)["imported"] is True
