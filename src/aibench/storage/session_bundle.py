"""Portable, integrity-checked bundles for persistent benchmark sessions."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import stat
import tempfile
import unicodedata
import zipfile
import zlib
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from aibench.core.errors import ConflictError, ValidationError
from aibench.core.plans import ExecutablePlan, PluginEnvironmentRef
from aibench.core.sessions import (
    ActionRequest,
    BenchmarkSession,
    ConversationTurn,
    DecisionRecord,
    PendingQuestion,
)
from aibench.engine.compile import freeze_plan
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.storage.workspace_backup import (
    _MANIFEST_NAME,
    _MAX_MANIFEST_BYTES,
    _is_link,
    _new_archive_path,
    _open_windows_directory_fd,
    _pinned_zip_archive,
    _windows_directory_chain,
    _write_archive,
    _zip_member_is_link,
)

_FORMAT = "aibench-session-bundle"
_VERSION = 1
_DATA_PATH = "session/data.json"
_PLAN_PREFIX = "session/plans/"
_MAX_DATA_BYTES = 64 * 1024 * 1024
_MAX_PLAN_BYTES = 32 * 1024 * 1024
_MAX_BUNDLE_PAYLOAD_BYTES = _MAX_DATA_BYTES + _MAX_PLAN_BYTES
_MAX_SESSION_ROWS = 250_000
_MAX_SESSION_ENTRIES = _MAX_SESSION_ROWS + 2
_MAX_SESSION_CENTRAL_DIRECTORY_BYTES = 64 * 1024 * 1024
_MAX_SESSION_ARCHIVE_BYTES = (
    _MAX_BUNDLE_PAYLOAD_BYTES
    + _MAX_MANIFEST_BYTES
    + _MAX_SESSION_CENTRAL_DIRECTORY_BYTES
    + _MAX_SESSION_ENTRIES * 128
    + 22
    + 65_535
    + 76
)
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WINDOWS_RESERVED_SESSION_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"{prefix}{number}" for prefix in ("COM", "LPT") for number in "123456789\u00b9\u00b2\u00b3"),
}
_PLAN_FILE = re.compile(r"^plan-[0-9a-f]{16}\.json$")
_TABLES = (
    "sessions",
    "decision_records",
    "pending_questions",
    "conversation_turns",
    "action_requests",
)
_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "sessions": ("session_id",),
    "decision_records": ("decision_id",),
    "pending_questions": ("session_id", "question_id"),
    "conversation_turns": ("turn_id",),
    "action_requests": ("action_id",),
}
_ROW_TYPES: dict[str, dict[str, type]] = {
    "sessions": {
        "session_id": str,
        "revision": int,
        "active_run_id": str,
        "data": str,
        "created_at": str,
        "updated_at": str,
    },
    "decision_records": {
        "decision_id": str,
        "session_id": str,
        "revision": int,
        "data": str,
        "created_at": str,
    },
    "pending_questions": {
        "session_id": str,
        "question_id": str,
        "draft_revision": int,
        "status": str,
        "data": str,
        "updated_at": str,
    },
    "conversation_turns": {
        "turn_id": str,
        "session_id": str,
        "sequence": int,
        "role": str,
        "message_id": str,
        "replies_to": str,
        "data": str,
        "created_at": str,
    },
    "action_requests": {
        "action_id": str,
        "session_id": str,
        "kind": str,
        "state": str,
        "run_id": str,
        "data": str,
        "created_at": str,
        "updated_at": str,
    },
}
_NULLABLE_ROW_COLUMNS = {
    ("sessions", "active_run_id"),
    ("conversation_turns", "message_id"),
    ("conversation_turns", "replies_to"),
    ("action_requests", "run_id"),
}
_INSERT_ORDER = _TABLES


@dataclass(frozen=True)
class _PinnedSessionDirectory:
    path: Path
    directory_fd: int | None
    root_fd: int
    sessions_fd: int
    leaf_fd: int
    ancestors: tuple[tuple[str, int, int], ...] = ()


def export_session_bundle(project_root: Path, session_id: str, destination: Path) -> dict[str, Any]:
    """Export one inactive conversation and its frozen plan revisions."""
    workspace = Workspace.at(project_root)
    if not workspace.db_path.is_file():
        raise ValidationError(f"no aibench workspace at {workspace.root}")
    if not _is_portable_session_id(session_id):
        raise ValidationError("session ID cannot be represented as a portable path")
    target = _new_archive_path(destination)
    if _is_inside(target, workspace.root):
        raise ValidationError("session bundle destination must be outside the .aibench workspace")

    data, plans, run_ids = _snapshot_session(workspace, session_id)
    bundle_session = _model_from_row(BenchmarkSession, data["tables"]["sessions"][0], "sessions")
    source_root = _resolved_project_root(bundle_session.project_root)
    _validate_session(data, plans, session_id, source_root)

    try:
        with tempfile.TemporaryDirectory(
            prefix=".aibench-session-bundle-", dir=target.parent
        ) as tmp:
            stage = Path(tmp)
            payload = stage / "payload"
            plan_dir = payload / "plans"
            plan_dir.mkdir(parents=True)
            files: list[dict[str, Any]] = []
            data_path = payload / "data.json"
            data_bytes = _canonical_json(data)
            if len(data_bytes) > _MAX_DATA_BYTES:
                raise ValidationError("session catalog exceeds the supported bundle size")
            data_path.write_bytes(data_bytes)
            files.append(_file_record(data_path, _DATA_PATH))
            for name, contents in sorted(plans.items()):
                path = plan_dir / name
                path.write_bytes(contents)
                files.append(_file_record(path, _PLAN_PREFIX + name))
            files.sort(key=lambda item: item["path"])
            total = sum(item["size_bytes"] for item in files)
            if total > _MAX_BUNDLE_PAYLOAD_BYTES:
                raise ValidationError("session exceeds the supported bundle size")
            manifest = {
                "format": _FORMAT,
                "format_version": _VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "session_id": session_id,
                "files": files,
            }
            manifest_bytes = _canonical_json(manifest)
            if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
                raise ValidationError("session bundle manifest exceeds the supported size")
            staged_archive = stage / "session.zip"
            _write_archive(staged_archive, payload, manifest_bytes, files)
            with staged_archive.open("r+b") as handle:
                os.fsync(handle.fileno())
            os.link(staged_archive, target, follow_symlinks=False)
    except FileExistsError as exc:
        raise ValidationError(f"session bundle destination already exists: {target}") from exc
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise ValidationError(f"could not export session bundle: {exc}") from exc

    return {
        "bundle": str(target),
        "session_id": session_id,
        "revision_count": len(data["tables"]["decision_records"]),
        "turn_count": len(data["tables"]["conversation_turns"]),
        "action_count": len(data["tables"]["action_requests"]),
        "referenced_run_ids": run_ids,
        "total_bytes": total,
        "format_version": _VERSION,
    }


def import_session_bundle(archive_path: Path, project_root: Path) -> dict[str, Any]:
    """Validate, rebase and import a conversation into another project's workspace."""
    destination_root = _resolved_project_root(str(project_root), must_exist=True)
    workspace = Workspace.at(destination_root)
    database: Database | None = None
    try:
        with _pinned_zip_archive(
            archive_path,
            max_archive_bytes=_MAX_SESSION_ARCHIVE_BYTES,
            max_entries=_MAX_SESSION_ENTRIES,
            max_central_directory_bytes=_MAX_SESSION_CENTRAL_DIRECTORY_BYTES,
        ) as zipped:
            manifest, members, records = _read_bundle(zipped)
            data, plans = _read_data(zipped, members, records, manifest)
            session_id = manifest["session_id"]
            source_session = _model_from_row(
                BenchmarkSession, data["tables"]["sessions"][0], "sessions"
            )
            source_root = source_session.project_root
            _validate_session(data, plans, session_id, source_root, validate_dependencies=False)
            rows = _rebase_rows(data["tables"], source_root, destination_root)
            rebased = {"source_project_root": str(destination_root), "tables": rows}
            _validate_session(rebased, plans, session_id, destination_root)
            run_ids = _referenced_run_ids(rows)

            schema_db = Database.open_in_memory()
            try:
                _validate_database_schema(Storage(schema_db), rows)
            finally:
                schema_db.close()

        database = Database.open_workspace(workspace)
        storage = Storage(database)
        _validate_database_schema(storage, rows)
        imported = _insert_session_rows(storage, rows, workspace, session_id, plans)
        return {
            "session_id": session_id,
            "imported": imported,
            "already_present": not imported,
            "revision_count": len(rows["decision_records"]),
            "turn_count": len(rows["conversation_turns"]),
            "action_count": len(rows["action_requests"]),
            "referenced_run_ids": run_ids,
            "format_version": _VERSION,
        }
    except (EOFError, OSError, sqlite3.Error, zipfile.BadZipFile, zlib.error) as exc:
        raise ValidationError(f"could not import session bundle: {exc}") from exc
    finally:
        if database is not None:
            database.close()


