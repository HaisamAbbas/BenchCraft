"""Storage-free statistical primitives for compatible metric comparisons.

The functions in this module operate on small value objects.  They do not load runs,
read storage, invoke evaluators, or depend on the rest of :mod:`aibench`.  A storage-facing
service can therefore map its records to these objects and serialize the returned plain
``dict`` values directly.

Two rules are deliberately strict:

* a non-``ok`` observation is a coverage loss and cannot carry a numeric value;
* both sides of a comparison must name the same scalar metric identity and direction.

Comparisons are paired by ``(case_id, repetition_id)``.  Numeric differences are averaged
within a case first, so cases with more repetitions do not receive more weight.  The
uncertainty calculation resamples whole groups, where a group is an explicit ``group_id``
or, when none was supplied, the ``case_id`` itself.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import combinations
from typing import Any, Literal, get_args

ObservationStatus = Literal[
    "ok",
    "missing",
    "error",
    "not_applicable",
    "skipped",
    "cancelled",
    "unavailable",
]
MetricDirection = Literal["higher", "lower", "target", "none"]
JudgeDecision = Literal["pass", "fail", "indeterminate", "not_evaluated"]

_STATUSES: tuple[ObservationStatus, ...] = get_args(ObservationStatus)
_DIRECTIONS: tuple[MetricDirection, ...] = get_args(MetricDirection)
_DECISIONS: tuple[JudgeDecision, ...] = get_args(JudgeDecision)
_ROUND_PLACES = 12

__all__ = [
    "COMPARISON_SCHEMA",
    "JUDGE_STABILITY_SCHEMA",
    "ComparisonSide",
    "ExecutionKey",
    "JudgeObservation",
    "MetricIdentity",
    "NumericObservation",
    "PairKey",
    "PairedObservation",
    "cluster_bootstrap_percentile_interval",
    "compare_numeric_metric",
    "construct_paired_observations",
    "summarize_judge_stability",
]


COMPARISON_SCHEMA = "aibench.paired-comparison/1"
JUDGE_STABILITY_SCHEMA = "aibench.judge-stability/1"


def _nonempty_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _nonnegative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer, not bool")
    if value < 0:
        raise ValueError(f"{name} must be non-negative")
    return value


def _finite_number(value: object, name: str) -> float:
    # bool is an int subclass, but a judge decision flag is not a score.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric, not bool or {type(value).__name__}")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def _finite_difference(current: float, baseline: float) -> float:
    difference = current - baseline
    if not math.isfinite(difference):
        raise ValueError("current - baseline is not finite")
    return difference


def _rounded(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("statistical result is not finite")
    return round(value, _ROUND_PLACES)


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else _rounded(numerator / denominator)


def _counter_with_statuses(values: Sequence[ObservationStatus]) -> dict[str, int]:
    counts = Counter(values)
    return {status: counts[status] for status in _STATUSES}


@dataclass(frozen=True, slots=True, order=True)
class PairKey:
    """Identity of one application repetition within a case."""

    case_id: str
    repetition_id: int

    def __post_init__(self) -> None:
        _nonempty_string(self.case_id, "case_id")
        _nonnegative_integer(self.repetition_id, "repetition_id")

    def as_dict(self) -> dict[str, str | int]:
        return {"case_id": self.case_id, "repetition_id": self.repetition_id}


@dataclass(frozen=True, slots=True, order=True)
class ExecutionKey:
    """Identity used to align repeated scoring passes for one application execution."""

    execution_id: str
    repetition_id: int

    def __post_init__(self) -> None:
        _nonempty_string(self.execution_id, "execution_id")
        _nonnegative_integer(self.repetition_id, "repetition_id")

    def as_dict(self) -> dict[str, str | int]:
        return {"execution_id": self.execution_id, "repetition_id": self.repetition_id}


@dataclass(frozen=True, slots=True)
class MetricIdentity:
    """Identity that must match exactly before numeric values are compared."""

    metric_id: str
    metric_version: str
    binding_hash: str
    direction: MetricDirection = "none"
    value_kind: Literal["scalar", "boolean"] = "scalar"

    def __post_init__(self) -> None:
        _nonempty_string(self.metric_id, "metric_id")
        _nonempty_string(self.metric_version, "metric_version")
        _nonempty_string(self.binding_hash, "binding_hash")
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"direction must be one of {_DIRECTIONS}, got {self.direction!r}")
        if self.value_kind not in {"scalar", "boolean"}:
            raise ValueError("only scalar or boolean numeric metric identities can be compared")

    def as_dict(self) -> dict[str, str]:
        return {
            "metric_id": self.metric_id,
            "metric_version": self.metric_version,
            "binding_hash": self.binding_hash,
            "direction": self.direction,
            "value_kind": self.value_kind,
        }


@dataclass(frozen=True, slots=True)
class NumericObservation:
    """One selected metric item.

    ``ok`` requires a finite numeric value.  Every other status requires ``value=None``;
    this makes accidental zero substitution impossible to validate downstream.
    """

    key: PairKey
    status: ObservationStatus
    value: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, PairKey):
            raise TypeError("key must be a PairKey")
        if self.status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {self.status!r}")
        if self.status == "ok":
            if self.value is None:
                raise TypeError("an ok observation requires a numeric value")
            object.__setattr__(self, "value", _finite_number(self.value, "value"))
        elif self.value is not None:
            raise ValueError(
                f"a {self.status} observation is a coverage loss and cannot carry a value"
            )


@dataclass(frozen=True, slots=True)
class ComparisonSide:
    """Selected keys and at most one final observation for each key on one run.

    Selected keys without an observation are counted explicitly as ``missing`` by the
    comparison.  This lets a service avoid manufacturing placeholder records while still
    preserving planned repetitions in every denominator.
    """

    run_id: str
    metric: MetricIdentity
    selected_keys: tuple[PairKey, ...]
    observations: tuple[NumericObservation, ...]

    def __post_init__(self) -> None:
        _nonempty_string(self.run_id, "run_id")
        if not isinstance(self.metric, MetricIdentity):
            raise TypeError("metric must be a MetricIdentity")
        object.__setattr__(self, "selected_keys", tuple(self.selected_keys))
        object.__setattr__(self, "observations", tuple(self.observations))


@dataclass(frozen=True, slots=True)
class PairedObservation:
    """A same-key pair, retained even when it is not a complete numeric pair."""

    key: PairKey
    group_id: str
    baseline_status: ObservationStatus
    current_status: ObservationStatus
    baseline_value: float | None
    current_value: float | None
    delta: float | None
    complete_numeric: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.key.as_dict(),
            "group_id": self.group_id,
            "baseline_status": self.baseline_status,
            "current_status": self.current_status,
            "baseline_value": self.baseline_value,
            "current_value": self.current_value,
            "delta": self.delta,
            "complete_numeric": self.complete_numeric,
        }


@dataclass(frozen=True, slots=True)
class JudgeObservation:
    """One scoring-pass observation for an application execution.

    A numeric value is optional for an ``ok`` categorical decision, but any supplied
    value must be numeric and finite.  Non-``ok`` observations cannot carry a score.
    """

    key: ExecutionKey
    scoring_id: str
    status: ObservationStatus
    value: float | None = None
    decision: JudgeDecision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.key, ExecutionKey):
            raise TypeError("key must be an ExecutionKey")
        _nonempty_string(self.scoring_id, "scoring_id")
        if self.status not in _STATUSES:
            raise ValueError(f"status must be one of {_STATUSES}, got {self.status!r}")
        if self.status == "ok":
            if self.decision not in ("pass", "fail", "indeterminate"):
                raise ValueError(
                    "an ok judge observation requires a pass, fail, or indeterminate decision"
                )
            if self.value is not None:
                object.__setattr__(self, "value", _finite_number(self.value, "value"))
        else:
            if self.value is not None:
                raise ValueError(
                    f"a {self.status} judge observation is a coverage loss and cannot carry a value"
                )
            if self.decision not in (None, "not_evaluated"):
                raise ValueError("a non-ok judge observation cannot have a pass/fail decision")


@dataclass(slots=True)
class _PreparedSide:
    side: ComparisonSide
    selected: tuple[PairKey, ...]
    records: dict[PairKey, NumericObservation]
    statuses: dict[PairKey, ObservationStatus]

    @property
    def status_counts(self) -> dict[str, int]:
        return _counter_with_statuses([self.statuses[key] for key in self.selected])


@dataclass(slots=True)
class _PreparedComparison:
    baseline: _PreparedSide
    current: _PreparedSide
    paired_keys: tuple[PairKey, ...]
    baseline_only: tuple[PairKey, ...]
    current_only: tuple[PairKey, ...]
    groups: dict[str, str]


def _prepare_side(side: ComparisonSide) -> _PreparedSide:
    if not isinstance(side, ComparisonSide):
        raise TypeError("side must be a ComparisonSide")

    ordered_selected = tuple(sorted(side.selected_keys))
    if len(set(ordered_selected)) != len(ordered_selected):
        raise ValueError(f"{side.run_id}: selected_keys contains a duplicate key")
    if not all(isinstance(key, PairKey) for key in ordered_selected):
        raise TypeError(f"{side.run_id}: every selected key must be a PairKey")

    selected = set(ordered_selected)
    records: dict[PairKey, NumericObservation] = {}
    for observation in side.observations:
        if not isinstance(observation, NumericObservation):
            raise TypeError(f"{side.run_id}: every observation must be a NumericObservation")
        if observation.key in records:
            raise ValueError(f"{side.run_id}: more than one observation for {observation.key!r}")
        if observation.key not in selected:
            raise ValueError(
                f"{side.run_id}: observation {observation.key!r} is not in selected_keys"
            )
        records[observation.key] = observation

    statuses = {
        key: records[key].status if key in records else "missing" for key in ordered_selected
    }
    return _PreparedSide(side, ordered_selected, records, statuses)


def _prepare_comparison(
    baseline: ComparisonSide,
    current: ComparisonSide,
    case_groups: Mapping[str, str | None] | None,
) -> _PreparedComparison:
    prepared_baseline = _prepare_side(baseline)
    prepared_current = _prepare_side(current)
    if prepared_baseline.side.metric != prepared_current.side.metric:
        raise ValueError(
            "metric identity, binding, scalar kind, and direction must match on both sides; "
            f"got {prepared_baseline.side.metric!r} and {prepared_current.side.metric!r}"
        )

    baseline_set = set(prepared_baseline.selected)
    current_set = set(prepared_current.selected)
    paired = tuple(sorted(baseline_set & current_set))
    baseline_only = tuple(sorted(baseline_set - current_set))
    current_only = tuple(sorted(current_set - baseline_set))

    case_ids = sorted({key.case_id for key in baseline_set | current_set})
    groups: dict[str, str] = {}
    for case_id in case_ids:
        group_id = case_groups.get(case_id) if case_groups is not None else None
        if group_id is None:
            groups[case_id] = case_id
        else:
            groups[case_id] = _nonempty_string(group_id, f"group_id for case {case_id!r}")

    return _PreparedComparison(
        prepared_baseline,
        prepared_current,
        paired,
        baseline_only,
        current_only,
        groups,
    )


def _pairs_for(prepared: _PreparedComparison) -> tuple[PairedObservation, ...]:
    pairs: list[PairedObservation] = []
    for key in prepared.paired_keys:
        left = prepared.baseline.records.get(key)
        right = prepared.current.records.get(key)
        left_status = prepared.baseline.statuses[key]
        right_status = prepared.current.statuses[key]
        complete = (
            left is not None and right is not None and left_status == "ok" and right_status == "ok"
        )
        delta: float | None = None
        if (
            complete
            and left is not None
            and right is not None
            and left.value is not None
            and right.value is not None
        ):
            delta = _finite_difference(right.value, left.value)
        pairs.append(
            PairedObservation(
                key=key,
                group_id=prepared.groups[key.case_id],
                baseline_status=left_status,
                current_status=right_status,
                baseline_value=left.value if left is not None else None,
                current_value=right.value if right is not None else None,
                delta=delta,
                complete_numeric=complete,
            )
        )
    return tuple(pairs)


def construct_paired_observations(
    baseline: ComparisonSide,
    current: ComparisonSide,
    *,
    case_groups: Mapping[str, str | None] | None = None,
) -> tuple[PairedObservation, ...]:
    """Construct deterministic same-key pairs without imputing missing observations."""

    return _pairs_for(_prepare_comparison(baseline, current, case_groups))


def _percentile(sorted_values: Sequence[float], probability: float) -> float:
    if not sorted_values:
        raise ValueError("percentile requires at least one value")
    position = (len(sorted_values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return sorted_values[lower]
    fraction = position - lower
    return sorted_values[lower] * (1.0 - fraction) + sorted_values[upper] * fraction


def cluster_bootstrap_percentile_interval(
    group_means: Mapping[str, float],
    *,
    replicate_count: int = 2_000,
    seed: int = 0,
    confidence_level: float = 0.95,
    weights: Mapping[str, int] | None = None,
    unit_name: str = "group",
    assumption_notes: Sequence[str] = (),
) -> dict[str, Any]:
    """Return a deterministic seeded cluster-bootstrap percentile interval.

    Groups are sorted before sampling, so mapping insertion order cannot change the random
    draws.  Optional positive integer weights are retained during resampling.  The
    comparison core uses case counts as weights, preserving equal weight per case after
    repetitions have first been averaged.
    """

    _nonnegative_integer(replicate_count, "replicate_count")
    if replicate_count < 1:
        raise ValueError("replicate_count must be at least 1")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer, not bool")
    level = _finite_number(confidence_level, "confidence_level")
    if not 0.0 < level < 1.0:
        raise ValueError("confidence_level must be strictly between zero and one")
    _nonempty_string(unit_name, "unit_name")

    units = tuple(sorted(group_means))
    if any(not isinstance(unit, str) or not unit for unit in units):
        raise TypeError("group_means keys must be non-empty strings")
    means = tuple(_finite_number(group_means[unit], f"group mean {unit!r}") for unit in units)
    if weights is None:
        group_weights = tuple(1 for _ in units)
        statistic = "mean_of_equal_weight_group_means"
    else:
        if set(weights) != set(units):
            raise ValueError("weights must contain exactly the group_means keys")
        group_weights = tuple(
            _nonnegative_integer(weights[unit], f"weight for group {unit!r}") for unit in units
        )
        if any(weight < 1 for weight in group_weights):
            raise ValueError("group weights must be positive")
        statistic = "weighted_mean_of_group_means"

    estimate = None
    if units:
        estimate = math.fsum(
            mean * weight for mean, weight in zip(means, group_weights, strict=True)
        ) / math.fsum(group_weights)
    metadata: dict[str, Any] = {
        "method": "cluster_bootstrap_percentile",
        "statistic": statistic,
        "confidence_level": _rounded(level),
        "percentiles": {
            "lower": _rounded((1.0 - level) / 2.0),
            "upper": _rounded(1.0 - (1.0 - level) / 2.0),
        },
        "seed": seed,
        "replicate_count": replicate_count,
        "independent_unit": unit_name,
        "independent_unit_count": len(units),
        "estimate": None if estimate is None else _rounded(estimate),
        "lower": None,
        "upper": None,
        "reason": None,
        "assumptions": [
            f"{unit_name} means are the independent resampling units",
            "groups are sampled with replacement",
            "each sampled group retains its supplied weight",
            "the percentile interval is conditional on the observed complete data",
            "no interval corrects selection bias or missingness",
        ],
    }
    for note in assumption_notes:
        if not isinstance(note, str) or not note:
            raise ValueError("assumption_notes must contain non-empty strings")
        if note not in metadata["assumptions"]:
            metadata["assumptions"].append(note)

    if not units:
        metadata["reason"] = "no_independent_groups"
        return metadata
    if len(units) < 2:
        metadata["reason"] = "requires_at_least_two_independent_groups"
        return metadata

    rng = random.Random(seed)
    replicate_estimates: list[float] = []
    for _ in range(replicate_count):
        sampled_indices = [rng.randrange(len(units)) for _ in units]
        numerator = math.fsum(means[index] * group_weights[index] for index in sampled_indices)
        denominator = math.fsum(group_weights[index] for index in sampled_indices)
        replicate_estimates.append(numerator / denominator)
    replicate_estimates.sort()
    lower_probability = (1.0 - level) / 2.0
    upper_probability = 1.0 - lower_probability
    metadata["lower"] = _rounded(_percentile(replicate_estimates, lower_probability))
    metadata["upper"] = _rounded(_percentile(replicate_estimates, upper_probability))
    return metadata


def _side_facts(prepared: _PreparedSide) -> dict[str, Any]:
    selected_cases = {key.case_id for key in prepared.selected}
    repetitions = {key.repetition_id for key in prepared.selected}
    return {
        "run_id": prepared.side.run_id,
        "selected": len(prepared.selected),
        "selected_case_count": len(selected_cases),
        "selected_repetition_ids": sorted(repetitions),
        "status_counts": prepared.status_counts,
    }


def _case_facts(
    prepared: _PreparedComparison,
    pairs: Sequence[PairedObservation],
) -> list[dict[str, Any]]:
    complete_by_case: dict[str, list[PairedObservation]] = defaultdict(list)
    selected_pair_counts = Counter(key.case_id for key in prepared.paired_keys)
    for pair in pairs:
        if pair.complete_numeric:
            complete_by_case[pair.key.case_id].append(pair)

    all_case_ids = sorted(
        {key.case_id for key in prepared.baseline.selected}
        | {key.case_id for key in prepared.current.selected}
    )
    rows: list[dict[str, Any]] = []
    for case_id in all_case_ids:
        complete = complete_by_case[case_id]
        count = len(complete)
        baseline_values = [pair.baseline_value for pair in complete]
        current_values = [pair.current_value for pair in complete]
        baseline_mean = (
            math.fsum(value for value in baseline_values if value is not None) / count
            if count
            else None
        )
        current_mean = (
            math.fsum(value for value in current_values if value is not None) / count
            if count
            else None
        )
        if baseline_mean is not None and current_mean is not None:
            delta = _finite_difference(current_mean, baseline_mean)
        else:
            delta = None
        rows.append(
            {
                "case_id": case_id,
                "group_id": prepared.groups[case_id],
                "selected_pair_count": selected_pair_counts[case_id],
                "complete_numeric_pair_count": count,
                "baseline_mean": None if baseline_mean is None else _rounded(baseline_mean),
                "current_mean": None if current_mean is None else _rounded(current_mean),
                "mean_current_minus_baseline": None if delta is None else _rounded(delta),
            }
        )
    return rows


def _group_facts(case_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in case_rows:
        grouped[str(row["group_id"])].append(row)

    result: list[dict[str, Any]] = []
    for group_id in sorted(grouped):
        rows = grouped[group_id]
        deltas = [
            float(row["mean_current_minus_baseline"])
            for row in rows
            if row["mean_current_minus_baseline"] is not None
        ]
        group_mean = math.fsum(deltas) / len(deltas) if deltas else None
        result.append(
            {
                "group_id": group_id,
                "case_ids": [str(row["case_id"]) for row in rows],
                "selected_case_count": len(rows),
                "complete_case_count": len(deltas),
                "mean_current_minus_baseline": None if group_mean is None else _rounded(group_mean),
            }
        )
    return result


def _favored_side(delta: float | None, direction: MetricDirection) -> str | None:
    if delta is None or direction in ("none", "target") or delta == 0.0:
        return None
    if direction == "higher":
        return "current" if delta > 0.0 else "baseline"
    return "current" if delta < 0.0 else "baseline"


def compare_numeric_metric(
    baseline: ComparisonSide,
    current: ComparisonSide,
    *,
    case_groups: Mapping[str, str | None] | None = None,
    bootstrap_replicates: int = 2_000,
    seed: int = 0,
    confidence_level: float = 0.95,
) -> dict[str, Any]:
    """Compare two selected scalar metric sides and return JSON-serializable facts.

    The point estimate is a case macro mean: complete repetitions are averaged within a
    case, then cases are averaged with equal weight.  Explicit groups are retained as
    bootstrap clusters; cases without an explicit group use their ``case_id`` as a
    one-case cluster.
    """

    prepared = _prepare_comparison(baseline, current, case_groups)
    pairs = _pairs_for(prepared)
    complete_pairs = [pair for pair in pairs if pair.complete_numeric]
    case_rows = _case_facts(prepared, pairs)
    complete_case_rows = [
        row for row in case_rows if row["mean_current_minus_baseline"] is not None
    ]
    group_rows = _group_facts(case_rows)

    baseline_selected = len(prepared.baseline.selected)
    current_selected = len(prepared.current.selected)
    paired_selected = len(prepared.paired_keys)
    complete_count = len(complete_pairs)
    if baseline_selected != paired_selected + len(prepared.baseline_only):
        raise RuntimeError("baseline denominator does not reconcile")
    if current_selected != paired_selected + len(prepared.current_only):
        raise RuntimeError("current denominator does not reconcile")
    if complete_count > paired_selected:
        raise RuntimeError("complete pair denominator exceeds paired selected")

    if complete_case_rows:
        case_macro_baseline = math.fsum(
            float(row["baseline_mean"]) for row in complete_case_rows
        ) / len(complete_case_rows)
        case_macro_current = math.fsum(
            float(row["current_mean"]) for row in complete_case_rows
        ) / len(complete_case_rows)
        case_macro_delta = _finite_difference(case_macro_current, case_macro_baseline)
        pair_weighted_baseline = (
            math.fsum(
                pair.baseline_value for pair in complete_pairs if pair.baseline_value is not None
            )
            / complete_count
        )
        pair_weighted_current = (
            math.fsum(
                pair.current_value for pair in complete_pairs if pair.current_value is not None
            )
            / complete_count
        )
        pair_weighted_delta = _finite_difference(pair_weighted_current, pair_weighted_baseline)
    else:
        case_macro_baseline = None
        case_macro_current = None
        case_macro_delta = None
        pair_weighted_baseline = None
        pair_weighted_current = None
        pair_weighted_delta = None

    independent_groups = {
        str(row["group_id"]): float(row["mean_current_minus_baseline"])
        for row in group_rows
        if row["mean_current_minus_baseline"] is not None
    }
    group_weights = {
        str(row["group_id"]): int(row["complete_case_count"])
        for row in group_rows
        if row["mean_current_minus_baseline"] is not None
    }
    uncertainty = cluster_bootstrap_percentile_interval(
        independent_groups,
        replicate_count=bootstrap_replicates,
        seed=seed,
        confidence_level=confidence_level,
        weights=group_weights,
        unit_name="explicit_group_id_or_case_id",
        assumption_notes=(
            "complete numeric pairs define the estimand; all coverage losses remain visible",
            "repetitions are averaged within case before cases receive equal macro weight",
            "explicit groups are treated as independent; cases are not independent within a group",
            "the raw difference current - baseline is retained in native metric units",
        ),
    )

    paired_status_counts = Counter((pair.baseline_status, pair.current_status) for pair in pairs)
    coverage = {
        "complete_numeric_pairs_over_paired_selected": _ratio(complete_count, paired_selected),
        "complete_numeric_pairs_over_baseline_selected": _ratio(complete_count, baseline_selected),
        "complete_numeric_pairs_over_current_selected": _ratio(complete_count, current_selected),
    }
    direction = baseline.metric.direction
    return {
        "schema": COMPARISON_SCHEMA,
        "comparison_scope": "one_compatible_scalar_metric_binding",
        "difference_definition": "current - baseline",
        "metric": baseline.metric.as_dict(),
        "sides": {
            "baseline": _side_facts(prepared.baseline),
            "current": _side_facts(prepared.current),
        },
        "denominators": {
            "baseline_selected": baseline_selected,
            "current_selected": current_selected,
            "paired_selected": paired_selected,
            "complete_numeric_pairs": complete_count,
            "baseline_only_key_count": len(prepared.baseline_only),
            "current_only_key_count": len(prepared.current_only),
            "baseline_only_keys": [key.as_dict() for key in prepared.baseline_only],
            "current_only_keys": [key.as_dict() for key in prepared.current_only],
            "coverage": coverage,
        },
        "paired_status_counts": {
            f"{left}->{right}": count
            for (left, right), count in sorted(paired_status_counts.items())
        },
        "case_macro": {
            "complete_case_count": len(complete_case_rows),
            "baseline_mean": None if case_macro_baseline is None else _rounded(case_macro_baseline),
            "current_mean": None if case_macro_current is None else _rounded(case_macro_current),
            "mean_current_minus_baseline": None
            if case_macro_delta is None
            else _rounded(case_macro_delta),
            "diagnostic_pair_weighted_mean_current_minus_baseline": None
            if pair_weighted_delta is None
            else _rounded(pair_weighted_delta),
        },
        "cases": case_rows,
        "groups": group_rows,
        "uncertainty": uncertainty,
        "direction_handling": {
            "direction": direction,
            "favored_side_by_raw_mean_difference": _favored_side(case_macro_delta, direction),
            "unit_conversion_applied": False,
            "generic_quality_score_calculated": False,
        },
    }


def _validate_optional_expectation(
    expected_repeats: int | None,
    expected_scoring_ids: Sequence[str] | None,
) -> tuple[int | None, tuple[str, ...] | None]:
    if expected_repeats is not None and expected_scoring_ids is not None:
        raise ValueError("provide expected_repeats or expected_scoring_ids, not both")
    if expected_repeats is not None:
        return _nonnegative_integer(expected_repeats, "expected_repeats"), None
    if expected_scoring_ids is None:
        return None, None
    ordered = tuple(sorted(expected_scoring_ids))
    if not ordered:
        raise ValueError("expected_scoring_ids must be non-empty when supplied")
    if any(not isinstance(scoring_id, str) or not scoring_id for scoring_id in ordered):
        raise TypeError("expected_scoring_ids must contain non-empty strings")
    if len(set(ordered)) != len(ordered):
        raise ValueError("expected_scoring_ids contains a duplicate")
    return len(ordered), ordered


def _distribution(
    rows: Sequence[Mapping[str, Any]],
    value_key: str,
    *,
    maximum: int,
    definition: str,
) -> dict[str, Any]:
    counts = Counter(int(row[value_key]) for row in rows)
    return {
        "definition": definition,
        "unit_count": len(rows),
        "counts": {str(value): counts[value] for value in range(maximum + 1)},
    }


def summarize_judge_stability(
    observations: Sequence[JudgeObservation],
    *,
    expected_units: Sequence[ExecutionKey] | None = None,
    expected_repeats: int | None = None,
    expected_scoring_ids: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Summarize judge repeat stability by ``execution_id`` and ``repetition_id``.

    ``expected_units`` makes entirely missing units visible.  ``expected_repeats`` or
    ``expected_scoring_ids`` makes individual missing passes visible.  If neither is
    supplied, the largest observed pass count is used and the result labels that inferred
    denominator; storage services should pass known scoring IDs when available.
    """

    explicit_repeats, scoring_ids = _validate_optional_expectation(
        expected_repeats, expected_scoring_ids
    )
    if explicit_repeats is not None and explicit_repeats < 1:
        raise ValueError("expected_repeats must be at least 1")

    by_unit: dict[ExecutionKey, list[JudgeObservation]] = defaultdict(list)
    seen_passes: set[tuple[ExecutionKey, str]] = set()
    for observation in observations:
        if not isinstance(observation, JudgeObservation):
            raise TypeError("every item must be a JudgeObservation")
        pass_identity = (observation.key, observation.scoring_id)
        if pass_identity in seen_passes:
            raise ValueError(
                "more than one observation for the same execution/repetition and scoring_id"
            )
        seen_passes.add(pass_identity)
        by_unit[observation.key].append(observation)
    for rows in by_unit.values():
        rows.sort(key=lambda observation: observation.scoring_id)

    observed_keys = set(by_unit)
    expected_key_set: set[ExecutionKey] | None = None
    if expected_units is not None:
        expected_key_set = set(expected_units)
        if any(not isinstance(key, ExecutionKey) for key in expected_units):
            raise TypeError("every expected unit must be an ExecutionKey")
        if len(expected_key_set) != len(expected_units):
            raise ValueError("expected_units contains a duplicate")
    denominator_keys = sorted(expected_key_set if expected_key_set is not None else observed_keys)
    all_keys = sorted(observed_keys | (expected_key_set or set()))

    maximum_observed = max((len(rows) for rows in by_unit.values()), default=0)
    if explicit_repeats is not None:
        expected_count = explicit_repeats
        expectation_basis = "explicit_repeat_count"
    elif scoring_ids is not None:
        expected_count = len(scoring_ids)
        expectation_basis = "explicit_scoring_ids"
    elif expected_key_set is not None:
        expected_count = max(maximum_observed, 1)
        expectation_basis = "inferred_max_observed"
    else:
        expected_count = maximum_observed
        expectation_basis = "observed_units" if observed_keys else "not_provided"

    unexpected_keys = (
        sorted(observed_keys - expected_key_set) if expected_key_set is not None else []
    )
    unit_rows: list[dict[str, Any]] = []
    missing_repeats: list[dict[str, Any]] = []
    for key in all_keys:
        rows = by_unit.get(key, [])
        observed_pass_count = len(rows)
        this_expected_count = expected_count if key in denominator_keys else observed_pass_count
        observed_ids = {row.scoring_id for row in rows}
        missing_ids = set(scoring_ids or ()) - observed_ids
        if not scoring_ids:
            missing_ids = {"" for _ in range(max(this_expected_count - observed_pass_count, 0))}
        unit_missing: list[dict[str, Any]] = []
        for scoring_id in sorted(missing_ids):
            detail = {
                **key.as_dict(),
                "scoring_id": scoring_id or None,
                "reason": "not_observed",
                "observed_pass_count": observed_pass_count,
                "expected_pass_count": this_expected_count,
            }
            unit_missing.append(detail)
            if key in denominator_keys:
                missing_repeats.append(detail)

        statuses = [row.status for row in rows]
        status_counts = _counter_with_statuses(statuses)
        status_evaluable = observed_pass_count >= 2
        status_stable = len(set(statuses)) == 1 if status_evaluable else None

        ok_rows = [row for row in rows if row.status == "ok"]
        decision_evaluable = len(ok_rows) >= 2
        decision_pairs = list(combinations(ok_rows, 2))
        agreeing_pairs = sum(left.decision == right.decision for left, right in decision_pairs)
        all_decisions_agree = (
            len({row.decision for row in ok_rows}) == 1 if decision_evaluable else None
        )

        numeric_values = [row.value for row in ok_rows if row.value is not None]
        numeric_evaluable = len(numeric_values) >= 2
        spread: float | None = None
        variance: float | None = None
        if numeric_evaluable:
            spread = max(numeric_values) - min(numeric_values)  # type: ignore[operator]
            numeric_mean = math.fsum(numeric_values) / len(numeric_values)  # type: ignore[arg-type]
            variance = math.fsum(
                (value - numeric_mean) ** 2  # type: ignore[operator]
                for value in numeric_values
            ) / len(numeric_values)
        pass_count = sum(row.decision == "pass" for row in rows)
        unexpected_ids = sorted(observed_ids - set(scoring_ids)) if scoring_ids is not None else []
        unit_rows.append(
            {
                **key.as_dict(),
                "observed_pass_count": observed_pass_count,
                "expected_pass_count": this_expected_count,
                "missing_pass_count": len(unit_missing),
                "missing_scoring_ids": [
                    row["scoring_id"] for row in unit_missing if row["scoring_id"] is not None
                ],
                "unexpected_scoring_ids": unexpected_ids,
                "status_counts": status_counts,
                "status_stability": {
                    "evaluable": status_evaluable,
                    "stable": status_stable,
                    "reason": None if status_evaluable else "requires_at_least_two_observed_passes",
                },
                "ok_repetition_count": len(ok_rows),
                "decision_agreement": {
                    "evaluable": decision_evaluable,
                    "all_decisions_agree": all_decisions_agree,
                    "decision_pair_count": len(decision_pairs),
                    "agreeing_decision_pair_count": agreeing_pairs,
                    "pairwise_agreement": _ratio(agreeing_pairs, len(decision_pairs)),
                    "reason": None if decision_evaluable else "requires_at_least_two_ok_repeats",
                },
                "numeric_repetition_count": len(numeric_values),
                "score_spread": None if spread is None else _rounded(spread),
                "score_variance": None if variance is None else _rounded(variance),
                "judge_pass_count": pass_count,
                "scoring_passes": [
                    {
                        "scoring_id": row.scoring_id,
                        "status": row.status,
                        "decision": row.decision,
                        "value": row.value,
                    }
                    for row in rows
                ],
            }
        )

    repeated_keys = [key for key in observed_keys if len(by_unit[key]) >= 2]
    stable_status_count = sum(
        len({row.status for row in by_unit[key]}) == 1 for key in repeated_keys
    )
    decision_keys = [
        key for key in repeated_keys if sum(row.status == "ok" for row in by_unit[key]) >= 2
    ]
    decision_pair_count = 0
    agreeing_decision_pair_count = 0
    all_agreement_unit_count = 0
    for key in decision_keys:
        ok_rows = [row for row in by_unit[key] if row.status == "ok"]
        all_agreement_unit_count += len({row.decision for row in ok_rows}) == 1
        pairs = list(combinations(ok_rows, 2))
        decision_pair_count += len(pairs)
        agreeing_decision_pair_count += sum(
            left.decision == right.decision for left, right in pairs
        )

    numeric_keys = [
        key
        for key in repeated_keys
        if sum(row.status == "ok" and row.value is not None for row in by_unit[key]) >= 2
    ]
    spreads: list[float] = []
    variances: list[float] = []
    for key in numeric_keys:
        values = [row.value for row in by_unit[key] if row.status == "ok" and row.value is not None]
        value_mean = math.fsum(values) / len(values)  # type: ignore[arg-type]
        spreads.append(max(values) - min(values))  # type: ignore[operator]
        variances.append(
            math.fsum((value - value_mean) ** 2 for value in values)  # type: ignore[operator]
            / len(values)
        )

    denominator_rows = [
        row
        for row in unit_rows
        if ExecutionKey(str(row["execution_id"]), int(row["repetition_id"])) in denominator_keys
    ]
    observed_pass_count = sum(len(by_unit[key]) for key in observed_keys)
    under_repeated_count = sum(
        row["observed_pass_count"] < row["expected_pass_count"] for row in denominator_rows
    )
    status_counts = Counter(row.status for rows in by_unit.values() for row in rows)
    decision_counts = Counter(
        row.decision for rows in by_unit.values() for row in rows if row.decision is not None
    )

    return {
        "schema": JUDGE_STABILITY_SCHEMA,
        "unit_key": ["execution_id", "repetition_id"],
        "expectation": {
            "basis": expectation_basis,
            "expected_repeat_count_per_unit": expected_count,
            "expected_scoring_ids": list(scoring_ids or ()),
            "note": (
                "Pass storage identifiers to identify each missing scoring pass"
                if scoring_ids is None
                else "Missing scoring passes are identified by scoring_id"
            ),
        },
        "denominators": {
            "expected_unit_count": len(denominator_keys),
            "observed_unit_count": len(observed_keys),
            "repeated_unit_count": len(repeated_keys),
            "under_repeated_unit_count": under_repeated_count,
            "observed_pass_count": observed_pass_count,
            "missing_repeat_count": len(missing_repeats),
        },
        "status_counts": {status: status_counts[status] for status in _STATUSES},
        "decision_counts": {decision: decision_counts[decision] for decision in _DECISIONS},
        "status_stability": {
            "definition": "all observed statuses agree within a unit",
            "eligible_repeated_unit_count": len(repeated_keys),
            "stable_unit_count": stable_status_count,
            "unstable_unit_count": len(repeated_keys) - stable_status_count,
            "stable_unit_rate": _ratio(stable_status_count, len(repeated_keys)),
            "not_evaluable_unit_count": len(observed_keys) - len(repeated_keys),
        },
        "decision_agreement": {
            "definition": "agreement is calculated only among ok repeats",
            "eligible_repeated_unit_count": len(decision_keys),
            "all_agreement_unit_count": all_agreement_unit_count,
            "all_agreement_unit_rate": _ratio(all_agreement_unit_count, len(decision_keys)),
            "decision_pair_count": decision_pair_count,
            "agreeing_decision_pair_count": agreeing_decision_pair_count,
            "pairwise_agreement": _ratio(agreeing_decision_pair_count, decision_pair_count),
        },
        "numeric_scores": {
            "eligible_repeated_unit_count": len(numeric_keys),
            "mean_spread": _rounded(math.fsum(spreads) / len(spreads)) if spreads else None,
            "max_spread": _rounded(max(spreads)) if spreads else None,
            "variance_definition": "population variance within each execution unit",
            "mean_variance": _rounded(math.fsum(variances) / len(variances)) if variances else None,
            "max_variance": _rounded(max(variances)) if variances else None,
            "reason": None if numeric_keys else "requires_at_least_two_numeric_ok_repeats_per_unit",
        },
        "pass_count_distribution": _distribution(
            denominator_rows,
            "judge_pass_count",
            maximum=max(
                expected_count,
                max((int(row["judge_pass_count"]) for row in denominator_rows), default=0),
            ),
            definition="number of decisions equal to pass per expected unit",
        ),
        "observation_count_distribution": _distribution(
            denominator_rows,
            "observed_pass_count",
            maximum=max(
                expected_count,
                max((int(row["observed_pass_count"]) for row in denominator_rows), default=0),
            ),
            definition="number of observed scoring passes per expected unit",
        ),
        "missing_repeats": missing_repeats,
        "unexpected_units": [key.as_dict() for key in unexpected_keys],
        "units": unit_rows,
        "assumptions": [
            "repeat agreement measures stability, not factual validity",
            "missing scoring passes are excluded from agreement and listed explicitly",
            "decision agreement uses only ok repeats",
            "missing numeric scores are excluded, never replaced with zero",
            "score variance is the population variance within an execution unit",
        ],
    }
