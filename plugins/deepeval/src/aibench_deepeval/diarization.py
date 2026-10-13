"""Deterministic episode-level diarization error rate over recorded annotations."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench_deepeval._version import __version__
from aibench_deepeval.voice import (
    MAX_AUDIO_DURATION_SECONDS,
    MAX_CASE_AUDIO_BYTES,
    MAX_VOICE_TURNS,
    _decode_wav,
)

MAX_SEGMENTS_PER_TURN = 128
MAX_SEGMENTS_PER_EPISODE = 512
MAX_SPEAKERS_PER_EPISODE = 64
MAX_EPISODE_AUDIO_BYTES = 4 * 1024 * 1024
MAX_SPEAKER_ID_CHARS = 64
MAX_COLLAR_MS = 1000


class _EvidenceLimit(ValueError):
    """An explicit evidence cap was exceeded before scoring."""


class DiarizationErrorRate(Evaluator):
    """Compute time-based DER from human reference and recorded hypothesis segments."""

    manifest = EvaluatorManifest(
        evaluator_id="deepeval.diarization_error_rate",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="aibench-deepeval",
        package_version=__version__,
        description=(
            "Deterministic diarization error rate over recorded speaker-labeled time segments; "
            "lower values are better."
        ),
        limitations=(
            "Evaluates supplied reference and application speaker annotations; it does not detect speakers or transcribe audio.",
            "Every episode turn must contain a reference annotation, an application annotation, and inline 16-bit PCM WAV audio to bound the timeline.",
            "Speaker labels are matched by one optimal permutation across the episode; the metric does not verify real-world speaker identity.",
            "Overlap is included by default. `skip_overlap` removes reference overlap from evaluation; `collar_ms` removes a total-width region centered on each reference active-speaker-set boundary.",
            "Episodes are limited to 50 turns, 128 segments per annotation per turn, 512 segments per annotation per episode, 64 speakers per side, and 4 MiB of decoded input audio.",
            f"Only inline PCM WAV up to {MAX_CASE_AUDIO_BYTES} bytes per input clip and {MAX_AUDIO_DURATION_SECONDS} seconds is accepted.",
        ),
        concepts=("speaker_diarization",),
        value_kind="scalar",
        direction=MetricDirection.LOWER,
        aggregation="mean",
        requires=(
            FieldRequirement(path="case.group_id"),
            FieldRequirement(path="episode.turns"),
        ),
        default_rule=DecisionRule(comparator="<=", threshold=0.2),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "collar_ms": {"type": "integer", "minimum": 0, "maximum": MAX_COLLAR_MS},
                "skip_overlap": {"type": "boolean"},
            },
        },
        uses_models=False,
        network_destinations=(),
        internal_retries=0,
        internal_concurrency=1,
        requires_worker=True,
    )

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        del ctx
        collar_ms = self.params.get("collar_ms", 0)
        skip_overlap = self.params.get("skip_overlap", False)
        if (
            isinstance(collar_ms, bool)
            or not isinstance(collar_ms, int)
            or not 0 <= collar_ms <= MAX_COLLAR_MS
            or not isinstance(skip_overlap, bool)
        ):
            return EvaluationOutcome.error("invalid_diarization_parameters")

        episode = view.get("episode.turns")
        if not isinstance(episode, (list, tuple)) or not episode:
            return EvaluationOutcome.not_applicable("missing:episode.turns")
        if len(episode) > MAX_VOICE_TURNS:
            return EvaluationOutcome.not_applicable("oversized:episode.turns")

        try:
            annotations, decoded_audio_bytes, reference_count, hypothesis_count = (
                _episode_annotations(episode)
            )
        except _EvidenceLimit as exc:
            return EvaluationOutcome.not_applicable(str(exc))
        except (AttributeError, TypeError, ValueError, OSError, EOFError):
            return EvaluationOutcome.not_applicable("invalid:episode.diarization_evidence")
        if not annotations:
            return EvaluationOutcome.not_applicable("missing:episode.diarization_evidence")
        if decoded_audio_bytes > MAX_EPISODE_AUDIO_BYTES:
            return EvaluationOutcome.not_applicable("oversized:episode.diarization_audio")
        if reference_count > MAX_SEGMENTS_PER_EPISODE:
            return EvaluationOutcome.not_applicable("oversized:episode.reference_segments")
        if hypothesis_count > MAX_SEGMENTS_PER_EPISODE:
            return EvaluationOutcome.not_applicable("oversized:episode.hypothesis_segments")

        collar_spans = [_collars(reference, collar_ms) for reference, _, _ in annotations]
        intervals: list[tuple[int, frozenset[str], frozenset[str]]] = []
        overlaps: dict[tuple[str, str], int] = defaultdict(int)
        reference_speakers: set[str] = set()
        hypothesis_speakers: set[str] = set()
        for (reference, hypothesis, duration_ms), collars in zip(annotations, collar_spans):
            reference_speakers.update(segment[2] for segment in reference)
            hypothesis_speakers.update(segment[2] for segment in hypothesis)
            turn_intervals = _turn_intervals(
                reference,
                hypothesis,
                duration_ms,
                collars,
                skip_overlap=skip_overlap,
            )
            intervals.extend(turn_intervals)
            for duration, active_reference, active_hypothesis in turn_intervals:
                for reference_id in active_reference:
                    for hypothesis_id in active_hypothesis:
                        overlaps[(reference_id, hypothesis_id)] += duration

        if len(reference_speakers) > MAX_SPEAKERS_PER_EPISODE:
            return EvaluationOutcome.not_applicable("oversized:episode.reference_speakers")
        if len(hypothesis_speakers) > MAX_SPEAKERS_PER_EPISODE:
            return EvaluationOutcome.not_applicable("oversized:episode.hypothesis_speakers")

        mapping = _speaker_mapping(reference_speakers, hypothesis_speakers, overlaps)
        false_alarm_ms = missed_detection_ms = confusion_ms = reference_speech_ms = 0
        for duration, active_reference, active_hypothesis in intervals:
            active_reference_count = len(active_reference)
            active_hypothesis_count = len(active_hypothesis)
            reference_speech_ms += active_reference_count * duration
            false_alarm_ms += max(active_hypothesis_count - active_reference_count, 0) * duration
            missed_detection_ms += (
                max(active_reference_count - active_hypothesis_count, 0) * duration
            )
            correct = sum(
                1
                for hypothesis_id in active_hypothesis
                if mapping.get(hypothesis_id) in active_reference
            )
            confusion_ms += (
                max(0, min(active_reference_count, active_hypothesis_count) - correct) * duration
            )

        if reference_speech_ms == 0:
            return EvaluationOutcome.not_applicable("no_reference_speech")
        der = (false_alarm_ms + missed_detection_ms + confusion_ms) / reference_speech_ms
        if not math.isfinite(der):
            return EvaluationOutcome.error("invalid_diarization_score")
        return EvaluationOutcome.ok(
            "scalar",
            der,
            evidence=("case.group_id", "episode.turns"),
            raw={
                "metric": "DiarizationErrorRate",
                "false_alarm_ms": false_alarm_ms,
                "missed_detection_ms": missed_detection_ms,
                "confusion_ms": confusion_ms,
                "reference_speaker_ms": reference_speech_ms,
                "reference_segments": reference_count,
                "hypothesis_segments": hypothesis_count,
                "reference_speakers": len(reference_speakers),
                "hypothesis_speakers": len(hypothesis_speakers),
                "turns_scored": len(annotations),
                "collar_ms": collar_ms,
                "skip_overlap": skip_overlap,
                "decoded_audio_bytes": decoded_audio_bytes,
                "der": der,
            },
        )


def _episode_annotations(
    episode: list[Any] | tuple[Any, ...],
) -> tuple[list[tuple[list[tuple[int, int, str]], list[tuple[int, int, str]], int]], int, int, int]:
    annotations: list[tuple[list[tuple[int, int, str]], list[tuple[int, int, str]], int]] = []
    decoded_audio_bytes = reference_count = hypothesis_count = 0
    reference_speakers: set[str] = set()
    hypothesis_speakers: set[str] = set()
    for index, turn in enumerate(episode):
        if not isinstance(turn, dict):
            raise TypeError("episode turn must be an object")
        case_input = turn.get("input")
        if not isinstance(case_input, dict) or not {"audio", "diarization_reference"} <= set(
            case_input
        ):
            return [], decoded_audio_bytes, reference_count, hypothesis_count
        _, size, duration = _decode_wav(
            case_input["audio"], f"episode.turn.{index}.user.audio", MAX_CASE_AUDIO_BYTES
        )
        decoded_audio_bytes += size
        if decoded_audio_bytes > MAX_EPISODE_AUDIO_BYTES:
            raise _EvidenceLimit("oversized:episode.diarization_audio")
        duration_ms = duration * 1000

        output = turn.get("output")
        if not isinstance(output, dict) or "diarization" not in output:
            return [], decoded_audio_bytes, reference_count, hypothesis_count
        reference = _segments(case_input["diarization_reference"], duration_ms)
        hypothesis = _segments(output["diarization"], duration_ms)
        if len(reference) > MAX_SEGMENTS_PER_TURN or len(hypothesis) > MAX_SEGMENTS_PER_TURN:
            raise _EvidenceLimit("oversized:episode.turn_segments")
        reference_speakers.update(segment[2] for segment in reference)
        hypothesis_speakers.update(segment[2] for segment in hypothesis)
        if len(reference_speakers) > MAX_SPEAKERS_PER_EPISODE:
            raise _EvidenceLimit("oversized:episode.reference_speakers")
        if len(hypothesis_speakers) > MAX_SPEAKERS_PER_EPISODE:
            raise _EvidenceLimit("oversized:episode.hypothesis_speakers")
        reference_count += len(reference)
        hypothesis_count += len(hypothesis)
        if reference_count > MAX_SEGMENTS_PER_EPISODE:
            raise _EvidenceLimit("oversized:episode.reference_segments")
        if hypothesis_count > MAX_SEGMENTS_PER_EPISODE:
            raise _EvidenceLimit("oversized:episode.hypothesis_segments")
        annotations.append((reference, hypothesis, math.floor(duration_ms)))
    return annotations, decoded_audio_bytes, reference_count, hypothesis_count


def _segments(value: Any, duration_ms: float) -> list[tuple[int, int, str]]:
    if not isinstance(value, dict) or set(value) != {"segments"}:
        raise ValueError("annotation must contain only segments")
    raw_segments = value["segments"]
    if not isinstance(raw_segments, list):
        raise TypeError("segments must be a list")
    segments: list[tuple[int, int, str]] = []
    for raw in raw_segments:
        if not isinstance(raw, dict) or set(raw) != {"start_ms", "end_ms", "speaker_id"}:
            raise ValueError("diarization segment has an invalid shape")
        start = raw["start_ms"]
        end = raw["end_ms"]
        speaker_id = raw["speaker_id"]
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or start < 0
            or end <= start
            or end > duration_ms
        ):
            raise ValueError("diarization timestamps are invalid or outside the audio clip")
        if (
            not isinstance(speaker_id, str)
            or not speaker_id.strip()
            or len(speaker_id) > MAX_SPEAKER_ID_CHARS
            or not speaker_id.isprintable()
        ):
            raise ValueError("diarization speaker_id is invalid")
        segments.append((start, end, speaker_id))
    return segments


def _collars(reference: list[tuple[int, int, str]], collar_ms: int) -> list[tuple[int, int]]:
    if collar_ms == 0:
        return []
    events: dict[int, Counter[str]] = defaultdict(Counter)
    for start, end, speaker_id in reference:
        events[start][speaker_id] += 1
        events[end][speaker_id] -= 1
    active: Counter[str] = Counter()
    previous: frozenset[str] = frozenset()
    boundaries: list[int] = []
    for timestamp in sorted(events):
        for speaker_id, delta in events[timestamp].items():
            active[speaker_id] += delta
            if active[speaker_id] <= 0:
                active.pop(speaker_id, None)
        current = frozenset(active)
        if current != previous:
            boundaries.append(timestamp)
            previous = current
    before = collar_ms // 2
    after = collar_ms - before
    spans = [(max(0, point - before), point + after) for point in boundaries]
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _turn_intervals(
    reference: list[tuple[int, int, str]],
    hypothesis: list[tuple[int, int, str]],
    duration_ms: int,
    collars: list[tuple[int, int]],
    *,
    skip_overlap: bool,
) -> list[tuple[int, frozenset[str], frozenset[str]]]:
    reference_events: dict[int, Counter[str]] = defaultdict(Counter)
    hypothesis_events: dict[int, Counter[str]] = defaultdict(Counter)
    for start, end, speaker_id in reference:
        reference_events[start][speaker_id] += 1
        reference_events[end][speaker_id] -= 1
    for start, end, speaker_id in hypothesis:
        hypothesis_events[start][speaker_id] += 1
        hypothesis_events[end][speaker_id] -= 1
    points = {0, duration_ms, *reference_events, *hypothesis_events}
    for start, end in collars:
        points.add(min(start, duration_ms))
        points.add(min(end, duration_ms))
    ordered = sorted(points)
    active_reference: Counter[str] = Counter()
    active_hypothesis: Counter[str] = Counter()
    intervals: list[tuple[int, frozenset[str], frozenset[str]]] = []
    collar_index = 0
    for index, start in enumerate(ordered[:-1]):
        for speaker_id, delta in reference_events.get(start, {}).items():
            active_reference[speaker_id] += delta
            if active_reference[speaker_id] <= 0:
                active_reference.pop(speaker_id, None)
        for speaker_id, delta in hypothesis_events.get(start, {}).items():
            active_hypothesis[speaker_id] += delta
            if active_hypothesis[speaker_id] <= 0:
                active_hypothesis.pop(speaker_id, None)
        end = ordered[index + 1]
        if end <= start:
            continue
        while collar_index < len(collars) and collars[collar_index][1] <= start:
            collar_index += 1
        in_collar = (
            collar_index < len(collars)
            and collars[collar_index][0] < end
            and collars[collar_index][1] > start
        )
        if in_collar or (skip_overlap and len(active_reference) > 1):
            continue
        intervals.append((end - start, frozenset(active_reference), frozenset(active_hypothesis)))
    return intervals


def _speaker_mapping(
    reference_speakers: set[str],
    hypothesis_speakers: set[str],
    overlaps: dict[tuple[str, str], int],
) -> dict[str, str]:
    """Maximum-weight one-to-one assignment using the Hungarian algorithm."""
    references = sorted(reference_speakers)
    hypotheses = sorted(hypothesis_speakers)
    size = max(len(references), len(hypotheses))
    if size == 0:
        return {}
    weights = [
        [
            overlaps.get((references[row], hypotheses[column]), 0)
            if row < len(references) and column < len(hypotheses)
            else 0
            for column in range(size)
        ]
        for row in range(size)
    ]
    maximum = max(max(row) for row in weights)
    costs = [[maximum - weight for weight in row] for row in weights]
    row_potential = [0] * (size + 1)
    column_potential = [0] * (size + 1)
    column_row = [0] * (size + 1)
    previous_column = [0] * (size + 1)
    for row in range(1, size + 1):
        column_row[0] = row
        current_column = 0
        minimum = [math.inf] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[current_column] = True
            current_row = column_row[current_column]
            delta = math.inf
            next_column = 0
            for column in range(1, size + 1):
                if used[column]:
                    continue
                reduced = (
                    costs[current_row - 1][column - 1]
                    - row_potential[current_row]
                    - column_potential[column]
                )
                if reduced < minimum[column]:
                    minimum[column] = reduced
                    previous_column[column] = current_column
                if minimum[column] < delta:
                    delta = minimum[column]
                    next_column = column
            for column in range(size + 1):
                if used[column]:
                    row_potential[column_row[column]] += int(delta)
                    column_potential[column] -= int(delta)
                else:
                    minimum[column] -= delta
            current_column = next_column
            if column_row[current_column] == 0:
                break
        while True:
            next_column = previous_column[current_column]
            column_row[current_column] = column_row[next_column]
            current_column = next_column
            if current_column == 0:
                break
    mapping: dict[str, str] = {}
    for column in range(1, size + 1):
        row = column_row[column]
        if 1 <= row <= len(references) and column <= len(hypotheses):
            mapping[hypotheses[column - 1]] = references[row - 1]
    return mapping


DIARIZATION_EVALUATORS = (DiarizationErrorRate,)
