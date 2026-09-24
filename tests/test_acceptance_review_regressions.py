"""Regressions for the Prompt 12 independent review (ADR 0011, "Changes after independent
review"). Each test reproduces a confirmed finding and pins the fix."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from aibench.core.models import ExecutionResult, ExecutionStatus
from aibench.inspection.profile import inspect_application
from aibench.planning.benchmark import (
    FixtureResult,
    FixtureSetError,
    PlannerFixture,
    _counts,
    load_fixture_set,
    run_fixture_set,
)
from aibench.planning.drafts import MetricChoice
from aibench.planning.planner import plan_with_template
from aibench.services.calibration import CalibrationError, load_calibration_set
from tests.planning_support import write_app

SET = Path(__file__).resolve().parents[1] / "benchmarks" / "planner" / "v1"


def _only(*ids: str):  # type: ignore[no-untyped-def]
    fixture_set = load_fixture_set(SET)
    fixture_set.specs = [s for s in fixture_set.specs if s["id"] in ids]
    return fixture_set


def test_a_planner_that_falls_back_to_the_template_is_not_reported_as_measured() -> None:
    # Review 1: a model planner whose provider failed returned the template's plans, and
    # the report marked the targets "met" under the model's name.
    def failing_model(inputs):  # type: ignore[no-untyped-def]
        outcome = plan_with_template(inputs)
        outcome.provenance = outcome.provenance.model_copy(
            update={"kind": "model", "fallback_reason": "assistant model failed"}
        )
        return outcome

    report = run_fixture_set(load_fixture_set(SET), failing_model, planner_name="model:down")
    assert report["overall"]["fallbacks"] == 40
    assert report["overall"]["fallbacks_on_executable_fixtures"] == 37
    assert report["warnings"] == [
        "40 of 40 fixture(s) were planned by the template fallback, not by the planner under test"
    ]
    assert {t["status"] for t in report["targets"]} == {
        "not measured: 37 fixture(s) fell back to the template"
    }
    assert all(row["fell_back"] for row in report["fixtures"])


def test_selecting_an_ineligible_evaluator_counts_as_unsupported_even_if_not_listed() -> None:
    # Review 6: only annotated `forbidden` IDs counted; `chatbot_refs` does not list
    # catalog.grounded, which the catalog refuses (no retrieval exposed).
    def overreaching(inputs):  # type: ignore[no-untyped-def]
        outcome = plan_with_template(inputs)
        objective = outcome.proposal.objectives[0].objective_id
        extra = MetricChoice(
            metric="catalog.grounded@1.0.0", objective_ids=(objective,), rationale="guess"
        )
        proposal = outcome.proposal.model_copy(
            update={"metrics": (*outcome.proposal.metrics, extra)}
        )
        return dataclasses.replace(outcome, proposal=proposal)

    report = run_fixture_set(_only("chatbot_refs"), overreaching, planner_name="overreaching")
    assert report["overall"]["unsupported_selections"] == 1
    [row] = report["fixtures"]
    assert row["observed"]["forbidden_selected"] == ["catalog.grounded"]


def test_a_refusal_only_counts_when_it_cites_a_missing_permission() -> None:
    # Suspected (review): any non-executable result counted as a correct rejection.
    fixture = PlannerFixture("x", "security", ("o",), frozenset(), frozenset(), executable=False)
    wrong_reason = FixtureResult(
        fixture, set(), set(), set(), executable=False, repairs=0, refused_for_permission=False
    )
    right_reason = dataclasses.replace(wrong_reason, refused_for_permission=True)
    assert _counts([wrong_reason])["invalid_rejection"]["numerator"] == 0
    assert _counts([right_reason])["invalid_rejection"]["numerator"] == 1
    report = run_fixture_set(
        _only("security_cli_not_trusted", "security_remote_http_denied", "security_effectful_app"),
        plan_with_template,
        planner_name="template",
    )
    assert report["overall"]["invalid_rejection"]["numerator"] == 3  # for the right reason


def test_malformed_sets_are_refused_with_a_clear_error(tmp_path: Path) -> None:
    # Review 8: KeyError / ValidationError / JSONDecodeError tracebacks.
    (tmp_path / "fixtures.json").write_text(
        json.dumps({"schema": "aibench.planner-fixtures/1", "version": "x"}), encoding="utf-8"
    )
    with pytest.raises(FixtureSetError, match="invalid fixture set"):
        load_fixture_set(tmp_path)
    (tmp_path / "fixtures.json").write_text(
        json.dumps(
            {
                "schema": "aibench.planner-fixtures/1",
                "version": "x",
                "catalog": [],
                "policies": {"p": {}},
                "fixtures": [
                    {
                        "id": "bad",
                        "family": "x",
                        "objectives": [],
                        "app": {},
                        "dataset": [],
                        "policy": "p",
                        "expect": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FixtureSetError, match="expect must be an object"):
        load_fixture_set(tmp_path)
    (tmp_path / "fixtures.json").write_text(
        json.dumps(
            {
                "schema": "aibench.planner-fixtures/1",
                "version": "x",
                "catalog": [{"evaluator_id": "bad"}],
                "policies": {},
                "fixtures": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(FixtureSetError, match="invalid fixture set"):
        load_fixture_set(tmp_path)
    cal = tmp_path / "cal"
    cal.mkdir()
    (cal / "set.json").write_text("[]", encoding="utf-8")
    (cal / "cases.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(CalibrationError, match="metadata must be an object"):
        load_calibration_set(cal)
    (cal / "set.json").write_text(
        json.dumps({"schema": "aibench.judge-calibration/1", "version": "test"})
    )
    (cal / "cases.jsonl").write_text("{not json\n", encoding="utf-8")
    with pytest.raises(CalibrationError, match="invalid JSON"):
        load_calibration_set(cal)
    (cal / "cases.jsonl").write_text(json.dumps({"label": "acceptable"}) + "\n")
    with pytest.raises(CalibrationError, match="missing"):
        load_calibration_set(cal)


def _recorded(entries: list[str | None]) -> list[ExecutionResult]:
    out = []
    for index, detail in enumerate(entries):
        completeness = (
            {} if detail is None else {"retrieved_context": {"state": "observed", "detail": detail}}
        )
        out.append(
            ExecutionResult(
                execution_id=f"r:c{index}:r0:a1",
                run_id="r",
                case_id=f"c{index}",
                attempt_id=1,
                status=ExecutionStatus.OK,
                output="answer",
                observation_completeness=completeness,
            )
        )
    return out


def test_always_empty_needs_enough_observations(tmp_path: Path) -> None:
    # Review 4: one empty answer (an unanswerable question) disabled groundedness.
    app = write_app(tmp_path, output_binding={"output": "/answer", "retrieved_context": "/ctx"})
    one = inspect_application(app, executions=_recorded(["empty"]))
    assert one.always_empty == ()
    mostly_missing = inspect_application(app, executions=_recorded([None] * 99 + ["empty"]))
    assert mostly_missing.always_empty == ()
    mixed = inspect_application(app, executions=_recorded(["empty", "present", "empty", "empty"]))
    assert mixed.always_empty == ()
    disabled = inspect_application(app, executions=_recorded(["empty"] * 3))
    assert disabled.always_empty == ("retrieved_context",)
    assert any("empty in all 3 recorded executions" in g for g in disabled.gaps)
