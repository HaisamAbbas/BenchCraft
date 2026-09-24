"""Constructed-data tests for the storage-free comparison and judge statistics core."""

from __future__ import annotations

import json
from collections.abc import Iterable

import pytest

from aibench.reporting.statistics import (
    ComparisonSide,
    ExecutionKey,
    JudgeObservation,
    MetricDirection,
    MetricIdentity,
    NumericObservation,
    ObservationStatus,
    PairKey,
    cluster_bootstrap_percentile_interval,
    compare_numeric_metric,
    construct_paired_observations,
    summarize_judge_stability,
)

METRIC = MetricIdentity(
    metric_id="example.quality",
    metric_version="1.0.0",
    binding_hash="binding-1",
    direction="higher",
)


def _observation(
    case_id: str,
    repetition_id: int,
    status: ObservationStatus,
    value: float | None = None,
) -> NumericObservation:
    return NumericObservation(PairKey(case_id, repetition_id), status, value)


def _side(
    run_id: str,
    selected: Iterable[tuple[str, int]],
    observations: Iterable[NumericObservation],
    *,
    metric: MetricIdentity = METRIC,
) -> ComparisonSide:
    return ComparisonSide(
        run_id=run_id,
        metric=metric,
        selected_keys=tuple(PairKey(case_id, repetition) for case_id, repetition in selected),
        observations=tuple(observations),
    )


def test_case_macro_weights_cases_equally_despite_unequal_repetition_coverage() -> None:
    selected = [("c1", 0), ("c1", 1), ("c2", 0), ("c2", 1)]
    baseline = _side(
        "baseline",
        selected,
        [
            _observation("c1", 0, "ok", 1),
            _observation("c1", 1, "ok", 1),
            _observation("c2", 0, "ok", 0),
            _observation("c2", 1, "error"),
        ],
    )
    current = _side(
        "current",
        selected,
        [
            _observation("c1", 0, "ok", 2),
            _observation("c1", 1, "ok", 4),
            _observation("c2", 0, "ok", 10),
            _observation("c2", 1, "ok", 5),
        ],
    )

    report = compare_numeric_metric(baseline, current, bootstrap_replicates=500, seed=17)

    # c1 mean delta is (1 + 3) / 2 = 2; c2's one complete delta is 10.
    # The case macro is 6, not the repetition-weighted 14 / 3.
    assert report["case_macro"] == {
        "complete_case_count": 2,
        "baseline_mean": 0.5,
        "current_mean": 6.5,
        "mean_current_minus_baseline": 6.0,
        "diagnostic_pair_weighted_mean_current_minus_baseline": pytest.approx(14 / 3),
    }
    assert report["denominators"] == {
        "baseline_selected": 4,
        "current_selected": 4,
        "paired_selected": 4,
        "complete_numeric_pairs": 3,
        "baseline_only_key_count": 0,
        "current_only_key_count": 0,
        "baseline_only_keys": [],
        "current_only_keys": [],
        "coverage": {
            "complete_numeric_pairs_over_paired_selected": 0.75,
            "complete_numeric_pairs_over_baseline_selected": 0.75,
            "complete_numeric_pairs_over_current_selected": 0.75,
        },
    }
    assert report["sides"]["baseline"]["status_counts"]["error"] == 1
    assert report["sides"]["current"]["status_counts"]["ok"] == 4
    assert report["paired_status_counts"] == {"error->ok": 1, "ok->ok": 3}
    assert report["uncertainty"]["estimate"] == 6.0
    assert report["direction_handling"] == {
        "direction": "higher",
        "favored_side_by_raw_mean_difference": "current",
        "unit_conversion_applied": False,
        "generic_quality_score_calculated": False,
    }


