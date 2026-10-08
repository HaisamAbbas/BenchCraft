"""Streaming JSONL ingestion (§6). Bounded per-line memory, precise line-number errors,
duplicate-ID detection, and a content hash over the validated dataset."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import NoReturn, Self

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import ValidationError
from aibench.core.models import BenchmarkCase, DatasetManifest
from aibench.datasets.normalize import normalize_case

MAX_LINE_BYTES = 1_000_000
DEFAULT_MAX_ERRORS = 1_000
DEFAULT_MAX_WARNINGS = 1_000

# Above this source file size, `ingest_dataset` switches its duplicate-ID index from an
# in-memory dict to a temporary on-disk SQLite table (§6: "use an on-disk index for very
# large files"), so the distinct-case-ID set no longer has to fit in process memory. Override
# with `large_file_threshold_bytes=...` or force with `force_disk_backed_dedup=...`.
LARGE_FILE_DEDUP_THRESHOLD_BYTES = 50 * 1024 * 1024  # 50 MB

# Defense in depth: `normalize_case` is expected to convert every malformed-input case into
# our own `ValidationError`, but this is the boundary that guarantees one bad line can never
# abort the whole ingestion or crash the CLI with a raw framework traceback.
_UNEXPECTED_LINE_ERRORS: tuple[type[Exception], ...] = (
    PydanticValidationError,
    TypeError,
    KeyError,
    AttributeError,
    ValueError,
)


class _DedupIndex:
    """Tracks each case ID's first-seen line number for duplicate detection.

    In-memory (a plain dict) by default — the common case for ordinary-sized datasets, and
    the cheapest option. `disk_backed=True` instead keeps the ID set in a temporary SQLite
    file: process memory for duplicate tracking then stays roughly constant regardless of how
    many distinct case IDs the dataset has, at the cost of a disk round trip per lookup. This
    is the piece of ingestion memory that scaled with dataset size even after
    `retain_cases=False` stopped retaining full case bodies; disk-backing it closes that gap
    for large files while keeping the (cheaper, still bounded-by-`max_errors`/`max_warnings`)
    default path unchanged for the common case.
    """

    def __init__(self, *, disk_backed: bool) -> None:
        self.disk_backed = disk_backed
        self._mem: dict[str, int] = {}
        self._conn: sqlite3.Connection | None = None
        self._tmp_path: str | None = None
        if disk_backed:
            fd, self._tmp_path = tempfile.mkstemp(prefix="aibench-dedup-", suffix=".sqlite3")
            os.close(fd)
            self._conn = sqlite3.connect(self._tmp_path)
            self._conn.execute(
                "CREATE TABLE seen (case_id TEXT PRIMARY KEY, line INTEGER NOT NULL)"
            )
            self._conn.execute("PRAGMA synchronous = OFF")  # local scratch file, safe to relax
            self._conn.commit()

    def record_if_new(self, case_id: str, line: int) -> int | None:
        """Return the previously recorded line if `case_id` was already seen; otherwise
        record `line` as its first occurrence and return None."""
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT line FROM seen WHERE case_id = ?", (case_id,)
            ).fetchone()
            if row is not None:
                return int(row[0])
            self._conn.execute("INSERT INTO seen (case_id, line) VALUES (?, ?)", (case_id, line))
            # No per-insert commit: this is a throwaway scratch file deleted in `close()`,
            # and SQLite's default isolation lets the same connection read its own
            # uncommitted writes, so per-row durability would only add transaction overhead
            # with no observable benefit here.
            return None

        existing = self._mem.get(case_id)
        if existing is not None:
            return existing
        self._mem[case_id] = line
        return None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._tmp_path is not None:
            try:
                os.unlink(self._tmp_path)
            except OSError:
                pass  # best-effort cleanup of a scratch temp file
            self._tmp_path = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


@dataclass
class LineError:
    line: int
    message: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"line {self.line}: {self.message}"


@dataclass
class IngestReport:
    manifest: DatasetManifest | None
    cases: list[BenchmarkCase] = field(default_factory=list)
    cases_retained: bool = True
    dedup_disk_backed: bool = False
    errors: list[LineError] = field(default_factory=list)
    errors_truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    warnings_truncated: bool = False
    duplicate_case_ids: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _reject_json_constant(name: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant {name} is not allowed")


def iter_jsonl_lines(path: Path) -> Iterator[tuple[int, str]]:
    """Yield (line_number, stripped_text) for each nonblank line, streaming one line at a
    time so memory stays bounded regardless of file size."""
    with path.open("r", encoding="utf-8") as handle:
        for lineno, raw_line in enumerate(handle, start=1):
            if len(raw_line.encode("utf-8")) > MAX_LINE_BYTES:
                raise ValidationError(
                    f"line exceeds max size of {MAX_LINE_BYTES} bytes", line=lineno
                )
            stripped = raw_line.strip()
            if not stripped:
                continue
            yield lineno, stripped


def ingest_dataset(
    path: Path,
    *,
    dataset_id: str | None = None,
    split: str | None = None,
    retain_cases: bool = True,
    max_errors: int = DEFAULT_MAX_ERRORS,
    max_warnings: int = DEFAULT_MAX_WARNINGS,
    large_file_threshold_bytes: int = LARGE_FILE_DEDUP_THRESHOLD_BYTES,
    force_disk_backed_dedup: bool | None = None,
) -> IngestReport:
    """Validate and normalize a JSONL dataset file. Never calls an application or provider;
    this is pure parsing/normalization over local bytes.

    Two independent memory knobs:

    - `retain_cases=True` (the default, suited to small fixtures and API callers that need the
      full parsed dataset) keeps every normalized `BenchmarkCase` in `IngestReport.cases`, so
      peak memory scales with dataset content size. `retain_cases=False` (what
      `aibench dataset validate` uses) keeps no case bodies at all.
    - Duplicate-ID tracking is independent of `retain_cases` — it must see every case ID
      regardless — and by default lives in an in-memory dict, so *that* still scales with the
      number of distinct case IDs. Once `path` exceeds `large_file_threshold_bytes` (default
      50 MB), or when `force_disk_backed_dedup=True` is passed explicitly, tracking moves to a
      temporary on-disk SQLite table instead, so process memory for duplicate detection stays
      roughly constant regardless of dataset size. `force_disk_backed_dedup=False` always
      forces the in-memory path even for huge files (mainly useful for tests/benchmarks).

    Combining `retain_cases=False` with disk-backed dedup on a large file keeps peak process
    memory bounded by `max_errors`/`max_warnings` plus a small constant, independent of both
    dataset content size and the number of distinct case IDs.
    """
    if not path.exists():
        raise ValidationError(f"dataset file not found: {path}")

    disk_backed = (
        force_disk_backed_dedup
        if force_disk_backed_dedup is not None
        else path.stat().st_size > large_file_threshold_bytes
    )

    cases: list[BenchmarkCase] = []
    errors: list[LineError] = []
    warnings: list[str] = []
    duplicates: list[str] = []
    errors_truncated = False
    warnings_truncated = False
    hasher = hashlib.sha256()
    occurrence = 0
    case_count = 0

    with _DedupIndex(disk_backed=disk_backed) as dedup:
        for lineno, line in iter_jsonl_lines(path):
            occurrence += 1
            hasher.update(line.encode("utf-8"))
            try:
                raw = json.loads(line, parse_constant=_reject_json_constant)
            except json.JSONDecodeError as exc:
                if len(errors) < max_errors:
                    errors.append(LineError(lineno, f"invalid JSON: {exc.msg}"))
                else:
                    errors_truncated = True
                continue
            except ValueError as exc:
                if len(errors) < max_errors:
                    errors.append(LineError(lineno, f"invalid JSON: {exc}"))
                else:
                    errors_truncated = True
                continue
            try:
                result = normalize_case(raw, line=lineno, occurrence_index=occurrence)
            except ValidationError as exc:
                if len(errors) < max_errors:
                    errors.append(LineError(lineno, str(exc)))
                else:
                    errors_truncated = True
                continue
            except _UNEXPECTED_LINE_ERRORS as exc:
                if len(errors) < max_errors:
                    errors.append(LineError(lineno, f"invalid case data: {exc}"))
                else:
                    errors_truncated = True
                continue

            case = result.case
            assert case is not None
            for w in result.warnings:
                if len(warnings) < max_warnings:
                    warnings.append(f"line {lineno}: {w}")
                else:
                    warnings_truncated = True

            first_seen_at = dedup.record_if_new(case.case_id, lineno)
            if first_seen_at is not None:
                duplicates.append(case.case_id)
                case = case.model_copy(update={"duplicate_of_line": first_seen_at})
            case_count += 1
            if retain_cases:
                cases.append(case)

    manifest = DatasetManifest(
        dataset_id=dataset_id or path.stem,
        content_hash="sha256:" + hasher.hexdigest(),
        case_count=case_count,
        source_refs=(str(path),),
        split=split,
        duplicate_case_ids=tuple(sorted(set(duplicates))),
    )

    return IngestReport(
        manifest=manifest,
        cases=cases,
        cases_retained=retain_cases,
        dedup_disk_backed=disk_backed,
        errors=errors,
        errors_truncated=errors_truncated,
        warnings=warnings,
        warnings_truncated=warnings_truncated,
        duplicate_case_ids=duplicates,
    )
