"""Schema-checked conversion of common tabular/document formats to dataset JSONL."""

from __future__ import annotations

import csv
import json
import os
import tempfile
from collections.abc import Generator, Iterator
from pathlib import Path
from threading import Lock
from typing import Any, Literal, NoReturn

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import ValidationError
from aibench.core.models import BenchmarkCase
from aibench.datasets.ingest import (
    DEFAULT_MAX_WARNINGS,
    MAX_LINE_BYTES,
    ingest_dataset,
)
from aibench.datasets.normalize import KNOWN_TOP_LEVEL_KEYS, normalize_case

DatasetFormat = Literal["auto", "jsonl", "json", "csv", "parquet"]
_STRUCTURED_CSV_FIELDS = {
    "input",
    "reference",
    "context",
    "expected_tools",
    "expectations",
    "fixtures",
    "repository",
    "metadata",
    "extensions",
    "provenance",
}
_CSV_FIELD_LIMIT_LOCK = Lock()
MAX_RECORD_COLUMNS = 256


class DatasetImportError(ValueError):
    """An import cannot be represented as a valid BenchCraft dataset."""


class _BoundedCsvLines(Iterator[str]):
    """Limit one logical CSV record before csv.reader materializes its fields."""

    def __init__(self, handle: Any) -> None:
        self._handle = handle
        self._record_bytes = 0
        self._record_columns = 1
        self._in_quotes = False
        self._at_field_start = True

    def __iter__(self) -> _BoundedCsvLines:
        return self

    def __next__(self) -> str:
        # TextIO.readline is bounded in characters; even for four-byte UTF-8 this limits
        # each temporary physical line to a small fixed allocation before byte validation.
        line = self._handle.readline(MAX_LINE_BYTES + 1)
        if line == "":
            raise StopIteration
        line_bytes = len(line.encode("utf-8"))
        self._record_bytes += line_bytes
        if self._record_bytes > MAX_LINE_BYTES:
            raise DatasetImportError(f"CSV record exceeds the {MAX_LINE_BYTES}-byte dataset limit")

        index = 0
        while index < len(line):
            char = line[index]
            if self._in_quotes:
                if char == '"':
                    if index + 1 < len(line) and line[index + 1] == '"':
                        index += 2
                        continue
                    self._in_quotes = False
            elif char == ",":
                self._record_columns += 1
                self._at_field_start = True
                if self._record_columns > MAX_RECORD_COLUMNS:
                    raise DatasetImportError(
                        f"CSV record exceeds the {MAX_RECORD_COLUMNS}-column limit"
                    )
            elif char == '"' and self._at_field_start:
                self._in_quotes = True
                self._at_field_start = False
            elif char not in "\r\n":
                self._at_field_start = False
            index += 1

        if not self._in_quotes:
            self._record_bytes = 0
            self._record_columns = 1
            self._at_field_start = True
        return line


def _reject_constant(name: str) -> NoReturn:
    raise ValueError(f"non-standard JSON constant {name} is not allowed")


def _source_format(path: Path, requested: DatasetFormat) -> str:
    if requested != "auto":
        return requested
    suffix = path.suffix.lower()
    inferred = {
        ".jsonl": "jsonl",
        ".ndjson": "jsonl",
        ".json": "json",
        ".csv": "csv",
        ".parquet": "parquet",
        ".pq": "parquet",
    }.get(suffix)
    if inferred is None:
        raise DatasetImportError(
            f"cannot infer format from {path.suffix or 'a file without an extension'}; "
            "choose --format jsonl, json, csv, or parquet"
        )
    return inferred


