"""`aibench evaluators ...` and `aibench score` end to end through the Typer app."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from tests.runner_support import EXAMPLE_APPS, REPO_ROOT

cli = CliRunner()
CUSTOM = str(REPO_ROOT / "examples" / "evaluators" / "refund_window.py")
METRICS = str(REPO_ROOT / "examples" / "metrics" / "support.metrics.json")
SUPPORT = str(REPO_ROOT / "examples" / "datasets" / "support.valid.jsonl")


def _smoke(workspace: Path) -> str:
    result = cli.invoke(
        app,
        [
            "app",
            "smoke",
            str(EXAMPLE_APPS / "cli_chatbot.app.json"),
            "--dataset",
            SUPPORT,
            "--workspace",
            str(workspace),
            "--trust-local-app",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.output)["run_id"]


def test_evaluators_list_and_describe() -> None:
    listed = cli.invoke(app, ["evaluators", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    ids = {m["evaluator_id"] for m in json.loads(listed.output)["evaluators"]}
    assert {"native.exact_match", "native.json_schema"} <= ids
    described = cli.invoke(app, ["evaluators", "describe", "native.json_schema@1"])
    assert described.exit_code == 0 and json.loads(described.output)["value_kind"] == "boolean"
    assert cli.invoke(app, ["evaluators", "describe", "native.nope"]).exit_code == 2


def test_custom_evaluator_needs_explicit_trust() -> None:
    refused = cli.invoke(app, ["evaluators", "list", "--custom-evaluator", CUSTOM])
    assert refused.exit_code == 2 and "trust" in refused.output
    allowed = cli.invoke(
        app, ["evaluators", "list", "--custom-evaluator", CUSTOM, "--trust-local-code"]
    )
    assert allowed.exit_code == 0 and "acme.refund_window" in allowed.output


def test_score_recorded_run_end_to_end(tmp_path: Path) -> None:
    run_id = _smoke(tmp_path)
    result = cli.invoke(
        app,
        [
            "score",
            run_id,
            "--metrics",
            METRICS,
            "--workspace",
            str(tmp_path),
            "--custom-evaluator",
            CUSTOM,
            "--trust-local-code",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    summaries = {
        (s["metric_id"], json.dumps(s["params"], sort_keys=True)): s for s in data["summaries"]
    }
    exact = summaries[("native.exact_match", "{}")]
    assert (exact["selected"], exact["completed"], exact["decisions"]["pass"]) == (4, 4, 3)
    refund = summaries[("acme.refund_window", "{}")]
    assert (refund["completed"], refund["not_applicable"], refund["eligible_coverage"]) == (
        2,
        2,
        0.5,
    )
    assert refund["value_summary"]["counts"] == {"correct": 2}


def test_score_rejects_bad_bindings_before_evaluating(tmp_path: Path) -> None:
    run_id = _smoke(tmp_path)
    metrics = tmp_path / "bad.metrics.json"
    metrics.write_text(
        json.dumps({"metrics": [{"metric": "native.exact_match", "params": {"nope": 1}}]}),
        encoding="utf-8",
    )
    result = cli.invoke(
        app, ["score", run_id, "--metrics", str(metrics), "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "no cases were evaluated" in result.output
    assert "nope" in result.output


def test_score_reports_unknown_runs_and_missing_workspaces(tmp_path: Path) -> None:
    missing_ws = cli.invoke(
        app, ["score", "r", "--metrics", METRICS, "--workspace", str(tmp_path / "none")]
    )
    assert missing_ws.exit_code == 2 and "no aibench workspace" in missing_ws.output
    _smoke(tmp_path)
    unknown = cli.invoke(
        app,
        [
            "score",
            "no-such-run",
            "--metrics",
            METRICS,
            "--workspace",
            str(tmp_path),
            "--custom-evaluator",
            CUSTOM,
            "--trust-local-code",
        ],
    )
    assert unknown.exit_code == 2 and "no run committed" in unknown.output
