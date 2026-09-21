"""01-T2/01-G1/01-G3: streaming ingestion over the four brief examples, duplicate
detection, line-precise errors, and bounded-memory streaming."""

from __future__ import annotations

import tracemalloc
from pathlib import Path

from aibench.datasets.ingest import ingest_dataset

FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "datasets"


def test_all_four_brief_examples_normalize_or_report_prerequisites() -> None:
    for name in ("chatbot.valid.jsonl", "rag.valid.jsonl", "tool.valid.jsonl"):
        report = ingest_dataset(FIXTURES / name)
        assert report.is_valid, f"{name}: {report.errors}"
        assert report.manifest.case_count > 0

    coding_report = ingest_dataset(FIXTURES / "coding.unsupported.jsonl")
    assert coding_report.is_valid  # normalizes...
    assert any(  # ...but precisely reports the missing execution prerequisites
        "missing execution prerequisites" in w for w in coding_report.warnings
    )


def test_malformed_dataset_reports_line_precise_errors() -> None:
    report = ingest_dataset(FIXTURES / "invalid.malformed.jsonl")
    assert not report.is_valid
    error_lines = {e.line for e in report.errors}
    assert 2 in error_lines  # malformed JSON
    assert 4 in error_lines  # unknown top-level field
    assert 5 in error_lines  # context must be a list


def test_malformed_lines_do_not_abort_the_whole_file() -> None:
    report = ingest_dataset(FIXTURES / "invalid.malformed.jsonl")
    valid_ids = {c.case_id for c in report.cases}
    assert "bad-001" in valid_ids
    assert "bad-003" in valid_ids


def test_duplicate_ids_are_reported_not_silently_deleted() -> None:
    report = ingest_dataset(FIXTURES / "invalid.duplicates.jsonl")
    assert report.is_valid
    assert "dup-001" in report.duplicate_case_ids
    assert len(report.cases) == 3  # both dup-001 rows are kept, not dropped
    generated = [c for c in report.cases if c.case_id.startswith("generated-")]
    assert len(generated) == 1


def test_content_hash_is_stable_across_runs() -> None:
    r1 = ingest_dataset(FIXTURES / "chatbot.valid.jsonl")
    r2 = ingest_dataset(FIXTURES / "chatbot.valid.jsonl")
    assert r1.manifest.content_hash == r2.manifest.content_hash


def test_missing_file_raises_before_any_processing() -> None:
    from aibench.core.errors import ValidationError

    try:
        ingest_dataset(FIXTURES / "does-not-exist.jsonl")
        assert False, "expected ValidationError"
    except ValidationError:
        pass


def _write_generated_dataset(path: Path, case_count: int) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i in range(case_count):
            # Content deliberately scales with i so a naive implementation that retains
            # every parsed case would show peak memory growing with total content size.
            payload = "x" * 200
            f.write(f'{{"case_id":"c-{i}","input":"{payload}-{i}"}}\n')


def test_retain_cases_true_loads_the_full_dataset(tmp_path) -> None:
    """`retain_cases=True` (the default) is an explicit full-load mode for small fixtures
    and API callers that need the parsed dataset — not a bounded-memory claim."""
    dataset_path = tmp_path / "small.jsonl"
    _write_generated_dataset(dataset_path, 200)
    report = ingest_dataset(dataset_path)
    assert report.cases_retained is True
    assert len(report.cases) == 200
    assert report.manifest.case_count == 200


def test_retain_cases_false_does_not_keep_parsed_cases(tmp_path) -> None:
    dataset_path = tmp_path / "small.jsonl"
    _write_generated_dataset(dataset_path, 200)
    report = ingest_dataset(dataset_path, retain_cases=False)
    assert report.cases_retained is False
    assert report.cases == []
    assert report.manifest.case_count == 200  # still counted correctly


