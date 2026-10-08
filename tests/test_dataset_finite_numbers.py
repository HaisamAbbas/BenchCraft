"""F05: case data cannot be silently changed by JSON storage serialization."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import ValidationError
from aibench.core.models import BenchmarkCase, Fixture
from aibench.datasets.ingest import ingest_dataset
from aibench.datasets.normalize import normalize_case
from aibench.storage.db import Database
from aibench.storage.repositories import Storage
from tests.engine_support import Harness

cli = CliRunner()


@pytest.mark.parametrize("token", ("NaN", "Infinity", "-Infinity", "1e999", "-1e999"))
@pytest.mark.parametrize("field", ("input", "metadata"))
def test_ingestion_reports_non_finite_numbers_and_keeps_other_lines(
    tmp_path: Path, token: str, field: str
) -> None:
    path = tmp_path / "numbers.jsonl"
    bad = f'{{"case_id":"bad","input":"question","{field}":{{"nested":[{token}]}}}}'
    if field == "input":
        bad = f'{{"case_id":"bad","input":{{"nested":[{token}]}}}}'
    path.write_text("\n" + bad + '\n{"case_id":"good","input":"NaN is text"}\n', encoding="utf-8")
    report = ingest_dataset(path)
    assert not report.is_valid
    assert [error.line for error in report.errors] == [2]
    assert [case.case_id for case in report.cases] == ["good"]
    assert report.cases[0].input == "NaN is text"


@pytest.mark.parametrize("number", (math.nan, math.inf, -math.inf))
@pytest.mark.parametrize("field", ("input", "metadata", "expectations", "extensions", "fixtures"))
def test_normalization_rejects_nested_non_finite_values(number: float, field: str) -> None:
    raw: dict[str, Any] = {"input": "question"}
    if field == "fixtures":
        raw[field] = [{"name": "hidden", "content": {"nested": [number]}}]
    elif field == "extensions":
        raw[field] = {"acme.value": {"nested": [number]}}
    else:
        raw[field] = {"nested": [number]}
    with pytest.raises(ValidationError, match="non-finite") as error:
        normalize_case(raw, line=8, occurrence_index=1)
    assert error.value.line == 8


def test_python_case_api_rejects_non_finite_preconstructed_fixture_content() -> None:
    fixture = Fixture(name="hidden", content={"nested": [math.nan]})
    with pytest.raises(PydanticValidationError, match="non-finite"):
        BenchmarkCase(case_id="bad", input="question", fixtures=(fixture,))
    with pytest.raises(PydanticValidationError, match="non-finite"):
        BenchmarkCase(case_id="bad", input={math.inf: "key"})


@pytest.mark.parametrize("number", (math.nan, math.inf, -math.inf))
def test_python_case_api_rejects_non_finite_dataclass_fields(number: float) -> None:
    @dataclass(frozen=True)
    class Payload:
        value: float

    with pytest.raises(PydanticValidationError, match="non-finite"):
        BenchmarkCase(case_id="bad", input={"nested": [Payload(number)]})
    finite = BenchmarkCase(case_id="good", input=Payload(2.5))
    assert finite.model_dump(mode="json")["input"] == {"value": 2.5}


@pytest.mark.parametrize("token", ("NaN", "1e999"))
def test_non_finite_cli_dataset_is_invalid_and_dispatches_nothing(
    tmp_path: Path, token: str
) -> None:
    h = Harness(tmp_path)
    dataset = tmp_path / "numbers.jsonl"
    dataset.write_text(f'{{"case_id":"bad","input":{token}}}\n', encoding="utf-8")
    checked = cli.invoke(app, ["dataset", "validate", str(dataset), "--json"])
    assert checked.exit_code == 2, checked.output
    assert not json.loads(checked.stdout)["valid"]
    plan = h.plan(dataset=dataset.name, application=h.cli_app())
    executed = cli.invoke(
        app,
        ["run", "--plan", str(plan), "--trust-local-app", "--workspace", str(tmp_path / "run")],
    )
    assert executed.exit_code == 2, executed.output
    assert not h.log.exists()
    assert not (tmp_path / "run" / ".aibench").exists()


def test_finite_values_round_trip_through_storage_without_mutation(tmp_path: Path) -> None:
    original = BenchmarkCase(
        case_id="finite",
        input={"nested": [0, -0.0, 1.25, 1e308, "NaN", None, True]},
        metadata={"threshold": -2.5},
        fixtures=(Fixture(name="doc", content={"amount": 4.75}),),
    )
    json.dumps(original.model_dump(mode="json"), allow_nan=False)
    database = Database.open(tmp_path / "cases.db")
    storage = Storage(database)
    try:
        from aibench.core.models import DatasetManifest

        manifest = DatasetManifest(dataset_id="finite", content_hash="sha256:finite", case_count=1)
        storage.commit_dataset(manifest)
        storage.commit_cases(manifest.content_hash, [original])
        [stored] = storage.list_cases(manifest.content_hash)
        assert stored.model_dump(mode="json") == original.model_dump(mode="json")
    finally:
        database.close()