def _snapshot_session(
    workspace: Workspace, session_id: str
) -> tuple[dict[str, Any], dict[str, bytes], list[str]]:
    database = Database.open_readonly(workspace.db_path)
    try:
        database.connection.execute("BEGIN")
        counts_and_sizes = []
        for table in _TABLES:
            columns = [
                row[1] for row in database.connection.execute(f'PRAGMA table_info("{table}")')
            ]
            row_size = " + ".join(
                f'COALESCE(length(CAST("{column}" AS BLOB)), 0)' for column in columns
            )
            counts_and_sizes.append(
                database.connection.execute(
                    f"SELECT COUNT(*), COALESCE(SUM({row_size}), 0) "
                    f'FROM "{table}" WHERE session_id = ?',
                    (session_id,),
                ).fetchone()
            )
        if (
            sum(int(row[0]) for row in counts_and_sizes) > _MAX_SESSION_ROWS
            or sum(int(row[1]) for row in counts_and_sizes) > _MAX_DATA_BYTES
        ):
            raise ValidationError("session exceeds the supported bundle size")
        selected: dict[str, list[dict[str, Any]]] = {}
        ordering = {
            "decision_records": " ORDER BY revision",
            "pending_questions": " ORDER BY draft_revision, question_id",
            "conversation_turns": " ORDER BY sequence",
            "action_requests": " ORDER BY created_at, rowid",
        }
        for table in _TABLES:
            query = f'SELECT * FROM "{table}" WHERE session_id = ?'
            cursor = database.connection.execute(query + ordering.get(table, ""), (session_id,))
            selected[table] = [dict(row) for row in cursor.fetchall()]
        if len(selected["sessions"]) != 1:
            raise ValidationError(f"no session {session_id!r} in this workspace")
        session = _model_from_row(BenchmarkSession, selected["sessions"][0], "sessions")
        if session.active_run_id is not None:
            raise ValidationError("cannot export a session while it has an active run")
        plans: dict[str, bytes] = {}
        plan_total = 0
        with _pinned_session_directory(workspace, session_id, create=False) as pin:
            for row in selected["decision_records"]:
                decision = _model_from_row(DecisionRecord, row, "decision_records")
                if _PLAN_FILE.fullmatch(decision.plan_file) is None:
                    raise ValidationError("session contains a plan filename that is not portable")
                if decision.plan_file in plans:
                    continue
                path = pin.path / decision.plan_file
                remaining = _MAX_PLAN_BYTES - plan_total
                contents = _read_plan_file(path, maximum=remaining, directory_fd=pin.directory_fd)
                _verify_pinned_session_directory(pin)
                plans[decision.plan_file] = contents
                plan_total += len(contents)
        data = {
            "source_project_root": str(Path(session.project_root).resolve()),
            "tables": selected,
        }
        return data, plans, _referenced_run_ids(selected)
    finally:
        try:
            database.connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        database.close()