def test_missing_and_unpaired_statuses_are_coverage_losses_not_zero_scores() -> None:
    baseline = _side(
        "baseline",
        [("c1", 0), ("c1", 1), ("c2", 0)],
        [_observation("c1", 0, "ok", 1), _observation("c2", 0, "ok", 2)],
    )
    current = _side(
        "current",
        [("c1", 0), ("c2", 0), ("c3", 0)],
        [
            _observation("c1", 0, "ok", 3),
            _observation("c2", 0, "error"),
            _observation("c3", 0, "ok", 4),
        ],
    )

    report = compare_numeric_metric(baseline, current, bootstrap_replicates=100)
    denominators = report["denominators"]
    assert denominators["paired_selected"] == 2
    assert denominators["complete_numeric_pairs"] == 1
    assert denominators["baseline_only_keys"] == [{"case_id": "c1", "repetition_id": 1}]
    assert denominators["current_only_keys"] == [{"case_id": "c3", "repetition_id": 0}]
    assert denominators["coverage"] == {
        "complete_numeric_pairs_over_paired_selected": 0.5,
        "complete_numeric_pairs_over_baseline_selected": pytest.approx(1 / 3),
        "complete_numeric_pairs_over_current_selected": pytest.approx(1 / 3),
    }
    assert report["sides"]["baseline"]["status_counts"]["missing"] == 1
    assert report["sides"]["current"]["status_counts"]["error"] == 1
    assert report["case_macro"]["mean_current_minus_baseline"] == 2.0

    pairs = construct_paired_observations(baseline, current)
    incomplete = next(pair for pair in pairs if pair.key.case_id == "c2")
    assert incomplete.baseline_value == 2.0
    assert incomplete.current_value is None
    assert incomplete.current_status == "error"
    assert incomplete.delta is None
    assert incomplete.complete_numeric is False

    with pytest.raises(ValueError, match="coverage loss"):
        _observation("c1", 9, "missing", 0)


def test_explicit_groups_change_the_bootstrap_independent_unit_count() -> None:
    selected = [("c1", 0), ("c2", 0), ("c3", 0), ("c4", 0)]
    baseline = _side("baseline", selected, [_observation(case, 0, "ok", 0) for case, _ in selected])
    current = _side(
        "current",
        selected,
        [
            _observation("c1", 0, "ok", 1),
            _observation("c2", 0, "ok", 3),
            _observation("c3", 0, "ok", 5),
            _observation("c4", 0, "ok", 9),
        ],
    )

    ungrouped = compare_numeric_metric(baseline, current, bootstrap_replicates=200, seed=9)
    grouped = compare_numeric_metric(
        baseline,
        current,
        case_groups={"c1": "g1", "c2": "g1", "c3": "g2", "c4": "g2"},
        bootstrap_replicates=200,
        seed=9,
    )

    assert ungrouped["uncertainty"]["independent_unit"] == "explicit_group_id_or_case_id"
    assert ungrouped["uncertainty"]["independent_unit_count"] == 4
    assert grouped["uncertainty"]["independent_unit_count"] == 2
    assert [
        (group["group_id"], group["mean_current_minus_baseline"]) for group in grouped["groups"]
    ] == [
        ("g1", 2.0),
        ("g2", 7.0),
    ]
    assert grouped["case_macro"]["mean_current_minus_baseline"] == 4.5
    assert grouped["uncertainty"]["estimate"] == 4.5
    assert grouped["uncertainty"]["lower"] <= 4.5 <= grouped["uncertainty"]["upper"]
    assert any(
        "not independent within a group" in note for note in grouped["uncertainty"]["assumptions"]
    )


def test_seeded_interval_and_all_reports_are_input_permutation_invariant() -> None:
    selected = [("c1", 0), ("c2", 0), ("c3", 0)]
    baseline_rows = [
        _observation("c1", 0, "ok", 0),
        _observation("c2", 0, "ok", 0),
        _observation("c3", 0, "ok", 0),
    ]
    current_rows = [
        _observation("c1", 0, "ok", 1),
        _observation("c2", 0, "ok", 4),
        _observation("c3", 0, "ok", 9),
    ]
    baseline = _side("baseline", selected, baseline_rows)
    current = _side("current", selected, current_rows)
    first = compare_numeric_metric(
        baseline,
        current,
        case_groups={"c3": "b", "c1": "a", "c2": "b"},
        bootstrap_replicates=300,
        seed=12345,
    )
    permuted = compare_numeric_metric(
        _side("baseline", reversed(selected), reversed(baseline_rows)),
        _side("current", reversed(selected), reversed(current_rows)),
        case_groups={"c2": "b", "c1": "a", "c3": "b"},
        bootstrap_replicates=300,
        seed=12345,
    )
    repeated = compare_numeric_metric(
        baseline,
        current,
        case_groups={"c1": "a", "c2": "b", "c3": "b"},
        bootstrap_replicates=300,
        seed=12345,
    )

    assert first == repeated
    assert first == permuted
    assert first["uncertainty"]["seed"] == 12345
    assert first["uncertainty"]["replicate_count"] == 300
    assert first["uncertainty"]["method"] == "cluster_bootstrap_percentile"


