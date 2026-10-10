"""Portable, integrity-checked bundles for completed benchmark runs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import tempfile
import unicodedata
import zipfile
from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from aibench.core.errors import ConflictError, ValidationError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    ApplicationSpec,
    Approval,
    ArtifactRef,
    BenchmarkCase,
    DatasetManifest,
    EvaluationPlan,
    EvaluationResult,
    ExecutionResult,
    ObservationClaim,
    RunManifest,
    UsageEvent,
    WorkItem,
)
from aibench.core.plans import ExecutablePlan
from aibench.services.runs import RESUMABLE_STATES
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage, _hash_of
from aibench.storage.workspace_backup import (
    _MAX_ENTRIES,
    _MAX_MANIFEST_BYTES,
    _MAX_TOTAL_BYTES,
    _new_archive_path,
    _pinned_zip_archive,
    _write_archive,
    _zip_member_is_link,
)

_FORMAT = "aibench-run-bundle"
_VERSION = 1
_MANIFEST = "manifest.json"
_DATA_PATH = "bundle/data.json"
_ARTIFACT_PREFIX = "bundle/artifacts/"
_MAX_DATA_BYTES = 256 * 1024 * 1024
_MAX_ARTIFACT_BYTES = 200 * 1024 * 1024
_MAX_ROWS = 1_000_000
_TERMINAL_RUN_STATES = frozenset(
    {"completed", "cancelled", "budget_exhausted", "failed", "succeeded"}
)
_TABLES = (
    "datasets",
    "cases",
    "applications",
    "profiles",
    "plans",
    "runs",
    "work_items",
    "execution_attempts",
    "evaluation_attempts",
    "metric_results",
    "artifacts",
    "usage_events",
    "approvals",
    "run_events",
    "remote_jobs",
    "trace_observations",
    "run_tags",
    "run_notes",
)
_INSERT_ORDER = _TABLES
_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "datasets": ("content_hash",),
    "cases": ("dataset_content_hash", "case_id", "source_line"),
    "applications": ("application_id",),
    "profiles": ("observation_id",),
    "plans": ("plan_id",),
    "runs": ("run_id",),
    "work_items": ("work_item_id",),
    "execution_attempts": ("execution_id",),
    "evaluation_attempts": (
        "run_id",
        "case_id",
        "repetition_id",
        "metric_id",
        "binding_hash",
        "attempt_number",
    ),
    "metric_results": ("result_id",),
    "artifacts": ("artifact_id",),
    "usage_events": ("usage_event_id",),
    "approvals": ("approval_id",),
    "run_events": ("run_id", "sequence"),
    "remote_jobs": ("job_id",),
    "trace_observations": ("import_id", "trace_id"),
    "run_tags": ("run_id", "tag"),
    "run_notes": ("run_id",),
}
_CONTENT_HASH_TABLES = {
    "applications": "content_hash",
    "profiles": "content_hash",
    "plans": "content_hash",
    "runs": "content_hash",
    "execution_attempts": "content_hash",
    "evaluation_attempts": "content_hash",
    "metric_results": "content_hash",
    "usage_events": "content_hash",
    "approvals": "content_hash",
    "cases": "content_hash",
}


def export_run_bundle(project_root: Path, run_id: str, destination: Path) -> dict[str, Any]:
    """Write a self-contained bundle for one terminal run without changing its workspace."""
    workspace = Workspace.at(project_root)
    if not workspace.db_path.is_file():
        raise ValidationError(f"no aibench workspace at {workspace.root}")
    target = _new_archive_path(destination)
    data, artifact_refs = _snapshot_run(workspace, run_id)
    run_manifest = RunManifest.model_validate_json(data["tables"]["runs"][0]["data"])
    params = run_manifest.parameters if isinstance(run_manifest.parameters, Mapping) else {}
    application_artifact_id = params.get("application_artifact_id")
    test_world = params.get("test_world")
    plan_artifact_id = params.get("plan_artifact_id")
    if _is_inside(target, workspace.root):
        raise ValidationError("run bundle destination must be outside the .aibench workspace")

    try:
        with tempfile.TemporaryDirectory(prefix=".aibench-run-bundle-", dir=target.parent) as temp:
            payload = Path(temp) / "payload"
            artifact_dir = payload / "artifacts"
            artifact_dir.mkdir(parents=True)
            records: list[dict[str, Any]] = []
            total_artifact_bytes = 0
            artifact_store = ArtifactStore(workspace.artifacts_dir, create=False)
            application_ref = next(
                (ref for ref in artifact_refs if ref.artifact_id == application_artifact_id), None
            )
            if application_ref is not None:
                _validate_application_artifact(
                    artifact_store.verify_ref(application_ref), run_manifest.application_hash
                )
            if test_world is not None:
                seed_ref = _frozen_seed_ref(test_world, artifact_refs)
                _validate_seed_artifact(artifact_store.verify_ref(seed_ref))
            if params.get("mode") == "manual_plan":
                plan_ref = next(
                    (ref for ref in artifact_refs if ref.artifact_id == plan_artifact_id), None
                )
                if plan_ref is None or plan_ref.digest != run_manifest.plan_hash:
                    raise ValidationError("run bundle frozen plan artifact does not match the run")
                _validate_executable_plan_artifact(artifact_store.verify_ref(plan_ref))
            seen_digests: dict[str, int] = {}
            for ref in artifact_refs:
                previous_size = seen_digests.get(ref.digest)
                if previous_size is not None:
                    if previous_size != ref.size_bytes:
                        raise ValidationError("run bundle contains conflicting artifact sizes")
                    continue
                if ref.size_bytes > _MAX_ARTIFACT_BYTES:
                    raise ValidationError(
                        f"artifact {ref.artifact_id!r} exceeds the run bundle size limit"
                    )
                blob = artifact_store.verify_ref(ref)
                blob_name = ref.digest.split(":", 1)[1]
                blob_path = artifact_dir / blob_name
                blob_path.write_bytes(blob)
                total_artifact_bytes += len(blob)
                if total_artifact_bytes > _MAX_TOTAL_BYTES:
                    raise ValidationError("run artifacts exceed the supported bundle size")
                seen_digests[ref.digest] = ref.size_bytes
                records.append(_file_record(blob_path, f"bundle/artifacts/{blob_name}"))

            data_path = payload / "data.json"
            data_bytes = json.dumps(
                data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            if len(data_bytes) > _MAX_DATA_BYTES:
                raise ValidationError("run catalog exceeds the supported bundle size")
            data_path.write_bytes(data_bytes)
            records.append(_file_record(data_path, _DATA_PATH))
            records.sort(key=lambda record: record["path"])
            manifest = {
                "format": _FORMAT,
                "format_version": _VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "run_id": run_id,
                "files": records,
            }
            manifest_bytes = json.dumps(
                manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
            if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
                raise ValidationError("run bundle manifest exceeds the supported size")
            archive_tmp = Path(temp) / "run.zip"
            _write_archive(archive_tmp, payload, manifest_bytes, records)
            with archive_tmp.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.link(archive_tmp, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise ValidationError(f"run bundle destination already exists: {target}") from exc
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise ValidationError(f"could not export run bundle: {exc}") from exc

    return {
        "bundle": str(target),
        "run_id": run_id,
        "artifact_count": len(artifact_refs),
        "blob_count": len(seen_digests),
        "total_bytes": sum(item["size_bytes"] for item in records),
        "format_version": _VERSION,
    }


def import_run_bundle(archive_path: Path, project_root: Path) -> dict[str, Any]:
    """Validate and import one terminal run, its dependencies, and artifact bytes."""
    workspace = Workspace.at(project_root)
    database: Database | None = None
    try:
        with _pinned_zip_archive(archive_path) as zipped:
            manifest, members = _read_bundle(zipped)
            data = _read_data(zipped, members, manifest)
            run_id = manifest["run_id"]
            rows = _validate_data(data, run_id)
            source_refs = _artifact_records(rows)
            run_manifest = _model_from_row(RunManifest, rows["runs"][0], "runs")
            params = run_manifest.parameters if isinstance(run_manifest.parameters, Mapping) else {}
            test_world = params.get("test_world")
            _validate_payloads(
                zipped,
                members,
                manifest,
                source_refs,
                application_artifact_id=params.get("application_artifact_id"),
                application_hash=run_manifest.application_hash,
                seed_artifact_id=(
                    test_world.get("seed_artifact_id") if isinstance(test_world, Mapping) else None
                ),
                plan_artifact_id=params.get("plan_artifact_id"),
                require_executable_plan=params.get("mode") == "manual_plan",
            )

            # Do not create or migrate the destination until the complete archive has passed
            # structural, catalog and blob-integrity validation.
            schema_db = Database.open_in_memory()
            try:
                _validate_database_schema(Storage(schema_db), rows)
            finally:
                schema_db.close()
            database = Database.open_workspace(workspace)
            storage = Storage(database)
            _validate_database_schema(storage, rows)
            artifacts = ArtifactStore(workspace.artifacts_dir, max_bytes=_MAX_ARTIFACT_BYTES)
            uri_map: dict[str, tuple[str, str]] = {}
            imported_refs: dict[str, ArtifactRef] = {}
            for source_row in source_refs:
                ref = _validated_artifact_ref(source_row)
                member = members[_ARTIFACT_PREFIX + ref.digest.split(":", 1)[1]]
                blob = _bounded_member_bytes(zipped, member, _MAX_ARTIFACT_BYTES)
                existing = storage.get_artifact(ref.artifact_id)
                if existing is not None and not _same_artifact_identity(existing, ref):
                    raise ConflictError(
                        f"artifact ID {ref.artifact_id!r} already exists with different content"
                    )
                if existing is not None:
                    local_ref = existing
                    artifacts.verify_ref(existing)
                else:
                    local_ref = artifacts.write_bytes(
                        blob,
                        mime_type=ref.mime_type,
                        run_id=ref.run_id,
                        redaction=ref.redaction,
                        artifact_id=ref.artifact_id,
                    )
                    if local_ref.digest != ref.digest:
                        raise ValidationError("imported artifact changed its content digest")
                uri_map[ref.artifact_id] = (ref.uri, local_ref.uri)
                imported_refs[ref.artifact_id] = local_ref

            rows = _relocate_artifacts(rows, uri_map, imported_refs)
            imported = _insert_bundle_rows(storage, rows)
            return {
                "run_id": run_id,
                "imported": imported,
                "already_present": not imported,
                "artifact_count": len(source_refs),
                "format_version": _VERSION,
            }
    except (ValidationError, ConflictError):
        raise
    except (OSError, sqlite3.Error, zipfile.BadZipFile, json.JSONDecodeError) as exc:
        raise ValidationError(f"could not import run bundle: {exc}") from exc
    finally:
        if database is not None:
            database.close()


def _snapshot_run(workspace: Workspace, run_id: str) -> tuple[dict[str, Any], list[ArtifactRef]]:
    db = Database.open_readonly(workspace.db_path)
    try:
        db.connection.execute("BEGIN")
        run = db.connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if run is None:
            raise ValidationError(f"no run committed with run_id={run_id!r}")
        run_row = dict(run)
        if run_row["status"] in RESUMABLE_STATES:
            raise ValidationError("only terminal runs can be exported as portable bundles")
        if run_row["status"] not in _TERMINAL_RUN_STATES:
            raise ValidationError("run has an unsupported status and cannot be exported")
        try:
            manifest = RunManifest.model_validate_json(run_row["data"])
        except (ValueError, TypeError) as exc:
            raise ValidationError("run manifest is malformed") from exc
        if manifest.run_id != run_id or _hash_of(run_row["data"]) != run_row["content_hash"]:
            raise ValidationError("run manifest does not match its committed identity")
        params = manifest.parameters if isinstance(manifest.parameters, Mapping) else {}
        mode = params.get("mode")

        selected: dict[str, list[dict[str, Any]]] = {name: [] for name in _TABLES}
        selected["runs"] = [run_row]
        dataset = _single_row(
            db.connection,
            "SELECT * FROM datasets WHERE content_hash = ?",
            (run_row["dataset_hash"],),
        )
        if dataset is None:
            raise ValidationError("run dataset manifest is missing from the workspace")
        selected["datasets"].append(dataset)
        dataset_cases = _query_rows(
            db.connection,
            "SELECT * FROM cases WHERE dataset_content_hash = ?",
            (run_row["dataset_hash"],),
        )
        if mode == "developer_smoke":
            selected["execution_attempts"] = _query_rows(
                db.connection,
                "SELECT * FROM execution_attempts WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            )
            smoke_case_ids = {
                row["case_id"]
                for row in selected["execution_attempts"]
                if isinstance(row.get("case_id"), str)
            }
            selected["cases"] = [
                row for row in dataset_cases if row.get("case_id") in smoke_case_ids
            ]
        else:
            selected["cases"] = dataset_cases

        app_id = manifest.application_id
        candidates = (
            _query_rows(
                db.connection, "SELECT * FROM applications WHERE application_id = ?", (app_id,)
            )
            if app_id is not None
            else _query_rows(db.connection, "SELECT * FROM applications", ())
        )
        applications = []
        for candidate in candidates:
            try:
                spec = ApplicationSpec.model_validate_json(candidate["data"])
            except (ValueError, TypeError):
                continue
            if content_hash(spec.model_dump(mode="json")) == run_row["application_hash"]:
                applications.append(candidate)
        selected["applications"] = applications
        if len(applications) != 1 or (
            manifest.application_id is not None
            and applications[0]["application_id"] != manifest.application_id
        ):
            raise ValidationError("run application dependency is missing or ambiguous")
        selected_app_ids = [row["application_id"] for row in applications]
        for selected_app_id in selected_app_ids:
            selected["profiles"].extend(
                _query_rows(
                    db.connection,
                    "SELECT * FROM profiles WHERE application_id = ?",
                    (selected_app_id,),
                )
            )
        for candidate in _query_rows(db.connection, "SELECT * FROM plans", ()):
            try:
                plan = EvaluationPlan.model_validate_json(candidate["data"])
            except (ValueError, TypeError):
                continue
            if content_hash(plan.model_dump(mode="json")) == run_row["plan_hash"]:
                selected["plans"].append(candidate)

        for table in _TABLES:
            if table in {"datasets", "cases", "applications", "profiles", "plans", "runs"}:
                continue
            if table == "execution_attempts" and mode == "developer_smoke":
                continue
            if table == "approvals":
                sql = "SELECT * FROM approvals WHERE approval_id = ?"
                query_params: tuple[object, ...] = (f"{run_id}:approval",)
            elif table == "run_tags":
                sql = "SELECT * FROM run_tags WHERE run_id = ?"
                query_params = (run_id,)
            elif table == "run_notes":
                sql = "SELECT * FROM run_notes WHERE run_id = ?"
                query_params = (run_id,)
            else:
                sql = f"SELECT * FROM {table} WHERE run_id = ?"
                query_params = (run_id,)
            if table == "execution_attempts":
                sql += " ORDER BY rowid"
            selected[table] = _query_rows(db.connection, sql, query_params)

        artifact_ids = {row["artifact_id"] for row in selected["artifacts"]}
        for table, table_rows in selected.items():
            for row in table_rows:
                for column in ("data", "payload"):
                    raw = row.get(column)
                    if isinstance(raw, str):
                        artifact_ids.update(_artifact_ids_in_json(raw, table=table))
        if artifact_ids:
            for batch in _batches(sorted(artifact_ids), 500):
                placeholders = ",".join("?" for _ in batch)
                selected["artifacts"].extend(
                    _query_rows(
                        db.connection,
                        f"SELECT * FROM artifacts WHERE artifact_id IN ({placeholders})",
                        tuple(batch),
                    )
                )
        selected["artifacts"] = _unique_rows("artifacts", selected["artifacts"])
        selected_artifact_ids = {row["artifact_id"] for row in selected["artifacts"]}
        if artifact_ids - selected_artifact_ids:
            raise ValidationError("run result refers to a missing artifact catalog entry")
        # A content-addressed artifact can be reused by more than one run. A portable
        # single-run bundle carries the bytes and reference, not the other run's ownership.
        for row in selected["artifacts"]:
            if row["run_id"] not in {None, run_id}:
                ref = _validated_artifact_ref(row)
                detached = ref.model_copy(update={"run_id": None})
                row["run_id"] = None
                row["content_hash"] = _hash_of(detached.model_dump_json())
        refs = [_validated_artifact_ref(row) for row in selected["artifacts"]]
        if len(refs) > _MAX_ENTRIES - 2:
            raise ValidationError("run has too many artifacts for the supported bundle format")
        bundle_data = {"tables": selected}
        _validate_data(bundle_data, run_id)
        encoded = json.dumps(bundle_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(encoded.encode("utf-8")) > _MAX_DATA_BYTES:
            raise ValidationError("run catalog exceeds the supported bundle size")
        return bundle_data, refs
    finally:
        with suppress(sqlite3.Error):
            db.connection.execute("ROLLBACK")
        db.close()


def _query_rows(
    connection: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> list[dict[str, Any]]:
    return [dict(row) for row in connection.execute(sql, params).fetchall()]


def _single_row(
    connection: sqlite3.Connection, sql: str, params: tuple[object, ...]
) -> dict[str, Any] | None:
    rows = _query_rows(connection, sql, params)
    if len(rows) > 1:
        raise ValidationError("run bundle dependency identity is ambiguous")
    return rows[0] if rows else None


def _unique_rows(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keyed: dict[tuple[object, ...], dict[str, Any]] = {}
    for row in rows:
        key = tuple(row[name] for name in _PRIMARY_KEYS[table])
        existing = keyed.get(key)
        if existing is not None and existing != row:
            raise ValidationError(f"workspace has conflicting duplicate {table} rows")
        keyed[key] = row
    return list(keyed.values())


def _artifact_ids_in_json(raw: str, *, table: str) -> set[str]:
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, RecursionError):
        return set()
    found: set[str] = set()

    def add_string(item: Any) -> None:
        if isinstance(item, str) and item:
            found.add(item)

    def walk_artifact_refs(item: Any) -> None:
        if isinstance(item, dict):
            if _is_artifact_ref_mapping(item):
                add_string(item.get("artifact_id"))
                return
            for child in item.values():
                walk_artifact_refs(child)
        elif isinstance(item, list):
            for child in item:
                walk_artifact_refs(child)

    if table == "runs":
        parameters = value.get("parameters")
        if isinstance(parameters, dict):
            add_string(parameters.get("application_artifact_id"))
            add_string(parameters.get("plan_artifact_id"))
            world = parameters.get("test_world")
            if isinstance(world, dict):
                add_string(world.get("seed_artifact_id"))
    elif table == "execution_attempts":
        trace_refs = value.get("trace_refs")
        if isinstance(trace_refs, list):
            for artifact_id in trace_refs:
                add_string(artifact_id)
        walk_artifact_refs(value.get("output"))
    elif table in {"evaluation_attempts", "metric_results"}:
        add_string(value.get("raw_artifact_ref"))
        walk_artifact_refs(value)
    elif table == "trace_observations":
        add_string(value.get("raw_artifact_id"))
        raw_ids = value.get("raw_artifact_ids")
        if isinstance(raw_ids, list):
            for artifact_id in raw_ids:
                add_string(artifact_id)
    elif table == "run_events":
        add_string(value.get("raw_artifact_id"))
    elif table == "remote_jobs":
        add_string(value.get("request_artifact"))
        add_string(value.get("output_items_artifact"))
    return found


def _is_artifact_ref_mapping(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(ArtifactRef.model_fields):
        return False
    try:
        ArtifactRef.model_validate(value)
    except (ValueError, TypeError):
        return False
    return True


def _validated_artifact_ref(row: dict[str, Any]) -> ArtifactRef:
    try:
        ref = ArtifactRef(
            artifact_id=row["artifact_id"],
            digest=row["digest"],
            uri=row["uri"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            redaction=row["redaction"],
            run_id=row["run_id"],
        )
    except (KeyError, ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("run bundle contains invalid artifact metadata") from exc
    if row.get("content_hash") != _hash_of(ref.model_dump_json()):
        raise ValidationError(f"artifact catalog record is invalid: {ref.artifact_id}")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", ref.digest) or ref.size_bytes < 0:
        raise ValidationError(f"artifact catalog record is invalid: {ref.artifact_id}")
    return ref


def _file_record(path: Path, name: str) -> dict[str, Any]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            size += len(block)
            digest.update(block)
    return {"path": name, "size_bytes": size, "sha256": digest.hexdigest()}


def _read_bundle(
    zipped: zipfile.ZipFile,
) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    infos = zipped.infolist()
    if not infos or len(infos) > _MAX_ENTRIES:
        raise ValidationError("run bundle has an invalid number of entries")
    members: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    for info in infos:
        name = info.filename
        if (
            name in members
            or unicodedata.normalize("NFD", name).casefold() in folded
            or "\\" in name
            or "\x00" in name
            or name.startswith("/")
            or PurePosixPath(name).as_posix() != name
            or any(part in {"", ".", ".."} for part in PurePosixPath(name).parts)
        ):
            raise ValidationError("run bundle contains an unsafe or duplicate path")
        if info.is_dir() or _zip_member_is_link(info) or info.flag_bits & 0x1:
            raise ValidationError("run bundle contains a link, directory or encrypted entry")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValidationError("run bundle uses an unsupported compression method")
        members[name] = info
        folded.add(unicodedata.normalize("NFD", name).casefold())
    manifest_info = members.get(_MANIFEST)
    if manifest_info is None or manifest_info.file_size > _MAX_MANIFEST_BYTES:
        raise ValidationError("run bundle manifest is missing or too large")
    try:
        manifest = json.loads(_bounded_member_bytes(zipped, manifest_info, _MAX_MANIFEST_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("run bundle manifest is malformed") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != _FORMAT
        or manifest.get("format_version") != _VERSION
        or not isinstance(manifest.get("run_id"), str)
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValidationError("run bundle format is unsupported")
    files = manifest["files"]
    if len(files) != len(members) - 1 or len(files) > _MAX_ENTRIES - 1:
        raise ValidationError("run bundle manifest does not match its archive")
    records: dict[str, dict[str, Any]] = {}
    total = 0
    for record in files:
        if not isinstance(record, dict):
            raise ValidationError("run bundle contains an invalid file record")
        path_value, size_value, digest_value = (
            record.get("path"),
            record.get("size_bytes"),
            record.get("sha256"),
        )
        if (
            not isinstance(path_value, str)
            or path_value not in members
            or path_value == _MANIFEST
            or isinstance(size_value, bool)
            or not isinstance(size_value, int)
            or size_value < 0
            or not isinstance(digest_value, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest_value)
            or members[path_value].file_size != size_value
            or path_value in records
            or (
                path_value != _DATA_PATH
                and re.fullmatch(_ARTIFACT_PREFIX + r"[0-9a-f]{64}", path_value) is None
            )
        ):
            raise ValidationError("run bundle manifest does not match its archive")
        records[path_value] = record
        total += size_value
        if total > _MAX_TOTAL_BYTES:
            raise ValidationError("run bundle exceeds the supported uncompressed size")
    if set(records) != set(members) - {_MANIFEST} or _DATA_PATH not in records:
        raise ValidationError("run bundle is missing required data")
    manifest["files"] = list(records.values())
    for name, record in records.items():
        if name == _DATA_PATH and record["size_bytes"] > _MAX_DATA_BYTES:
            raise ValidationError("run bundle catalog exceeds the supported size")
        if name.startswith(_ARTIFACT_PREFIX) and record["size_bytes"] > _MAX_ARTIFACT_BYTES:
            raise ValidationError("run bundle artifact exceeds the supported size")
    return manifest, members


def _bounded_member_bytes(zipped: zipfile.ZipFile, member: zipfile.ZipInfo, maximum: int) -> bytes:
    if member.file_size > maximum:
        raise ValidationError("run bundle entry exceeds its supported size")
    chunks: list[bytes] = []
    total = 0
    with zipped.open(member, "r") as source:
        while block := source.read(min(1024 * 1024, maximum - total + 1)):
            total += len(block)
            if total > maximum or total > member.file_size:
                raise ValidationError("run bundle entry exceeds its declared size")
            chunks.append(block)
    if total != member.file_size:
        raise ValidationError("run bundle entry is truncated")
    return b"".join(chunks)


def _read_data(
    zipped: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
) -> dict[str, Any]:
    _verify_member(zipped, members[_DATA_PATH], _record_for(manifest, _DATA_PATH))
    try:
        data = json.loads(_bounded_member_bytes(zipped, members[_DATA_PATH], _MAX_DATA_BYTES))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("run bundle catalog is malformed") from exc
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise ValidationError("run bundle catalog has an unsupported structure")
    return data


def _record_for(manifest: dict[str, Any], name: str) -> dict[str, Any]:
    return next(item for item in manifest["files"] if item["path"] == name)


def _validate_payloads(
    zipped: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    manifest: dict[str, Any],
    artifacts: list[dict[str, Any]],
    *,
    application_artifact_id: Any,
    application_hash: str,
    seed_artifact_id: Any,
    plan_artifact_id: Any,
    require_executable_plan: bool,
) -> None:
    required = {_DATA_PATH}
    digest_sizes: dict[str, int] = {}
    application_digest = None
    seed_digest = None
    plan_digest = None
    for row in artifacts:
        ref = _validated_artifact_ref(row)
        digest = ref.digest.split(":", 1)[1]
        if ref.artifact_id == application_artifact_id:
            application_digest = digest
        if ref.artifact_id == seed_artifact_id:
            seed_digest = digest
        if ref.artifact_id == plan_artifact_id:
            plan_digest = digest
        name = _ARTIFACT_PREFIX + digest
        required.add(name)
        member = members.get(name)
        if member is None:
            raise ValidationError(f"run bundle is missing artifact bytes for {ref.artifact_id!r}")
        if member.file_size != ref.size_bytes:
            raise ValidationError(f"run bundle artifact size mismatch for {ref.artifact_id!r}")
        if digest in digest_sizes and digest_sizes[digest] != ref.size_bytes:
            raise ValidationError("run bundle contains conflicting artifact sizes")
        digest_sizes[digest] = ref.size_bytes
    if set(members) != required | {_MANIFEST}:
        raise ValidationError("run bundle contains unreferenced or unexpected entries")
    for name in required:
        if name == _DATA_PATH:
            continue
        record = _record_for(manifest, name)
        _verify_member(zipped, members[name], record)
        digest = name.removeprefix(_ARTIFACT_PREFIX)
        if record["sha256"] != digest:
            raise ValidationError("run bundle artifact path does not match its digest")
        if digest == application_digest:
            raw = _bounded_member_bytes(zipped, members[name], _MAX_ARTIFACT_BYTES)
            _validate_application_artifact(raw, application_hash)
        if digest == seed_digest:
            raw = _bounded_member_bytes(zipped, members[name], _MAX_ARTIFACT_BYTES)
            _validate_seed_artifact(raw)
        if digest == plan_digest and require_executable_plan:
            raw = _bounded_member_bytes(zipped, members[name], _MAX_ARTIFACT_BYTES)
            _validate_executable_plan_artifact(raw)


def _validate_application_artifact(raw: bytes, expected_hash: str) -> None:
    try:
        spec = ApplicationSpec.model_validate_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("run bundle frozen application artifact is invalid") from exc
    if content_hash(spec.model_dump(mode="json")) != expected_hash:
        raise ValidationError("run bundle frozen application artifact does not match the run")


def _validate_seed_artifact(raw: bytes) -> None:
    try:
        seed = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError("run bundle frozen test-world seed is invalid JSON") from exc
    if not isinstance(seed, dict | list):
        raise ValidationError("run bundle frozen test-world seed must be a JSON object or list")


def _validate_executable_plan_artifact(raw: bytes) -> None:
    try:
        ExecutablePlan.model_validate_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("run bundle frozen plan artifact is invalid") from exc


def _frozen_seed_ref(test_world: Any, refs: list[ArtifactRef]) -> ArtifactRef:
    if not isinstance(test_world, Mapping):
        raise ValidationError("run bundle frozen test world is malformed")
    world_id = test_world.get("world_id")
    seed_artifact_id = test_world.get("seed_artifact_id")
    seed_hash = test_world.get("seed_hash")
    seed_ref = next((ref for ref in refs if ref.artifact_id == seed_artifact_id), None)
    if (
        not isinstance(world_id, str)
        or not world_id.strip()
        or not isinstance(seed_hash, str)
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", seed_hash)
        or seed_ref is None
        or seed_ref.digest != seed_hash
    ):
        raise ValidationError("run bundle frozen test world seed does not match its artifact")
    return seed_ref


def _verify_member(
    zipped: zipfile.ZipFile, member: zipfile.ZipInfo, record: dict[str, Any]
) -> None:
    digest = hashlib.sha256()
    total = 0
    with zipped.open(member, "r") as source:
        while block := source.read(1024 * 1024):
            total += len(block)
            if total > record["size_bytes"]:
                raise ValidationError(
                    f"run bundle entry exceeds its declared size: {member.filename}"
                )
            digest.update(block)
    if total != record["size_bytes"] or digest.hexdigest() != record["sha256"]:
        raise ValidationError(f"run bundle entry failed its integrity check: {member.filename}")


def _artifact_records(rows: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    artifacts = rows.get("artifacts", [])
    for row in artifacts:
        _validated_artifact_ref(row)
    return artifacts


def _validate_data(data: dict[str, Any], run_id: str) -> dict[str, list[dict[str, Any]]]:
    tables = data.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(_TABLES):
        raise ValidationError("run bundle catalog contains unsupported tables")
    rows: dict[str, list[dict[str, Any]]] = {}
    total_rows = 0
    for table in _TABLES:
        items = tables[table]
        if not isinstance(items, list):
            raise ValidationError(f"run bundle table {table} is malformed")
        total_rows += len(items)
        if total_rows > _MAX_ROWS:
            raise ValidationError("run bundle has too many catalog records")
        rows[table] = []
        for item in items:
            if not isinstance(item, dict) or any(
                not isinstance(name, str) or not _sqlite_value(value)
                for name, value in item.items()
            ):
                raise ValidationError(f"run bundle table {table} contains an invalid row")
            if any(name not in item for name in _PRIMARY_KEYS[table]):
                raise ValidationError(f"run bundle table {table} is missing a primary key")
            rows[table].append(item)
        rows[table] = _unique_rows(table, rows[table])
    run_rows = rows["runs"]
    if len(run_rows) != 1 or run_rows[0].get("run_id") != run_id:
        raise ValidationError("run bundle identity does not match its catalog")
    run = run_rows[0]
    if run.get("status") in RESUMABLE_STATES:
        raise ValidationError("only terminal runs can be imported from portable bundles")
    if run.get("status") not in _TERMINAL_RUN_STATES:
        raise ValidationError("run bundle contains an unsupported run status")
    try:
        manifest = _model_from_row(RunManifest, run, "runs")
    except (KeyError, ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("run bundle manifest is malformed") from exc
    if (
        manifest.run_id != run_id
        or manifest.dataset_hash != run.get("dataset_hash")
        or manifest.application_hash != run.get("application_hash")
        or manifest.plan_hash != run.get("plan_hash")
        or manifest.created_at.isoformat() != run.get("created_at")
        or _hash_of(run["data"]) != run.get("content_hash")
    ):
        raise ValidationError("run bundle run record fails its integrity check")
    _require_dependency(rows, "datasets", "content_hash", run["dataset_hash"])

    typed: dict[str, list[Any]] = {table: [] for table in _TABLES}
    for table, model_type in (
        ("applications", ApplicationSpec),
        ("profiles", ObservationClaim),
        ("plans", EvaluationPlan),
        ("cases", BenchmarkCase),
        ("work_items", WorkItem),
        ("execution_attempts", ExecutionResult),
        ("evaluation_attempts", EvaluationResult),
        ("metric_results", EvaluationResult),
        ("usage_events", UsageEvent),
        ("approvals", Approval),
    ):
        typed[table] = [_model_from_row(model_type, row, table) for row in rows[table]]

    datasets = [_dataset_from_row(row) for row in rows["datasets"]]
    if len(datasets) != 1:
        raise ValidationError("run bundle must contain exactly one dataset dependency")
    dataset = datasets[0]
    if dataset.content_hash != run["dataset_hash"]:
        raise ValidationError("run bundle dataset identity does not match the run")
    cases_by_id: dict[str, BenchmarkCase] = {}
    for row, case in zip(rows["cases"], typed["cases"], strict=True):
        if (
            row.get("dataset_content_hash") != dataset.content_hash
            or row.get("case_id") != case.case_id
            or row.get("source_line") != case.source_line
            or row.get("duplicate_of_line") != case.duplicate_of_line
            or row.get("group_id") != case.group_id
        ):
            raise ValidationError("run bundle case row does not match its typed data")
        cases_by_id[case.case_id] = case
    params = manifest.parameters if isinstance(manifest.parameters, Mapping) else {}
    mode = params.get("mode")
    if mode == "developer_smoke":
        smoke_case_ids = [result.case_id for result in typed["execution_attempts"]]
        if (
            dataset.case_count < len(rows["cases"])
            or len(rows["cases"]) != len(smoke_case_ids)
            or set(cases_by_id) != set(smoke_case_ids)
        ):
            raise ValidationError("run bundle smoke cases do not match its selected executions")
    elif dataset.case_count != len(rows["cases"]):
        raise ValidationError("run bundle case count does not match its dataset manifest")

    app_specs: dict[str, ApplicationSpec] = {}
    for row, spec in zip(rows["applications"], typed["applications"], strict=True):
        if (
            row.get("application_id") != spec.application_id
            or row.get("runner") != spec.runner.value
            or row.get("target") != spec.target
        ):
            raise ValidationError("run bundle application row does not match its typed data")
        app_specs[spec.application_id] = spec
    matching_apps = [
        spec
        for spec in app_specs.values()
        if content_hash(spec.model_dump(mode="json")) == manifest.application_hash
    ]
    if len(rows["applications"]) != 1 or len(matching_apps) != 1:
        raise ValidationError("run bundle application dependency is missing, extra or mismatched")
    if (
        manifest.application_id is not None
        and manifest.application_id != matching_apps[0].application_id
    ):
        raise ValidationError("run bundle application ID does not match the run manifest")

    for row, claim in zip(rows["profiles"], typed["profiles"], strict=True):
        if (
            row.get("observation_id") != claim.observation_id
            or row.get("capability") != claim.capability
            or row.get("state") != claim.state.value
            or row.get("application_id") not in app_specs
        ):
            raise ValidationError("run bundle profile row does not match its typed data")
    matching_plans = [
        plan
        for plan in typed["plans"]
        if content_hash(plan.model_dump(mode="json")) == manifest.plan_hash
    ]
    for row, plan in zip(rows["plans"], typed["plans"], strict=True):
        if row.get("plan_id") != plan.plan_id or row.get("policy_hash") != plan.policy_hash:
            raise ValidationError("run bundle plan row does not match its typed data")
    if rows["plans"] and (len(rows["plans"]) != 1 or len(matching_plans) != 1):
        raise ValidationError("run bundle plan dependency is extra or does not match the run")
    plan_artifact_id = params.get("plan_artifact_id")
    artifact_refs_by_id = {
        row.get("artifact_id"): _validated_artifact_ref(row) for row in rows["artifacts"]
    }
    application_artifact_id = params.get("application_artifact_id")
    if "application_artifact_id" in params and (
        not isinstance(application_artifact_id, str)
        or not application_artifact_id
        or application_artifact_id not in artifact_refs_by_id
    ):
        raise ValidationError("run bundle frozen application artifact is missing")
    test_world = params.get("test_world")
    if test_world is not None:
        _frozen_seed_ref(test_world, list(artifact_refs_by_id.values()))
    artifact_plan_matches = (
        isinstance(plan_artifact_id, str)
        and plan_artifact_id in artifact_refs_by_id
        and artifact_refs_by_id[plan_artifact_id].digest == manifest.plan_hash
    )
    if mode == "manual_plan" and not artifact_plan_matches:
        raise ValidationError("run bundle frozen plan artifact does not match the run")
    if mode == "delegated_suite":
        suite = params.get("suite")
        if (
            not isinstance(suite, Mapping)
            or content_hash({"kind": "delegated_suite", **suite}) != manifest.plan_hash
        ):
            raise ValidationError("run bundle delegated plan dependency does not match the run")
        if rows["plans"]:
            raise ValidationError("run bundle has an unrelated plan catalog row")
    elif mode == "developer_smoke":
        case_ids = [result.case_id for result in typed["execution_attempts"]]
        if len(case_ids) != len(set(case_ids)):
            raise ValidationError("run bundle smoke plan contains duplicate case executions")
        if content_hash({"kind": "developer_smoke", "case_ids": case_ids}) != manifest.plan_hash:
            raise ValidationError("run bundle smoke plan dependency does not match the run")
        if rows["plans"]:
            raise ValidationError("run bundle has an unrelated plan catalog row")
    elif artifact_plan_matches:
        if rows["plans"] and len(matching_plans) != 1:
            raise ValidationError("run bundle plan catalog row is unrelated to the run")
    elif len(rows["plans"]) != 1 or len(matching_plans) != 1:
        raise ValidationError("run bundle is missing the frozen plan dependency")

    for row, item in zip(rows["work_items"], typed["work_items"], strict=True):
        if (
            row.get("work_item_id") != item.work_item_id
            or row.get("run_id") != item.run_id
            or row.get("kind") != item.kind
            or row.get("task_key") != item.task_key
            or row.get("state") != item.state.value
            or row.get("attempt") != item.attempt
        ):
            raise ValidationError("run bundle work item row does not match its typed data")
    execution_ids: set[str] = set()
    for row, result in zip(rows["execution_attempts"], typed["execution_attempts"], strict=True):
        if (
            row.get("execution_id") != result.execution_id
            or row.get("run_id") != result.run_id
            or row.get("case_id") != result.case_id
            or row.get("repetition_id") != result.repetition_id
            or row.get("attempt_id") != result.attempt_id
            or row.get("status") != result.status.value
            or result.case_id not in cases_by_id
        ):
            raise ValidationError("run bundle execution row does not match its typed data")
        execution_ids.add(result.execution_id)
    for table in ("evaluation_attempts", "metric_results"):
        for row, result in zip(rows[table], typed[table], strict=True):
            if (
                row.get("run_id") != result.run_id
                or row.get("case_id") != result.case_id
                or row.get("metric_id") != result.metric_id
                or row.get("status") != result.status.value
                or row.get("decision") != result.decision.value
                or result.case_id not in cases_by_id
                or (table == "metric_results" and row.get("result_id") != result.result_id)
                or (
                    table == "evaluation_attempts"
                    and (
                        row.get("repetition_id") != result.repetition_id
                        or row.get("attempt_number") != result.attempt_number
                        or row.get("binding_hash") != (result.binding_hash or "")
                    )
                )
            ):
                raise ValidationError(f"run bundle {table} row does not match its typed data")
            if result.execution_id is not None and result.execution_id not in execution_ids:
                raise ValidationError(f"run bundle {table} refers to a missing execution")
    for row, event in zip(rows["usage_events"], typed["usage_events"], strict=True):
        if (
            row.get("usage_event_id") != event.usage_event_id
            or row.get("run_id") != event.run_id
            or row.get("role") != event.role.value
            or row.get("provider") != event.provider
            or row.get("cost") != event.cost
        ):
            raise ValidationError("run bundle usage event row does not match its typed data")
    if any(row.get("approval_id") != f"{run_id}:approval" for row in rows["approvals"]):
        raise ValidationError("run bundle contains approval data for another run")
    for row, approval in zip(rows["approvals"], typed["approvals"], strict=True):
        if row.get("scope_hash") != approval.scope_hash:
            raise ValidationError("run bundle approval row does not match its typed data")

    for table in _TABLES:
        for row in rows[table]:
            if "run_id" in row and row["run_id"] != run_id and table != "artifacts":
                raise ValidationError(f"run bundle has an unrelated {table} row")
    for table, column in _CONTENT_HASH_TABLES.items():
        for row in rows[table]:
            if row.get(column) != _hash_of(row["data"]):
                raise ValidationError(f"run bundle {table} record fails its integrity check")
    artifact_rows = {row["artifact_id"]: row for row in rows["artifacts"]}
    referenced_artifacts: set[str] = set()
    for table, table_rows in rows.items():
        for row in table_rows:
            for column in ("data", "payload"):
                raw = row.get(column)
                if isinstance(raw, str):
                    referenced_artifacts.update(_artifact_ids_in_json(raw, table=table))
    if referenced_artifacts - set(artifact_rows):
        raise ValidationError("run bundle refers to a missing artifact catalog entry")
    for row in rows["artifacts"]:
        ref = _validated_artifact_ref(row)
        if ref.run_id not in {None, run_id}:
            raise ValidationError("run bundle contains an artifact owned by another run")
    _validate_untyped_rows(rows, execution_ids)
    return rows


def _model_from_row(model_type: Any, row: dict[str, Any], table: str) -> Any:
    raw = row.get("data")
    if not isinstance(raw, str):
        raise ValidationError(f"run bundle {table} data is missing")
    try:
        return model_type.model_validate_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError(f"run bundle {table} data is invalid") from exc


def _dataset_from_row(row: dict[str, Any]) -> DatasetManifest:
    try:
        manifest = DatasetManifest(
            content_hash=row["content_hash"],
            dataset_id=row["dataset_id"],
            schema_version=row["schema_version"],
            case_count=row["case_count"],
            source_refs=json.loads(row["source_refs"]),
            split=row["split"],
            duplicate_case_ids=json.loads(row["duplicate_case_ids"]),
            created_at=row["created_at"],
        )
    except (KeyError, ValueError, TypeError, RecursionError) as exc:
        raise ValidationError("run bundle dataset manifest is invalid") from exc
    if row.get("manifest_hash") != _hash_of(manifest.model_dump_json()):
        raise ValidationError("run bundle dataset manifest fails its integrity check")
    return manifest


def _validate_untyped_rows(rows: dict[str, list[dict[str, Any]]], execution_ids: set[str]) -> None:
    for row in rows["run_events"]:
        payload = _json_object(row.get("payload"), "run event payload")
        if row.get("run_id") is None or not isinstance(row.get("sequence"), int):
            raise ValidationError("run bundle contains an invalid run event")
        if not isinstance(row.get("event_type"), str) or not isinstance(payload, dict):
            raise ValidationError("run bundle contains an invalid run event")
    for row in rows["remote_jobs"]:
        data = _json_object(row.get("data"), "remote job data")
        if not isinstance(data, dict) or not all(
            isinstance(row.get(key), str) and row[key]
            for key in ("job_id", "run_id", "plugin_id", "state", "fingerprint")
        ):
            raise ValidationError("run bundle contains an invalid remote job")
    for row in rows["trace_observations"]:
        data = _json_object(row.get("data"), "trace observation")
        raw_ids = data.get("raw_artifact_ids")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("partial_reasons"), list)
            or not all(isinstance(reason, str) for reason in data["partial_reasons"])
            or (
                raw_ids is not None
                and (
                    not isinstance(raw_ids, list)
                    or not all(isinstance(artifact_id, str) for artifact_id in raw_ids)
                )
            )
            or row.get("run_id") is None
            or isinstance(row.get("complete"), bool)
            or not isinstance(row.get("complete"), int)
            or row["complete"] not in {0, 1}
            or (row.get("execution_id") is not None and row["execution_id"] not in execution_ids)
            or row.get("trace_id") is None
            or row.get("import_id") is None
            or bool(row.get("complete")) != (not data["partial_reasons"])
        ):
            raise ValidationError("run bundle contains an invalid trace observation")
    for table in ("run_tags", "run_notes"):
        for row in rows[table]:
            if not isinstance(row.get("run_id"), str):
                raise ValidationError(f"run bundle contains an invalid {table} record")
            value = row.get("tag" if table == "run_tags" else "note")
            if not isinstance(value, str):
                raise ValidationError(f"run bundle contains an invalid {table} record")


def _json_object(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise ValidationError(f"run bundle {label} is malformed")
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError(f"run bundle {label} is malformed") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"run bundle {label} is malformed")
    return value


def _sqlite_value(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _require_dependency(
    rows: dict[str, list[dict[str, Any]]], table: str, column: str, value: object
) -> None:
    if not any(row.get(column) == value for row in rows[table]):
        raise ValidationError(f"run bundle is missing its {table} dependency")


def _batches(values: list[str], size: int) -> list[list[str]]:
    return [values[offset : offset + size] for offset in range(0, len(values), size)]


def _same_artifact_identity(left: ArtifactRef, right: ArtifactRef) -> bool:
    return (
        left.artifact_id == right.artifact_id
        and left.digest == right.digest
        and left.mime_type == right.mime_type
        and left.size_bytes == right.size_bytes
        and left.redaction == right.redaction
        and left.run_id == right.run_id
    )


def _relocate_artifacts(
    rows: dict[str, list[dict[str, Any]]],
    uri_map: dict[str, tuple[str, str]],
    refs: dict[str, ArtifactRef],
) -> dict[str, list[dict[str, Any]]]:
    relocated = {table: [dict(row) for row in items] for table, items in rows.items()}
    for row in relocated["artifacts"]:
        ref = refs[row["artifact_id"]]
        row["uri"] = ref.uri
        row["content_hash"] = _hash_of(ref.model_dump_json())
    for table, items in relocated.items():
        if table not in {"execution_attempts", "evaluation_attempts", "metric_results"}:
            continue
        for row in items:
            for column in ("data", "payload"):
                raw = row.get(column)
                if not isinstance(raw, str):
                    continue
                try:
                    value = json.loads(raw)
                except (json.JSONDecodeError, RecursionError):
                    continue
                transformed = _replace_artifact_uris(value, uri_map, refs)
                if transformed != value:
                    row[column] = json.dumps(transformed, ensure_ascii=False, separators=(",", ":"))
                    if column == "data":
                        row["content_hash"] = _hash_of(row[column])
    return relocated


def _replace_artifact_uris(
    value: Any, uri_map: dict[str, tuple[str, str]], refs: dict[str, ArtifactRef]
) -> Any:
    if isinstance(value, list):
        return [_replace_artifact_uris(item, uri_map, refs) for item in value]
    if isinstance(value, dict):
        transformed = {
            key: _replace_artifact_uris(item, uri_map, refs) for key, item in value.items()
        }
        artifact_id = transformed.get("artifact_id")
        source_uri = transformed.get("uri")
        locations = uri_map.get(artifact_id) if isinstance(artifact_id, str) else None
        if (
            _is_artifact_ref_mapping(value)
            and isinstance(artifact_id, str)
            and artifact_id in refs
            and locations is not None
            and isinstance(transformed.get("digest"), str)
            and transformed["digest"] == refs[artifact_id].digest
            and isinstance(source_uri, str)
            and source_uri == locations[0]
        ):
            transformed["uri"] = locations[1]
            transformed["run_id"] = refs[artifact_id].run_id
        return transformed
    return value


def _insert_bundle_rows(storage: Storage, rows: dict[str, list[dict[str, Any]]]) -> bool:
    connection = storage.conn
    inserted = False
    connection.execute("BEGIN IMMEDIATE")
    try:
        for table in _INSERT_ORDER:
            columns = _table_columns(connection, table)
            for row in rows[table]:
                if set(row) != set(columns):
                    raise ValidationError(f"run bundle {table} schema does not match this version")
                pk = _PRIMARY_KEYS[table]
                where = " AND ".join(f'"{name}" IS ?' for name in pk)
                existing = connection.execute(
                    f'SELECT * FROM "{table}" WHERE {where}', tuple(row[name] for name in pk)
                ).fetchone()
                if existing is not None:
                    if dict(existing) != row:
                        raise ConflictError(
                            f"{table} already contains different data for "
                            f"{tuple(row[name] for name in pk)!r}"
                        )
                    continue
                names = tuple(row)
                columns_sql = ", ".join(f'"{name}"' for name in names)
                placeholders = ", ".join("?" for _ in names)
                connection.execute(
                    f'INSERT INTO "{table}" ({columns_sql}) VALUES ({placeholders})',
                    tuple(row[name] for name in names),
                )
                inserted = True
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    return inserted


def _validate_database_schema(storage: Storage, rows: dict[str, list[dict[str, Any]]]) -> None:
    for table in _TABLES:
        expected = set(_table_columns(storage.conn, table))
        if any(set(row) != expected for row in rows[table]):
            raise ValidationError(f"run bundle {table} schema does not match this aibench version")


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False