def _validate_session(
    data: dict[str, Any],
    plans: dict[str, bytes],
    session_id: str,
    project_root: Path,
    *,
    validate_dependencies: bool = True,
) -> None:
    if not isinstance(data, dict) or set(data) != {"source_project_root", "tables"}:
        raise ValidationError("session bundle catalog has an unsupported structure")
    source_context = data.get("source_project_root")
    if not isinstance(source_context, str):
        raise ValidationError("session bundle project path context is inconsistent")
    tables = data.get("tables")
    if not isinstance(tables, dict) or set(tables) != set(_TABLES):
        raise ValidationError("session bundle catalog contains unsupported tables")
    rows: dict[str, list[dict[str, Any]]] = {}
    total = 0
    for table in _TABLES:
        items = tables[table]
        if not isinstance(items, list):
            raise ValidationError(f"session bundle {table} is malformed")
        total += len(items)
        if total > _MAX_SESSION_ROWS:
            raise ValidationError("session bundle has too many records")
        rows[table] = []
        for row in items:
            if not isinstance(row, dict) or not _row_has_expected_types(table, row):
                raise ValidationError(f"session bundle {table} contains an invalid row")
            rows[table].append(row)
    if len(rows["sessions"]) != 1:
        raise ValidationError("session bundle must contain exactly one session")
    session = _model_from_row(BenchmarkSession, rows["sessions"][0], "sessions")
    if (
        session.session_id != session_id
        or rows["sessions"][0].get("session_id") != session_id
        or not _is_portable_session_id(session_id)
        or session.active_run_id is not None
        or rows["sessions"][0].get("revision") != session.revision
        or rows["sessions"][0].get("active_run_id") is not None
    ):
        raise ValidationError("session bundle identity or active-run state is invalid")
    if source_context != session.project_root:
        raise ValidationError("session bundle project path context is inconsistent")
    if validate_dependencies and (
        _resolved_project_root(session.project_root) != project_root.resolve()
        or _resolved_project_root(source_context) != project_root.resolve()
    ):
        raise ValidationError("session bundle project root does not match its path context")

    turns: dict[str, ConversationTurn] = {}
    turn_sequences: set[int] = set()
    delivery_ids: set[tuple[str, str]] = set()
    for row in rows["conversation_turns"]:
        turn = _model_from_row(ConversationTurn, row, "conversation_turns")
        if (
            turn.session_id != session_id
            or row.get("session_id") != session_id
            or row.get("turn_id") != turn.turn_id
            or row.get("sequence") != turn.sequence
            or row.get("role") != turn.role
            or row.get("message_id") != turn.message_id
            or row.get("replies_to") != turn.replies_to
            or turn.turn_id in turns
            or turn.sequence in turn_sequences
            or (turn.message_id is not None and (turn.role, turn.message_id) in delivery_ids)
        ):
            raise ValidationError("session bundle conversation turn is inconsistent")
        turns[turn.turn_id] = turn
        turn_sequences.add(turn.sequence)
        if turn.message_id is not None:
            delivery_ids.add((turn.role, turn.message_id))
    if turn_sequences != set(range(1, len(turn_sequences) + 1)):
        raise ValidationError("session bundle conversation sequence is not contiguous")
    replies: set[str] = set()
    for turn in turns.values():
        if turn.replies_to is not None:
            parent = turns.get(turn.replies_to)
            if (
                turn.role != "assistant"
                or parent is None
                or parent.role != "user"
                or turn.replies_to in replies
            ):
                raise ValidationError("session bundle has an invalid conversation reply link")
            replies.add(turn.replies_to)

    decisions: dict[str, DecisionRecord] = {}
    by_revision: dict[int, DecisionRecord] = {}
    for row in rows["decision_records"]:
        decision = _model_from_row(DecisionRecord, row, "decision_records")
        if (
            decision.session_id != session_id
            or row.get("decision_id") != decision.decision_id
            or row.get("revision") != decision.revision
            or decision.decision_id in decisions
            or decision.revision in by_revision
            or row.get("session_id") != session_id
            or (decision.source_turn_id is not None and decision.source_turn_id not in turns)
            or _PLAN_FILE.fullmatch(decision.plan_file) is None
        ):
            raise ValidationError("session bundle decision is inconsistent")
        decisions[decision.decision_id] = decision
        by_revision[decision.revision] = decision
    if (
        not by_revision
        or sorted(by_revision) != list(range(1, session.revision + 1))
        or session.decision_id not in decisions
        or by_revision[session.revision].decision_id != session.decision_id
    ):
        raise ValidationError("session bundle decision history is incomplete")
    for revision, decision in by_revision.items():
        if revision == 1 and decision.supersedes is not None:
            raise ValidationError("initial session decision unexpectedly supersedes another")
        if revision > 1 and decision.supersedes != by_revision[revision - 1].decision_id:
            raise ValidationError("session bundle decision lineage is invalid")
        draft = decision.draft
        if (
            not isinstance(draft, Mapping)
            or draft.get("revision") != revision
            or draft.get("plan_hash") != decision.plan_hash
            or draft.get("plan_file") != decision.plan_file
            or draft.get("supersedes")
            != (by_revision[revision - 1].plan_hash if revision > 1 else None)
        ):
            raise ValidationError("session bundle draft does not match its decision")
        contents = plans.get(decision.plan_file)
        if contents is None or len(contents) > _MAX_PLAN_BYTES:
            raise ValidationError(f"session bundle is missing plan {decision.plan_file!r}")
        try:
            plan = ExecutablePlan.model_validate_json(contents)
            _, plan_hash = freeze_plan(plan)
        except (ValueError, TypeError, RecursionError) as exc:
            raise ValidationError(f"session bundle plan {decision.plan_file!r} is invalid") from exc
        if plan.plan_id != session_id or plan_hash != decision.plan_hash:
            raise ValidationError(
                f"session bundle plan {decision.plan_file!r} fails its identity check"
            )
        if validate_dependencies:
            _validate_project_dependencies(session, decision, plan, project_root)
    expected_plan_files = {decision.plan_file for decision in decisions.values()}
    if set(plans) != expected_plan_files:
        raise ValidationError("session bundle contains missing or unreferenced plan files")

    questions: set[str] = set()
    for row in rows["pending_questions"]:
        question = _model_from_row(PendingQuestion, row, "pending_questions")
        if (
            row.get("session_id") != session_id
            or row.get("question_id") != question.question_id
            or row.get("draft_revision") != question.draft_revision
            or row.get("status") != question.status
            or question.draft_revision < 1
            or question.draft_revision > session.revision
            or question.question_id in questions
        ):
            raise ValidationError("session bundle question is inconsistent")
        questions.add(question.question_id)

    actions: dict[str, ActionRequest] = {}
    for row in rows["action_requests"]:
        action = _model_from_row(ActionRequest, row, "action_requests")
        if (
            action.session_id != session_id
            or row.get("session_id") != session_id
            or row.get("action_id") != action.action_id
            or row.get("kind") != action.kind.value
            or row.get("state") != action.state.value
            or row.get("run_id") != action.run_id
            or action.action_id in actions
            or (action.source_turn_id is not None and action.source_turn_id not in turns)
        ):
            raise ValidationError("session bundle action is inconsistent")
        actions[action.action_id] = action
    for turn in turns.values():
        if any(ref not in decisions for ref in turn.decision_refs) or any(
            ref not in actions for ref in turn.action_refs
        ):
            raise ValidationError("session bundle turn has a dangling decision or action link")


