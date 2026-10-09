"""The versioned planner fixture set and `aibench plan benchmark` (12-T2, 12-G3, 12-G4).

The template baseline's observed numbers are pinned so any change to the planner or the
annotations is visible. They are observations, not targets: the recall target is not met,
and the test says so rather than relabelling it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.planning.benchmark import (
    FixtureSetError,
    load_fixture_set,
    run_fixture_set,
    wilson,
)
from aibench.planning.planner import plan_with_template

SET = Path(__file__).resolve().parents[1] / "benchmarks" / "planner" / "v1"
cli = CliRunner()


def test_the_fixture_set_is_versioned_diverse_and_honestly_unreviewed() -> None:
    fixture_set = load_fixture_set(SET)
    specs = fixture_set.specs
    assert fixture_set.version == "planner-fixtures-v1"
    assert 30 <= len(specs) <= 50
    families = {s["family"] for s in specs}
    # §23: known RAG, agent, chatbot, black-box, partial telemetry, misleading configs,
    # a declared voice app without audio evidence, a retriever disabled at runtime
    for family in (
        "rag",
        "agent",
        "chatbot",
        "blackbox",
        "partial_telemetry",
        "misleading",
        "voice",
        "retriever_disabled",
    ):
        assert family in families, family
    assert sum(1 for s in specs if s["holdout"]) >= 10  # held-out families
    assert fixture_set.reviewers == []  # no reviewers are claimed
    assert all(s["review"]["status"] == "unreviewed" for s in specs)
    assert {s["expect"]["executable"] for s in specs} == {True, False}  # rejection cases


def test_template_baseline_is_reported_against_targets_without_relabelling() -> None:
    report = run_fixture_set(load_fixture_set(SET), plan_with_template, planner_name="template")
    o = report["overall"]
    assert (o["selection_precision"]["numerator"], o["selection_precision"]["denominator"]) == (
        17,
        17,
    )
    assert (o["selection_recall"]["numerator"], o["selection_recall"]["denominator"]) == (17, 21)
    assert (o["gap_precision"]["numerator"], o["gap_precision"]["denominator"]) == (22, 28)
    assert o["gap_recall"]["value"] == 1.0
    assert (o["first_pass_valid"]["numerator"], o["first_pass_valid"]["denominator"]) == (37, 37)
    assert (o["invalid_rejection"]["numerator"], o["invalid_rejection"]["denominator"]) == (3, 3)
    assert o["unsupported_selections"] == 0
    status = {t["measure"]: t["status"] for t in report["targets"]}
    assert status["selection_recall"] == "not met"  # observed 0.8095 < 0.85, kept visible
    assert status["selection_precision"] == status["first_pass_valid"] == "met"
    # the development families are what the template was written against; held-out
    # families expose what keyword matching cannot do
    assert report["development_families"]["selection_recall"]["value"] == 1.0
    assert report["holdout_families"]["selection_recall"]["value"] == 0.5
    assert report["review"]["status"] == "unverified: no fixture has been reviewed"
    wrong = sorted(f["id"] for f in report["fixtures"] if not f["correct"])
    assert wrong == [
        "budget_objective",
        "image_captions",
        "multilingual_spanish",
        "negated_latency",
        "paraphrase_made_up",
        "paraphrase_right_answer",
    ]


def test_a_retriever_that_returns_nothing_at_runtime_is_a_gap_not_a_metric() -> None:
    """§23's disabled-retriever case: declared retrieval, empty in every recorded run.
    Before Prompt 12 the template selected the groundedness judge anyway."""
    fixture_set = load_fixture_set(SET)
    fixture_set.specs = [s for s in fixture_set.specs if s["id"] == "retriever_disabled_runtime"]
    [row] = run_fixture_set(fixture_set, plan_with_template, planner_name="template")["fixtures"]
    assert row["correct"], row
    assert row["observed"]["select"] == [] and row["observed"]["gaps"] == ["groundedness"]


def test_wilson_intervals() -> None:
    assert wilson(0, 0) is None
    low, high = wilson(17, 21)  # type: ignore[misc]
    assert 0.59 < low < 0.61 and 0.92 < high < 0.93
    assert wilson(3, 3)[1] == 1.0  # type: ignore[index]


def test_plan_benchmark_command(tmp_path: Path) -> None:
    out = tmp_path / "report.json"
    result = cli.invoke(app, ["plan", "benchmark", "--fixtures", str(SET), "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())  # the console wraps long lines
    assert "selection_recall: 0.8095 = 17/21" in text
    assert "not met" in text and "no fixture has been reviewed" in text
    assert json.loads(out.read_text(encoding="utf-8"))["overall"]["fixtures"] == 40
    gated = cli.invoke(app, ["plan", "benchmark", "--fixtures", str(SET), "--require-targets"])
    assert gated.exit_code == 1  # a target is not met
    no_config = cli.invoke(app, ["plan", "benchmark", "--fixtures", str(SET), "--planner", "model"])
    assert no_config.exit_code == 2
    provider = tmp_path / "provider.json"
    provider.write_text(
        json.dumps(
            {"base_url": "https://api.example.com/v1", "model": "m", "api_key": "env:EXAMPLE_KEY"}
        ),
        encoding="utf-8",
    )
    denied = cli.invoke(
        app,
        [
            "plan",
            "benchmark",
            "--fixtures",
            str(SET),
            "--planner",
            "model",
            "--provider-config",
            str(provider),
            "--json",
        ],
    )
    assert denied.exit_code == 4  # the policy does not permit the planner endpoint
    denial_document = json.loads(denied.stdout)
    assert "not contacted" in denial_document["message"]
    assert denial_document["details"]


def test_invalid_fixture_sets_are_refused(tmp_path: Path) -> None:
    (tmp_path / "fixtures.json").write_text(json.dumps({"schema": "other"}), encoding="utf-8")
    try:
        load_fixture_set(tmp_path)
    except FixtureSetError as exc:
        assert "expected schema" in str(exc)
    else:
        raise AssertionError("an invalid fixture set was accepted")


def test_plan_benchmark_reports_invalid_policy_as_json(tmp_path: Path) -> None:
    provider = tmp_path / "provider.json"
    provider.write_text(
        json.dumps({"base_url": "https://api.example.com/v1", "model": "m"}),
        encoding="utf-8",
    )
    policy = tmp_path / "policy.json"
    policy.write_text("{", encoding="utf-8")
    result = cli.invoke(
        app,
        [
            "plan",
            "benchmark",
            "--fixtures",
            str(SET),
            "--planner",
            "model",
            "--provider-config",
            str(provider),
            "--policy",
            str(policy),
            "--json",
        ],
    )
    assert result.exit_code == 2, result.output
    document = json.loads(result.stdout)
    assert document["status"] == "error" and document["exit_code"] == 2
    assert "invalid policy" in document["message"]
    assert "Traceback" not in result.output


def test_fixture_id_cannot_escape_the_temporary_benchmark_directory(tmp_path: Path) -> None:
    fixture_root = tmp_path / "set"
    fixture_root.mkdir()
    escaped = tmp_path / "outside"
    (fixture_root / "fixtures.json").write_text(
        json.dumps(
            {
                "schema": "aibench.planner-fixtures/1",
                "version": "probe",
                "catalog": [],
                "policies": {"p": {}},
                "fixtures": [
                    {
                        "id": str(escaped),
                        "family": "probe",
                        "objectives": ["answer accurately"],
                        "app": {"runner": "http", "url": "http://127.0.0.1:9/answer"},
                        "dataset": [{"count": 1, "fields": {"expected_output": "ok"}}],
                        "policy": "p",
                        "expect": {"select": [], "gaps": []},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FixtureSetError, match="id must be a safe identifier"):
        load_fixture_set(fixture_root)
    assert not escaped.exists()
