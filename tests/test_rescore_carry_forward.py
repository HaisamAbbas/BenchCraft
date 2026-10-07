"""Retrying only what failed. A rescore repeated the judge on all 15 cases (about 45 s each
on a free endpoint, 12 minutes) when 2 had failed. With `carry_forward` a pass keeps the
finished results of earlier passes and evaluates only what is missing or failed; every pass
stays complete and says which results it carried."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Console

from aibench.core.models import (
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from aibench.services.scoring import is_carried
from aibench.tui import render
from aibench.tui.commands import Commands
from tests.scoring_support import RUN_ID, Seeded, case, execution
from tests.session_support import SessionHarness

OK = ExecutionStatus.OK


class Flaky(Evaluator):
    """A judge that hits a rate limit once on the cases in `fail_once`, and remembers each
    case it was asked about."""

    manifest = EvaluatorManifest.model_validate(
        {
            "evaluator_id": "tests.flaky",
            "version": "1.0.0",
            "plugin_id": "tests",
            "plugin_version": "0",
            "description": "a judge that is rate limited once",
            "value_kind": "scalar",
            "direction": MetricDirection.HIGHER,
            "aggregation": "mean",
            "uses_models": True,
            "requires": (FieldRequirement(path="execution.output", non_empty=False),),
            "default_rule": {"comparator": ">=", "threshold": 0.5},
        }
    )
    asked: ClassVar[list[str]] = []
    fail_once: ClassVar[set[str]] = set()

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        case_id = view.case.case_id
        Flaky.asked.append(case_id)
        if case_id in Flaky.fail_once:
            Flaky.fail_once.discard(case_id)
            raise RuntimeError("judge HTTP 429: rate limit reached")
        return EvaluationOutcome.ok("scalar", 0.9)


def _registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    registry.register(Flaky)
    return registry


def _seeded(tmp_path: Path) -> Seeded:
    seeded = Seeded(tmp_path)
    ids = ("a", "b", "c")
    seeded.seed([case(c, "yes") for c in ids], [execution(c, "yes") for c in ids])
    Flaky.asked.clear()
    Flaky.fail_once.clear()
    return seeded


BINDING = {"metric": "tests.flaky"}


def test_a_carry_forward_pass_evaluates_only_what_failed(tmp_path: Path) -> None:
    seeded = _seeded(tmp_path)
    registry = _registry()
    Flaky.fail_once.add("b")

    first = seeded.score([BINDING], registry=registry)
    by_case = {r.case_id: r for r in first.results}
    assert [by_case[c].status for c in "abc"] == [OK, ExecutionStatus.ERROR, OK]
    assert Flaky.asked == ["a", "b", "c"] and first.carried == 0

    second = seeded.score([BINDING], registry=registry, carry_forward=True)
    assert Flaky.asked == ["a", "b", "c", "b"]  # a and c were not asked about again
    assert second.carried == 2
    by_case = {r.case_id: r for r in second.results}
    assert all(by_case[c].status is OK for c in "abc")  # the pass is complete
    assert [is_carried(by_case[c]) for c in "abc"] == [True, False, True]
    summary = second.summaries[0]
    assert (summary.selected, summary.completed, summary.errors) == (3, 3, 0)

    carried = by_case["a"]
    assert carried.value == first.results[0].value  # the value is the earlier one
    assert carried.provenance["carried_forward"]["source_result_id"] == first.results[0].result_id
    assert "cache" not in carried.provenance
    # Nothing was called for it in this pass, so no calls or cost are attributed to it.
    assert carried.resources["model_calls"] == 0 and carried.resources["cost"] == 0.0
    stored = seeded.storage.list_metric_results(RUN_ID, scoring_id=second.scoring_id)
    assert {(r.case_id, r.result_id, is_carried(r)) for r in stored} == {
        (r.case_id, r.result_id, is_carried(r)) for r in second.results
    }  # what the pass returned is what it stored


def test_without_carry_forward_every_case_is_evaluated_again(tmp_path: Path) -> None:
    seeded = _seeded(tmp_path)
    registry = _registry()
    seeded.score([BINDING], registry=registry)
    again = seeded.score([BINDING], registry=registry)
    assert Flaky.asked == ["a", "b", "c", "a", "b", "c"]
    assert again.carried == 0 and not any(is_carried(r) for r in again.results)


def test_nothing_is_carried_when_the_metric_settings_changed(tmp_path: Path) -> None:
    """A result is only reused for the same stored answer and the same metric settings."""
    seeded = _seeded(tmp_path)
    registry = _registry()
    first = seeded.score([{"metric": "native.exact_match"}], registry=registry)
    same = seeded.score([{"metric": "native.exact_match"}], registry=registry, carry_forward=True)
    assert same.carried == 3
    changed = seeded.score(
        [{"metric": "native.exact_match", "params": {"case_sensitive": False}}],
        registry=registry,
        carry_forward=True,
    )
    assert changed.carried == 0 and len(changed.results) == 3
    assert first.carried == 0


def test_a_failed_result_is_never_carried_and_a_missing_metric_is_evaluated(
    tmp_path: Path,
) -> None:
    seeded = _seeded(tmp_path)
    registry = _registry()
    Flaky.fail_once.update({"a", "b", "c"})
    seeded.score([BINDING], registry=registry)  # all three fail
    Flaky.asked.clear()
    retry = seeded.score(
        [BINDING, {"metric": "native.exact_match"}], registry=registry, carry_forward=True
    )
    assert Flaky.asked == ["a", "b", "c"]  # nothing finished, so nothing to carry
    assert retry.carried == 0 and all(r.status is OK for r in retry.results)


def test_the_rescore_command_carries_by_default_and_all_evaluates_everything(
    tmp_path: Path,
) -> None:
    h = SessionHarness(tmp_path)
    ctl = h.open_session({"a": "answer", "b": "answer"}, objectives=("catch wrong answers",))
    try:

        async def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
            started = await ctl.start_run(action_id="run-1", expected_revision=1)
            done = await ctl.wait_for_run(started.run_id)
            assert done is not None and done.state.value == "completed"
            commands = Commands(ctl)
            default = await commands.run("/rescore")
            everything = await commands.run("/rescore all")
            bad = await commands.run("/rescore all run-1 extra")
            assert not bad.ok and "usage: /rescore [all] [RUN_ID]" in bad.data["error"]
            return default.data, everything.data

        default, everything = asyncio.run(scenario())
    finally:
        ctl.storage.db.close()
    assert default["carried_forward"] == 2 and default["evaluated_now"] == 0  # all finished
    assert everything["carried_forward"] == 0 and everything["evaluated_now"] == 2

    console = Console(file=io.StringIO(), width=120, highlight=False)
    render.rescored(console, default)
    render.rescored(console, everything)
    shown = console.file.getvalue()  # type: ignore[attr-defined]
    assert "carried forward 2 finished result(s); evaluated 0 now" in shown
    assert "/rescore all evaluates everything again" in shown
    assert shown.count("carried forward") == 1  # a full rescore says nothing about carrying


def test_needs_attention_clears_once_a_rescore_has_finished_what_the_run_left_failed(
    tmp_path: Path,
) -> None:
    """The run's own work records still say "failed" after a rescore settled those
    evaluations, so the status line kept saying "needs attention 11" with every metric
    scored. A failed evaluation a later pass finished is no longer waiting for attention."""
    from aibench.core.models import WorkItem, WorkItemState
    from aibench.engine.engine import evaluation_key
    from aibench.services.runs import run_status

    seeded = _seeded(tmp_path)
    registry = _registry()
    Flaky.fail_once.add("b")
    first = seeded.score([BINDING], registry=registry)
    failed = next(r for r in first.results if r.status is ExecutionStatus.ERROR)
    assert failed.binding_hash is not None
    for case_id in "abc":  # the run's records, as the engine leaves them
        key = evaluation_key(case_id, 0, failed.binding_hash)
        state = WorkItemState.FAILED if case_id == "b" else WorkItemState.SUCCEEDED
        seeded.storage.commit_work_item(
            WorkItem(
                work_item_id=f"w-{case_id}",
                run_id=RUN_ID,
                task_key=key,
                kind="evaluation",
                state=state,
                last_error="judge HTTP 429" if case_id == "b" else None,
            )
        )
    seeded.storage.update_run_status(RUN_ID, "completed")

    before = run_status(seeded.storage, RUN_ID)
    assert before["counts"]["evaluation"] == {"succeeded": 2, "failed": 1}
    assert [item["state"] for item in before["needs_attention"]] == ["failed"]

    seeded.score([BINDING], registry=registry, carry_forward=True)  # b is evaluated again
    after = run_status(seeded.storage, RUN_ID)
    assert after["needs_attention"] == []
    assert after["counts"]["evaluation"] == {"succeeded": 3}


def test_an_evaluation_a_rescore_could_not_finish_needs_attention_even_if_the_run_was_clean(
    tmp_path: Path,
) -> None:
    """A rescore's answer-relevancy case timed out, yet the status line said "needs attention
    0": the run's own records (all succeeded) were all it read. The latest pass counts."""
    from aibench.core.models import WorkItem, WorkItemState
    from aibench.engine.engine import evaluation_key
    from aibench.services.runs import run_status

    seeded = _seeded(tmp_path)
    registry = _registry()
    first = seeded.score([BINDING], registry=registry)  # every case scored
    binding_hash = first.results[0].binding_hash
    assert binding_hash is not None
    for case_id in "abc":  # the run's records: all succeeded
        seeded.storage.commit_work_item(
            WorkItem(
                work_item_id=f"w-{case_id}",
                run_id=RUN_ID,
                task_key=evaluation_key(case_id, 0, binding_hash),
                kind="evaluation",
                state=WorkItemState.SUCCEEDED,
            )
        )
    seeded.storage.update_run_status(RUN_ID, "completed")
    assert run_status(seeded.storage, RUN_ID)["needs_attention"] == []

    Flaky.fail_once.add("c")  # the rescore: c fails this time
    seeded.score([BINDING], registry=registry)
    status = run_status(seeded.storage, RUN_ID)
    assert [item["task_key"] for item in status["needs_attention"]] == [
        evaluation_key("c", 0, binding_hash)
    ]
    assert "429" in (status["needs_attention"][0]["reason"] or "")
    assert status["counts"]["evaluation"] == {"succeeded": 2, "failed": 1}

    seeded.score([BINDING], registry=registry)  # the next rescore finishes c
    assert run_status(seeded.storage, RUN_ID)["needs_attention"] == []