def _iter_json_array(handle: Any) -> Iterator[Any]:
    """Decode a top-level JSON array incrementally, retaining only one record at a time."""
    decoder = json.JSONDecoder(parse_constant=_reject_constant)
    buffer = ""
    position = 0
    eof = False

    def fill() -> None:
        nonlocal buffer, eof
        chunk = handle.read(64 * 1024)
        if chunk == "":
            eof = True
        else:
            buffer += chunk

    def whitespace() -> None:
        nonlocal position, buffer
        while True:
            while position < len(buffer) and buffer[position] in " \t\r\n":
                position += 1
            if position < len(buffer) or eof:
                return
            buffer = ""
            position = 0
            fill()

    def compact() -> None:
        nonlocal position, buffer
        if position > 64 * 1024:
            buffer = buffer[position:]
            position = 0

    def value() -> Any:
        nonlocal position
        while True:
            try:
                result, end = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                if eof:
                    raise
                if len(buffer) - position > MAX_LINE_BYTES:
                    raise DatasetImportError(
                        f"JSON record exceeds the {MAX_LINE_BYTES}-byte dataset limit"
                    )
                fill()
            except ValueError as exc:
                raise DatasetImportError(f"invalid JSON value: {exc}") from exc
            except RecursionError as exc:
                raise DatasetImportError("JSON record is nested too deeply to import") from exc
            else:
                if end - position > MAX_LINE_BYTES:
                    raise DatasetImportError(
                        f"JSON record exceeds the {MAX_LINE_BYTES}-byte dataset limit"
                    )
                position = end
                compact()
                return result

    whitespace()
    if position >= len(buffer) or buffer[position] != "[":
        raise DatasetImportError("JSON dataset must be a top-level array of case objects")
    position += 1
    whitespace()
    if position < len(buffer) and buffer[position] == "]":
        position += 1
    else:
        while True:
            whitespace()
            if position < len(buffer) and buffer[position] in ",]":
                raise DatasetImportError("JSON dataset contains a missing array item")
            yield value()
            whitespace()
            if position >= len(buffer):
                raise DatasetImportError("JSON dataset array is not terminated")
            delimiter = buffer[position]
            position += 1
            if delimiter == "]":
                break
            if delimiter != ",":
                raise DatasetImportError("JSON dataset array items must be comma-separated")
    whitespace()
    if position < len(buffer) or not eof:
        raise DatasetImportError("JSON dataset contains content after its top-level array")


def _iter_csv_records(
    path: Path, source_to_target: dict[str, str]
) -> Generator[tuple[int, Any], None, None]:
    """Stream CSV records under the importer limit and restore csv's global setting."""
    with _CSV_FIELD_LIMIT_LOCK:
        previous_limit = csv.field_size_limit()
        reader: Any = None
        try:
            # The stdlib CSV parser defaults to 128 KiB per field, below the dataset's
            # one-megabyte record limit. Its setting is process-global, so restore it on exit.
            csv.field_size_limit(MAX_LINE_BYTES)
            try:
                with path.open("r", encoding="utf-8-sig", newline="") as handle:
                    bounded_lines = _BoundedCsvLines(handle)
                    reader = csv.DictReader(bounded_lines, strict=True)
                    headers = reader.fieldnames
                    if not headers:
                        raise DatasetImportError("CSV input must have a header row")
                    if len(headers) > MAX_RECORD_COLUMNS:
                        raise DatasetImportError(
                            f"CSV header exceeds the {MAX_RECORD_COLUMNS}-column limit"
                        )
                    if any(not header.strip() for header in headers):
                        raise DatasetImportError("CSV column names must not be blank")
                    if len(set(headers)) != len(headers):
                        raise DatasetImportError("CSV column names must be unique")
                    missing_columns = sorted(set(source_to_target) - set(headers))
                    if missing_columns:
                        raise DatasetImportError(
                            "mapped CSV column(s) not found: " + ", ".join(missing_columns)
                        )
                    for record_number, row in enumerate(reader, start=1):
                        physical_line = reader.line_num
                        location = f"record {record_number} at physical line {physical_line}"
                        if None in row:
                            raise DatasetImportError(
                                f"{location}: row has more values than the CSV header"
                            )
                        if any(value is None for value in row.values()):
                            raise DatasetImportError(
                                f"{location}: row has fewer values than the CSV header"
                            )
                        converted: dict[str, Any] = {}
                        for key, value in row.items():
                            if value is None or (value == "" and key not in {"input", "case_id"}):
                                continue
                            first_nonspace = value.lstrip(" \t\r\n")[:1]
                            target_field = source_to_target.get(key, key)
                            if target_field in _STRUCTURED_CSV_FIELDS and first_nonspace in {
                                "[",
                                "{",
                            }:
                                try:
                                    converted[key] = json.loads(
                                        value, parse_constant=_reject_constant
                                    )
                                except (json.JSONDecodeError, ValueError, RecursionError) as exc:
                                    raise DatasetImportError(
                                        f"{location}: column {key!r} must contain valid JSON: {exc}"
                                    ) from exc
                            else:
                                converted[key] = value
                        yield record_number, converted
            except UnicodeDecodeError as exc:
                raise DatasetImportError("CSV input is not valid UTF-8") from exc
            except csv.Error as exc:
                physical_line = reader.line_num if reader is not None else "unknown"
                raise DatasetImportError(
                    f"malformed CSV near physical line {physical_line}: {exc}"
                ) from exc
        finally:
            csv.field_size_limit(previous_limit)


