"""Retrying only what failed. A rescore repeated the judge on all 15 cases (about 45 s each
on a free endpoint, 12 minutes) when 2 had failed. With `carry_forward` a pass keeps the
finished results of earlier passes and evaluates only what is missing or failed; every pass
stays complete and says which results it carried."""

from __future__ import annotations

import asyncio
import io
from dataclasses import replace
from pathlib import Path
from typing import Any, ClassVar

from rich.console import Console

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
from aibench.evaluators.worker_client import WorkerSpec
from aibench.registry import EvaluatorRegistry
from aibench.services.scoring import (
    BindingScorer,
    _carry_identity_is_complete,
    declared_dependency_identity,
    evaluation_compatibility_identity,
    is_carried,
)
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
            "package_name": "fixture-flaky",
            "package_version": "1.0.0",
            "description": "a judge that is rate limited once",
            "value_kind": "scalar",
            "direction": MetricDirection.HIGHER,
            "aggregation": "mean",
            "uses_models": True,
            "requires": (FieldRequirement(path="execution.output", non_empty=False),),
            "default_rule": {"comparator": ">=", "threshold": 0.5},
            "parameters_schema": {
                "type": "object",
                "properties": {
                    "model": {"type": "string"},
                    "timeout": {"type": "number"},
                },
            },
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
    seeded.seed(
        [case(c, "yes") for c in ids],
        [execution(c, "yes") for c in ids],
        application=ApplicationSpec(
            application_id="fixture-app",
            runner="cli",
            target="fixture.py",
            input_binding={"input": "/input"},
            output_binding={"output": "/output"},
        ),
    )
    Flaky.asked.clear()
    Flaky.fail_once.clear()
    return seeded


BINDING = {"metric": "tests.flaky", "params": {"model": "fixture-model"}}


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
    assert (
        carried.provenance["carried_forward"]["producer_compatibility_hash"]
        == first.results[0].provenance["compatibility"]["compatibility_hash"]
    )
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


def test_changed_plugin_or_dependency_identity_forces_fresh_evaluation(tmp_path: Path) -> None:
    seeded = _seeded(tmp_path)
    metric_id = "tests.versioned"
    values = {"v1": 0.1, "plugin-v2": 0.9, "dependency-v2": 0.8}
    calls = {key: 0 for key in values}

    def versioned_factory(name: str, plugin_version: str, package_version: str):
        class Versioned(Evaluator):
            manifest = EvaluatorManifest.model_validate(
                {
                    "evaluator_id": metric_id,
                    "version": "1.0.0",
                    "plugin_id": "tests",
                    "plugin_version": plugin_version,
                    "package_name": "fixture-versioned",
                    "package_version": package_version,
                    "description": "versioned carry-forward fixture",
                    "value_kind": "scalar",
                    "direction": "higher",
                    "aggregation": "mean",
                    "uses_models": True,
                    "parameters_schema": {
                        "type": "object",
                        "properties": {"model": {"type": "string"}},
                    },
                    "requires": [{"path": "execution.output", "non_empty": False}],
                }
            )

            async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext):
                calls[name] += 1
                return EvaluationOutcome.ok("scalar", values[name])

        return Versioned

    binding = {"metric": metric_id, "params": {"model": "fixture-model"}}

    def run(factory):
        registry = EvaluatorRegistry.with_native()
        registry.register(factory)
        return seeded.score([binding], registry=registry, carry_forward=True)

    try:
        first = run(versioned_factory("v1", "1.0.0", "1.0.0"))
        assert first.carried == 0 and calls["v1"] == 3
        changed_plugin = run(versioned_factory("plugin-v2", "2.0.0", "1.0.0"))
        assert changed_plugin.carried == 0 and calls["plugin-v2"] == 3
        assert all(
            result.provenance["compatibility"]["plugin_version"] == "2.0.0"
            for result in changed_plugin.results
        )
        changed_dependency = run(versioned_factory("dependency-v2", "2.0.0", "2.0.0"))
        assert changed_dependency.carried == 0 and calls["dependency-v2"] == 3
        assert all(
            result.value is not None and result.value.value == 0.8
            for result in changed_dependency.results
        )
    finally:
        seeded.storage.db.close()


