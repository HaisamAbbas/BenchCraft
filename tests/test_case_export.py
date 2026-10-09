"""Serialization safety and schema guarantees for case-result exports."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aibench.core.hashes import content_hash
from aibench.core.models import (
    BenchmarkCase,
    DatasetManifest,
    ExecutionResult,
    ExecutionStatus,
    RunManifest,
    WorkItem,
    WorkItemState,
)
from aibench.services.case_export import (
    build_case_export,
    export_path_component,
    render_case_export,
)
from aibench.services.reports import ReportError
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database
from aibench.storage.repositories import Storage


def test_jsonl_preserves_case_content_and_emits_one_schema_row_per_line() -> None:
    document = {
        "rows": [
            {
                "schema": "aibench.case-result/1",
                "case_id": "case-1",
                "case": {"input": {"text": "exact = preserved"}},
                "metrics": [],
            },
            {
                "schema": "aibench.case-result/1",
                "case_id": "case-2",
                "case": None,
                "metrics": [],
            },
        ]
    }

    rendered = render_case_export(document, "jsonl")
    rows = [json.loads(line) for line in rendered.splitlines()]

    assert len(rows) == 2
    assert rows[0]["case"]["input"]["text"] == "exact = preserved"
    assert rows[1]["case"] is None


def test_csv_uses_stable_columns_quotes_json_and_blocks_formula_cells() -> None:
    document = {
        "rows": [
            {
                "schema": "aibench.case-result/1",
                "run_id": "=1+1",
                "case_id": "@case",
                "content": "included",
                "case": {"input": {"text": "= harmless data"}},
                "execution": {
                    "error": "=not a spreadsheet formula",
                    "timing": {"wall_ms": 42, "first_token_ms": 17},
                    "usage": {"input_tokens": 9},
                    "tool_events": [{"name": "lookup", "input": "query"}],
                    "world_state": {"location": "lab"},
                    "trace_refs": ["sha256:trace"],
                    "observation_completeness": {"output": "complete"},
                },
                "metrics": [{"metric_id": "native.score", "value": 1}],
            },
            {
                "schema": "aibench.case-result/1",
                "execution": {"error": None},
                "metrics": [],
            },
            {
                "schema": "aibench.case-result/1",
                "execution": {"error": ""},
                "metrics": [],
            },
        ]
    }

    rendered = render_case_export(document, "csv")
    row, no_error, empty_error = list(csv.DictReader(io.StringIO(rendered)))

    assert row["run_id"] == "'=1+1"
    assert row["case_id"] == "'@case"
    assert json.loads(row["error"]) == "=not a spreadsheet formula"
    assert json.loads(row["timing"]) == {"first_token_ms": 17, "wall_ms": 42}
    assert json.loads(row["usage"]) == {"input_tokens": 9}
    assert json.loads(row["case"])["input"]["text"] == "= harmless data"
    assert json.loads(row["tool_events"]) == [{"name": "lookup", "input": "query"}]
    assert json.loads(row["world_state"]) == {"location": "lab"}
    assert json.loads(row["trace_refs"]) == ["sha256:trace"]
    assert json.loads(row["observation_completeness"]) == {"output": "complete"}
    assert json.loads(row["metrics"])[0]["value"] == 1
    assert no_error["error"] == ""
    assert json.loads(empty_error["error"]) == ""


def test_export_path_component_cannot_escape_workspace_and_is_deterministic() -> None:
    first = export_path_component("../../outside\\run-id")

    assert "/" not in first and "\\" not in first and ".." not in first
    assert export_path_component("../../outside\\run-id") == first
    assert export_path_component("../../other") != first


def test_render_rejects_unknown_formats_and_malformed_rows() -> None:
    with pytest.raises(ReportError, match="format must be"):
        render_case_export({"rows": []}, "html")
    with pytest.raises(ReportError, match="rows are malformed"):
        render_case_export({"rows": ["not an object"]}, "jsonl")


def test_export_keeps_selected_work_without_execution_and_includes_recorded_legacy_rows(
    tmp_path: Path,
) -> None:
    storage = Storage(Database.open_in_memory())
    cases = [
        BenchmarkCase(case_id="planned", input={"question": "not dispatched"}),
        BenchmarkCase(case_id="legacy", input={"question": "recorded"}),
    ]
    dataset_hash = content_hash([case.model_dump(mode="json") for case in cases])
    storage.commit_dataset(
        DatasetManifest(
            dataset_id="export-tests",
            content_hash=dataset_hash,
            case_count=len(cases),
            created_at=datetime(2024, 1, 1, tzinfo=UTC),
        )
    )
    storage.commit_cases(dataset_hash, cases)
    storage.commit_run(
        RunManifest(
            run_id="case-export-run",
            dataset_hash=dataset_hash,
            application_hash="sha256:application",
            plan_hash="sha256:plan",
            parameters={},
        ),
        status="running",
    )
    storage.commit_work_item(
        WorkItem(
            work_item_id="case-export-run:exec:planned:r0",
            run_id="case-export-run",
            task_key="exec:planned:r0",
            kind="execution",
            state=WorkItemState.PENDING,
        )
    )
    storage.commit_execution_attempt(
        ExecutionResult(
            execution_id="case-export-run:legacy:r0:a0",
            run_id="case-export-run",
            case_id="legacy",
            repetition_id=0,
            attempt_id=0,
            status=ExecutionStatus.OK,
            output="recorded output",
        )
    )

    document = build_case_export(
        storage,
        ArtifactStore(tmp_path / "artifacts", create=False),
        "case-export-run",
    )

    assert document["row_count"] == 2
    rows = {row["case_id"]: row for row in document["rows"]}
    assert rows["planned"]["work_state"] == "pending"
    assert rows["planned"]["execution"] is None
    assert rows["legacy"]["work_state"] == "recorded"
    assert rows["legacy"]["execution"]["output"] == "recorded output"
    storage.db.close()