def test_bounded_memory_with_retain_cases_false(tmp_path) -> None:
    """This is the mode `aibench dataset validate` uses (§6, §15). Compare peak memory for
    the *same* dataset under `retain_cases=True` (keeps every parsed `BenchmarkCase`) vs
    `retain_cases=False` (keeps only bounded summary/dedup state): the duplicate-ID map is
    identical in both modes, so any large gap is attributable to not retaining full case
    bodies — the defect the code review identified."""
    dataset_path = tmp_path / "generated.jsonl"
    _write_generated_dataset(dataset_path, 3000)

    tracemalloc.start()
    retained_report = ingest_dataset(dataset_path, retain_cases=True)
    _current, retained_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    unretained_report = ingest_dataset(dataset_path, retain_cases=False)
    _current, unretained_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert retained_report.is_valid and unretained_report.is_valid
    assert len(retained_report.cases) == 3000
    assert unretained_report.cases == []
    assert unretained_report.manifest.case_count == retained_report.manifest.case_count

    # Not retaining full case bodies should use meaningfully less peak memory for the same
    # dataset; this is a regression guard against silently reintroducing full retention.
    assert unretained_peak < retained_peak * 0.6


def test_disk_backed_dedup_bounds_memory_for_duplicate_id_tracking(tmp_path) -> None:
    """`retain_cases=False` alone still kept every distinct case ID in an in-memory dict for
    duplicate detection, so memory still grew with the number of distinct IDs even with no
    case bodies retained. `force_disk_backed_dedup=True` moves that tracking to a temporary
    SQLite file instead; this isolates and measures that specific fix by using tiny per-line
    content (so case-body retention differences can't explain the result) with many distinct
    IDs (so only duplicate-ID-tracking memory differs between the two modes)."""
    dataset_path = tmp_path / "many_ids.jsonl"
    case_count = 20000
    with dataset_path.open("w", encoding="utf-8") as f:
        for i in range(case_count):
            f.write(f'{{"case_id":"id-{i:08d}","input":"x"}}\n')

    tracemalloc.start()
    in_memory_report = ingest_dataset(
        dataset_path, retain_cases=False, force_disk_backed_dedup=False
    )
    _current, in_memory_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    tracemalloc.start()
    disk_report = ingest_dataset(dataset_path, retain_cases=False, force_disk_backed_dedup=True)
    _current, disk_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert in_memory_report.manifest.case_count == case_count
    assert disk_report.manifest.case_count == case_count
    assert in_memory_report.dedup_disk_backed is False
    assert disk_report.dedup_disk_backed is True

    # Disk-backed duplicate tracking should use meaningfully less peak memory than the
    # in-memory dict for the same number of distinct IDs.
    assert disk_peak < in_memory_peak * 0.6


def test_disk_backed_dedup_still_detects_duplicates_correctly(tmp_path) -> None:
    dataset_path = tmp_path / "dupes.jsonl"
    dataset_path.write_text(
        '{"case_id":"dup-001","input":"first"}\n'
        '{"case_id":"dup-001","input":"second"}\n'
        '{"case_id":"unique-1","input":"third"}\n',
        encoding="utf-8",
    )
    report = ingest_dataset(
        dataset_path, retain_cases=True, force_disk_backed_dedup=True
    )
    assert report.dedup_disk_backed is True
    assert report.duplicate_case_ids == ["dup-001"]
    second_dup = [c for c in report.cases if c.case_id == "dup-001"][1]
    assert second_dup.duplicate_of_line == 1


def test_large_file_auto_switches_to_disk_backed_dedup(tmp_path) -> None:
    dataset_path = tmp_path / "big.jsonl"
    dataset_path.write_text('{"case_id":"c1","input":"' + "x" * 2000 + '"}\n', encoding="utf-8")
    # A tiny threshold forces the file to be treated as "large" without writing 50MB in tests.
    report = ingest_dataset(dataset_path, large_file_threshold_bytes=100)
    assert report.dedup_disk_backed is True


def test_small_file_stays_in_memory_by_default(tmp_path) -> None:
    dataset_path = tmp_path / "small.jsonl"
    dataset_path.write_text('{"case_id":"c1","input":"hi"}\n', encoding="utf-8")
    report = ingest_dataset(dataset_path)
    assert report.dedup_disk_backed is False