def _validate_project_dependencies(
    session: BenchmarkSession,
    decision: DecisionRecord,
    plan: ExecutablePlan,
    project_root: Path,
) -> None:
    _portable_path(session.project_root, project_root, "project root", expected="dir")
    if session.policy_path is not None:
        _portable_path(session.policy_path, project_root, "policy", expected="file")
    choices = decision.choices
    application = _portable_path(
        choices.application, project_root, "application config", expected="file"
    )
    dataset = _portable_path(choices.dataset, project_root, "dataset", expected="file")
    plan_path = (
        Workspace.at(project_root).root / "sessions" / session.session_id / decision.plan_file
    )
    if (plan_path.parent / plan.application).resolve(strict=False) != application:
        raise ValidationError("session frozen plan application path does not rebase to its config")
    if (plan_path.parent / plan.dataset).resolve(strict=False) != dataset:
        raise ValidationError("session frozen plan dataset path does not rebase to its dataset")
    for raw in session.plugin_environments:
        try:
            PluginEnvironmentRef.model_validate(raw)
        except (ValueError, TypeError) as exc:
            raise ValidationError("session bundle plugin environment is invalid") from exc


def _rebase_rows(
    tables: dict[str, list[dict[str, Any]]], source_root: str | Path, destination_root: Path
) -> dict[str, list[dict[str, Any]]]:
    rebased = {table: [dict(row) for row in rows] for table, rows in tables.items()}
    destination_root = destination_root.resolve()
    session_row = rebased["sessions"][0]
    session = _model_from_row(BenchmarkSession, session_row, "sessions")
    session = session.model_copy(
        update={
            "project_root": str(destination_root),
            "policy_path": (
                _rebase_path(session.policy_path, source_root, destination_root)
                if session.policy_path is not None
                else None
            ),
            "trusted_local": False,
        }
    )
    session_row["data"] = session.model_dump_json()
    session_row["revision"] = session.revision
    session_row["active_run_id"] = None
    for row in rebased["decision_records"]:
        decision = _model_from_row(DecisionRecord, row, "decision_records")
        choices = decision.choices.model_copy(
            update={
                "application": _rebase_path(
                    decision.choices.application, source_root, destination_root
                ),
                "dataset": _rebase_path(decision.choices.dataset, source_root, destination_root),
            }
        )
        updated = decision.model_copy(update={"choices": choices})
        row["data"] = updated.model_dump_json()
    return rebased