def test_worker_environment_identity_includes_dependency_lock_and_fails_closed(
    tmp_path: Path,
) -> None:
    seeded = _seeded(tmp_path)
    metric = _registry().resolve_binding(
        MetricBinding(metric="tests.flaky", params={"model": "fixture-model"})
    )
    try:
        spec = WorkerSpec(
            python=Path("plugin-env/python"),
            target="plugin:factory",
            dependency_lock_hash="lock-v1",
            extra_paths_hash="paths-v1",
            python_runtime_identity="cpython-3.12.10-cp312-win_amd64",
        )
        with_worker = replace(
            metric, factory=type("WorkerFlaky", (metric.factory,), {"spec": spec})
        )
        changed_worker = replace(
            with_worker,
            factory=type(
                "WorkerFlakyV2",
                (metric.factory,),
                {"spec": replace(spec, dependency_lock_hash="lock-v2")},
            ),
        )
        assert declared_dependency_identity([with_worker]) != declared_dependency_identity(
            [changed_worker]
        )

        def cache_key_for(resolved_metric):
            scorer = BindingScorer(
                seeded.storage,
                seeded.artifacts,
                "score-worker-cache",
                resolved_metric,
                30.0,
                None,
                application=ApplicationSpec(
                    application_id="fixture-app",
                    runner="cli",
                    target="fixture.py",
                    input_binding={"input": "/input"},
                    output_binding={"output": "/output"},
                ),
                dependency_lock_hash=declared_dependency_identity([resolved_metric]),
            )
            scorer.cache_policy_hash = "policy-v1"
            return scorer._cache_key(execution("a", "yes"), [case("a", "yes")])

        key_v1 = cache_key_for(with_worker)
        assert key_v1 is not None and key_v1 != cache_key_for(changed_worker)
        changed_runtime = replace(
            with_worker,
            factory=type(
                "WorkerWithChangedPython",
                (metric.factory,),
                {
                    "spec": replace(
                        spec, python_runtime_identity="cpython-3.13.0-cp313-win_amd64"
                    )
                },
            ),
        )
        assert key_v1 != cache_key_for(changed_runtime)
        changed_extra_paths = replace(
            with_worker,
            factory=type(
                "WorkerWithChangedPath",
                (metric.factory,),
                {"spec": replace(spec, extra_paths_hash="paths-v2")},
            ),
        )
        assert declared_dependency_identity([with_worker]) != declared_dependency_identity(
            [changed_extra_paths]
        )

        unversioned = replace(
            metric,
            manifest=metric.manifest.model_copy(
                update={"package_name": None, "package_version": None}
            ),
            factory=type(
                "UnidentifiedWorker",
                (metric.factory,),
                {"spec": replace(spec, dependency_lock_hash=None)},
            ),
        )
        assert declared_dependency_identity([unversioned]) is None

        untracked_extra_path = replace(
            with_worker,
            factory=type(
                "UntrackedPathWorker",
                (metric.factory,),
                {
                    "spec": replace(
                        spec,
                        extra_paths=(Path("plugin-env/custom"),),
                        extra_paths_hash=None,
                    )
                },
            ),
        )
        assert declared_dependency_identity([untracked_extra_path]) is None

        native_metric = EvaluatorRegistry.with_native().resolve_binding(
            MetricBinding(metric="native.exact_match")
        )
        native_worker = replace(
            native_metric,
            factory=type("NativeWorker", (native_metric.factory,), {"spec": spec}),
        )
        native_worker_v2 = replace(
            native_metric,
            factory=type(
                "NativeWorkerV2",
                (native_metric.factory,),
                {"spec": replace(spec, dependency_lock_hash="lock-v2")},
            ),
        )
        assert not native_worker.manifest.uses_models
        assert declared_dependency_identity([native_worker]) != declared_dependency_identity(
            [native_worker_v2]
        )
        unidentified_native_worker = replace(
            native_metric,
            factory=type(
                "UnidentifiedNativeWorker",
                (native_metric.factory,),
                {"spec": replace(spec, dependency_lock_hash=None)},
            ),
        )
        assert declared_dependency_identity([unidentified_native_worker]) is None
        identity = evaluation_compatibility_identity(
            unidentified_native_worker,
            application=ApplicationSpec(
                application_id="fixture-app",
                runner="cli",
                target="fixture.py",
                input_binding={"input": "/input"},
                output_binding={"output": "/output"},
            ),
            dependency_lock_hash=None,
        )
        assert not _carry_identity_is_complete(identity, requires_dependency_identity=True)
    finally:
        seeded.storage.db.close()


def test_evaluation_cache_is_disabled_when_model_identity_is_unknown(tmp_path: Path) -> None:
    seeded = _seeded(tmp_path)
    resolved = _registry().resolve_binding(
        MetricBinding(metric="tests.flaky", params={"model": "fixture-model"})
    )
    unversioned = replace(
        resolved,
        manifest=resolved.manifest.model_copy(
            update={"package_name": None, "package_version": None}
        ),
    )
    scorer = BindingScorer(
        seeded.storage,
        seeded.artifacts,
        "score-cache-identity",
        unversioned,
        30.0,
        None,
        application=ApplicationSpec(
            application_id="fixture-app",
            runner="cli",
            target="fixture.py",
            input_binding={"input": "/input"},
            output_binding={"output": "/output"},
        ),
        dependency_lock_hash=declared_dependency_identity([unversioned]),
    )
    scorer.cache_policy_hash = "policy-v1"
    try:
        assert scorer._cache_key(execution("a", "yes"), [case("a", "yes")]) is None
    finally:
        seeded.storage.db.close()


def test_unverified_instrumentation_identity_disables_carry_forward(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("a", "yes")], [execution("a", "yes")])
    try:
        first = seeded.score([{"metric": "native.exact_match"}])
        second = seeded.score([{"metric": "native.exact_match"}], carry_forward=True)
        assert first.results[0].provenance["compatibility"]["instrumentation"]["verified"] is False
        assert second.carried == 0
    finally:
        seeded.storage.db.close()


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


