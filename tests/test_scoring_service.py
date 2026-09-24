"""Stored-output scoring pipeline (04-T4; gates 04-G1..G3): applicability, status
separation, conformance, accounting, persistence, deterministic aggregation, and proof
that rescoring never invokes the application."""

from __future__ import annotations

import asyncio
import json
import random
import sys
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import (
    ApplicationSpec,
    Decision,
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricBinding,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import BindingValidationError, EvaluatorRegistry
from aibench.reporting.aggregation import summarize
from aibench.services.scoring import evaluation_compatibility_identity, select_final_executions
from tests.scoring_support import Seeded, case, execution

OK, NA, ERR, SKIP = (
    ExecutionStatus.OK,
    ExecutionStatus.NOT_APPLICABLE,
    ExecutionStatus.ERROR,
    ExecutionStatus.SKIPPED,
)


def _manifest(evaluator_id: str, **update: Any) -> EvaluatorManifest:
    base = {
        "evaluator_id": evaluator_id,
        "version": "1.0.0",
        "plugin_id": "tests",
        "plugin_version": "0",
        "description": "test evaluator",
        "value_kind": "scalar",
        "direction": MetricDirection.HIGHER,
        "aggregation": "mean",
        "requires": (FieldRequirement(path="execution.output", non_empty=False),),
        "default_rule": {"comparator": ">=", "threshold": 0.5},
    }
    base.update(update)
    return EvaluatorManifest.model_validate(base)


class Length(Evaluator):
    """Scalar: output length / 10, capped at 1."""

    manifest = _manifest("tests.length")

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        return EvaluationOutcome.ok("scalar", min(len(view.get("execution.output")) / 10, 1.0))


class Grounded(Evaluator):
    """Needs non-empty observed retrieval, like a faithfulness metric would."""

    manifest = _manifest(
        "tests.grounded",
        value_kind="boolean",
        aggregation="rate",
        default_rule={"comparator": "is_true"},
        requires=(FieldRequirement(path="execution.retrieved_context"),),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        return EvaluationOutcome.ok("boolean", True)


class Crashes(Evaluator):
    manifest = _manifest("tests.crashes")

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        raise ZeroDivisionError("evaluator bug")


class Hangs(Evaluator):
    manifest = _manifest("tests.hangs")

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")


class WrongKind(Evaluator):
    manifest = _manifest("tests.wrong_kind")

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        return EvaluationOutcome.ok("boolean", True)  # manifest promises a scalar


class Judge(Evaluator):
    """Model-backed: optionally reports usage."""

    manifest = _manifest("tests.judge", uses_models=True)
    report = False

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        if self.report:
            ctx.report_usage(provider="p", calls=1, tokens={"input": 10}, cost=0.002)
        return EvaluationOutcome.ok("scalar", 0.9)


class ReportingJudge(Judge):
    manifest = _manifest("tests.reporting_judge", uses_models=True)
    report = True


def _registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    for factory in (Length, Grounded, Crashes, Hangs, WrongKind, Judge, ReportingJudge):
        registry.register(factory)
    return registry


def test_every_case_gets_exactly_one_honest_status(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case(c, "Refunds within 30 days.") for c in ("ok", "wrong", "app_failed", "ghost_ref")],
        [
            execution("ok", "Refunds within 30 days."),
            execution("wrong", "No refunds."),
            execution("app_failed", status=ExecutionStatus.ERROR, error_kind="timeout"),
            execution("unrecorded_case", "x"),  # execution whose Golden case is not stored
        ],
    )
    report = seeded.score([{"metric": "native.exact_match"}])
    by_case = {r.case_id: r for r in report.results}
    assert (by_case["ok"].status, by_case["ok"].decision) == (OK, Decision.PASS)
    assert (by_case["wrong"].status, by_case["wrong"].decision) == (OK, Decision.FAIL)
    assert by_case["app_failed"].status is SKIP
    assert by_case["app_failed"].reason == "execution_error:timeout"
    assert by_case["unrecorded_case"].reason == "case_not_recorded"
    # Selection is the run's recorded executions: a dataset case that was never executed
    # (ghost_ref) is not part of this run's selection (see ADR 0003 for the Prompt 06 rule).
    assert "ghost_ref" not in by_case
    [summary] = report.summaries
    assert (summary.selected, summary.eligible, summary.completed, summary.unavailable) == (
        4,
        2,
        2,
        2,
    )
    assert summary.value_summary == {"true": 1, "false": 1, "rate": 0.5, "denominator": "completed"}
    assert summary.completed_coverage == 0.5
    assert summary.reasons == {"case_not_recorded": 1, "execution_error": 1}


def test_missing_and_empty_retrieval_are_distinct_not_applicable_reasons(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("unknown"), case("empty"), case("present")],
        [
            execution("unknown", "a"),  # retrieval never observed: None
            execution("empty", "a", retrieved_context=()),
            execution("present", "a", retrieved_context=("doc",)),
        ],
    )
    report = seeded.score([{"metric": "tests.grounded"}], registry=_registry())
    reasons = {r.case_id: (r.status, r.reason) for r in report.results}
    assert reasons == {
        "unknown": (NA, "missing:execution.retrieved_context"),
        "empty": (NA, "empty:execution.retrieved_context"),
        "present": (OK, None),
    }
    [summary] = report.summaries
    assert summary.not_applicable == 2 and summary.eligible_coverage == round(1 / 3, 6)
    assert summary.reasons == {"empty": 1, "missing": 1}


