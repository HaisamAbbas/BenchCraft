"""Regression tests for the independent review of Prompt 04 (see ADR 0003)."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import (
    Decision,
    DecisionRule,
    ExecutionStatus,
    FieldRequirement,
    MetricBinding,
)
from aibench.evaluators.native import ExactMatch
from aibench.evaluators.protocol import EvaluationOutcome, Evaluator
from aibench.registry import BindingValidationError, EvaluatorRegistry, RegistryError
from tests.scoring_support import Seeded, case, execution
from tests.test_scoring_service import _manifest

OK, ERR = ExecutionStatus.OK, ExecutionStatus.ERROR


def test_unusable_output_counts_as_a_failure_not_lost_coverage(tmp_path: Path) -> None:
    """Review #1: null or structured output on hard cases must not raise the pass rate."""
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case(c, "A") for c in ("right", "null", "structured")],
        [
            execution("right", "A"),
            execution("null", None),
            execution("structured", {"answer": "B"}),
        ],
    )
    [summary] = seeded.score([{"metric": "native.exact_match"}]).summaries
    assert (summary.completed, summary.not_applicable) == (3, 0)
    assert summary.value_summary["rate"] == round(1 / 3, 6)
    assert summary.decisions["fail"] == 2


class _BadRaw(Evaluator):
    manifest = _manifest("tests.bad_raw")

    async def evaluate(self, view: Any, ctx: Any) -> EvaluationOutcome:
        return EvaluationOutcome.ok("scalar", 1.0, raw={1, 2})


class _ReturnsNone(Evaluator):
    manifest = _manifest("tests.returns_none")

    async def evaluate(self, view: Any, ctx: Any) -> EvaluationOutcome:
        return None  # type: ignore[return-value]


class _Nan(Evaluator):
    manifest = _manifest("tests.nan")

    async def evaluate(self, view: Any, ctx: Any) -> EvaluationOutcome:
        return EvaluationOutcome.ok("scalar", math.nan)


@pytest.mark.parametrize("factory", [_BadRaw, _ReturnsNone, _Nan])
def test_broken_evaluators_become_error_results_and_the_pass_continues(
    tmp_path: Path, factory: type[Evaluator]
) -> None:
    """Review #2/#5: every evaluator defect is a recorded error; the pass completes."""
    registry = EvaluatorRegistry.with_native()
    registry.register(factory)
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1", "x"), case("c2", "x")], [execution("c1", "x"), execution("c2", "x")])
    report = seeded.score(
        [{"metric": factory.manifest.evaluator_id}, {"metric": "native.exact_match"}],
        registry=registry,
    )
    broken = [r for r in report.results if r.metric_id == factory.manifest.evaluator_id]
    assert [(r.status, r.decision) for r in broken] == [(ERR, Decision.NOT_EVALUATED)] * 2
    assert all(r.value is None for r in broken)
    assert len(report.results) == 4  # the other metric still ran


def test_unknown_requirement_paths_are_refused_before_scoring(tmp_path: Path) -> None:
    """Review #2: a typo'd requirement path must fail validation, not crash mid-pass."""

    class Typo(Evaluator):
        manifest = _manifest("tests.typo", requires=(FieldRequirement(path="execution.outputs"),))

        async def evaluate(self, view: Any, ctx: Any) -> EvaluationOutcome:
            return EvaluationOutcome.ok("scalar", 1.0)

    registry = EvaluatorRegistry.with_native()
    registry.register(Typo)
    with pytest.raises(BindingValidationError, match="unknown evaluation view path"):
        registry.validate([MetricBinding(metric="tests.typo")])


def test_untrusted_regex_in_a_case_schema_cannot_stall_scoring(tmp_path: Path) -> None:
    """Review #3: catastrophic backtracking from dataset content is bounded by a hard timeout."""
    seeded = Seeded(tmp_path)
    evil = {"type": "string", "pattern": "^(a+)+$"}
    seeded.seed([case("c1", expectations={"sch": evil})], [execution("c1", "a" * 40 + "!")])
    started = time.perf_counter()
    [result] = seeded.score(
        [
            {
                "metric": "native.json_schema",
                "params": {"schema_field": "case.expectations.sch", "parse_text": False},
            }
        ],
        timeout_seconds=2,
    ).results
    assert time.perf_counter() - started < 10
    assert result.status is ERR and "timeout" in (result.reason or "")


def test_decision_rules_reject_non_finite_and_boolean_thresholds() -> None:
    """Review #5."""
    for bad in (math.nan, math.inf, True):
        with pytest.raises(ValueError):
            DecisionRule(comparator=">=", threshold=bad)
    assert DecisionRule(comparator=">=", threshold=1).threshold == 1.0