def _rebase_path(value: str, source_root: str | Path, destination_root: Path) -> str:
    source_text = str(source_root)
    if _is_windows_path(source_text):
        path = PureWindowsPath(value)
        if not path.is_absolute():
            raise ValidationError(f"session dependency is not absolute: {value}")
        try:
            relative_parts = path.relative_to(PureWindowsPath(source_text)).parts
        except ValueError as exc:
            raise ValidationError(
                f"session dependency is outside its project root: {value}"
            ) from exc
        if any(part in {"", ".", ".."} for part in relative_parts):
            raise ValidationError(f"session dependency escapes its project root: {value}")
        relative = Path(*relative_parts)
    else:
        resolved = Path(value).resolve(strict=False)
        try:
            relative = resolved.relative_to(Path(source_text).resolve(strict=False))
        except ValueError as exc:
            raise ValidationError(
                f"session dependency is outside its project root: {value}"
            ) from exc
    rebased = (destination_root / relative).resolve(strict=False)
    if not _is_inside(rebased, destination_root):
        raise ValidationError("session dependency escapes the destination project")
    return str(rebased)


def _is_windows_path(value: str) -> bool:
    path = PureWindowsPath(value)
    return bool(path.drive) or ("\\" in value and not value.startswith("/"))


def _portable_path(value: str, root: Path, label: str, *, expected: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError(f"session {label} is not an absolute path")
    resolved = path.resolve(strict=False)
    if not _is_inside(resolved, root):
        raise ValidationError(
            f"session {label} is outside the project and cannot be rebased: {value}"
        )
    if expected == "file" and not resolved.is_file():
        raise ValidationError(f"session {label} is missing: {value}")
    if expected == "dir" and not resolved.is_dir():
        raise ValidationError(f"session {label} is missing: {value}")
    if expected == "path" and not resolved.exists():
        raise ValidationError(f"session {label} is missing: {value}")
    return resolved


def _read_bundle(
    zipped: zipfile.ZipFile,
) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo], dict[str, dict[str, Any]]]:
    infos = zipped.infolist()
    if not infos or len(infos) > _MAX_SESSION_ENTRIES:
        raise ValidationError("session bundle has an invalid number of entries")
    members: dict[str, zipfile.ZipInfo] = {}
    folded: set[str] = set()
    for info in infos:
        name = info.filename
        pure = PurePosixPath(name)
        if (
            name in members
            or unicodedata.normalize("NFD", name).casefold() in folded
            or "\\" in name
            or "\x00" in name
            or name.startswith("/")
            or pure.as_posix() != name
            or any(part in {"", ".", ".."} for part in pure.parts)
        ):
            raise ValidationError("session bundle contains an unsafe or duplicate path")
        if info.is_dir() or _zip_member_is_link(info) or info.flag_bits & 0x1:
            raise ValidationError("session bundle contains a link, directory or encrypted entry")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ValidationError("session bundle uses an unsupported compression method")
        members[name] = info
        folded.add(unicodedata.normalize("NFD", name).casefold())
    manifest_info = members.get(_MANIFEST_NAME)
    if manifest_info is None or manifest_info.file_size > _MAX_MANIFEST_BYTES:
        raise ValidationError("session bundle manifest is missing or too large")
    manifest = _json_member(zipped, manifest_info, _MAX_MANIFEST_BYTES, "manifest")
    if (
        not isinstance(manifest, dict)
        or manifest.get("format") != _FORMAT
        or isinstance(manifest.get("format_version"), bool)
        or manifest.get("format_version") != _VERSION
        or not isinstance(manifest.get("session_id"), str)
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValidationError("session bundle format is unsupported")
    records: dict[str, dict[str, Any]] = {}
    total = 0
    plan_total = 0
    for record in manifest["files"]:
        if not isinstance(record, dict):
            raise ValidationError("session bundle contains an invalid file record")
        path_value, size, digest = (
            record.get("path"),
            record.get("size_bytes"),
            record.get("sha256"),
        )
        if (
            not isinstance(path_value, str)
            or path_value not in members
            or path_value == _MANIFEST_NAME
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or members[path_value].file_size != size
            or path_value in records
            or (
                path_value != _DATA_PATH
                and re.fullmatch(_PLAN_PREFIX + r"plan-[0-9a-f]{16}\.json", path_value) is None
            )
        ):
            raise ValidationError("session bundle manifest does not match its archive")
        records[path_value] = record
        total += size
        if path_value.startswith(_PLAN_PREFIX):
            plan_total += size
        if total > _MAX_BUNDLE_PAYLOAD_BYTES or plan_total > _MAX_PLAN_BYTES:
            raise ValidationError("session bundle exceeds the supported uncompressed size")
    if (
        set(records) != set(members) - {_MANIFEST_NAME}
        or _DATA_PATH not in records
        or len(records) > _MAX_SESSION_ROWS + 1
    ):
        raise ValidationError("session bundle is missing required data")
    if records[_DATA_PATH]["size_bytes"] > _MAX_DATA_BYTES:
        raise ValidationError("session bundle catalog exceeds the supported size")
    if any(
        name.startswith(_PLAN_PREFIX) and record["size_bytes"] > _MAX_PLAN_BYTES
        for name, record in records.items()
    ):
        raise ValidationError("session bundle plan exceeds the supported size")
    manifest["files"] = list(records.values())
    return manifest, members, records


def _read_data(
    zipped: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    records: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, bytes]]:
    data_bytes = _verified_member(zipped, members[_DATA_PATH], records[_DATA_PATH])
    data = _parse_json(data_bytes, "session catalog")
    if not isinstance(data, dict) or not isinstance(data.get("tables"), dict):
        raise ValidationError("session bundle catalog has an unsupported structure")
    session_rows = data["tables"].get("sessions")
    if (
        not isinstance(session_rows, list)
        or len(session_rows) != 1
        or not isinstance(session_rows[0], dict)
        or session_rows[0].get("session_id") != manifest["session_id"]
    ):
        raise ValidationError("session bundle identity does not match its catalog")
    plans: dict[str, bytes] = {}
    for name, member in members.items():
        if name.startswith(_PLAN_PREFIX):
            content = _verified_member(zipped, member, records[name])
            plans[name.removeprefix(_PLAN_PREFIX)] = content
    return data, plans


