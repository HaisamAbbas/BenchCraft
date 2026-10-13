"""Conversation-level scoring: a metric that requires `episode.turns` is scored on each turn
with the conversation up to and including it, assembled from the run's recorded executions.

Uses an in-process test evaluator that records exactly what it received, so the harness's
part is checked without any plugin (the DeepEval metrics built on it are covered in
tests/test_deepeval_conversational.py)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aibench.core.models import (
    DecisionRule,
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
from aibench.inspection.dataset_summary import summarize_cases
from aibench.registry import EvaluatorRegistry
from aibench.services.scoring import (
    MAX_EVALUATOR_EPISODE_TURNS,
    episode_prefixes,
)
from tests.scoring_support import Seeded, case, execution


def _conversation_metric(*, retrieval: bool) -> type[Evaluator]:
    requires = [
        FieldRequirement(path="case.group_id"),
        FieldRequirement(path="episode.turns"),
        FieldRequirement(path="execution.output", non_empty=False),
    ]
    if retrieval:
        requires.append(FieldRequirement(path="execution.retrieved_context", non_empty=False))

    class Recorder(Evaluator):
        manifest = EvaluatorManifest(
            evaluator_id="test.conversation_rag" if retrieval else "test.conversation",
            version="1.0.0",
            plugin_id="test",
            plugin_version="1",
            description="records the conversation it is given",
            value_kind="scalar",
            direction=MetricDirection.HIGHER,
            aggregation="mean",
            requires=tuple(requires),
            default_rule=DecisionRule(comparator=">=", threshold=0.0),
        )

        async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
            turns = view.get("episode.turns")
            return EvaluationOutcome.ok("scalar", float(len(turns)), raw={"turns": turns})

    return Recorder


def _raw(seeded: Seeded, result: Any) -> dict[str, Any]:
    import json

    return json.loads(
        seeded.artifacts.read_bytes(seeded.storage.get_artifact(result.raw_artifact_ref))
    )


def _seed(seeded: Seeded, *, turn_one_status: ExecutionStatus = ExecutionStatus.OK) -> None:
    cases = [
        case("t1", group_id="ep"),
        case("solo"),
        case("t2", group_id="ep"),
        case("t3", group_id="ep"),
    ]
    seeded.seed(
        cases,
        [
            execution("t1", "first answer", status=turn_one_status, retrieved_context=("doc 1",)),
            execution("solo", "alone"),
            execution("t2", "second answer", retrieved_context=("doc 2",)),
            execution("t3", "third answer"),
            # Another repetition of the episode: never mixed into repetition 0's conversation.
            execution("t1", "rep1 first", repetition_id=1),
            execution("t2", "rep1 second", repetition_id=1),
        ],
    )


def _score(
    tmp_path: Path, *, retrieval: bool = False, **seed: Any
) -> tuple[Seeded, dict[Any, Any]]:
    seeded = Seeded(tmp_path)
    _seed(seeded, **seed)
    registry = EvaluatorRegistry.with_native()
    metric = _conversation_metric(retrieval=retrieval)
    registry.register(metric)
    report = seeded.score([{"metric": metric.manifest.evaluator_id}], registry=registry)
    return seeded, {(r.case_id, r.repetition_id): r for r in report.results}


def test_each_turn_is_scored_on_the_conversation_so_far(tmp_path: Path) -> None:
    seeded, results = _score(tmp_path)
    assert results[("t1", 0)].value.value == 1.0
    assert results[("t2", 0)].value.value == 2.0
    assert results[("t3", 0)].value.value == 3.0  # the whole episode
    third = _raw(seeded, results[("t3", 0)])["turns"]
    assert [(t["case_id"], t["input"], t["output"]) for t in third] == [
        ("t1", "question t1", "first answer"),
        ("t2", "question t2", "second answer"),
        ("t3", "question t3", "third answer"),
    ]
    # Only what the metric reads is assembled: no retrieval was asked for.
    assert all(set(t) == {"case_id", "input", "output"} for t in third)
    rep1 = _raw(seeded, results[("t2", 1)])["turns"]
    assert [t["output"] for t in rep1] == ["rep1 first", "rep1 second"]


def test_a_case_in_no_episode_is_not_applicable(tmp_path: Path) -> None:
    _, results = _score(tmp_path)
    solo = results[("solo", 0)]
    assert (solo.status, solo.reason) == (ExecutionStatus.NOT_APPLICABLE, "missing:case.group_id")


def test_a_conversation_with_a_failed_earlier_turn_is_not_scored(tmp_path: Path) -> None:
    _, results = _score(tmp_path, turn_one_status=ExecutionStatus.ERROR)
    assert results[("t1", 0)].status is ExecutionStatus.SKIPPED  # its own execution failed
    for later in ("t2", "t3"):
        result = results[(later, 0)]
        assert (result.status, result.reason) == (
            ExecutionStatus.NOT_APPLICABLE,
            "episode_incomplete:t1",
        )


def test_retrieval_is_added_to_each_turn_only_when_the_metric_reads_it(tmp_path: Path) -> None:
    seeded, results = _score(tmp_path, retrieval=True)
    turns = _raw(seeded, results[("t2", 0)])["turns"]
    assert [t["retrieved_context"] for t in turns] == [["doc 1"], ["doc 2"]]
    third = results[("t3", 0)]  # t3 recorded no retrieval: the metric's own requirement
    assert (third.status, third.reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "missing:execution.retrieved_context",
    )


def test_scalar_episode_metric_means_weight_cases_equally(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [
            case("a1", group_id="episode-a"),
            case("a2", group_id="episode-a"),
            case("b1", group_id="episode-b"),
        ],
        [
            execution("a1", "first"),
            execution("a1", "repeat", repetition_id=1),
            execution("a2", "second"),
            execution("b1", "other"),
        ],
    )
    registry = EvaluatorRegistry.with_native()
    metric = _conversation_metric(retrieval=False)
    registry.register(metric)
    report = seeded.score([{"metric": metric.manifest.evaluator_id}], registry=registry)

    [summary] = report.summaries
    assert summary.value_summary["mean"] == round((1 + 2 + 1) / 3, 6)
    assert summary.value_summary["repetition_weighted_mean"] == 1.25
    assert summary.value_summary["case_count"] == 3
    assert summary.value_summary["n"] == 4


def test_episode_order_follows_the_dataset_and_ignores_other_cases() -> None:
    cases = [
        case("a1", group_id="a"),
        case("x"),
        case("b1", group_id="b"),
        case("a2", group_id="a"),
    ]
    prefixes = episode_prefixes(cases)
    assert "x" not in prefixes
    assert [c.case_id for c in prefixes["a2"]] == ["a1", "a2"]
    assert [c.case_id for c in prefixes["b1"]] == ["b1"]


def test_long_episode_prefix_storage_is_bounded() -> None:
    cases = [case(f"long-{index:04d}", group_id="long") for index in range(1_000)]

    prefixes = episode_prefixes(cases)

    assert len(prefixes) == len(cases)
    assert len(prefixes["long-0000"]) == 1
    assert len(prefixes[f"long-{MAX_EVALUATOR_EPISODE_TURNS - 1:04d}"]) == (
        MAX_EVALUATOR_EPISODE_TURNS
    )
    sentinel = prefixes[f"long-{MAX_EVALUATOR_EPISODE_TURNS:04d}"]
    assert len(sentinel) == MAX_EVALUATOR_EPISODE_TURNS + 1
    assert prefixes["long-0999"] is sentinel


def test_evaluator_episode_limits_are_applied_before_worker_dispatch(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from aibench.services import scoring

    monkeypatch.setattr(scoring, "MAX_EVALUATOR_EPISODE_BYTES", 160)
    cases = [case("bounded-1", group_id="bounded"), case("bounded-2", group_id="bounded")]
    seeded = Seeded(tmp_path)
    seeded.seed(
        cases,
        [execution("bounded-1", "short"), execution("bounded-2", "x" * 180)],
    )
    registry = EvaluatorRegistry.with_native()
    metric = _conversation_metric(retrieval=False)
    registry.register(metric)

    report = seeded.score([{"metric": metric.manifest.evaluator_id}], registry=registry)
    results = {result.case_id: result for result in report.results}

    assert results["bounded-1"].status is ExecutionStatus.OK
    assert (results["bounded-2"].status, results["bounded-2"].reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "episode_too_large",
    )


def test_episode_after_turn_limit_is_not_applicable(tmp_path: Path) -> None:
    cases = [
        case(f"bounded-{index:02d}", group_id="bounded")
        for index in range(MAX_EVALUATOR_EPISODE_TURNS + 1)
    ]
    seeded = Seeded(tmp_path)
    seeded.seed(cases, [execution(item.case_id, item.case_id) for item in cases])
    registry = EvaluatorRegistry.with_native()
    metric = _conversation_metric(retrieval=False)
    registry.register(metric)

    report = seeded.score([{"metric": metric.manifest.evaluator_id}], registry=registry)
    results = {result.case_id: result for result in report.results}

    assert all(
        results[item.case_id].status is ExecutionStatus.OK
        for item in cases[:MAX_EVALUATOR_EPISODE_TURNS]
    )
    assert (
        results[cases[-1].case_id].status,
        results[cases[-1].case_id].reason,
    ) == (ExecutionStatus.NOT_APPLICABLE, "episode_too_many_turns")


def test_episode_payload_bound_counts_worker_json_unicode_escaping(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from aibench.services import scoring

    monkeypatch.setattr(scoring, "MAX_EVALUATOR_EPISODE_BYTES", 400)
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("unicode", group_id="unicode")],
        [execution("unicode", "漢" * 80)],
    )
    registry = EvaluatorRegistry.with_native()
    metric = _conversation_metric(retrieval=False)
    registry.register(metric)

    report = seeded.score([{"metric": metric.manifest.evaluator_id}], registry=registry)

    [result] = report.results
    assert (result.status, result.reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "episode_too_large",
    )


def test_only_datasets_with_episodes_report_group_ids() -> None:
    plain = summarize_cases([case("a"), case("b")], dataset_id="d", content_hash="h")
    assert plain.present("case.group_id") == 0
    assert "case.group_id" not in {f.path for f in plain.fields}  # unchanged summaries
    grouped = summarize_cases(
        [case("a", group_id="g"), case("b", group_id="g"), case("c")],
        dataset_id="d",
        content_hash="h",
    )
    assert grouped.present("case.group_id") == 2
