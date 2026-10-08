"""F19: coverage follows frozen selection rather than workspace dataset history."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from tests.engine_support import Harness

cli = CliRunner()


def _run(h: Harness, *, selection: dict[str, Any], repetitions: int = 1, budget: int = 20) -> str:
    plan = h.plan(
        dataset="data.jsonl",
        application=h.cli_app(),
        selection=selection,
        repetitions=repetitions,
        budgets={"max_application_calls": budget},
    )
    result = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(plan),
            "--trust-local-app",
            "--workspace",
            str(h.workspace.root.parent),
            "--json",
        ],
    )
    assert result.exit_code in (0, 3), result.output
    return json.loads(result.stdout)["run_id"]


@pytest.mark.parametrize(
    "selection",
    (
        {"limit": 2},
        {"sample_size": 2, "seed": 17},
        {"case_ids": ["selected:r2:a", "selected:b"]},
        {"where": [{"path": "case.input", "op": "equals", "value": "select"}]},
    ),
)
def test_subset_comparison_ignores_other_cataloged_cases(
    tmp_path: Path, selection: dict[str, Any]
) -> None:
    h = Harness(tmp_path)
    h.dataset({"selected:r2:a": "select", "selected:b": "select", "other": "exclude"})
    _run(h, selection={})  # populate the same dataset catalog with all three cases
    baseline = _run(h, selection=selection, repetitions=2)
    current = _run(h, selection=selection, repetitions=2)
    calls_before = h.count()
    result = cli.invoke(
        app,
        [
            "compare",
            baseline,
            current,
            "--workspace",
            str(h.workspace.root.parent),
            "--bootstrap-replicates",
            "100",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    [metric] = report["metrics"]
    gate = metric["coverage_gate"]
    assert gate["passed"]
    assert gate["paired_selected"] == 4
    assert gate["complete_numeric_pairs"] == 4
    assert gate["complete_numeric_pairs_over_required_selected"] == 1.0
    assert h.count() == calls_before


def test_partial_subset_keeps_missing_selected_cases_in_comparison(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.dataset({"a": "question", "b": "question", "unselected": "question"})
    _run(h, selection={})
    baseline = _run(h, selection={"limit": 2})
    current = _run(h, selection={"limit": 2}, budget=1)
    result = cli.invoke(
        app,
        [
            "compare",
            baseline,
            current,
            "--workspace",
            str(h.workspace.root.parent),
            "--bootstrap-replicates",
            "100",
            "--json",
        ],
    )
    assert result.exit_code == 1, result.output
    [metric] = json.loads(result.stdout)["metrics"]
    assert metric["coverage_gate"]["paired_selected"] == 2
    assert metric["coverage_gate"]["complete_numeric_pairs"] == 1
    assert metric["coverage_gate"]["complete_numeric_pairs_over_required_selected"] == 0.5

    # A changed binding has no matching evaluation work items in the original run.
    # Its missing output must still come from selected execution work, not the catalog.
    plan = h.plan(
        dataset="data.jsonl",
        application=h.cli_app(),
        selection={"limit": 2},
        metrics=[{"metric": "native.exact_match", "params": {"case_sensitive": False}}],
    )
    passes = []
    for run_id in (baseline, current):
        evaluated = cli.invoke(
            app,
            [
                "evaluate",
                run_id,
                "--plan",
                str(plan),
                "--workspace",
                str(h.workspace.root.parent),
                "--json",
            ],
        )
        assert evaluated.exit_code == 0, evaluated.output
        passes.append(json.loads(evaluated.stdout)["scoring_id"])
    compared = cli.invoke(
        app,
        [
            "compare",
            baseline,
            current,
            "--baseline-scoring",
            passes[0],
            "--current-scoring",
            passes[1],
            "--workspace",
            str(h.workspace.root.parent),
            "--bootstrap-replicates",
            "100",
            "--json",
        ],
    )
    assert compared.exit_code == 1, compared.output
    [metric] = json.loads(compared.stdout)["metrics"]
    assert metric["coverage_gate"]["paired_selected"] == 2
    assert metric["coverage_gate"]["complete_numeric_pairs"] == 1