def _json_member(zipped: zipfile.ZipFile, member: zipfile.ZipInfo, maximum: int, label: str) -> Any:
    return _parse_json(_bounded_member_bytes(zipped, member, maximum), f"session bundle {label}")


def _parse_json(raw: bytes, label: str) -> Any:
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValidationError(f"{label} is malformed") from exc


def _bounded_member_bytes(zipped: zipfile.ZipFile, member: zipfile.ZipInfo, maximum: int) -> bytes:
    if member.file_size > maximum:
        raise ValidationError("session bundle entry exceeds its supported size")
    chunks: list[bytes] = []
    total = 0
    with zipped.open(member, "r") as source:
        while block := source.read(min(1024 * 1024, maximum - total + 1)):
            total += len(block)
            if total > maximum or total > member.file_size:
                raise ValidationError("session bundle entry exceeds its declared size")
            chunks.append(block)
    if total != member.file_size:
        raise ValidationError("session bundle entry is truncated")
    return b"".join(chunks)


def _verified_member(
    zipped: zipfile.ZipFile, member: zipfile.ZipInfo, record: dict[str, Any]
) -> bytes:
    raw = _bounded_member_bytes(zipped, member, record["size_bytes"])
    if hashlib.sha256(raw).hexdigest() != record["sha256"]:
        raise ValidationError(f"session bundle entry failed its integrity check: {member.filename}")
    return raw


def _insert_session_rows(
    storage: Storage,
    rows: dict[str, list[dict[str, Any]]],
    workspace: Workspace,
    session_id: str,
    plans: dict[str, bytes],
) -> bool:
    connection = storage.conn
    inserted = False
    connection.execute("BEGIN IMMEDIATE")
    try:
        with _pinned_session_directory(workspace, session_id, create=True) as pin:
            created_files: list[tuple[str, tuple[int, int]]] = []
            try:
                for table in _INSERT_ORDER:
                    columns = set(_table_columns(connection, table))
                    for row in rows[table]:
                        if set(row) != columns:
                            raise ValidationError(
                                f"session bundle {table} schema does not match this version"
                            )
                        primary = _PRIMARY_KEYS[table]
                        where = " AND ".join(f'"{name}" IS ?' for name in primary)
                        existing = connection.execute(
                            f'SELECT * FROM "{table}" WHERE {where}',
                            tuple(row[name] for name in primary),
                        ).fetchone()
                        if existing is not None:
                            if dict(existing) != row:
                                raise ConflictError(
                                    f"{table} already contains different data for "
                                    f"{tuple(row[name] for name in primary)!r}"
                                )
                            continue
                        names = tuple(row)
                        sql_columns = ", ".join(f'"{name}"' for name in names)
                        placeholders = ", ".join("?" for _ in names)
                        connection.execute(
                            f'INSERT INTO "{table}" ({sql_columns}) VALUES ({placeholders})',
                            tuple(row[name] for name in names),
                        )
                        inserted = True
                for name, contents in plans.items():
                    target = pin.path / name
                    existing_info = _session_plan_stat(target, pin.directory_fd)
                    if existing_info is not None:
                        if not _existing_plan_matches(target, contents, pin.directory_fd):
                            raise ConflictError(
                                f"session plan {name!r} already exists with different data"
                            )
                        continue
                    staging_name, descriptor = _create_session_plan_staging(
                        pin.path, name, pin.directory_fd
                    )
                    try:
                        with os.fdopen(descriptor, "wb") as handle:
                            handle.write(contents)
                            handle.flush()
                            os.fsync(handle.fileno())
                            staging_identity = os.fstat(handle.fileno())
                        _link_session_plan(pin.path, staging_name, name, pin.directory_fd)
                        created_files.append(
                            (name, (staging_identity.st_dev, staging_identity.st_ino))
                        )
                        target_info = _session_plan_stat(target, pin.directory_fd)
                        if (
                            target_info is None
                            or not stat.S_ISREG(target_info.st_mode)
                            or (target_info.st_dev, target_info.st_ino)
                            != (staging_identity.st_dev, staging_identity.st_ino)
                        ):
                            raise ConflictError(
                                f"session plan {name!r} changed while it was being imported"
                            )
                        inserted = True
                    except FileExistsError:
                        if not _existing_plan_matches(target, contents, pin.directory_fd):
                            raise ConflictError(
                                f"session plan {name!r} already exists with different data"
                            )
                    finally:
                        _unlink_session_plan(pin.path, staging_name, pin.directory_fd)
                _verify_pinned_session_directory(pin)
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                for name, identity in created_files:
                    _unlink_session_plan_if_identity(pin.path, name, pin.directory_fd, identity)
                raise
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return inserted