def test_constant_group_effect_has_a_degenerate_interval() -> None:
    selected = [("c1", 0), ("c2", 0), ("c3", 0)]
    baseline = _side("baseline", selected, [_observation(case, 0, "ok", 4) for case, _ in selected])
    current = _side("current", selected, [_observation(case, 0, "ok", 6) for case, _ in selected])

    report = compare_numeric_metric(baseline, current, bootstrap_replicates=137, seed=8)
    assert report["uncertainty"]["independent_unit_count"] == 3
    assert report["uncertainty"]["lower"] == 2.0
    assert report["uncertainty"]["upper"] == 2.0
    assert report["uncertainty"]["reason"] is None


def test_zero_and_one_independent_group_report_why_no_interval_exists() -> None:
    empty = compare_numeric_metric(
        _side("baseline", (), ()),
        _side("current", (), ()),
        bootstrap_replicates=10,
    )
    assert empty["case_macro"]["mean_current_minus_baseline"] is None
    assert empty["uncertainty"]["independent_unit_count"] == 0
    assert empty["uncertainty"]["lower"] is None
    assert empty["uncertainty"]["upper"] is None
    assert empty["uncertainty"]["reason"] == "no_independent_groups"

    one = compare_numeric_metric(
        _side("baseline", [("c1", 0)], [_observation("c1", 0, "ok", 1)]),
        _side("current", [("c1", 0)], [_observation("c1", 0, "ok", 2)]),
        bootstrap_replicates=10,
    )
    assert one["case_macro"]["mean_current_minus_baseline"] == 1.0
    assert one["uncertainty"]["estimate"] == 1.0
    assert one["uncertainty"]["independent_unit_count"] == 1
    assert one["uncertainty"]["reason"] == "requires_at_least_two_independent_groups"

    direct = cluster_bootstrap_percentile_interval({"only": 3.0}, replicate_count=10)
    assert direct["lower"] is None
    assert direct["reason"] == "requires_at_least_two_independent_groups"


def test_nonnumeric_boolean_score_incompatible_identity_and_bad_seed_are_rejected() -> None:
    with pytest.raises(TypeError, match="must be numeric"):
        _observation("c1", 0, "ok", "0.5")
    with pytest.raises(TypeError, match="not bool"):
        _observation("c1", 0, "ok", True)
    with pytest.raises(ValueError, match="finite"):
        _observation("c1", 0, "ok", float("inf"))
    with pytest.raises(TypeError, match="repetition_id"):
        PairKey("c1", True)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="not bool"):
        cluster_bootstrap_percentile_interval(
            {"g1": 1.0, "g2": 2.0},
            replicate_count=10,
            seed=True,  # type: ignore[arg-type]
        )

    other_metric = MetricIdentity("other.metric", "1.0.0", "binding-2", "lower")
    baseline = _side("b", [("c", 0)], [_observation("c", 0, "ok", 1)])
    current = _side(
        "c",
        [("c", 0)],
        [_observation("c", 0, "ok", 2)],
        metric=other_metric,
    )
    with pytest.raises(ValueError, match="metric identity"):
        compare_numeric_metric(baseline, current)


@pytest.mark.parametrize(
    "status",
    ["missing", "error", "not_applicable", "skipped", "cancelled", "unavailable"],
)
def test_every_coverage_loss_rejects_a_numeric_substitute(status: ObservationStatus) -> None:
    key = PairKey("case-1", 0)
    comparison_observation = NumericObservation(key, status, None)
    judge_observation = JudgeObservation(
        ExecutionKey("execution-1", 0), "judge-a", status, None, "not_evaluated"
    )
    assert comparison_observation.value is None
    assert judge_observation.value is None

    with pytest.raises(ValueError, match="coverage loss"):
        NumericObservation(key, status, 0)
    with pytest.raises(ValueError, match="coverage loss"):
        JudgeObservation(ExecutionKey("execution-1", 0), "judge-a", status, 0, "not_evaluated")