def _iter_records(
    path: Path, source_format: str, source_to_target: dict[str, str]
) -> Generator[tuple[int, Any], None, None]:
    if source_format == "jsonl":
        try:
            jsonl_handle = path.open("rb")
        except OSError as exc:
            raise DatasetImportError(f"cannot read JSONL input: {exc}") from exc
        with jsonl_handle:
            line_number = 0
            while raw_line := jsonl_handle.readline(MAX_LINE_BYTES + 1):
                line_number += 1
                if len(raw_line) > MAX_LINE_BYTES:
                    raise DatasetImportError(
                        f"line {line_number} exceeds the {MAX_LINE_BYTES}-byte dataset limit"
                    )
                try:
                    line = raw_line.decode("utf-8").strip(" \t\r\n")
                except UnicodeDecodeError as exc:
                    raise DatasetImportError(
                        f"line {line_number}: JSONL input is not valid UTF-8"
                    ) from exc
                if not line:
                    continue
                try:
                    yield line_number, json.loads(line, parse_constant=_reject_constant)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise DatasetImportError(f"line {line_number}: invalid JSON: {exc}") from exc
                except RecursionError as exc:
                    raise DatasetImportError(
                        f"line {line_number}: JSON record is nested too deeply to import"
                    ) from exc
        return

    if source_format == "json":
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                for index, record in enumerate(_iter_json_array(handle), start=1):
                    yield index, record
        except UnicodeDecodeError as exc:
            raise DatasetImportError("JSON input is not valid UTF-8") from exc
        except json.JSONDecodeError as exc:
            raise DatasetImportError(f"invalid JSON near character {exc.pos}: {exc.msg}") from exc
        return

    if source_format == "csv":
        yield from _iter_csv_records(path, source_to_target)
        return

    if source_format == "parquet":
        try:
            import pyarrow as arrow
            from pyarrow import parquet
        except ImportError as exc:
            raise DatasetImportError(
                "Parquet import requires the optional dependency; install with `pip install 'aibench[parquet]'`"
            ) from exc
        try:
            parquet_file = parquet.ParquetFile(path)
            row_number = 0
            for batch in parquet_file.iter_batches(batch_size=1):
                if batch.num_columns > MAX_RECORD_COLUMNS:
                    raise DatasetImportError(
                        f"Parquet row exceeds the {MAX_RECORD_COLUMNS}-column limit"
                    )
                if batch.nbytes > MAX_LINE_BYTES:
                    raise DatasetImportError(
                        f"Parquet row exceeds the {MAX_LINE_BYTES}-byte dataset limit"
                    )
                for record in batch.to_pylist():
                    row_number += 1
                    yield row_number, record
        except DatasetImportError:
            raise
        except (arrow.ArrowException, OSError, ValueError) as exc:
            raise DatasetImportError(f"cannot read Parquet input: {exc}") from exc
        return

    raise DatasetImportError(f"unsupported dataset format: {source_format}")


