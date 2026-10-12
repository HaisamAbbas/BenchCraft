"""Bounded DeepEval voice metrics over stored conversational episodes.

The adapter reconstructs text and inline 16-bit PCM WAV turns from the execution history.
Call-relative timestamps come from the dataset/application record; no connector, file path,
remote audio fetch, TTS, STT, or live voice simulation is involved.
"""

from __future__ import annotations

import base64
import io
import math
import os
import wave
from dataclasses import dataclass
from typing import Any, ClassVar

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
from aibench_deepeval.faithfulness import PINNED_DEEPEVAL, _require_pinned_deepeval
from aibench_deepeval.metrics import _jsonable
from aibench_deepeval.vision import checked_text

MAX_CASE_AUDIO_BYTES = 512 * 1024
MAX_OUTPUT_AUDIO_BYTES = 1 * 1024 * 1024
MAX_EPISODE_AUDIO_BYTES = 4 * 1024 * 1024
MAX_VOICE_TURNS = 50
MAX_TIMESTAMP_MS = 24 * 60 * 60 * 1000
MAX_AUDIO_DURATION_SECONDS = 120
_EPISODE_TURNS = "episode.turns"


@dataclass(frozen=True)
class VoiceMetricSpec:
    name: str
    upstream: str
    summary: str
    concepts: tuple[str, ...]
    limitations: tuple[str, ...] = ()


VOICE_METRIC_SPECS = (
    VoiceMetricSpec(
        "turn_taking_naturalness",
        "TurnTakingNaturalnessMetric",
        "the timing and overlap between timestamped user and assistant speech",
        ("turn_taking",),
        (
            (
                "Requires call-relative start times and decodable PCM WAV duration for each "
                "audio clip; clips without these are not applicable rather than assumed sequential."
            ),
            (
                "The score is a DeepEval heuristic over supplied clips and timestamps, not a "
                "universal human-preference measure."
            ),
        ),
    ),
    VoiceMetricSpec(
        "agent_responsiveness",
        "AgentResponsivenessMetric",
        "the presence of assistant audio, the responsiveness signal represented by this adapter",
        ("responsiveness",),
        (
            (
                "The stored case format has one user turn and one assistant turn per case. "
                "It cannot represent reprompt events, call-ending reasons, interruption events, "
                "or live response latency, so this metric is limited to the upstream detector's "
                "missing-audio signal."
            ),
        ),
    ),
    VoiceMetricSpec(
        "audio_integrity",
        "AudioIntegrityMetric",
        "whether assistant audio contains dropouts, clipping, loops, or abrupt cutoffs",
        ("audio_integrity",),
    ),
    VoiceMetricSpec(
        "voice_naturalness",
        "VoiceNaturalnessMetric",
        "acoustic naturalness indicators in assistant speech",
        ("voice_naturalness",),
    ),
    VoiceMetricSpec(
        "speech_intelligibility",
        "SpeechIntelligibilityMetric",
        "signal-to-noise, volume, clipping, and dropout indicators in assistant speech",
        ("speech_intelligibility",),
    ),
    VoiceMetricSpec(
        "voice_consistency",
        "VoiceConsistencyMetric",
        "voice characteristics across multiple assistant turns",
        ("voice_consistency",),
        ("Requires at least two decodable assistant audio turns in the episode.",),
    ),
    VoiceMetricSpec(
        "voice_reliability",
        "VoiceReliabilityMetric",
        "assistant responsiveness and audio integrity with critical failures preserved",
        ("voice_reliability",),
    ),
)


def _manifest(spec: VoiceMetricSpec) -> EvaluatorManifest:
    return EvaluatorManifest(
        evaluator_id=f"deepeval.{spec.name}",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="deepeval",
        package_version=PINNED_DEEPEVAL,
        description=f"DeepEval {PINNED_DEEPEVAL} {spec.upstream}: {spec.summary}.",
        limitations=(
            (
                "Scores are dependency-free heuristics over the provided transcripts and WAV "
                "samples; they do not prove perceived quality in production conditions."
            ),
            (
                "Episodes are reconstructed from stored case input and execution output; earlier "
                "incomplete turns make the whole prefix not applicable."
            ),
            *spec.limitations,
            (
                "Only inline 16-bit PCM WAV is accepted for voice analysis. URLs, paths, MP3, "
                "other WAV encodings, and remote audio retrieval are unsupported."
            ),
            (
                "A stored episode is limited to 50 turns and a 7 MiB serialized evidence "
                "payload by the host before it is sent to the evaluator worker."
            ),
        ),
        concepts=spec.concepts,
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=(
            FieldRequirement(path="case.group_id"),
            FieldRequirement(path=_EPISODE_TURNS),
            FieldRequirement(path="execution.output", non_empty=False),
        ),
        default_rule=DecisionRule(comparator=">=", threshold=0.5),
        parameters_schema={"type": "object", "additionalProperties": False},
        uses_models=False,
        network_destinations=(),
        internal_retries=0,
        requires_worker=True,
    )