def test_a_failure_left_under_old_judge_settings_clears_when_the_new_settings_scored_it(
    tmp_path: Path,
) -> None:
    """After the judge's model and timeout were changed, a rescore scored everything, yet the
    status still said "needs attention 1": the run's failed answer-relevancy evaluation was
    recorded under the old settings, which are a different binding, so no later result
    matched it. The newest pass finishing the same metric on the same case replaces it."""
    from aibench.core.models import WorkItem, WorkItemState
    from aibench.engine.engine import evaluation_key
    from aibench.services.runs import run_status

    seeded = _seeded(tmp_path)
    registry = _registry()
    Flaky.fail_once.add("b")
    first = seeded.score([BINDING], registry=registry)  # the run: b failed
    failed = next(r for r in first.results if r.status is ExecutionStatus.ERROR)
    assert failed.binding_hash is not None
    for case_id in "abc":
        seeded.storage.commit_work_item(
            WorkItem(
                work_item_id=f"w-{case_id}",
                run_id=RUN_ID,
                task_key=evaluation_key(case_id, 0, failed.binding_hash),
                kind="evaluation",
                state=WorkItemState.FAILED if case_id == "b" else WorkItemState.SUCCEEDED,
                last_error="timeout:evaluation exceeded 600.0s" if case_id == "b" else None,
            )
        )
    seeded.storage.update_run_status(RUN_ID, "completed")
    assert len(run_status(seeded.storage, RUN_ID)["needs_attention"]) == 1

    changed = {**BINDING, "params": {"timeout": 400}}  # new settings: a new binding
    rescored = seeded.score([changed], registry=registry, carry_forward=True)
    assert rescored.results[0].binding_hash != failed.binding_hash
    assert all(r.status is OK for r in rescored.results)
    status = run_status(seeded.storage, RUN_ID)
    assert status["needs_attention"] == []
    assert status["counts"]["evaluation"] == {"succeeded": 3}

    Flaky.fail_once.add("c")  # but a failure of the new settings still shows
    seeded.score([changed], registry=registry)
    status = run_status(seeded.storage, RUN_ID)
    assert [i["task_key"] for i in status["needs_attention"]] == [
        evaluation_key("c", 0, failed.binding_hash)
    ]


def test_an_evaluation_the_run_never_reached_needs_attention_until_a_rescore_scores_it(
    tmp_path: Path,
) -> None:
    """A run stopped at its time limit before scoring one case: the result was recorded as
    skipped and its work record as succeeded, so the status said "needs attention 1" while two
    evaluations were unfinished, and "0" if only the skipped one remained."""
    from aibench.core.models import WorkItem, WorkItemState
    from aibench.engine.engine import evaluation_key
    from aibench.services.runs import run_status

    seeded = Seeded(tmp_path)
    seeded.seed(
        [case(c, "yes") for c in "abc"],
        [
            execution("a", "yes"),
            execution("b", "yes"),
            execution("c", status=ExecutionStatus.ERROR),  # its own execution failed
        ],
    )
    registry = _registry()
    Flaky.asked.clear()
    first = seeded.score([BINDING], registry=registry)
    skipped = [r for r in first.results if r.status is ExecutionStatus.SKIPPED]
    assert [r.case_id for r in skipped] == ["c"]
    binding_hash = first.results[0].binding_hash
    assert binding_hash is not None
    for case_id in "abc":  # the run's records: every evaluation "succeeded"
        seeded.storage.commit_work_item(
            WorkItem(
                work_item_id=f"w-{case_id}",
                run_id=RUN_ID,
                task_key=evaluation_key(case_id, 0, binding_hash),
                kind="evaluation",
                state=WorkItemState.SUCCEEDED,
            )
        )
    seeded.storage.update_run_status(RUN_ID, "budget_exhausted")
    # c was skipped because its execution failed: that failure is listed on its own, so the
    # evaluation is not listed a second time.
    assert run_status(seeded.storage, RUN_ID)["needs_attention"] == []

    # b was never reached: the run stopped at its time limit.
    reached = next(r for r in first.results if r.case_id == "b")
    never_reached = reached.model_copy(
        update={
            "result_id": reached.result_id + ":late",
            "status": ExecutionStatus.SKIPPED,
            "reason": "not_evaluated:max_wall_seconds=3600.0 reached",
            "value": None,
            "decision": Decision.NOT_EVALUATED,
            "scoring_id": "score-after-the-limit",
        }
    )
    seeded.storage.commit_metric_result(never_reached)
    status = run_status(seeded.storage, RUN_ID)
    assert [i["task_key"] for i in status["needs_attention"]] == [
        evaluation_key("b", 0, binding_hash)
    ]
    assert "max_wall_seconds" in (status["needs_attention"][0]["reason"] or "")
    assert status["counts"]["evaluation"] == {"succeeded": 2, "failed": 1}
