"""01-T4/01-G3: `aibench dataset validate PATH` CLI, malformed input fails before any
app/provider call (there is none to call here — that's the point)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.datasets.transform import _publish_directory_no_replace

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "datasets"


def _write_cases(path: Path, cases: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(case, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
            for case in cases
        ),
        encoding="utf-8",
    )


def test_valid_dataset_exits_zero() -> None:
    result = runner.invoke(app, ["dataset", "validate", str(FIXTURES / "rag.valid.jsonl")])
    assert result.exit_code == 0
    assert "cases: 2" in result.stdout


def test_invalid_dataset_exits_two() -> None:
    result = runner.invoke(app, ["dataset", "validate", str(FIXTURES / "invalid.malformed.jsonl")])
    assert result.exit_code == 2


def test_json_output_is_well_formed() -> None:
    result = runner.invoke(
        app, ["dataset", "validate", str(FIXTURES / "rag.valid.jsonl"), "--json"]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["valid"] is True
    assert payload["case_count"] == 2


def test_dataset_diff_counts_changes_by_case_id_and_bounds_details(tmp_path: Path) -> None:
    left = tmp_path / "old.jsonl"
    right = tmp_path / "new.jsonl"
    _write_cases(
        left,
        [
            {"case_id": "stable", "input": "same"},
            {"case_id": "changed", "input": "old"},
            {"case_id": "removed", "input": "gone"},
        ],
    )
    _write_cases(
        right,
        [
            {"case_id": "stable", "input": "same"},
            {"case_id": "changed", "input": "new"},
            {"case_id": "added-a", "input": "first"},
            {"case_id": "added-b", "input": "second"},
        ],
    )

    result = runner.invoke(
        app, ["dataset", "diff", str(left), str(right), "--limit", "1", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"] == {
        "added": 2,
        "removed": 1,
        "changed": 1,
        "unchanged": 1,
        "has_changes": True,
    }
    assert payload["details"]["added_case_ids"] == ["added-a"]
    assert payload["details"]["added_omitted"] == 1
    assert payload["details"]["removed_case_ids"] == ["removed"]
    assert payload["details"]["changed_case_ids"] == ["changed"]
    assert payload["left"]["content_hash"].startswith("sha256:")


def test_dataset_diff_ignores_input_order_and_json_formatting(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    left.write_text(
        '{"case_id":"a", "input":{"a":1,"b":2}}\n{"case_id":"b","input":"two"}\n',
        encoding="utf-8",
    )
    right.write_text(
        '{"input":"two","case_id":"b"}\n{"input":{"b":2,"a":1},"case_id":"a"}\n',
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["dataset", "diff", str(left), str(right), "--fail-on-change", "--json"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["has_changes"] is False
    assert payload["summary"]["unchanged"] == 2
    assert payload["left"]["content_hash"] != payload["right"]["content_hash"]
    assert payload["_cli"]["exit_code"] == 0


def test_dataset_diff_bounds_limit_with_json_error_document(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_cases(left, [{"case_id": "a", "input": "one"}])
    _write_cases(right, [{"case_id": "a", "input": "two"}])

    result = runner.invoke(
        app, ["dataset", "diff", str(left), str(right), "--limit", "1001", "--json"]
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "between 0 and 1000" in payload["message"]


def test_dataset_diff_rejects_non_integer_limit_as_json_error(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_cases(left, [{"case_id": "a", "input": "one"}])
    _write_cases(right, [{"case_id": "a", "input": "two"}])

    result = runner.invoke(
        app, ["dataset", "diff", str(left), str(right), "--limit", "nope", "--json"]
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["message"] == "--limit must be an integer"


def test_dataset_diff_byte_bounds_large_case_id_details(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_cases(left, [])
    _write_cases(right, [{"case_id": "z" * 20_000, "input": "prompt"}])

    result = runner.invoke(app, ["dataset", "diff", str(left), str(right), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["added"] == 1
    assert payload["details"]["added_case_ids"] == []
    assert payload["details"]["added_omitted"] == 1
    assert len(result.stdout) < 2_000


def test_dataset_diff_escapes_terminal_control_sequences_in_case_ids(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_cases(left, [])
    _write_cases(right, [{"case_id": "\x1b]0;owned\x07[bold]", "input": "prompt"}])

    result = runner.invoke(app, ["dataset", "diff", str(left), str(right)])

    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.stdout
    assert "\\u001b" in result.stdout
    assert "[bold]" in result.stdout


def test_dataset_diff_fail_on_change_has_stable_json_exit_code(tmp_path: Path) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_cases(left, [{"case_id": "a", "input": "before"}])
    _write_cases(right, [{"case_id": "a", "input": "after"}])

    result = runner.invoke(
        app, ["dataset", "diff", str(left), str(right), "--fail-on-change", "--json"]
    )

    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["summary"]["changed"] == 1
    assert payload["_cli"]["exit_code"] == result.exit_code


def test_dataset_diff_rejects_duplicate_ids_and_reports_json_error(tmp_path: Path) -> None:
    left = tmp_path / "duplicate.jsonl"
    right = tmp_path / "right.jsonl"
    _write_cases(
        left,
        [
            {"case_id": "duplicate", "input": "first"},
            {"case_id": "duplicate", "input": "second"},
        ],
    )
    _write_cases(right, [{"case_id": "other", "input": "value"}])

    result = runner.invoke(app, ["dataset", "diff", str(left), str(right), "--json"])

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert "duplicate case_id at line 2" in payload["message"]
    assert payload["exit_code"] == result.exit_code


@pytest.mark.parametrize("case", [{"input": "missing"}, {"case_id": "  ", "input": "blank"}])
def test_dataset_diff_requires_non_empty_explicit_case_ids(
    tmp_path: Path, case: dict[str, object]
) -> None:
    left = tmp_path / "left.jsonl"
    right = tmp_path / "right.jsonl"
    _write_cases(left, [case])
    _write_cases(right, [{"case_id": "other", "input": "value"}])

    result = runner.invoke(app, ["dataset", "diff", str(left), str(right), "--json"])

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert "requires a non-empty explicit case_id" in payload["message"]


def test_dataset_deduplicate_removes_identical_records_and_normalizes_output(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "deduplicated.jsonl"
    _write_cases(
        source,
        [
            {"case_id": "a", "input": {"x": 1, "y": 2}},
            {"case_id": "b", "input": "other"},
            {"input": {"y": 2, "x": 1}, "case_id": "a"},
        ],
    )

    result = runner.invoke(app, ["dataset", "deduplicate", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["input_case_count"] == 3
    assert payload["output_case_count"] == 2
    assert payload["duplicates_removed"] == 1
    assert payload["conflicting_duplicate_count"] == 0
    cases = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [case["case_id"] for case in cases] == ["a", "b"]
    assert cases[0]["input"] == {"x": 1, "y": 2}


def test_dataset_deduplicate_rejects_conflicts_without_publishing(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "deduplicated.jsonl"
    _write_cases(
        source,
        [
            {"case_id": "same", "input": "first"},
            {"case_id": "same", "input": "second"},
        ],
    )

    result = runner.invoke(app, ["dataset", "deduplicate", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert "choose --on-conflict first or last" in result.stdout
    assert not output.exists()


@pytest.mark.parametrize(
    ("policy", "expected"),
    [("first", "before"), ("last", "after")],
)
def test_dataset_deduplicate_explicitly_resolves_conflicting_ids(
    tmp_path: Path, policy: str, expected: str
) -> None:
    source = tmp_path / f"{policy}.jsonl"
    output = tmp_path / f"{policy}-deduplicated.jsonl"
    _write_cases(
        source,
        [
            {"case_id": "same", "input": "before"},
            {"case_id": "between", "input": "stable"},
            {"case_id": "same", "input": "after"},
        ],
    )

    result = runner.invoke(
        app,
        [
            "dataset",
            "deduplicate",
            str(source),
            str(output),
            "--on-conflict",
            policy,
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["conflicting_duplicate_count"] == 1
    cases = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert [case["case_id"] for case in cases] == (
        ["same", "between"] if policy == "first" else ["between", "same"]
    )
    selected = next(case for case in cases if case["case_id"] == "same")
    assert selected["input"] == expected


def test_dataset_deduplicate_never_replaces_output_and_invalid_policy_is_json(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "existing.jsonl"
    _write_cases(source, [{"case_id": "a", "input": "value"}])
    output.write_text("preserve\n", encoding="utf-8")

    existing = runner.invoke(app, ["dataset", "deduplicate", str(source), str(output), "--json"])
    invalid = runner.invoke(
        app,
        [
            "dataset",
            "deduplicate",
            str(source),
            str(tmp_path / "other.jsonl"),
            "--on-conflict",
            "skip",
            "--json",
        ],
    )

    assert existing.exit_code == 2, existing.output
    assert output.read_text(encoding="utf-8") == "preserve\n"
    assert invalid.exit_code == 2, invalid.output
    assert json.loads(invalid.stdout)["status"] == "error"


def test_dataset_deduplicate_requires_explicit_case_ids(tmp_path: Path) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "deduplicated.jsonl"
    _write_cases(source, [{"input": "generated ids are order-dependent"}])

    result = runner.invoke(app, ["dataset", "deduplicate", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "non-empty explicit case_id" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_dataset_deduplicate_reports_normalization_warnings(tmp_path: Path) -> None:
    source = tmp_path / "legacy.jsonl"
    output = tmp_path / "normalized.jsonl"
    _write_cases(
        source,
        [
            {
                "case_id": "legacy",
                "input": "prompt",
                "context": ["judge-only context"],
                "expected_tools": ["search"],
            }
        ],
    )

    result = runner.invoke(app, ["dataset", "deduplicate", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert any("context" in warning for warning in payload["warnings"])
    assert any("expected_tools" in warning for warning in payload["warnings"])
    canonical = json.loads(output.read_text(encoding="utf-8"))
    assert canonical["reference"]["context"] == ["judge-only context"]


def test_dataset_deduplicate_reports_temp_cleanup_failure_after_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.jsonl"
    output = tmp_path / "deduplicated.jsonl"
    _write_cases(source, [{"case_id": "a", "input": "value"}])
    original_unlink = Path.unlink

    def fail_output_temp_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith(".deduplicated.jsonl.") and path.suffix == ".tmp":
            raise PermissionError("temporary link is held by another process")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_output_temp_unlink)
    result = runner.invoke(app, ["dataset", "deduplicate", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["temporary_cleanup_warning"] is True
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["case_id"] == "a"
    monkeypatch.setattr(Path, "unlink", original_unlink)
    for temporary in tmp_path.glob(".deduplicated.jsonl.*.tmp"):
        temporary.unlink(missing_ok=True)


def test_dataset_split_is_deterministic_and_keeps_group_ids_together(tmp_path: Path) -> None:
    source = tmp_path / "cases.jsonl"
    first_dir = tmp_path / "first-split"
    second_dir = tmp_path / "second-split"
    _write_cases(
        source,
        [
            {
                "case_id": f"{group}-{case}",
                "group_id": str(group),
                "input": f"prompt {group}-{case}",
            }
            for group in range(8)
            for case in range(2)
        ],
    )
    arguments = [
        "dataset",
        "split",
        str(source),
        "{output}",
        "--train",
        "0.5",
        "--validation",
        "0.25",
        "--test",
        "0.25",
        "--seed",
        "73",
        "--json",
    ]

    first = runner.invoke(app, [arg.format(output=first_dir) for arg in arguments])
    second = runner.invoke(app, [arg.format(output=second_dir) for arg in arguments])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["input_case_count"] == 16
    assert first_payload["group_count"] == 8
    assert {name: detail["case_count"] for name, detail in first_payload["splits"].items()} == {
        "train": 8,
        "validation": 4,
        "test": 4,
    }
    assert (first_dir / "manifest.json").is_file()
    assert json.loads((first_dir / "manifest.json").read_text(encoding="utf-8"))["seed"] == 73
    membership: dict[str, set[str]] = {}
    for name in ("train", "validation", "test"):
        first_file = first_dir / f"{name}.jsonl"
        second_file = second_dir / f"{name}.jsonl"
        assert first_file.read_bytes() == second_file.read_bytes()
        for line in first_file.read_text(encoding="utf-8").splitlines():
            case = json.loads(line)
            membership.setdefault(case["group_id"], set()).add(name)
    assert all(len(splits) == 1 for splits in membership.values())
    assert second_payload["seed"] == first_payload["seed"]


def test_dataset_split_creates_empty_zero_ratio_output_and_falls_back_to_case_ids(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cases.jsonl"
    output_dir = tmp_path / "splits"
    _write_cases(
        source,
        [
            {"case_id": "a", "input": "one"},
            {"case_id": "b", "input": "two"},
        ],
    )

    result = runner.invoke(
        app,
        [
            "dataset",
            "split",
            str(source),
            str(output_dir),
            "--train",
            "0.5",
            "--validation",
            "0.5",
            "--test",
            "0",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["group_count"] == 2
    assert (output_dir / "test.jsonl").read_bytes() == b""
    assert payload["splits"]["test"]["case_count"] == 0


def test_dataset_split_populates_every_nonzero_split_when_enough_groups_exist(
    tmp_path: Path,
) -> None:
    source = tmp_path / "cases.jsonl"
    output_dir = tmp_path / "splits"
    _write_cases(
        source,
        [{"case_id": f"case-{index}", "input": f"prompt {index}"} for index in range(3)],
    )

    result = runner.invoke(app, ["dataset", "split", str(source), str(output_dir), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert all(
        payload["splits"][name]["case_count"] > 0 for name in ("train", "validation", "test")
    )


def test_dataset_split_rejects_fewer_groups_than_nonzero_splits(tmp_path: Path) -> None:
    source = tmp_path / "cases.jsonl"
    output_dir = tmp_path / "splits"
    _write_cases(source, [{"case_id": "a", "group_id": "one", "input": "prompt"}])

    result = runner.invoke(app, ["dataset", "split", str(source), str(output_dir), "--json"])

    assert result.exit_code == 2, result.output
    assert (
        "cannot populate 3 non-zero splits from 1 independent group"
        in json.loads(result.stdout)["message"]
    )
    assert not output_dir.exists()


def test_dataset_split_symlink_loop_uses_json_error_envelope(tmp_path: Path) -> None:
    source = tmp_path / "loop.jsonl"
    try:
        source.symlink_to(source)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    result = runner.invoke(
        app, ["dataset", "split", str(source), str(tmp_path / "splits"), "--json"]
    )

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["exit_code"] == result.exit_code


def test_dataset_split_rejects_bad_ratios_and_duplicate_ids_as_json(tmp_path: Path) -> None:
    source = tmp_path / "cases.jsonl"
    output_dir = tmp_path / "splits"
    _write_cases(
        source,
        [
            {"case_id": "same", "input": "one"},
            {"case_id": "same", "input": "two"},
        ],
    )

    bad_ratio = runner.invoke(
        app,
        [
            "dataset",
            "split",
            str(source),
            str(output_dir),
            "--train",
            "0.5",
            "--validation",
            "0.5",
            "--test",
            "0.5",
            "--json",
        ],
    )
    duplicate = runner.invoke(app, ["dataset", "split", str(source), str(output_dir), "--json"])

    assert bad_ratio.exit_code == 2, bad_ratio.output
    assert "must sum to 1" in json.loads(bad_ratio.stdout)["message"]
    assert duplicate.exit_code == 2, duplicate.output
    assert "split requires unique IDs" in json.loads(duplicate.stdout)["message"]
    assert not output_dir.exists()


def test_dataset_split_preserves_existing_directory_and_hides_failed_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "cases.jsonl"
    output_dir = tmp_path / "splits"
    _write_cases(
        source,
        [{"case_id": f"case-{index}", "input": f"prompt {index}"} for index in range(3)],
    )
    output_dir.mkdir()
    sentinel = output_dir / "preserve.txt"
    sentinel.write_text("keep", encoding="utf-8")

    existing = runner.invoke(app, ["dataset", "split", str(source), str(output_dir), "--json"])
    assert existing.exit_code == 2, existing.output
    assert sentinel.read_text(encoding="utf-8") == "keep"

    sentinel.unlink()
    output_dir.rmdir()
    real_publish = _publish_directory_no_replace

    def fail_directory_publish(source_path: Path, target_path: Path) -> None:
        if target_path == output_dir:
            raise PermissionError("simulated publication failure")
        real_publish(source_path, target_path)

    monkeypatch.setattr(
        "aibench.datasets.transform._publish_directory_no_replace", fail_directory_publish
    )
    failed = runner.invoke(app, ["dataset", "split", str(source), str(output_dir), "--json"])

    assert failed.exit_code == 2, failed.output
    assert "could not atomically publish" in json.loads(failed.stdout)["message"]
    assert not output_dir.exists()


def test_dataset_split_reports_staging_path_when_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "cases.jsonl"
    output_dir = tmp_path / "splits"
    _write_cases(
        source,
        [{"case_id": f"case-{index}", "input": f"prompt {index}"} for index in range(3)],
    )
    temporary_directories: list[object] = []
    staging_paths: list[Path] = []
    temporary_directory_type = __import__("tempfile").TemporaryDirectory
    real_cleanup = temporary_directory_type.cleanup
    real_publish = _publish_directory_no_replace

    def fail_cleanup(instance: object) -> None:
        temporary_directories.append(instance)
        staging_paths.append(Path(instance.name))  # type: ignore[attr-defined]
        raise PermissionError("simulated cleanup failure")

    monkeypatch.setattr(
        "aibench.datasets.transform.tempfile.TemporaryDirectory.cleanup", fail_cleanup
    )

    def fail_publish(source_path: Path, target_path: Path) -> None:
        if target_path == output_dir:
            raise PermissionError("simulated publication failure")
        real_publish(source_path, target_path)

    monkeypatch.setattr("aibench.datasets.transform._publish_directory_no_replace", fail_publish)
    failed = runner.invoke(app, ["dataset", "split", str(source), str(output_dir), "--json"])
    payload = json.loads(failed.stdout)

    assert failed.exit_code == 2, failed.output
    assert "simulated publication failure" in payload["message"]
    assert "could not remove temporary split directory" in payload["message"]
    assert str(staging_paths[0]) in payload["message"]
    assert staging_paths[0].is_dir()
    assert not output_dir.exists()

    monkeypatch.undo()
    for temporary_directory in temporary_directories:
        real_cleanup(temporary_directory)


def test_dataset_split_preserves_directory_created_during_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "cases.jsonl"
    output_dir = tmp_path / "splits"
    _write_cases(
        source,
        [{"case_id": f"case-{index}", "input": f"prompt {index}"} for index in range(3)],
    )
    real_publish = _publish_directory_no_replace

    def create_racing_destination(stage_dir: Path, target_dir: Path) -> None:
        target_dir.mkdir()
        real_publish(stage_dir, target_dir)

    monkeypatch.setattr(
        "aibench.datasets.transform._publish_directory_no_replace", create_racing_destination
    )
    result = runner.invoke(app, ["dataset", "split", str(source), str(output_dir), "--json"])

    assert result.exit_code == 2, result.output
    assert "output directory already exists" in json.loads(result.stdout)["message"]
    assert output_dir.is_dir()
    assert list(output_dir.iterdir()) == []


def test_missing_file_exits_two() -> None:
    result = runner.invoke(app, ["dataset", "validate", str(FIXTURES / "nope.jsonl")])
    assert result.exit_code == 2


def test_missing_file_json_uses_shared_error_document(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dataset", "validate", str(tmp_path / "missing.jsonl"), "--json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert (
        payload["message"]
        == f"invalid dataset: dataset file not found: {tmp_path / 'missing.jsonl'}"
    )
    assert payload["exit_code"] == payload["_cli"]["exit_code"] == result.exit_code
    assert payload["schema"] == "aibench.cli-error/1"
    assert payload["_cli"]["schema"] == "aibench.cli-output/1"


def test_invalid_utf8_dataset_is_a_json_input_error(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.jsonl"
    invalid.write_bytes(b"\xff\xfe\n")
    result = runner.invoke(app, ["dataset", "validate", str(invalid), "--json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["message"] == f"invalid dataset: dataset file is not valid UTF-8: {invalid}"
    assert payload["exit_code"] == payload["_cli"]["exit_code"] == result.exit_code


def test_validate_rejects_jsonl_with_nonstandard_whitespace(tmp_path: Path) -> None:
    source = tmp_path / "unicode-whitespace.jsonl"
    source.write_text('\u00a0{"case_id":"bad","input":"prompt"}\n', encoding="utf-8")

    result = runner.invoke(app, ["dataset", "validate", str(source), "--json"])

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    assert payload["errors"][0]["line"] == 1
    assert "invalid JSON" in payload["errors"][0]["message"]


def test_validate_rejects_oversized_jsonl_without_newline(tmp_path: Path) -> None:
    source = tmp_path / "oversized.jsonl"
    source.write_text('{"case_id":"large","input":"' + ("x" * 1_010_000) + '"}', encoding="utf-8")

    result = runner.invoke(app, ["dataset", "validate", str(source), "--json"])

    assert result.exit_code == 2, result.output
    assert "line exceeds max size of 1000000 bytes" in json.loads(result.stdout)["message"]


def test_import_csv_normalizes_schema_and_reports_content_hash(tmp_path: Path) -> None:
    source = tmp_path / "cases.csv"
    source.write_text(
        "case_id,input,reference,metadata\n"
        'csv-1,What is 2+2?,"  {""answer"":""4""}"," {""topic"":""math""}"\n',
        encoding="utf-8",
    )
    output = tmp_path / "imported.jsonl"

    result = runner.invoke(
        app,
        [
            "dataset",
            "import",
            str(source),
            str(output),
            "--dataset-id",
            "math-v1",
            "--split",
            "development",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["case_count"] == 1
    assert payload["dataset_id"] == "math-v1"
    assert payload["split"] == "development"
    assert payload["content_hash"].startswith("sha256:")
    imported = json.loads(output.read_text(encoding="utf-8"))
    assert imported["input"] == "What is 2+2?"
    assert imported["reference"]["answer"] == "4"
    assert imported["metadata"] == {"topic": "math"}


def test_import_csv_parses_legacy_structured_columns(tmp_path: Path) -> None:
    source = tmp_path / "legacy.csv"
    source.write_text(
        "case_id,input,context,expected_tools\n"
        'legacy,prompt,"[""reference text""]","[""search""]"\n',
        encoding="utf-8",
    )
    output = tmp_path / "legacy.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    imported = json.loads(output.read_text(encoding="utf-8"))
    assert imported["reference"]["context"] == ["reference text"]
    assert imported["reference"]["tools"]["tool_names"] == ["search"]
    assert len(payload["warnings"]) == 2


def test_import_maps_noncanonical_csv_columns_to_case_schema(tmp_path: Path) -> None:
    source = tmp_path / "external.csv"
    source.write_text(
        "prompt,target_answer\nWhat is 2+2?,4\n",
        encoding="utf-8",
    )
    output = tmp_path / "mapped.jsonl"

    result = runner.invoke(
        app,
        [
            "dataset",
            "import",
            str(source),
            str(output),
            "--map",
            "input=prompt",
            "--map",
            "expected_output=target_answer",
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    imported = json.loads(output.read_text(encoding="utf-8"))
    assert imported["input"] == "What is 2+2?"
    assert imported["reference"]["answer"] == "4"


def test_import_rejects_mapping_collision(tmp_path: Path) -> None:
    source = tmp_path / "external.json"
    source.write_text(
        json.dumps([{"case_id": "a", "input": "canonical", "prompt": "alias"}]),
        encoding="utf-8",
    )
    output = tmp_path / "mapped.jsonl"

    result = runner.invoke(
        app,
        ["dataset", "import", str(source), str(output), "--map", "input=prompt", "--json"],
    )

    assert result.exit_code == 2, result.output
    assert "both mapped source field" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_json_array_handles_multiple_records_and_validates_output(tmp_path: Path) -> None:
    source = tmp_path / "cases.json"
    records = [{"case_id": "large", "input": "x" * (70 * 1024)}]
    records.extend({"case_id": f"case-{idx}", "input": {"prompt": "x"}} for idx in range(1_600))
    source.write_text(
        json.dumps(records),
        encoding="utf-8",
    )
    output = tmp_path / "cases.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["case_count"] == 1_601
    validation = runner.invoke(app, ["dataset", "validate", str(output), "--json"])
    assert validation.exit_code == 0, validation.output
    assert json.loads(validation.stdout)["case_count"] == 1_601


def test_import_refuses_existing_target_without_modifying_it(tmp_path: Path) -> None:
    source = tmp_path / "cases.json"
    source.write_text(json.dumps([{"case_id": "a", "input": "prompt"}]), encoding="utf-8")
    output = tmp_path / "existing.jsonl"
    output.write_text("keep this file\n", encoding="utf-8")

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert output.read_text(encoding="utf-8") == "keep this file\n"


def test_import_invalid_case_leaves_no_output(tmp_path: Path) -> None:
    source = tmp_path / "cases.json"
    source.write_text(json.dumps([{"case_id": "broken", "unexpected": 1}]), encoding="utf-8")
    output = tmp_path / "invalid.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert "record 1" in payload["message"]
    assert not output.exists()
    assert not list(tmp_path.glob(".invalid.jsonl.*.tmp"))


def test_import_rejects_json_with_nonstandard_numbers(tmp_path: Path) -> None:
    source = tmp_path / "cases.json"
    source.write_text('[{"case_id":"bad","input":NaN}]', encoding="utf-8")
    output = tmp_path / "invalid.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert not output.exists()


def test_import_rejects_non_json_whitespace_in_json_array(tmp_path: Path) -> None:
    source = tmp_path / "cases.json"
    source.write_text('[\u00a0{"case_id":"bad","input":"prompt"}]', encoding="utf-8")
    output = tmp_path / "invalid.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert not output.exists()


def test_import_rejects_jsonl_with_nonstandard_whitespace(tmp_path: Path) -> None:
    source = tmp_path / "unicode-whitespace.jsonl"
    source.write_text('\u00a0{"case_id":"bad","input":"prompt"}\n', encoding="utf-8")
    output = tmp_path / "unicode-whitespace-out.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "line 1: invalid JSON" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_csv_text_with_unicode_whitespace_prefix_is_not_parsed_as_json(tmp_path: Path) -> None:
    source = tmp_path / "unicode-prefix.csv"
    source.write_text('case_id,input\ncase-1,\u00a0{"prompt":"keep as text"}\n', encoding="utf-8")
    output = tmp_path / "unicode-prefix.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    imported = json.loads(output.read_text(encoding="utf-8"))
    assert imported["input"] == '\u00a0{"prompt":"keep as text"}'


def test_import_rejects_unpaired_unicode_surrogate_cleanly(tmp_path: Path) -> None:
    source = tmp_path / "cases.json"
    source.write_text('[{"case_id":"bad","input":"\\ud800"}]', encoding="utf-8")
    output = tmp_path / "invalid.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "invalid Unicode" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_csv_accepts_fields_above_stdlib_default_limit(tmp_path: Path) -> None:
    source = tmp_path / "large.csv"
    source.write_text("case_id,input\nlarge," + ("x" * 140_000) + "\n", encoding="utf-8")
    output = tmp_path / "large.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["case_count"] == 1


def test_import_restores_csv_field_limit(tmp_path: Path) -> None:
    source = tmp_path / "cases.csv"
    source.write_text("case_id,input\na,prompt\n", encoding="utf-8")
    output = tmp_path / "cases.jsonl"
    original_limit = csv.field_size_limit()

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    assert csv.field_size_limit() == original_limit


def test_csv_errors_report_physical_line_after_multiline_record(tmp_path: Path) -> None:
    source = tmp_path / "cases.csv"
    source.write_text(
        'case_id,input\nok,"first line\nsecond line"\nbad,too,many\n', encoding="utf-8"
    )
    output = tmp_path / "cases.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    message = json.loads(result.stdout)["message"]
    assert "record 2 at physical line 4" in message
    assert not output.exists()


def test_import_jsonl_recursion_error_is_a_handled_input_error(tmp_path: Path) -> None:
    source = tmp_path / "deep.jsonl"
    source.write_text(
        '{"case_id":"deep","input":' + ("[" * 3_000) + "0" + ("]" * 3_000) + "}\n",
        encoding="utf-8",
    )
    output = tmp_path / "deep-out.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "nested too deeply" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_rejects_oversized_jsonl_without_newline_before_reading_full_line(
    tmp_path: Path,
) -> None:
    source = tmp_path / "oversized.jsonl"
    source.write_text('{"case_id":"large","input":"' + ("x" * 1_010_000) + '"}', encoding="utf-8")
    output = tmp_path / "oversized-out.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "line 1 exceeds the 1000000-byte" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_rejects_oversized_multiline_csv_record(tmp_path: Path) -> None:
    source = tmp_path / "oversized.csv"
    source.write_text(
        'case_id,input\nlarge,"' + ("x" * 600_000) + "\n" + ("y" * 500_000) + '"\n',
        encoding="utf-8",
    )
    output = tmp_path / "oversized.csv.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "CSV record exceeds the 1000000-byte" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_rejects_csv_with_too_many_columns_before_row_conversion(tmp_path: Path) -> None:
    source = tmp_path / "wide.csv"
    columns = [f"field_{index}" for index in range(257)]
    source.write_text(",".join(columns) + "\n", encoding="utf-8")
    output = tmp_path / "wide.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "256-column limit" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_reports_compatibility_normalization_warnings(tmp_path: Path) -> None:
    source = tmp_path / "legacy.json"
    source.write_text(
        json.dumps(
            [
                {
                    "case_id": "legacy",
                    "input": "prompt",
                    "context": ["reference-only context"],
                    "expected_tools": ["search"],
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "canonical.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 0, result.output
    warnings = json.loads(result.stdout)["warnings"]
    assert len(warnings) == 2
    assert all(warning.startswith("record 1:") for warning in warnings)
    assert any("reference.context" in warning for warning in warnings)
    assert any("expected_tools" in warning for warning in warnings)


def test_import_rejects_malformed_csv_without_output(tmp_path: Path) -> None:
    source = tmp_path / "broken.csv"
    source.write_text('case_id,input\ncase-1,"unclosed input\n', encoding="utf-8")
    output = tmp_path / "broken.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    assert "malformed CSV" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_reports_missing_optional_parquet_dependency(tmp_path: Path) -> None:
    source = tmp_path / "cases.parquet"
    source.write_bytes(b"not parquet")
    output = tmp_path / "cases.jsonl"

    result = runner.invoke(
        app, ["dataset", "import", str(source), str(output), "--trust-parquet", "--json"]
    )

    # If pyarrow is installed, the invalid source still fails cleanly. Without it, the
    # actionable optional-dependency message is returned.
    assert result.exit_code == 2, result.output
    assert json.loads(result.stdout)["status"] == "error"
    assert not output.exists()


def test_import_parquet_when_optional_dependency_is_installed(tmp_path: Path) -> None:
    pyarrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    source = tmp_path / "cases.parquet"
    parquet.write_table(
        pyarrow.Table.from_pylist([{"case_id": "parquet-1", "input": {"prompt": "hello"}}]),
        source,
    )
    output = tmp_path / "cases.jsonl"

    result = runner.invoke(
        app, ["dataset", "import", str(source), str(output), "--trust-parquet", "--json"]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["format"] == "parquet"
    assert payload["case_count"] == 1
    assert json.loads(output.read_text(encoding="utf-8"))["input"] == {"prompt": "hello"}


def test_import_rejects_oversized_parquet_row_after_trusted_decode(tmp_path: Path) -> None:
    pyarrow = pytest.importorskip("pyarrow")
    parquet = pytest.importorskip("pyarrow.parquet")
    source = tmp_path / "oversized.parquet"
    parquet.write_table(
        pyarrow.Table.from_pylist([{"case_id": "large", "input": "x" * 1_010_000}]),
        source,
    )
    output = tmp_path / "oversized-parquet.jsonl"

    result = runner.invoke(
        app, ["dataset", "import", str(source), str(output), "--trust-parquet", "--json"]
    )

    assert result.exit_code == 2, result.output
    assert "Parquet row exceeds the 1000000-byte" in json.loads(result.stdout)["message"]
    assert not output.exists()


def test_import_requires_explicit_trust_for_parquet(tmp_path: Path) -> None:
    source = tmp_path / "cases.parquet"
    source.write_bytes(b"parquet input")
    output = tmp_path / "cases.jsonl"

    result = runner.invoke(app, ["dataset", "import", str(source), str(output), "--json"])

    assert result.exit_code == 2, result.output
    message = json.loads(result.stdout)["message"]
    assert "only trusted files" in message
    assert "--trust-parquet" in message
    assert not output.exists()


def test_malformed_nested_input_produces_line_errors_not_a_crash() -> None:
    """Regression test for the code-review finding that malformed nested `fixtures`,
    `context`, `reference`, `provenance`, and non-namespaced `extensions` keys could raise
    a raw pydantic.ValidationError / TypeError instead of a clean CLI exit."""
    result = runner.invoke(
        app, ["dataset", "validate", str(FIXTURES / "invalid.nested.jsonl"), "--json"]
    )
    assert result.exit_code == 2
    payload = json.loads(result.stdout)
    assert payload["valid"] is False
    error_lines = {e["line"] for e in payload["errors"]}
    assert error_lines == {1, 2, 3, 4, 5}