@pytest.mark.parametrize(
    ("metric", "reason_prefix"),
    [
        ("tests.crashes", "evaluator_exception:ZeroDivisionError"),
        ("tests.hangs", "timeout:"),
        ("tests.wrong_kind", "conformance:expected a scalar value"),
    ],
)
def test_evaluator_failures_are_errors_never_scores(
    tmp_path: Path, metric: str, reason_prefix: str
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "hello")])
    [result] = seeded.score([{"metric": metric}], registry=_registry(), timeout_seconds=0.2).results
    assert result.status is ERR
    assert result.decision is Decision.NOT_EVALUATED
    assert result.value is None
    assert (result.reason or "").startswith(reason_prefix)


def test_decision_uses_the_frozen_rule_and_scalar_mean_is_labelled(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case(c) for c in "abc"],
        [execution("a", "12345"), execution("b", "123"), execution("c", "1234567890123")],
    )
    report = seeded.score(
        [{"metric": "tests.length", "rule": {"comparator": ">=", "threshold": 0.4}}],
        registry=_registry(),
    )
    decisions = {r.case_id: r.decision for r in report.results}
    assert decisions == {"a": Decision.PASS, "b": Decision.FAIL, "c": Decision.PASS}
    [summary] = report.summaries
    assert summary.value_summary == {
        "n": 3,
        "mean": 0.6,
        "min": 0.3,
        "max": 1.0,
        "denominator": "completed",
    }
    assert report.results[0].rule is not None and report.results[0].rule.threshold == 0.4


def test_model_usage_is_unknown_unless_reported(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "x")])
    report = seeded.score(
        [{"metric": "tests.judge"}, {"metric": "tests.reporting_judge"}], registry=_registry()
    )
    silent, reporting = sorted(report.results, key=lambda r: r.metric_id)
    assert silent.resources["cost"] is None and silent.resources["accounting"] == "unknown"
    assert reporting.resources["cost"] == 0.002 and reporting.resources["accounting"] == "reported"
    events = seeded.storage.list_usage_events("run-1")
    assert [(e.role.value, e.calls, e.cost) for e in events] == [("evaluator", 1, 0.002)]