class VoiceMetric(Evaluator):
    """Run one pinned DeepEval voice metric against the episode prefix."""

    spec: ClassVar[VoiceMetricSpec]

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        os.environ.update({"DEEPEVAL_TELEMETRY_OPT_OUT": "YES", "DEEPEVAL_DISABLE_DOTENV": "YES"})
        _require_pinned_deepeval()
        import deepeval.metrics.voice  # noqa: F401 - surface package incompatibility before scoring

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        del ctx  # pinned voice metrics are local signal analysis; they make no provider calls.
        try:
            test_case, decoded_bytes, not_applicable = _voice_test_case(view.get(_EPISODE_TURNS))
        except (TypeError, ValueError, wave.Error, EOFError) as exc:
            return EvaluationOutcome.not_applicable(str(exc))
        if test_case is None:
            return EvaluationOutcome.not_applicable(not_applicable or "voice_evidence_unavailable")

        import deepeval.metrics.voice as voice_metrics

        upstream = getattr(voice_metrics, self.spec.upstream)
        metric = upstream(threshold=0.5, include_reason=True, strict_mode=False, verbose_mode=False)
        try:
            score = await metric.a_measure(test_case, _show_indicator=False)
        except (TypeError, ValueError, wave.Error, EOFError) as exc:
            return EvaluationOutcome.not_applicable(
                f"voice_metric_input_unavailable:{type(exc).__name__}"
            )
        if score is None:
            reason = getattr(metric, "reason", None)
            return EvaluationOutcome.not_applicable(
                f"voice_metric_not_applicable:{str(reason or 'insufficient evidence')[:180]}"
            )
        if (
            isinstance(score, bool)
            or not isinstance(score, (int, float))
            or not math.isfinite(score)
        ):
            return EvaluationOutcome.error("voice_metric_invalid_score")
        if not 0 <= score <= 1:
            return EvaluationOutcome.error("voice_metric_score_out_of_range")
        return EvaluationOutcome.ok(
            "scalar",
            float(score),
            evidence=("case.group_id", _EPISODE_TURNS, "execution.output"),
            raw={
                "deepeval_version": PINNED_DEEPEVAL,
                "metric": self.spec.upstream,
                "turn_count": len(test_case.turns) // 2,
                "audio_bytes": decoded_bytes,
                "reason": getattr(metric, "reason", None),
                "score_breakdown": _jsonable(getattr(metric, "score_breakdown", None)),
                "upstream_success": getattr(metric, "success", None),
                "upstream_threshold": getattr(metric, "threshold", None),
            },
        )


def _voice_test_case(turns: Any) -> tuple[Any | None, int, str | None]:
    if not isinstance(turns, (list, tuple)):
        return None, 0, "missing:episode.turns"
    if not 1 <= len(turns) <= MAX_VOICE_TURNS:
        return None, 0, "invalid:episode.turn_count"

    from deepeval.test_case import ConversationalTestCase

    built: list[Any] = []
    total_audio_bytes = 0
    for index, item in enumerate(turns):
        if not isinstance(item, dict):
            return None, total_audio_bytes, f"invalid:episode.turn.{index}"
        user = _decode_user_turn(item.get("input"), index)
        if isinstance(user, str):
            return None, total_audio_bytes, user
        user_turn, user_bytes = user
        total_audio_bytes += user_bytes
        if total_audio_bytes > MAX_EPISODE_AUDIO_BYTES:
            return None, total_audio_bytes, "oversized:episode.audio"
        built.append(user_turn)

        assistant = _decode_assistant_turn(item.get("output"), index)
        if isinstance(assistant, str):
            return None, total_audio_bytes, assistant
        assistant_turn, assistant_bytes = assistant
        total_audio_bytes += assistant_bytes
        if total_audio_bytes > MAX_EPISODE_AUDIO_BYTES:
            return None, total_audio_bytes, "oversized:episode.audio"
        built.append(assistant_turn)

    return ConversationalTestCase(turns=built, voice=True), total_audio_bytes, None


