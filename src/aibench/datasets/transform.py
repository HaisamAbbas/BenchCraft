"""Validated, bounded-memory dataset transformations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal, NoReturn

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import ValidationError
from aibench.core.models import BenchmarkCase
from aibench.datasets.ingest import DEFAULT_MAX_WARNINGS, MAX_LINE_BYTES, iter_jsonl_lines
from aibench.datasets.normalize import normalize_case

ConflictPolicy = Literal["error", "first", "last"]


class DatasetTransformError(ValueError):
    """A dataset transformation could not safely complete."""


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant {name} is not allowed")


def _canonical_case(case: BenchmarkCase, *, path: Path, line_number: int) -> str:
    try:
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
    except (TypeError, ValueError, RecursionError, UnicodeEncodeError) as exc:
        raise DatasetTransformError(f"{path}: line {line_number}: {exc}") from exc
    if len(encoded) + 1 > MAX_LINE_BYTES:
        raise DatasetTransformError(
            f"{path}: line {line_number}: normalized case exceeds {MAX_LINE_BYTES} bytes"
        )
    return canonical


def _iter_canonical_cases(
    path: Path,
) -> Iterator[tuple[int, BenchmarkCase, str, tuple[str, ...]]]:
    if not path.is_file():
        raise DatasetTransformError(f"dataset file not found: {path}")

    for occurrence, (line_number, line) in enumerate(iter_jsonl_lines(path), start=1):
        try:
            raw = json.loads(line, parse_constant=_reject_constant)
        except (json.JSONDecodeError, ValueError) as exc:
            raise DatasetTransformError(f"{path}: line {line_number}: invalid JSON: {exc}") from exc
        except RecursionError as exc:
            raise DatasetTransformError(
                f"{path}: line {line_number}: JSON record is nested too deeply"
            ) from exc
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("case_id"), str)
            or not raw.get("case_id", "").strip()
        ):
            raise DatasetTransformError(
                f"{path}: line {line_number}: dataset transformations require a non-empty explicit case_id"
            )
        try:
            normalized = normalize_case(raw, line=line_number, occurrence_index=occurrence)
            case = normalized.case
            if case is None:
                raise DatasetTransformError(f"{path}: line {line_number}: no case was produced")
            canonical = _canonical_case(case, path=path, line_number=line_number)
            warnings = tuple(normalized.warnings)
        except DatasetTransformError:
            raise
        except (
            ValidationError,
            PydanticValidationError,
            TypeError,
            KeyError,
            AttributeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise DatasetTransformError(f"{path}: line {line_number}: {exc}") from exc
        yield line_number, case, canonical, warnings


def _target_for(source: Path, output: Path) -> tuple[Path, Path]:
    origin = source.resolve()
    target = output.resolve()
    if origin == target:
        raise DatasetTransformError("output path must differ from the input path")
    if not target.parent.is_dir():
        raise DatasetTransformError(f"output directory does not exist: {target.parent}")
    if target.exists():
        raise DatasetTransformError(f"output already exists: {target}")
    return origin, target


def _publish_file(temporary: Path, target: Path) -> None:
    try:
        os.link(temporary, target)
    except FileExistsError as exc:
        raise DatasetTransformError(f"output already exists: {target}") from exc
    except OSError as exc:
        raise DatasetTransformError(f"could not publish output without replacement: {exc}") from exc


def deduplicate_dataset(
    source: Path,
    output: Path,
    *,
    on_conflict: str = "error",
) -> dict[str, Any]:
    """Remove repeated case IDs and publish normalized JSONL without replacing a file.

    Identical normalized repeats are always removed. A repeated ID with different normalized
    content fails by default; callers may explicitly retain its first or last occurrence.
    SQLite holds the index and case bodies so memory use stays bounded by one input record.
    """
    if on_conflict not in {"error", "first", "last"}:
        raise DatasetTransformError("--on-conflict must be one of: error, first, last")
    origin, target = _target_for(source, output)

    temporary_output: Path | None = None
    temporary_database: Path | None = None
    connection: sqlite3.Connection | None = None
    published = False
    temporary_cleanup_warning = False
    input_count = 0
    duplicate_count = 0
    conflict_count = 0
    warnings: list[str] = []
    warnings_truncated = False
    output_hasher = hashlib.sha256()
    try:
        database_fd, database_name = tempfile.mkstemp(
            prefix="aibench-dataset-dedup-", suffix=".sqlite3"
        )
        os.close(database_fd)
        temporary_database = Path(database_name)
        connection = sqlite3.connect(temporary_database)
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute(
            "CREATE TABLE cases ("
            "case_id TEXT PRIMARY KEY, first_canonical TEXT NOT NULL, "
            "selected_canonical TEXT NOT NULL, "
            "first_line INTEGER NOT NULL, selected_line INTEGER NOT NULL)"
        )
        for line_number, case, canonical, case_warnings in _iter_canonical_cases(origin):
            input_count += 1
            for warning in case_warnings:
                if len(warnings) < DEFAULT_MAX_WARNINGS:
                    warnings.append(f"line {line_number}: {warning}")
                else:
                    warnings_truncated = True
            row = connection.execute(
                "SELECT first_canonical, first_line FROM cases WHERE case_id = ?",
                (case.case_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO cases VALUES (?, ?, ?, ?, ?)",
                    (case.case_id, canonical, canonical, line_number, line_number),
                )
                continue

            duplicate_count += 1
            first_canonical, first_line = row
            if canonical != first_canonical:
                conflict_count += 1
                if on_conflict == "error":
                    raise DatasetTransformError(
                        f"{origin}: line {line_number}: case_id conflicts with different "
                        f"content first seen at line {first_line}; choose --on-conflict first "
                        "or last to resolve it"
                    )
            if on_conflict == "last":
                connection.execute(
                    "UPDATE cases SET selected_canonical = ?, selected_line = ? WHERE case_id = ?",
                    (canonical, line_number, case.case_id),
                )

        connection.commit()
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary_output = Path(handle.name)
            cursor = connection.execute(
                "SELECT selected_canonical FROM cases ORDER BY selected_line, case_id"
            )
            for (canonical,) in cursor:
                encoded = canonical.encode("utf-8") + b"\n"
                handle.write(encoded)
                output_hasher.update(encoded[:-1])
        assert connection is not None
        connection.close()
        connection = None
        assert temporary_database is not None
        temporary_database.unlink()
        temporary_database = None
        _publish_file(temporary_output, target)
        published = True
    except DatasetTransformError:
        raise
    except (OSError, sqlite3.Error, ValidationError) as exc:
        raise DatasetTransformError(f"could not deduplicate dataset: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        if temporary_database is not None:
            temporary_database.unlink(missing_ok=True)
        if temporary_output is not None:
            try:
                temporary_output.unlink(missing_ok=True)
            except OSError:
                if published:
                    temporary_cleanup_warning = True

    return {
        "source": str(origin),
        "output": str(target),
        "input_case_count": input_count,
        "output_case_count": input_count - duplicate_count,
        "duplicates_removed": duplicate_count,
        "conflicting_duplicate_count": conflict_count,
        "on_conflict": on_conflict,
        "warnings": warnings,
        "warnings_truncated": warnings_truncated,
        "temporary_cleanup_warning": temporary_cleanup_warning,
        "content_hash": "sha256:" + output_hasher.hexdigest(),
    }