def test_invalid_bindings_fail_before_any_case_is_evaluated(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1", "a")], [execution("c1", "a")])
    with pytest.raises(BindingValidationError):
        seeded.score([{"metric": "native.exact_match"}, {"metric": "native.missing_metric"}])
    assert seeded.storage.list_metric_results("run-1") == []
    assert seeded.storage.list_evaluation_attempts("run-1") == []


def test_final_attempt_is_scored_and_rescoring_adds_new_attempts(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("c1", "right")],
        [
            execution("c1", "wrong", attempt_id=0),
            execution("c1", "right", attempt_id=1),
            execution("c1", "right", repetition_id=1),
        ],
    )
    finals = select_final_executions(seeded.storage.list_execution_attempts("run-1"))
    assert [(e.repetition_id, e.attempt_id) for e in finals] == [(0, 1), (1, 0)]
    first = seeded.score([{"metric": "native.exact_match"}])
    second = seeded.score([{"metric": "native.exact_match"}])
    assert first.scoring_id != second.scoring_id
    assert all(r.decision is Decision.PASS for r in first.results + second.results)
    attempts = seeded.storage.list_evaluation_attempts("run-1")
    # Attempt N = Nth scoring of that (case, repetition) by that binding; nothing overwritten.
    assert sorted((a.repetition_id, a.attempt_number) for a in attempts) == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
    ]
    stored = seeded.storage.list_metric_results("run-1")
    assert {r.scoring_id for r in stored} == {first.scoring_id, second.scoring_id}
    assert first.summaries[0].as_dict() | {"binding_hash": ""} == second.summaries[0].as_dict() | {
        "binding_hash": ""
    }