def test_attempt_numbers_count_rescoring_per_binding_and_repetition(tmp_path: Path) -> None:
    """Review #6: attempt_number means "how many times this binding scored this execution"."""
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1", "x")], [execution("c1", "x"), execution("c1", "x", repetition_id=1)])
    bindings = [
        {"metric": "native.exact_match"},
        {"metric": "native.exact_match", "params": {"case_sensitive": False}},
    ]
    first = seeded.score(bindings)
    second = seeded.score(bindings)
    assert {r.attempt_number for r in first.results} == {0}
    assert {r.attempt_number for r in second.results} == {1}


def test_duplicate_bindings_are_detected_after_resolution() -> None:
    """Review #7: spelling the same metric differently is still a duplicate."""
    with pytest.raises(BindingValidationError, match="duplicate binding"):
        EvaluatorRegistry.with_native().validate(
            [
                MetricBinding(metric="native.exact_match"),
                MetricBinding(metric="native.exact_match@1.0.0", rule={"comparator": "is_true"}),
            ]
        )


def test_reserved_native_namespace_and_collisions_are_refused(tmp_path: Path) -> None:
    """Review #8."""
    registry = EvaluatorRegistry.with_native()
    with pytest.raises(RegistryError, match="reserved"):
        registry.register_external(ExactMatch.manifest.model_copy(update={"version": "9.0.0"}))
    local = tmp_path / "shadow.py"
    local.write_text(
        "from aibench.evaluators.native import ExactMatch\n"
        "class Shadow(ExactMatch):\n    manifest = ExactMatch.manifest.model_copy(update={'version': '9.0.0'})\n"
        "EVALUATORS = (Shadow,)\n",
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="reserved"):
        registry.load_local_file(local, trusted=True)
    assert registry.resolve("native.exact_match")[0].version == "1.0.0"


def test_duplicate_case_ids_are_not_scored_against_an_arbitrary_golden(tmp_path: Path) -> None:
    """Review #8: two stored Goldens share a case_id; which one applies is ambiguous."""
    seeded = Seeded(tmp_path)
    first, second = case("dup", "A"), case("dup", "B").model_copy(update={"source_line": 2})
    seeded.seed([first, second], [execution("dup", "A")])
    [result] = seeded.score([{"metric": "native.exact_match"}]).results
    assert (result.status, result.reason) == (ExecutionStatus.SKIPPED, "duplicate_case_id")


def test_metric_results_can_be_read_per_scoring_pass(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1", "x")], [execution("c1", "x")])
    first = seeded.score([{"metric": "native.exact_match"}])
    seeded.score([{"metric": "native.exact_match"}])
    only_first = seeded.storage.list_metric_results("run-1", scoring_id=first.scoring_id)
    assert [r.result_id for r in only_first] == [r.result_id for r in first.results]


class _PrepareFails(Evaluator):
    manifest = _manifest("tests.prepare_fails")

    async def prepare(self, params: Any) -> None:
        raise RuntimeError("cannot load judge config")

    async def evaluate(self, view: Any, ctx: Any) -> EvaluationOutcome:
        raise AssertionError("must not be called after a failed prepare")


class _CloseFails(Evaluator):
    manifest = _manifest("tests.close_fails")

    async def evaluate(self, view: Any, ctx: Any) -> EvaluationOutcome:
        return EvaluationOutcome.ok("scalar", 1.0)

    async def close(self) -> None:
        raise RuntimeError("cleanup failed")


def test_prepare_and_close_failures_never_abort_the_pass(tmp_path: Path) -> None:
    """Second review P2: lifecycle hooks outside evaluate() must not escape score_all."""
    registry = EvaluatorRegistry.with_native()
    registry.register(_PrepareFails)
    registry.register(_CloseFails)
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("c1", "x"), case("c2", "x")],
        [execution("c1", "x"), execution("c2", status=ExecutionStatus.ERROR)],
    )
    report = seeded.score(
        [
            {"metric": "tests.prepare_fails"},
            {"metric": "tests.close_fails"},
            {"metric": "native.exact_match"},
        ],
        registry=registry,
    )
    by_metric: dict[str, list[Any]] = {}
    for r in report.results:
        by_metric.setdefault(r.metric_id, []).append(r)
    prep = {r.case_id: r for r in by_metric["tests.prepare_fails"]}
    assert (prep["c1"].status, prep["c1"].decision) == (ERR, Decision.NOT_EVALUATED)
    assert (prep["c1"].reason or "").startswith("evaluator_prepare_failed:RuntimeError")
    assert prep["c2"].status is ExecutionStatus.SKIPPED  # app failure still reported as such
    closing = {r.case_id: r for r in by_metric["tests.close_fails"]}
    assert closing["c1"].status is OK  # the scores stand; the cleanup failure is a warning
    assert any("tests.close_fails" in w and "cleanup failed" in w for w in report.warnings)
    assert len(by_metric["native.exact_match"]) == 2  # later metrics still ran
    assert len(seeded.storage.list_metric_results("run-1", scoring_id=report.scoring_id)) == 6
