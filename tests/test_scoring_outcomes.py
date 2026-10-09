"""F06: scoring status, quality gates, CLI codes and headless chat propagation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import (
    Decision,
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricDirection,
)
from aibench.core.plans import ReleaseGate
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from tests.engine_support import Harness
from tests.scoring_support import RUN_ID, Seeded, case, execution
from tests.session_support import ScriptedProvider, SessionHarness, call, say
from tests.test_scoring_service import _registry


class HalfCrashes(Evaluator):
    manifest = EvaluatorManifest.model_validate(
        {
            "evaluator_id": "test.half_crashes",
            "version": "1.0.0",
            "plugin_id": "tests",
            "plugin_version": "1",
            "description": "fails only one selected answer",
            "value_kind": "scalar",
            "direction": MetricDirection.HIGHER,
            "aggregation": "mean",
            "requires": (FieldRequirement(path="execution.output", non_empty=False),),
        }
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        if view.get("execution.output") == "bad":
            return EvaluationOutcome.error("fixture evaluator failure")
        return EvaluationOutcome.ok("scalar", 0.9)


def test_all_and_mixed_evaluator_errors_are_incomplete(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("a"), case("b")], [execution("a", "bad"), execution("b", "good")])
    try:
        all_errors = seeded.score([{"metric": "tests.crashes"}], registry=_registry())
        assert all(result.status is ExecutionStatus.ERROR for result in all_errors.results)
        assert all_errors.exit_code == 3
        assert all_errors.outcome == {
            "complete": False,
            "unhealthy_work": {"errors": 2},
            "gates_failed": [],
            "gates_undecided": [],
        }
        registry = EvaluatorRegistry.with_native()
        registry.register(HalfCrashes)
        mixed = seeded.score(
            [{"metric": HalfCrashes.manifest.evaluator_id}],
            registry=registry,
        )
        assert sorted(r.status for r in mixed.results) == [
            ExecutionStatus.ERROR,
            ExecutionStatus.OK,
        ]
        assert mixed.exit_code == 3
        assert mixed.outcome["unhealthy_work"] == {"errors": 1}
    finally:
        seeded.storage.db.close()


def test_low_scores_are_complete_and_gate_failure_is_exit_one(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("a", "correct")], [execution("a", "incorrect")])
    try:
        ordinary = seeded.score([{"metric": "native.exact_match"}])
        assert ordinary.results[0].decision is Decision.FAIL
        assert ordinary.outcome["complete"] is True
        assert ordinary.exit_code == 0
        gated = seeded.score(
            [{"metric": "native.exact_match"}],
            gates=(ReleaseGate(gate_id="all-cases-pass", binding=0, min_pass_rate=1),),
        )
        assert gated.exit_code == 1
        assert gated.outcome["complete"] is True
        assert gated.outcome["gates_failed"] == ["all-cases-pass"]
        stored = seeded.storage.list_run_events(RUN_ID)[-1]["payload"]
        assert stored["exit_code"] == 1
        assert stored["outcome"] == gated.outcome
    finally:
        seeded.storage.db.close()


def test_not_applicable_is_complete_but_cancelled_and_empty_passes_are_not(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("a", "expected")], [execution("a", "answer")])
    try:
        na = seeded.score([{"metric": "tests.grounded"}], registry=_registry())
        assert na.results[0].status is ExecutionStatus.NOT_APPLICABLE
        assert na.exit_code == 0
        cancelled = seeded.score([{"metric": "native.exact_match"}], cancel=asyncio.Event())
        assert cancelled.exit_code == 0  # a clear event is not a cancellation request
        cancel = asyncio.Event()
        cancel.set()
        cancelled = seeded.score([{"metric": "native.exact_match"}], cancel=cancel)
        assert cancelled.results[0].status is ExecutionStatus.CANCELLED
        assert cancelled.exit_code == 3
        from aibench.services.scoring import scoring_pass_outcome as decide_outcome

        outcome, gates, code = decide_outcome([])
        assert (outcome["complete"], gates, code) == (False, [], 3)
    finally:
        seeded.storage.db.close()


def test_evaluate_returns_three_for_missing_selected_work_and_json_outcome(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "hi", "b": "hi"}),
        application=h.cli_app(),
        budgets={"max_application_calls": 1},
    )
    run_id = h.create(plan)
    h.execute(run_id)
    result = CliRunner().invoke(
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
    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["outcome"]["complete"] is False
    assert payload["outcome"]["unhealthy_work"] == {"unavailable": 1}
    assert payload["exit_code"] == 3
    assert h.count() == 1


def test_evaluate_returns_one_for_a_complete_failed_release_gate(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.dataset({"a": "hi"})
    (h.root / "data.jsonl").write_text(
        json.dumps({"case_id": "a", "input": "hi", "expected_output": "no"}) + "\n",
        encoding="utf-8",
    )
    plan = h.plan(
        dataset="data.jsonl",
        application=h.cli_app(),
        gates=[{"gate_id": "all-pass", "binding": 0, "min_pass_rate": 1.0}],
    )
    run_id = h.create(plan)
    h.execute(run_id)
    result = CliRunner().invoke(
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
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["outcome"]["complete"] is True
    assert payload["outcome"]["gates_failed"] == ["all-pass"]
    assert payload["gates"][0]["status"] == "fail"


def test_score_returns_three_for_unavailable_recorded_output(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    plan = h.plan(
        dataset=h.dataset({"a": "crash now"}),
        application=h.cli_app(),
        retry={"max_attempts": 1},
    )
    run_id = h.create(plan)
    h.execute(run_id)
    metrics = h.root / "metrics.json"
    metrics.write_text(
        json.dumps({"metrics": [{"metric": "native.exact_match"}]}), encoding="utf-8"
    )
    result = CliRunner().invoke(
        app,
        [
            "score",
            run_id,
            "--metrics",
            str(metrics),
            "--workspace",
            str(h.workspace.root.parent),
            "--json",
        ],
    )
    assert result.exit_code == 3, result.output
    payload = json.loads(result.output)
    assert payload["outcome"]["complete"] is False
    assert payload["outcome"]["unhealthy_work"] == {"unavailable": 1}
    assert payload["exit_code"] == 3
    assert h.count() == 1  # rescoring consumes the stored failed attempt; it never reruns the app
    text_result = CliRunner().invoke(
        app,
        ["score", run_id, "--metrics", str(metrics), "--workspace", str(h.workspace.root.parent)],
    )
    assert text_result.exit_code == 3, text_result.output
    assert "outcome: incomplete (exit code 3)" in text_result.output
    assert h.count() == 1


def test_headless_chat_slash_rescore_propagates_incomplete_exit_three(tmp_path: Path) -> None:
    h = SessionHarness(tmp_path)
    # The CLI resolves its database directly from --project. Keep the helper and CLI on
    # the same workspace so this exercises the persisted session rather than a new project.
    from aibench.storage.db import Workspace

    h.workspace = Workspace.at(h.root)
    h.workspace.ensure_directories()
    controller = h.open_session(
        {"a": "hi", "b": "hi"},
        objectives=("check every answer",),
        policy={"ceilings": {"max_application_calls": 1}},
    )

    async def run():  # type: ignore[no-untyped-def]
        started = await controller.start_run(action_id="partial", expected_revision=1)
        finished = await controller.wait_for_run(started.run_id)
        assert finished is not None

    try:
        asyncio.run(run())
        result = CliRunner().invoke(
            app,
            ["chat", "--project", str(h.root), "--send", "/rescore", "--json"],
        )
        assert result.exit_code == 3, result.output
        payload = json.loads(result.output)
        assert payload["exit_code"] == 3
        assert payload["command"] == "/rescore"
        assert payload["data"]["outcome"]["complete"] is False
    finally:
        controller.storage.db.close()


def test_headless_natural_language_rescore_propagates_incomplete_exit_three(
    tmp_path: Path, monkeypatch
) -> None:
    from aibench.cli import chat as chat_cli
    from aibench.storage.db import Workspace

    h = SessionHarness(tmp_path)
    h.workspace = Workspace.at(h.root)
    h.workspace.ensure_directories()
    controller = h.open_session(
        {"a": "hi", "b": "hi"},
        objectives=("check every answer",),
        policy={"ceilings": {"max_application_calls": 1}},
    )

    async def run():  # type: ignore[no-untyped-def]
        started = await controller.start_run(action_id="partial", expected_revision=1)
        finished = await controller.wait_for_run(started.run_id)
        assert finished is not None

    provider = ScriptedProvider(
        [
            call("rescore_run", user_quote="Please rescore this run."),
            say("Rescored the stored outputs; one selected item is unavailable."),
        ]
    )
    monkeypatch.setattr(chat_cli, "open_provider", lambda *_, **__: (provider, []))
    try:
        asyncio.run(run())
        result = CliRunner().invoke(
            app,
            [
                "chat",
                "--project",
                str(h.root),
                "--provider-config",
                str(h.root / "provider.json"),
                "--send",
                "Please rescore this run.",
                "--json",
            ],
        )
        assert result.exit_code == 3, result.output
        payload = json.loads(result.output)
        assert payload["exit_code"] == 3
        assert payload["outcome"]["rescores"][0]["exit_code"] == 3
        assert payload["outcome"]["rescores"][0]["outcome"]["complete"] is False
    finally:
        controller.storage.db.close()