@pytest.mark.parametrize(
    ("direction", "baseline", "current", "expected_delta", "expected_favored"),
    [
        ("higher", 1.0, 3.0, 2.0, "current"),
        ("lower", 3.0, 1.0, -2.0, "current"),
        ("target", 3.0, 1.0, -2.0, None),
        ("none", 3.0, 1.0, -2.0, None),
    ],
)
def test_direction_is_reported_without_converting_the_metric(
    direction: MetricDirection,
    baseline: float,
    current: float,
    expected_delta: float,
    expected_favored: str | None,
) -> None:
    metric = MetricIdentity("example.metric", "1.0.0", "binding", direction=direction)
    report = compare_numeric_metric(
        _side(
            "baseline",
            [("c1", 0)],
            [_observation("c1", 0, "ok", baseline)],
            metric=metric,
        ),
        _side(
            "current",
            [("c1", 0)],
            [_observation("c1", 0, "ok", current)],
            metric=metric,
        ),
        bootstrap_replicates=10,
    )

    assert report["difference_definition"] == "current - baseline"
    assert report["case_macro"]["mean_current_minus_baseline"] == expected_delta
    handling = report["direction_handling"]
    assert handling["direction"] == direction
    assert handling["favored_side_by_raw_mean_difference"] == expected_favored
    assert handling["unit_conversion_applied"] is False


def test_repeated_judge_status_decision_score_and_pass_distribution() -> None:
    key = ExecutionKey("execution-1", 0)
    observations = [
        JudgeObservation(key, "pass-3", "ok", 0.9, "fail"),
        JudgeObservation(key, "pass-1", "ok", 0.7, "pass"),
        JudgeObservation(key, "pass-2", "ok", 0.8, "pass"),
    ]

    report = summarize_judge_stability(
        observations,
        expected_units=[key],
        expected_scoring_ids=["pass-3", "pass-1", "pass-2"],
    )

    assert report["denominators"] == {
        "expected_unit_count": 1,
        "observed_unit_count": 1,
        "repeated_unit_count": 1,
        "under_repeated_unit_count": 0,
        "observed_pass_count": 3,
        "missing_repeat_count": 0,
    }
    assert report["status_stability"]["stable_unit_count"] == 1
    assert report["status_stability"]["stable_unit_rate"] == 1.0
    decision = report["decision_agreement"]
    assert decision["eligible_repeated_unit_count"] == 1
    assert decision["all_agreement_unit_count"] == 0
    assert decision["decision_pair_count"] == 3
    assert decision["agreeing_decision_pair_count"] == 1
    assert decision["pairwise_agreement"] == pytest.approx(1 / 3)
    scores = report["numeric_scores"]
    assert scores["mean_spread"] == pytest.approx(0.2)
    assert scores["max_spread"] == pytest.approx(0.2)
    assert scores["mean_variance"] == pytest.approx(0.006666666667)
    assert scores["variance_definition"] == "population variance within each execution unit"
    assert report["pass_count_distribution"]["counts"] == {
        "0": 0,
        "1": 0,
        "2": 1,
        "3": 0,
    }
    assert report["units"][0]["status_stability"]["stable"] is True
    assert json.dumps(report)


def test_repeated_judge_marks_status_instability_without_scoring_the_error() -> None:
    key = ExecutionKey("execution-1", 0)
    report = summarize_judge_stability(
        [
            JudgeObservation(key, "judge-a", "ok", 0.7, "pass"),
            JudgeObservation(key, "judge-b", "error", None, "not_evaluated"),
        ],
        expected_repeats=2,
    )

    assert report["denominators"]["repeated_unit_count"] == 1
    assert report["status_stability"]["stable_unit_count"] == 0
    assert report["status_stability"]["unstable_unit_count"] == 1
    assert report["status_stability"]["stable_unit_rate"] == 0.0
    assert report["decision_agreement"]["eligible_repeated_unit_count"] == 0
    assert report["numeric_scores"]["reason"] == (
        "requires_at_least_two_numeric_ok_repeats_per_unit"
    )
    assert report["units"][0]["scoring_passes"][1]["value"] is None