def import_dataset(
    source: Path,
    output: Path,
    *,
    source_format: DatasetFormat = "auto",
    dataset_id: str | None = None,
    split: str | None = None,
    field_mappings: tuple[str, ...] = (),
    trust_parquet: bool = False,
) -> dict[str, Any]:
    """Convert records to canonical JSONL and publish only after every case validates.

    The final target is created exclusively, so an existing file is never replaced. A
    temporary file in the target directory makes the final hard-link publication atomic.
    """
    if not source.is_file():
        raise DatasetImportError(f"dataset file not found: {source}")
    fmt = _source_format(source, source_format)
    if fmt == "parquet" and not trust_parquet:
        raise DatasetImportError(
            "Parquet pages are decompressed before row-size checks; import only trusted files "
            "and acknowledge this with --trust-parquet"
        )
    target = output.resolve()
    origin = source.resolve()
    if target == origin:
        raise DatasetImportError("output path must differ from the input path")
    if not target.parent.is_dir():
        raise DatasetImportError(f"output directory does not exist: {target.parent}")

    source_to_target: dict[str, str] = {}
    seen_targets: set[str] = set()
    for mapping in field_mappings:
        target_field, separator, source_field = mapping.partition("=")
        target_field = target_field.strip()
        source_field = source_field.strip()
        if not separator or not target_field or not source_field:
            raise DatasetImportError(
                f"invalid field mapping {mapping!r}; expected CASE_FIELD=SOURCE_FIELD"
            )
        if target_field not in KNOWN_TOP_LEVEL_KEYS:
            raise DatasetImportError(
                f"unknown target case field {target_field!r}; supported fields: "
                + ", ".join(sorted(KNOWN_TOP_LEVEL_KEYS))
            )
        if source_field in source_to_target:
            raise DatasetImportError(f"source field {source_field!r} is mapped more than once")
        if target_field in seen_targets:
            raise DatasetImportError(f"target case field {target_field!r} is mapped more than once")
        source_to_target[source_field] = target_field
        seen_targets.add(target_field)

    temporary_path: Path | None = None
    count = 0
    warnings: list[str] = []
    warnings_truncated = False
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            record_iterator = _iter_records(source, fmt, source_to_target)
            try:
                for record_number, raw in record_iterator:
                    count += 1
                    try:
                        if source_to_target and isinstance(raw, dict):
                            raw = dict(raw)
                            for source_field, target_field in source_to_target.items():
                                if source_field not in raw:
                                    continue
                                if target_field in raw and target_field != source_field:
                                    raise DatasetImportError(
                                        f"record {record_number}: both mapped source field "
                                        f"{source_field!r} and target field {target_field!r} exist"
                                    )
                                raw[target_field] = raw.pop(source_field)
                        normalized = normalize_case(raw, line=record_number, occurrence_index=count)
                        for warning in normalized.warnings:
                            if len(warnings) < DEFAULT_MAX_WARNINGS:
                                warnings.append(f"record {record_number}: {warning}")
                            else:
                                warnings_truncated = True
                        case = normalized.case
                        if case is None:
                            raise DatasetImportError(
                                f"record {record_number}: no case was produced"
                            )
                        # Exercise the public schema model at the import boundary. `normalize_case`
                        # already constructs this model, but this assertion guards future changes
                        # to the conversion path.
                        case = BenchmarkCase.model_validate(case.model_dump(mode="python"))
                        line = json.dumps(
                            case.model_dump(
                                mode="json",
                                exclude={"source_line", "duplicate_of_line"},
                                exclude_none=True,
                            ),
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        )
                    except (
                        ValidationError,
                        PydanticValidationError,
                        TypeError,
                        ValueError,
                        RecursionError,
                    ) as exc:
                        raise DatasetImportError(f"record {record_number}: {exc}") from exc
                    try:
                        encoded_line = line.encode("utf-8")
                    except UnicodeEncodeError as exc:
                        raise DatasetImportError(
                            f"record {record_number}: case contains invalid Unicode text"
                        ) from exc
                    if len(encoded_line) > MAX_LINE_BYTES:
                        raise DatasetImportError(
                            f"record {record_number}: normalized case exceeds {MAX_LINE_BYTES} bytes"
                        )
                    handle.write(line)
                    handle.write("\n")
            finally:
                record_iterator.close()

        if count == 0:
            raise DatasetImportError("input contains no dataset records")

        report = ingest_dataset(
            temporary_path,
            dataset_id=dataset_id or source.stem,
            split=split,
            retain_cases=False,
        )
        if not report.is_valid or report.manifest is None:
            details = "; ".join(str(error) for error in report.errors[:5])
            raise DatasetImportError(f"normalized output failed validation: {details}")

        try:
            os.link(temporary_path, target)
        except FileExistsError as exc:
            raise DatasetImportError(f"output already exists: {target}") from exc
        except OSError as exc:
            raise DatasetImportError(
                f"could not publish output without replacement: {exc}"
            ) from exc

        return {
            "source": str(origin),
            "output": str(target),
            "format": fmt,
            "dataset_id": report.manifest.dataset_id,
            "split": report.manifest.split,
            "case_count": report.manifest.case_count,
            "content_hash": report.manifest.content_hash,
            "duplicate_case_ids": sorted(set(report.duplicate_case_ids)),
            "warnings": warnings,
            "warnings_truncated": warnings_truncated,
        }
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