def _validate_database_schema(storage: Storage, rows: dict[str, list[dict[str, Any]]]) -> None:
    for table in _TABLES:
        expected = set(_table_columns(storage.conn, table))
        if any(set(row) != expected for row in rows[table]):
            raise ValidationError(
                f"session bundle {table} schema does not match this aibench version"
            )


def _table_columns(connection: sqlite3.Connection, table: str) -> tuple[str, ...]:
    return tuple(row[1] for row in connection.execute(f'PRAGMA table_info("{table}")'))


def _model_from_row(model_type: Any, row: dict[str, Any], table: str) -> Any:
    raw = row.get("data")
    if not isinstance(raw, str):
        raise ValidationError(f"session bundle {table} data is missing")
    try:
        return model_type.model_validate_json(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValidationError(f"session bundle {table} data is invalid") from exc


def _referenced_run_ids(rows: dict[str, list[dict[str, Any]]]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for row in rows.get("action_requests", []):
        action = _model_from_row(ActionRequest, row, "action_requests")
        if action.run_id is not None and action.run_id not in seen:
            result.append(action.run_id)
            seen.add(action.run_id)
    session_rows = rows.get("sessions", [])
    if len(session_rows) == 1:
        session = _model_from_row(BenchmarkSession, session_rows[0], "sessions")
        for run_id in session.event_cursors:
            if run_id not in seen:
                result.append(run_id)
                seen.add(run_id)
    return result


def _resolved_project_root(value: str, *, must_exist: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError("session bundle project root is not absolute")
    try:
        root = path.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"could not resolve session project root: {exc}") from exc
    if must_exist and not root.is_dir():
        raise ValidationError("destination project root must be an existing directory")
    return root


@contextmanager
def _pinned_session_directory(
    workspace: Workspace, session_id: str, *, create: bool
) -> Iterator[_PinnedSessionDirectory]:
    if not _is_portable_session_id(session_id):
        raise ValidationError("session ID cannot be represented as a portable path")
    root = workspace.root.resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("session workspace directory is missing")
    sessions = root / "sessions"
    directory = sessions / session_id
    if os.name == "nt":
        descriptors: list[int] = []
        try:
            root_chain = _windows_directory_chain(root)
            for current in root_chain:
                descriptors.append(_pin_windows_session_directory(current))
            root_fd = descriptors[-1]
            if create:
                sessions.mkdir(exist_ok=True)
            descriptors.append(_pin_windows_session_directory(sessions))
            sessions_fd = descriptors[-1]
            if create:
                directory.mkdir(exist_ok=True)
            descriptors.append(_pin_windows_session_directory(directory))
            yield _PinnedSessionDirectory(directory, None, root_fd, sessions_fd, descriptors[-1])
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors = []
    try:
        anchor = Path(root.anchor)
        parent_fd = os.open(anchor, flags)
        descriptors.append(parent_fd)
        _verify_pinned_directory_path(anchor, parent_fd)
        ancestors: list[tuple[str, int, int]] = []
        root_parts = root.relative_to(anchor).parts
        current_path = anchor
        for part in root_parts:
            descriptor = os.open(part, flags, dir_fd=parent_fd)
            descriptors.append(descriptor)
            current_path /= part
            _verify_pinned_session_child(part, parent_fd, current_path, descriptor)
            ancestors.append((part, parent_fd, descriptor))
            parent_fd = descriptor
        root_fd = parent_fd
        parent_fd = root_fd
        sessions_fd = root_fd
        for part in ("sessions", session_id):
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            descriptor = os.open(part, flags, dir_fd=parent_fd)
            descriptors.append(descriptor)
            child_path = root / "sessions" / session_id if part == session_id else sessions
            _verify_pinned_session_child(part, parent_fd, child_path, descriptor)
            parent_fd = descriptor
            if part == "sessions":
                sessions_fd = descriptor
        yield _PinnedSessionDirectory(
            directory,
            parent_fd,
            root_fd,
            sessions_fd,
            parent_fd,
            tuple(ancestors),
        )
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _pin_windows_session_directory(path: Path) -> int:
    descriptor: int | None = None
    try:
        descriptor = _open_windows_directory_fd(path, share_mode=0x03)
        _verify_pinned_directory_path(path, descriptor)
        return descriptor
    except (OSError, ValidationError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(f"session directory changed while being pinned: {path}") from exc


def _verify_pinned_directory_path(path: Path, descriptor: int) -> None:
    try:
        current = path.lstat()
        pinned = os.fstat(descriptor)
    except OSError as exc:
        raise ValidationError("session directory changed while being pinned") from exc
    if (
        _is_link(current)
        or not stat.S_ISDIR(current.st_mode)
        or not stat.S_ISDIR(pinned.st_mode)
        or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise ValidationError("session directory changed while being pinned")


def _verify_pinned_session_directory(pin: _PinnedSessionDirectory) -> None:
    if os.name == "nt":
        _verify_pinned_directory_path(pin.path.parent.parent, pin.root_fd)
        _verify_pinned_directory_path(pin.path.parent, pin.sessions_fd)
        _verify_pinned_directory_path(pin.path, pin.leaf_fd)
        return
    for index, (name, parent_fd, descriptor) in enumerate(pin.ancestors):
        ancestor_path = Path(pin.path.anchor).joinpath(
            *(part for part, _, _ in pin.ancestors[: index + 1])
        )
        _verify_pinned_session_child(name, parent_fd, ancestor_path, descriptor)
    _verify_pinned_session_child("sessions", pin.root_fd, pin.path.parent, pin.sessions_fd)
    _verify_pinned_session_child(pin.path.name, pin.sessions_fd, pin.path, pin.leaf_fd)


def _verify_pinned_session_child(name: str, parent_fd: int, path: Path, descriptor: int) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        pinned = os.fstat(descriptor)
    except OSError as exc:
        raise ValidationError("session directory changed while being pinned") from exc
    if (
        _is_link(current)
        or not stat.S_ISDIR(current.st_mode)
        or not stat.S_ISDIR(pinned.st_mode)
        or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
    ):
        raise ValidationError(f"session directory changed while being pinned: {path}")


def _session_plan_stat(path: Path, directory_fd: int | None) -> os.stat_result | None:
    try:
        if directory_fd is not None:
            return os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        return path.lstat()
    except FileNotFoundError:
        return None


def _create_session_plan_staging(
    directory: Path, name: str, directory_fd: int | None
) -> tuple[str, int]:
    if directory_fd is not None:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        for _ in range(16):
            staging = f".{name}.{secrets.token_hex(12)}.tmp"
            try:
                return staging, os.open(staging, flags, 0o600, dir_fd=directory_fd)
            except FileExistsError:
                continue
        raise OSError("could not allocate a unique session plan staging file")
    descriptor, staging = tempfile.mkstemp(prefix=f".{name}.", suffix=".tmp", dir=directory)
    return Path(staging).name, descriptor


def _link_session_plan(directory: Path, staging: str, name: str, directory_fd: int | None) -> None:
    if directory_fd is not None:
        os.link(
            staging,
            name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
            follow_symlinks=False,
        )
    else:
        os.link(directory / staging, directory / name, follow_symlinks=False)


def _unlink_session_plan(directory: Path, name: str, directory_fd: int | None) -> None:
    try:
        if directory_fd is not None:
            os.unlink(name, dir_fd=directory_fd)
        else:
            (directory / name).unlink()
    except FileNotFoundError:
        pass


def _unlink_session_plan_if_identity(
    directory: Path,
    name: str,
    directory_fd: int | None,
    identity: tuple[int, int],
) -> None:
    path = directory / name
    info = _session_plan_stat(path, directory_fd)
    if info is not None and stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity:
        _unlink_session_plan(directory, name, directory_fd)


def _is_portable_session_id(session_id: str) -> bool:
    if _SESSION_ID.fullmatch(session_id) is None or session_id.endswith("."):
        return False
    device_stem = session_id.partition(".")[0].upper()
    return device_stem not in _WINDOWS_RESERVED_SESSION_STEMS


def _sqlite_value(value: Any) -> bool:
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _row_has_expected_types(table: str, row: dict[str, Any]) -> bool:
    expected = _ROW_TYPES[table]
    if set(row) != set(expected):
        return False
    for column, expected_type in expected.items():
        value = row[column]
        if value is None and (table, column) in _NULLABLE_ROW_COLUMNS:
            continue
        if type(value) is not expected_type or not _sqlite_value(value):
            return False
    return all(row[column] is not None for column in _PRIMARY_KEYS[table])


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _file_record(path: Path, name: str) -> dict[str, Any]:
    raw = path.read_bytes()
    return {"path": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def _read_plan_file(
    path: Path,
    *,
    maximum: int = _MAX_PLAN_BYTES,
    directory_fd: int | None = None,
) -> bytes:
    descriptor: int | None = None
    maximum = min(maximum, _MAX_PLAN_BYTES)
    if maximum < 0:
        raise ValidationError("session exceeds the supported frozen plan size")
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = (
            os.open(path, flags)
            if directory_fd is None
            else os.open(path.name, flags, dir_fd=directory_fd)
        )
        before = os.fstat(descriptor)
        current = _session_plan_stat(path, directory_fd)
        if (
            current is None
            or _is_link(current)
            or not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or before.st_size > maximum
        ):
            raise ValidationError("session plan is not a stable, bounded regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            contents = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            current = _session_plan_stat(path, directory_fd)
        if (
            len(contents) != before.st_size
            or len(contents) > maximum
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)
            or (after.st_size, after.st_mtime_ns, after.st_ctime_ns)
            != (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            or current is None
            or _is_link(current)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError("session plan changed while it was being exported")
        return contents
    except ValidationError:
        raise
    except OSError as exc:
        raise ValidationError(f"could not read session plan: {exc}") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _existing_plan_matches(path: Path, contents: bytes, directory_fd: int | None = None) -> bool:
    try:
        info = _session_plan_stat(path, directory_fd)
        return (
            info is not None
            and stat.S_ISREG(info.st_mode)
            and info.st_size == len(contents)
            and _read_plan_file(path, directory_fd=directory_fd) == contents
        )
    except (OSError, ValidationError):
        return False


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False