def test_repeated_judge_missing_passes_and_missing_units_are_explicit() -> None:
    e1 = ExecutionKey("execution-1", 0)
    e2 = ExecutionKey("execution-2", 0)
    e3 = ExecutionKey("execution-3", 0)
    observations = [
        JudgeObservation(e1, "judge-a", "ok", 0.5, "pass"),
        JudgeObservation(e2, "judge-a", "ok", 0.4, "pass"),
        JudgeObservation(e2, "judge-b", "ok", 0.6, "pass"),
    ]

    report = summarize_judge_stability(
        observations,
        expected_units=[e3, e1, e2],
        expected_scoring_ids=["judge-b", "judge-a"],
    )

    assert report["denominators"] == {
        "expected_unit_count": 3,
        "observed_unit_count": 2,
        "repeated_unit_count": 1,
        "under_repeated_unit_count": 2,
        "observed_pass_count": 3,
        "missing_repeat_count": 3,
    }
    missing = {(row["execution_id"], row["scoring_id"]) for row in report["missing_repeats"]}
    assert missing == {
        ("execution-1", "judge-b"),
        ("execution-3", "judge-a"),
        ("execution-3", "judge-b"),
    }
    assert report["status_stability"]["eligible_repeated_unit_count"] == 1
    assert report["decision_agreement"]["pairwise_agreement"] == 1.0
    assert report["numeric_scores"]["mean_variance"] == pytest.approx(0.01)
    assert report["pass_count_distribution"]["counts"] == {"0": 1, "1": 1, "2": 1}
    assert report["observation_count_distribution"]["counts"] == {"0": 1, "1": 1, "2": 1}
    e3 = next(row for row in report["units"] if row["execution_id"] == "execution-3")
    assert e3["status_counts"]["missing"] == 0
    assert e3["missing_pass_count"] == 2


def test_repeated_judge_rejects_non_numeric_scores_and_duplicate_passes() -> None:
    key = ExecutionKey("execution-1", 0)
    with pytest.raises(TypeError, match="not bool"):
        JudgeObservation(key, "judge-a", "ok", True, "pass")
    with pytest.raises(TypeError, match="must be numeric"):
        JudgeObservation(key, "judge-a", "ok", "high", "pass")
    with pytest.raises(ValueError, match="coverage loss"):
        JudgeObservation(key, "judge-a", "error", 0.0, "not_evaluated")
    duplicate = [JudgeObservation(key, "judge-a", "ok", 0.5, "pass")] * 2
    with pytest.raises(ValueError, match="same execution/repetition and scoring_id"):
        summarize_judge_stability(duplicate)


def test_repeated_judge_infers_uneven_pass_counts_and_keeps_placeholders_explicit() -> None:
    e1 = ExecutionKey("execution-1", 0)
    e2 = ExecutionKey("execution-2", 0)
    report = summarize_judge_stability(
        [
            JudgeObservation(e1, "judge-a", "ok", None, "pass"),
            JudgeObservation(e2, "judge-a", "ok", 0.4, "pass"),
            JudgeObservation(e2, "judge-b", "ok", 0.6, "pass"),
        ],
        expected_units=[e1, e2],
    )

    assert report["expectation"]["basis"] == "inferred_max_observed"
    assert report["expectation"]["expected_repeat_count_per_unit"] == 2
    assert report["denominators"]["missing_repeat_count"] == 1
    assert report["missing_repeats"][0]["execution_id"] == "execution-1"
    assert report["missing_repeats"][0]["scoring_id"] is None
    assert report["pass_count_distribution"]["counts"] == {"0": 0, "1": 1, "2": 1}
    assert json.dumps(report)


def test_explicit_empty_expected_units_still_report_observed_units() -> None:
    observed = ExecutionKey("execution-1", 0)
    report = summarize_judge_stability(
        [JudgeObservation(observed, "judge-a", "ok", 0.5, "pass")],
        expected_units=[],
        expected_scoring_ids=["judge-a"],
    )
    assert report["denominators"]["expected_unit_count"] == 0
    assert report["denominators"]["observed_unit_count"] == 1
    assert report["unexpected_units"] == [observed.as_dict()]