def test_scoring_pass_freezes_separate_compatibility_identities(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1", "yes")], [execution("c1", "yes")])
    report = seeded.score(
        [{"metric": "tests.length", "rule": {"comparator": ">=", "threshold": 0.4}}],
        registry=_registry(),
    )
    event = next(
        item
        for item in seeded.storage.list_run_events("run-1")
        if item["event_type"] == "scoring_pass" and item["payload"]["scoring_id"] == report.scoring_id
    )
    identity = next(iter(event["payload"]["metric_profiles"].values()))["compatibility"]
    [result] = report.results
    assert identity == json.loads(result.model_dump_json())["provenance"]["compatibility"]
    assert identity["binding_hash"] == result.binding_hash
    assert identity["judge"] == {"kind": "not_used", "digest": None, "verified": True}
    assert identity["rubric"]["verified"] is True
    assert identity["instrumentation"]["verified"] is False  # this direct fixture has no app spec
    assert identity["compatibility_hash"].startswith("sha256:")

    changed = seeded.score(
        [{"metric": "tests.length", "rule": {"comparator": ">=", "threshold": 0.5}}],
        registry=_registry(),
    )
    [changed_result] = changed.results
    assert (
        changed_result.provenance["compatibility"]["compatibility_hash"]
        != identity["compatibility_hash"]
    )


def test_application_revision_can_change_without_changing_instrumentation_identity() -> None:
    metric = _registry().resolve_binding(MetricBinding(metric="tests.length"))
    base = ApplicationSpec(
        application_id="app",
        runner="cli",
        target="old-binary",
        revision="r1",
        output_binding={"output": "/answer"},
        input_binding={"input": "/question"},
    )
    revised = base.model_copy(update={"target": "new-binary", "revision": "r2"})
    changed_observation = base.model_copy(
        update={"output_binding": {"output": "/payload.answer"}}
    )
    before = evaluation_compatibility_identity(metric, application=base)
    after = evaluation_compatibility_identity(metric, application=revised)
    incompatible = evaluation_compatibility_identity(metric, application=changed_observation)
    assert after.compatibility_hash == before.compatibility_hash
    assert incompatible.compatibility_hash != before.compatibility_hash


def test_aggregation_is_deterministic_and_never_crosses_metrics(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    ids = [f"c{i:02d}" for i in range(20)]
    seeded.seed(
        [case(c, "yes") for c in ids],
        [execution(c, "yes" if i % 3 else "no") for i, c in enumerate(ids)],
    )
    report = seeded.score(
        [{"metric": "native.exact_match"}, {"metric": "tests.length"}], registry=_registry()
    )
    exact = next(s for s in report.summaries if s.metric_id == "native.exact_match")
    shuffled = [r for r in report.results if r.metric_id == "native.exact_match"]
    random.Random(7).shuffle(shuffled)
    manifest = EvaluatorRegistry.with_native().resolve("native.exact_match")[0]
    again = summarize(shuffled, manifest=manifest, binding_hash=exact.binding_hash)
    assert again.as_dict() == exact.as_dict()
    assert json.dumps(exact.as_dict(), sort_keys=True) == json.dumps(
        again.as_dict(), sort_keys=True
    )
    assert len(report.summaries) == 2  # one per metric; no combined score exists
    assert all("overall" not in json.dumps(s.as_dict()) for s in report.summaries)


def test_rescoring_recorded_outputs_never_invokes_the_application(tmp_path: Path) -> None:
    """04-G3 against a real application: an effect-counting HTTP app and a CLI app that
    counts its invocations in a file. Scoring twice leaves both counters unchanged."""
    from aibench.datasets.ingest import ingest_dataset
    from aibench.runners import create_runner, load_application
    from aibench.services.execution import run_developer_smoke
    from aibench.services.scoring import score_recorded_run
    from aibench.storage.artifacts import ArtifactStore
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage
    from tests.runner_support import EXAMPLE_APPS, REPO_ROOT, load_example, run, serving

    counter_file = tmp_path / "invocations.txt"
    script = tmp_path / "counting_app.py"
    script.write_text(
        "import json, pathlib, sys\n"
        f"p = pathlib.Path({str(counter_file)!r})\n"
        "p.write_text(str(int(p.read_text() or 0) + 1) if p.exists() else '1')\n"
        "json.load(sys.stdin)\nprint(json.dumps({'output': 'Booked trip to Dubai.'}))\n",
        encoding="utf-8",
    )
    cli_config = tmp_path / "counting.app.json"
    cli_config.write_text(
        json.dumps(
            {
                "application_id": "counting",
                "runner": "cli",
                "target": "counting",
                "transport": {"kind": "cli", "argv": [sys.executable, str(script)]},
            }
        ),
        encoding="utf-8",
    )
    effect_app = load_example("effect_counter_app")
    server = effect_app.make_server(port=0)
    ws = Workspace.at(tmp_path)
    ws.ensure_directories()
    storage = Storage(Database.open_workspace(ws))
    artifacts = ArtifactStore(ws.artifacts_dir)
    report = ingest_dataset(REPO_ROOT / "examples" / "datasets" / "booking.valid.jsonl")
    assert report.manifest is not None
    bindings = [
        __import__("aibench.core.models", fromlist=["MetricBinding"]).MetricBinding(
            metric="native.exact_match"
        )
    ]

    with serving(server) as base:
        http_app = load_application(EXAMPLE_APPS / "effect_counter.app.json")
        http_app = type(http_app)(
            spec=http_app.spec.model_copy(
                update={
                    "transport": http_app.spec.transport.model_copy(
                        update={"url": f"{base}/book", "healthcheck_url": None, "reset_url": None}
                    )
                }  # type: ignore[union-attr]
            ),
            base_dir=http_app.base_dir,
        )
        runs = []
        for app, trusted in ((http_app, False), (load_application(cli_config), True)):

            async def smoke(app=app, trusted=trusted):  # type: ignore[no-untyped-def]
                async with create_runner(app, trusted_local=trusted) as runner:
                    return await run_developer_smoke(
                        runner,
                        app.spec,
                        report.manifest,
                        report.cases,
                        storage=storage,
                        artifacts=artifacts,
                    )

            runs.append(run(smoke()).run_id)
        effects_after_run, invocations_after_run = server.count, counter_file.read_text()
        assert (effects_after_run, invocations_after_run) == (2, "2")

        for run_id in runs:
            for _ in range(2):
                scored = run(
                    score_recorded_run(
                        storage=storage,
                        artifacts=artifacts,
                        registry=EvaluatorRegistry.with_native(),
                        run_id=run_id,
                        bindings=bindings,
                    )
                )
                assert scored.summaries[0].completed == 2
        assert server.count == effects_after_run
        assert len(server.received) == 2
        assert counter_file.read_text() == invocations_after_run
    storage.db.close()


def test_view_treats_unobserved_tool_events_as_missing_not_empty() -> None:
    """`ExecutionResult.tool_events` defaults to () even when tools were never observed;
    only an `observed` completeness entry makes that empty tuple a real observation."""
    from aibench.evaluators.protocol import MISSING, EvaluationView

    unobserved = execution("c1", "x")
    observed_empty = execution(
        "c1",
        "x",
        observation_completeness={"tool_events": {"state": "observed", "detail": "empty"}},
    )
    assert EvaluationView(case("c1"), unobserved).get("execution.tool_events") is MISSING
    assert EvaluationView(case("c1"), observed_empty).state("execution.tool_events") == "empty"
