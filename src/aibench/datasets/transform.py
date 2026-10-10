"""Validated, bounded-memory dataset transformations."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from decimal import Decimal, InvalidOperation, localcontext
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


def _publish_directory_no_replace(source: Path, target: Path) -> None:
    """Atomically publish a directory only when its destination does not exist."""
    if os.name == "nt":
        # MoveFileW, used by os.rename on Windows, fails if any destination exists.
        os.rename(source, target)
        return

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform.startswith("linux"):
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2 is not None:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                -100,
                os.fsencode(source),
                -100,
                os.fsencode(target),
                1,  # AT_FDCWD, RENAME_NOREPLACE
            )
            if result == 0:
                return
            error_number = ctypes.get_errno()
            if error_number not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                raise OSError(error_number, os.strerror(error_number), str(target))
    elif sys.platform == "darwin":
        renamex_np = getattr(libc, "renamex_np", None)
        if renamex_np is not None:
            renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            renamex_np.restype = ctypes.c_int
            if renamex_np(os.fsencode(source), os.fsencode(target), 0x00000004) == 0:
                return
            error_number = ctypes.get_errno()
            raise OSError(error_number, os.strerror(error_number), str(target))

    raise OSError(
        errno.ENOTSUP,
        "atomic no-replace directory publication is not supported on this platform",
        str(target),
    )


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


def _parse_ratio(name: str, value: str) -> Decimal:
    if len(value) > 32:
        raise DatasetTransformError(f"--{name} ratio is too long")
    try:
        ratio = Decimal(value)
    except InvalidOperation as exc:
        raise DatasetTransformError(f"--{name} must be a number between 0 and 1") from exc
    if not ratio.is_finite() or not Decimal(0) <= ratio <= Decimal(1):
        raise DatasetTransformError(f"--{name} must be a number between 0 and 1")
    exponent = ratio.as_tuple().exponent
    if not isinstance(exponent, int) or abs(exponent) > 12:
        raise DatasetTransformError(f"--{name} supports at most 12 decimal places")
    return ratio


def split_dataset(
    source: Path,
    output_dir: Path,
    *,
    train: str = "0.8",
    validation: str = "0.1",
    test: str = "0.1",
    seed: str = "0",
) -> dict[str, Any]:
    """Split a JSONL dataset deterministically while keeping group IDs together.

    Assignment order is derived from a stable hash of the seed and group identity, not Python's
    process-randomized hash. The SQLite index and staging files keep case bodies off the Python
    heap. The new output directory is created exclusively and never replaces an existing path.
    """
    ratios = {
        "train": _parse_ratio("train", train),
        "validation": _parse_ratio("validation", validation),
        "test": _parse_ratio("test", test),
    }
    if sum(ratios.values(), Decimal(0)) != Decimal(1):
        raise DatasetTransformError("--train, --validation, and --test ratios must sum to 1")
    active_splits = [name for name, ratio in ratios.items() if ratio > 0]
    if len(active_splits) < 2:
        raise DatasetTransformError("dataset split requires at least two non-zero ratios")
    if len(seed) > 40:
        raise DatasetTransformError("--seed is too long")
    try:
        seed_value = int(seed)
    except ValueError as exc:
        raise DatasetTransformError("--seed must be an integer") from exc

    try:
        origin = source.resolve()
        output_absolute = output_dir.absolute()
        target = output_absolute.parent.resolve() / output_absolute.name
    except (OSError, RuntimeError) as exc:
        raise DatasetTransformError(f"could not resolve dataset split path: {exc}") from exc
    if origin == target:
        raise DatasetTransformError("output directory must differ from the input file")
    if not target.parent.is_dir():
        raise DatasetTransformError(f"output parent directory does not exist: {target.parent}")
    if target.exists() or target.is_symlink():
        raise DatasetTransformError(f"output directory already exists: {target}")

    split_names = ("train", "validation", "test")
    split_counts = dict.fromkeys(split_names, 0)
    group_counts = dict.fromkeys(split_names, 0)
    split_hashers = {name: hashlib.sha256() for name in split_names}
    warnings: list[str] = []
    warnings_truncated = False
    input_hasher = hashlib.sha256()
    input_count = 0
    temporary_database: Path | None = None
    staging: tempfile.TemporaryDirectory[str] | None = None
    connection: sqlite3.Connection | None = None
    temporary_directory_cleanup_warning = False
    published = False
    temporary_directory: str | None = None

    try:
        database_fd, database_name = tempfile.mkstemp(
            prefix="aibench-dataset-split-", suffix=".sqlite3", dir=target.parent
        )
        os.close(database_fd)
        temporary_database = Path(database_name)
        connection = sqlite3.connect(temporary_database)
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        connection.execute(
            "CREATE TABLE cases ("
            "case_id TEXT PRIMARY KEY, split_group TEXT NOT NULL, "
            "canonical TEXT NOT NULL, source_line INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE groups ("
            "split_group TEXT PRIMARY KEY, case_count INTEGER NOT NULL, "
            "rank BLOB NOT NULL, split_name TEXT)"
        )

        seed_prefix = f"aibench-dataset-split/1\0{seed_value}\0".encode("ascii")
        for line_number, case, canonical, case_warnings in _iter_canonical_cases(origin):
            input_count += 1
            input_hasher.update(canonical.encode("utf-8"))
            for warning in case_warnings:
                if len(warnings) < DEFAULT_MAX_WARNINGS:
                    warnings.append(f"line {line_number}: {warning}")
                else:
                    warnings_truncated = True

            split_group = (
                "group:" + case.group_id
                if isinstance(case.group_id, str) and case.group_id.strip()
                else "case:" + case.case_id
            )
            try:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO cases VALUES (?, ?, ?, ?)",
                    (case.case_id, split_group, canonical, line_number),
                )
            except sqlite3.Error as exc:
                raise DatasetTransformError(f"could not index dataset {origin}: {exc}") from exc
            if cursor.rowcount == 0:
                raise DatasetTransformError(
                    f"{origin}: line {line_number}: duplicate case ID; split requires unique IDs"
                )

            rank = hashlib.sha256(seed_prefix + split_group.encode("utf-8")).digest()
            connection.execute(
                "INSERT INTO groups (split_group, case_count, rank) VALUES (?, 1, ?) "
                "ON CONFLICT(split_group) DO UPDATE SET case_count = case_count + 1",
                (split_group, rank),
            )

        if input_count == 0:
            raise DatasetTransformError("input contains no dataset records")
        connection.commit()
        total_count = input_count
        assigned = dict.fromkeys(split_names, 0)
        group_total = int(connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0])
        if group_total < len(active_splits):
            raise DatasetTransformError(
                f"cannot populate {len(active_splits)} non-zero splits from "
                f"{group_total} independent group(s); reduce the number of non-zero ratios "
                "or add independent groups"
            )

        with localcontext() as context:
            context.prec = 48
            targets = {name: Decimal(total_count) * ratio for name, ratio in ratios.items()}
            groups = connection.execute(
                "SELECT split_group, case_count FROM groups "
                "ORDER BY case_count DESC, rank, split_group"
            )
            for group_index, (split_group, group_size) in enumerate(groups, start=1):
                empty_splits = [name for name in active_splits if group_counts[name] == 0]
                candidates_to_score = active_splits
                groups_remaining = group_total - group_index + 1
                if groups_remaining <= len(empty_splits):
                    candidates_to_score = empty_splits
                candidates: list[tuple[Decimal, str]] = []
                for name in candidates_to_score:
                    target_count = targets[name]
                    before = Decimal(assigned[name]) - target_count
                    after = before + int(group_size)
                    delta = (after * after - before * before) / (target_count * target_count)
                    candidates.append((delta, name))
                _, selected = min(candidates)
                connection.execute(
                    "UPDATE groups SET split_name = ? WHERE split_group = ?",
                    (selected, split_group),
                )
                assigned[selected] += int(group_size)
                group_counts[selected] += 1
        connection.commit()

        staging = tempfile.TemporaryDirectory(prefix=".aibench-dataset-split-", dir=target.parent)
        stage_dir = Path(staging.name)
        split_details: dict[str, dict[str, Any]] = {}
        for name in split_names:
            staged_file = stage_dir / f"{name}.jsonl"
            with staged_file.open("wb") as handle:
                rows = connection.execute(
                    "SELECT c.canonical FROM cases AS c "
                    "JOIN groups AS g ON g.split_group = c.split_group "
                    "WHERE g.split_name = ? ORDER BY c.source_line",
                    (name,),
                )
                for (canonical,) in rows:
                    encoded = canonical.encode("utf-8") + b"\n"
                    handle.write(encoded)
                    split_hashers[name].update(encoded[:-1])
                    split_counts[name] += 1
            split_details[name] = {
                "path": str(target / f"{name}.jsonl"),
                "case_count": split_counts[name],
                "group_count": group_counts[name],
                "requested_ratio": format(ratios[name].normalize(), "f"),
                "content_hash": "sha256:" + split_hashers[name].hexdigest(),
            }

        group_total = sum(group_counts.values())
        manifest = {
            "schema": "aibench.dataset-split/1",
            "seed": seed_value,
            "grouping": "group_id, falling back to case_id when unset",
            "normalized_input_hash": "sha256:" + input_hasher.hexdigest(),
            "input_case_count": input_count,
            "group_count": group_total,
            "requested_ratios": {
                name: format(ratio.normalize(), "f") for name, ratio in ratios.items()
            },
            "splits": split_details,
            "warnings": warnings,
            "warnings_truncated": warnings_truncated,
        }
        (stage_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )

        connection.close()
        connection = None
        assert temporary_database is not None
        temporary_database.unlink()
        temporary_database = None

        try:
            # Staging lives beside the target, so the directory rename publishes all
            # completed files atomically and never exposes a partial split to readers.
            _publish_directory_no_replace(stage_dir, target)
        except OSError as exc:
            if isinstance(exc, FileExistsError):
                raise DatasetTransformError(f"output directory already exists: {target}") from exc
            raise DatasetTransformError(
                f"could not atomically publish split outputs without replacement: {exc}"
            ) from exc
        published = True
    except DatasetTransformError:
        raise
    except (OSError, sqlite3.Error, ValidationError) as exc:
        raise DatasetTransformError(f"could not split dataset: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
        if temporary_database is not None:
            temporary_database.unlink(missing_ok=True)
        if staging is not None:
            active_error = sys.exception()
            try:
                staging.cleanup()
            except OSError as cleanup_error:
                if published:
                    temporary_directory_cleanup_warning = True
                    temporary_directory = staging.name
                else:
                    cleanup_message = (
                        f"could not remove temporary split directory {staging.name}: "
                        f"{cleanup_error}"
                    )
                    if active_error is not None:
                        raise DatasetTransformError(
                            f"{active_error}; {cleanup_message}"
                        ) from active_error
                    raise DatasetTransformError(cleanup_message)

    return {
        "output": str(target),
        "seed": seed_value,
        "input_case_count": input_count,
        "group_count": sum(group_counts.values()),
        "normalized_input_hash": "sha256:" + input_hasher.hexdigest(),
        "requested_ratios": {
            name: format(ratio.normalize(), "f") for name, ratio in ratios.items()
        },
        "splits": split_details,
        "warnings": warnings,
        "warnings_truncated": warnings_truncated,
        "temporary_directory_cleanup_warning": temporary_directory_cleanup_warning,
        "temporary_directory": temporary_directory,
    }
