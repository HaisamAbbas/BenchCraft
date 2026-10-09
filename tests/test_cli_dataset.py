"""01-T4/01-G3: `aibench dataset validate PATH` CLI, malformed input fails before any
app/provider call (there is none to call here — that's the point)."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app

runner = CliRunner()
FIXTURES = Path(__file__).resolve().parents[1] / "examples" / "datasets"


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


def test_missing_file_exits_two() -> None:
    result = runner.invoke(app, ["dataset", "validate", str(FIXTURES / "nope.jsonl")])
    assert result.exit_code == 2


def test_missing_file_json_uses_shared_error_document(tmp_path: Path) -> None:
    result = runner.invoke(app, ["dataset", "validate", str(tmp_path / "missing.jsonl"), "--json"])
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["message"] == f"invalid dataset: dataset file not found: {tmp_path / 'missing.jsonl'}"
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
