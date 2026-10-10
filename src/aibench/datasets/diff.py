"""Bounded, identity-aware comparisons between validated JSONL datasets."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any, NoReturn

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import ValidationError
from aibench.datasets.ingest import MAX_LINE_BYTES, iter_jsonl_lines
from aibench.datasets.normalize import normalize_case

DEFAULT_DETAIL_LIMIT = 50
MAX_DETAIL_LIMIT = 1_000
MAX_DETAIL_BYTES_PER_CATEGORY = 16_384


class DatasetDiffError(ValueError):
    """A dataset cannot be safely validated or compared."""


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant {name} is not allowed")


def _load_dataset(
    connection: sqlite3.Connection,
    path: Path,
    side: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise DatasetDiffError(f"dataset file not found: {path}")

    hasher = hashlib.sha256()
    count = 0
    for line_number, line in iter_jsonl_lines(path):
        count += 1
        hasher.update(line.encode("utf-8"))
        try:
            raw = json.loads(line, parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise DatasetDiffError(f"{path}: line {line_number}: invalid JSON: {exc}") from exc
        except RecursionError as exc:
            raise DatasetDiffError(
                f"{path}: line {line_number}: JSON record is nested too deeply"
            ) from exc

        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("case_id"), str)
            or not raw.get("case_id", "").strip()
        ):
            raise DatasetDiffError(
                f"{path}: line {line_number}: dataset diff requires a non-empty explicit case_id"
            )

        try:
            normalized = normalize_case(raw, line=line_number, occurrence_index=count)
            case = normalized.case
            if case is None:
                raise DatasetDiffError(f"{path}: line {line_number}: no case was produced")
            canonical = json.dumps(
                case.model_dump(
                    mode="json",
                    exclude={"source_line", "duplicate_of_line"},
                    exclude_none=True,
                ),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            encoded = canonical.encode("utf-8")
        except DatasetDiffError:
            raise
        except (
            ValidationError,
            PydanticValidationError,
            TypeError,
            ValueError,
            RecursionError,
            UnicodeEncodeError,
        ) as exc:
            raise DatasetDiffError(f"{path}: line {line_number}: {exc}") from exc

        if len(encoded) > MAX_LINE_BYTES:
            raise DatasetDiffError(
                f"{path}: line {line_number}: normalized case exceeds {MAX_LINE_BYTES} bytes"
            )
        try:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO cases (side, case_id, canonical, source_line) "
                "VALUES (?, ?, ?, ?)",
                (side, case.case_id, canonical, line_number),
            )
        except sqlite3.Error as exc:
            raise DatasetDiffError(f"could not index dataset {path}: {exc}") from exc
        if cursor.rowcount == 0:
            first = connection.execute(
                "SELECT source_line FROM cases WHERE side = ? AND case_id = ?",
                (side, case.case_id),
            ).fetchone()
            first_line = first[0] if first else "unknown"
            raise DatasetDiffError(
                f"{path}: duplicate case_id at line {line_number} (first seen at line "
                f"{first_line}); dataset diff requires unique case IDs"
            )

    return {
        "path": str(path.resolve()),
        "dataset_id": path.stem,
        "content_hash": "sha256:" + hasher.hexdigest(),
        "case_count": count,
    }


def _count(connection: sqlite3.Connection, query: str, values: tuple[str, ...]) -> int:
    row = connection.execute(query, values).fetchone()
    return int(row[0]) if row else 0


def _sample_ids(
    connection: sqlite3.Connection,
    query: str,
    values: tuple[str, ...],
    limit: int,
    total_count: int,
) -> tuple[list[str], int]:
    if limit == 0:
        return [], total_count
    cursor = connection.execute(query, (*values, limit))
    sampled: list[str] = []
    used_bytes = 0
    for row in cursor:
        case_id = str(row[0])
        encoded = json.dumps(case_id, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if used_bytes + len(encoded) > MAX_DETAIL_BYTES_PER_CATEGORY:
            continue
        sampled.append(case_id)
        used_bytes += len(encoded)
    return sampled, total_count - len(sampled)


def diff_datasets(left: Path, right: Path, *, limit: int = DEFAULT_DETAIL_LIMIT) -> dict[str, Any]:
    """Compare normalized cases by unique explicit case ID with an on-disk index.

    Missing or repeated IDs are rejected because there is no stable way to pair such rows.
    Case details are never returned; only bounded ID samples and exact counts are exposed.
    """
    if not 0 <= limit <= MAX_DETAIL_LIMIT:
        raise DatasetDiffError(f"detail limit must be between 0 and {MAX_DETAIL_LIMIT}")

    with tempfile.TemporaryDirectory(prefix="aibench-dataset-diff-") as directory:
        database = Path(directory) / "diff.sqlite3"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(database)
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            connection.execute(
                "CREATE TABLE cases ("
                "side TEXT NOT NULL, "
                "case_id TEXT NOT NULL, "
                "canonical TEXT NOT NULL, "
                "source_line INTEGER NOT NULL, "
                "PRIMARY KEY (side, case_id)"
                ")"
            )
            left_info = _load_dataset(connection, left, "left")
            right_info = _load_dataset(connection, right, "right")
            connection.commit()

            left_only = (
                "SELECT l.case_id FROM cases AS l "
                "LEFT JOIN cases AS r ON r.side = ? AND r.case_id = l.case_id "
                "WHERE l.side = ? AND r.case_id IS NULL ORDER BY l.case_id LIMIT ?"
            )
            right_only = (
                "SELECT r.case_id FROM cases AS r "
                "LEFT JOIN cases AS l ON l.side = ? AND l.case_id = r.case_id "
                "WHERE r.side = ? AND l.case_id IS NULL ORDER BY r.case_id LIMIT ?"
            )
            changed = (
                "SELECT l.case_id FROM cases AS l "
                "JOIN cases AS r ON r.side = ? AND r.case_id = l.case_id "
                "WHERE l.side = ? AND l.canonical != r.canonical ORDER BY l.case_id LIMIT ?"
            )
            added_count = _count(
                connection,
                "SELECT COUNT(*) FROM cases AS r LEFT JOIN cases AS l "
                "ON l.side = ? AND l.case_id = r.case_id "
                "WHERE r.side = ? AND l.case_id IS NULL",
                ("left", "right"),
            )
            removed_count = _count(
                connection,
                "SELECT COUNT(*) FROM cases AS l LEFT JOIN cases AS r "
                "ON r.side = ? AND r.case_id = l.case_id "
                "WHERE l.side = ? AND r.case_id IS NULL",
                ("right", "left"),
            )
            changed_count = _count(
                connection,
                "SELECT COUNT(*) FROM cases AS l JOIN cases AS r "
                "ON r.side = ? AND r.case_id = l.case_id "
                "WHERE l.side = ? AND l.canonical != r.canonical",
                ("right", "left"),
            )
            common_count = _count(
                connection,
                "SELECT COUNT(*) FROM cases AS l JOIN cases AS r "
                "ON r.side = ? AND r.case_id = l.case_id WHERE l.side = ?",
                ("right", "left"),
            )
            added_ids, added_omitted = _sample_ids(
                connection, right_only, ("left", "right"), limit, added_count
            )
            removed_ids, removed_omitted = _sample_ids(
                connection, left_only, ("right", "left"), limit, removed_count
            )
            changed_ids, changed_omitted = _sample_ids(
                connection, changed, ("right", "left"), limit, changed_count
            )
        except DatasetDiffError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise DatasetDiffError(f"could not compare datasets: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()

    unchanged_count = common_count - changed_count
    return {
        "left": left_info,
        "right": right_info,
        "summary": {
            "added": added_count,
            "removed": removed_count,
            "changed": changed_count,
            "unchanged": unchanged_count,
            "has_changes": bool(added_count or removed_count or changed_count),
        },
        "details": {
            "added_case_ids": added_ids,
            "added_omitted": added_omitted,
            "removed_case_ids": removed_ids,
            "removed_omitted": removed_omitted,
            "changed_case_ids": changed_ids,
            "changed_omitted": changed_omitted,
            "limit_per_category": limit,
            "byte_limit_per_category": MAX_DETAIL_BYTES_PER_CATEGORY,
        },
    }