def _decode_user_turn(value: Any, index: int) -> tuple[Any, int] | str:
    if (
        not isinstance(value, dict)
        or not {"transcript", "audio"} <= set(value)
        or not set(value) <= {"transcript", "audio", "audio_start_ms"}
    ):
        return f"invalid:episode.turn.{index}.user_shape"
    try:
        transcript = checked_text(value["transcript"], "user transcript")
        audio, size, duration = _decode_wav(
            value["audio"], f"episode.turn.{index}.user.audio", MAX_CASE_AUDIO_BYTES
        )
        raw_start = value.get("audio_start_ms")
        start_ms = (
            _timestamp(raw_start, f"episode.turn.{index}.user.audio_start_ms")
            if raw_start is not None
            else None
        )
    except (TypeError, ValueError, wave.Error, EOFError) as exc:
        return str(exc)
    from deepeval.test_case import Audio, Turn

    turn_audio = Audio(
        dataBase64=base64.b64encode(audio).decode("ascii"),
        mimeType="audio/wav",
        encoding="wav",
        sampleRate=_wav_sample_rate(audio),
        duration=duration,
        start_time=start_ms / 1000 if start_ms is not None else None,
    )
    return Turn(role="user", content=transcript, audio=turn_audio), size


def _decode_assistant_turn(value: Any, index: int) -> tuple[Any, int] | str:
    from deepeval.test_case import Audio, Turn

    if isinstance(value, str):
        try:
            transcript = checked_text(value, "assistant transcript")
        except ValueError as exc:
            return str(exc)
        return Turn(role="assistant", content=transcript), 0
    if (
        not isinstance(value, dict)
        or not set(value)
        <= {
            "transcript",
            "audio",
            "audio_start_ms",
        }
        or "transcript" not in value
    ):
        return f"invalid:episode.turn.{index}.assistant_shape"
    try:
        transcript = checked_text(value["transcript"], "assistant transcript")
        encoded_audio = value.get("audio")
        if encoded_audio is None:
            if value.get("audio_start_ms") is not None:
                raise ValueError("invalid:assistant_audio_start_without_audio")
            return Turn(role="assistant", content=transcript), 0
        audio, size, duration = _decode_wav(
            encoded_audio,
            f"episode.turn.{index}.assistant.audio",
            MAX_OUTPUT_AUDIO_BYTES,
        )
        raw_start = value.get("audio_start_ms")
        start_ms = (
            _timestamp(raw_start, f"episode.turn.{index}.assistant.audio_start_ms")
            if raw_start is not None
            else None
        )
    except (TypeError, ValueError, wave.Error, EOFError) as exc:
        return str(exc)

    turn_audio = Audio(
        dataBase64=base64.b64encode(audio).decode("ascii"),
        mimeType="audio/wav",
        encoding="wav",
        sampleRate=_wav_sample_rate(audio),
        duration=duration,
        start_time=start_ms / 1000 if start_ms is not None else None,
    )
    return (
        Turn(
            role="assistant",
            content=transcript,
            audio=turn_audio,
        ),
        size,
    )


def _decode_wav(value: Any, label: str, max_bytes: int) -> tuple[bytes, int, float]:
    from aibench_deepeval.audio import decode_inline_audio

    audio_format, audio = decode_inline_audio(value, label)
    if audio_format != "wav":
        raise ValueError(f"unsupported:{label.replace('.', '_')}_format")
    if len(audio) > max_bytes:
        raise ValueError(f"oversized:{label.replace('.', '_')}")
    try:
        with wave.open(io.BytesIO(audio), "rb") as reader:
            if (
                reader.getcomptype() != "NONE"
                or reader.getsampwidth() != 2
                or reader.getnchannels() not in {1, 2}
                or not 8_000 <= reader.getframerate() <= 96_000
                or reader.getnframes() <= 0
            ):
                raise ValueError("unsupported:voice_wav_encoding")
            duration = reader.getnframes() / reader.getframerate()
            if not math.isfinite(duration) or duration > MAX_AUDIO_DURATION_SECONDS:
                raise ValueError("oversized:voice_audio_duration")
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"invalid:{label.replace('.', '_')}_wav") from exc
    return audio, len(audio), duration


def _wav_sample_rate(audio: bytes) -> int:
    with wave.open(io.BytesIO(audio), "rb") as reader:
        return reader.getframerate()


def _timestamp(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_TIMESTAMP_MS:
        raise ValueError(f"invalid:{label.replace('.', '_')}")
    return value


def _evaluator_class(spec: VoiceMetricSpec) -> type[VoiceMetric]:
    name = "".join(part.title() for part in spec.name.split("_"))
    return type(name, (VoiceMetric,), {"spec": spec, "manifest": _manifest(spec)})


VOICE_EVALUATORS = tuple(_evaluator_class(spec) for spec in VOICE_METRIC_SPECS)
