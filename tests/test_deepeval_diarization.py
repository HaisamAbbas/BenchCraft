"""Episode-level DER over recorded speaker annotations in the DeepEval plugin worker."""

from __future__ import annotations

from aibench.planning.catalog import CONCEPTS, concepts_in
from aibench.registry import EvaluatorRegistry
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env

pytestmark = requires_plugin_env

_SETUP = r"""
import asyncio, base64, io, json, struct, wave
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval.diarization import DiarizationErrorRate, _speaker_mapping

def audio(duration_ms=200):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
        output.writeframes(struct.pack("<h", 1000) * (16 * duration_ms))
    return {"mime_type":"audio/wav", "data_base64":base64.b64encode(buffer.getvalue()).decode()}

def segment(start, end, speaker):
    return {"start_ms":start, "end_ms":end, "speaker_id":speaker}

def turn(reference, hypothesis, duration_ms=200):
    return {"input":{"audio":audio(duration_ms),
        "diarization_reference":{"segments":reference}},
        "output":{"transcript":"recorded output", "diarization":{"segments":hypothesis}}}

async def score(turns, params=None):
    evaluator = DiarizationErrorRate()
    await evaluator.prepare(params or {})
    case = BenchmarkCase(case_id="turn", input={}, group_id="call")
    execution = ExecutionResult(execution_id="e", run_id="r", case_id="turn", status="ok", output={})
    return await evaluator.evaluate(EvaluationView(case=case, execution=execution, episode=tuple(turns)),
        EvaluatorContext(run_id="r", scoring_id="s"))

def summarize(result):
    return {"status":result.status.value, "reason":result.reason,
        "value":result.value.value if result.value else None, "raw":result.raw}
"""


def test_diarization_metric_is_discovered_with_recorded_evidence_contract() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]
    manifest, _ = registry.resolve("deepeval.diarization_error_rate@1")

    assert manifest.plugin_version == "0.2.0rc24"
    assert manifest.package_name == "aibench-deepeval"
    assert manifest.package_version == "0.2.0rc24"
    assert manifest.direction.value == "lower"
    assert manifest.default_rule is not None
    assert manifest.default_rule.comparator == "<="
    assert manifest.default_rule.threshold == 0.2
    assert not manifest.uses_models and not manifest.network_destinations
    assert manifest.requires_worker
    assert set(manifest.concepts) <= set(CONCEPTS)
    assert "speaker_diarization" in concepts_in("Evaluate speaker diarization quality")
    assert {item.path for item in manifest.requires} == {"case.group_id", "episode.turns"}
    assert any("does not detect speakers" in item for item in manifest.limitations)


def test_der_scores_label_permutation_and_reports_component_counts() -> None:
    result = plugin_python(
        _SETUP
        + """
import asyncio
permuted = [
    turn([segment(0, 100, "ref_a")], [segment(0, 100, "hyp_z")]),
    turn([segment(0, 100, "ref_b")], [segment(0, 100, "hyp_a")]),
]
confused = [
    turn([segment(0, 100, "ref_a")], [segment(0, 100, "merged")]),
    turn([segment(0, 100, "ref_b")], [segment(0, 100, "merged")]),
]
miss_and_false_alarm = [turn([segment(0, 100, "ref")], [segment(50, 150, "hyp")])]
values = [summarize(asyncio.run(score(turns))) for turns in
    (permuted, confused, miss_and_false_alarm)]
assignment = _speaker_mapping({"r1", "r2"}, {"h1", "h2"},
    {("r1", "h1"):9, ("r1", "h2"):8, ("r2", "h1"):8})
print(json.dumps({"values":values,"assignment":assignment}))
"""
    )
    permuted, confused, miss_false = result["values"]
    assert permuted["status"] == "ok" and permuted["value"] == 0
    assert permuted["raw"]["reference_segments"] == 2
    assert permuted["raw"]["hypothesis_segments"] == 2
    assert permuted["raw"]["reference_speakers"] == 2
    assert confused["value"] == 0.5
    assert confused["raw"]["confusion_ms"] == 100
    assert confused["raw"]["reference_speaker_ms"] == 200
    assert miss_false["value"] == 1
    assert miss_false["raw"]["missed_detection_ms"] == 50
    assert miss_false["raw"]["false_alarm_ms"] == 50
    assert miss_false["raw"]["reference_speaker_ms"] == 100
    assert result["assignment"] == {"h1": "r2", "h2": "r1"}
    assert "hyp_z" not in str(permuted["raw"]) and "ref_a" not in str(permuted["raw"])


def test_der_overlap_collar_and_invalid_evidence_are_explicit() -> None:
    result = plugin_python(
        _SETUP
        + """
import asyncio
overlapping = turn([segment(0,100,"r1"),segment(0,100,"r2")],
    [segment(0,100,"h1"),segment(0,100,"h2")])
exact = turn([segment(0,100,"ref")], [segment(0,100,"hyp")])
invalid = turn([segment(0,201,"ref")], [segment(0,100,"hyp")])
too_many = turn([segment(i % 199, i % 199 + 1,"ref") for i in range(129)], [])
too_many_speakers = turn([segment(i, i + 1, f"speaker-{i}") for i in range(65)], [])
too_many_episode_segments = [
    turn([segment(0, 10, "ref")] * 128, []) for _ in range(4)
] + [turn([segment(0, 10, "ref")], [])]
too_many_turns = [turn([], []) for _ in range(51)]
too_much_audio = [turn([], [], 16_382) for _ in range(9)]
values = [
    summarize(asyncio.run(score([overlapping]))),
    summarize(asyncio.run(score([overlapping], {"skip_overlap":True}))),
    summarize(asyncio.run(score([exact], {"collar_ms":40}))),
    summarize(asyncio.run(score([invalid]))),
    summarize(asyncio.run(score([too_many]))),
    summarize(asyncio.run(score([too_many_speakers]))),
    summarize(asyncio.run(score(too_many_episode_segments))),
    summarize(asyncio.run(score(too_many_turns))),
    summarize(asyncio.run(score(too_much_audio))),
    summarize(asyncio.run(score([exact], {"collar_ms": 1001}))),
    summarize(asyncio.run(score([turn([], [segment(0,20,"hyp")])]))),
    summarize(asyncio.run(score([{"input":{"audio":audio(200)},"output":{}}]))),
]
print(json.dumps(values))
"""
    )
    (
        overlap,
        skipped,
        collared,
        invalid,
        capped,
        capped_speakers,
        capped_episode_segments,
        capped_turns,
        capped_audio,
        invalid_parameters,
        no_reference,
        missing,
    ) = result
    assert overlap["status"] == "ok" and overlap["value"] == 0
    assert overlap["raw"]["reference_speaker_ms"] == 200
    assert skipped == {
        "status": "not_applicable",
        "reason": "no_reference_speech",
        "value": None,
        "raw": None,
    }
    assert collared["value"] == 0 and collared["raw"]["reference_speaker_ms"] == 60
    assert invalid["status"] == "not_applicable"
    assert invalid["reason"] == "invalid:episode.diarization_evidence"
    assert capped["reason"] == "oversized:episode.turn_segments"
    assert capped_speakers["reason"] == "oversized:episode.reference_speakers"
    assert capped_episode_segments["reason"] == "oversized:episode.reference_segments"
    assert capped_turns["reason"] == "oversized:episode.turns"
    assert capped_audio["reason"] == "oversized:episode.diarization_audio"
    assert invalid_parameters["status"] == "error"
    assert invalid_parameters["reason"] == "invalid_diarization_parameters"
    assert no_reference["reason"] == "no_reference_speech"
    assert missing["reason"] == "missing:episode.diarization_evidence"


def test_voice_episode_adapter_preserves_annotations_for_companion_metric() -> None:
    result = plugin_python(
        r"""
import base64, io, json, struct, wave
from aibench_deepeval.voice import _decode_user_turn, _decode_assistant_turn
buffer = io.BytesIO()
with wave.open(buffer,"wb") as output:
    output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
    output.writeframes(struct.pack("<h",1000)*160)
audio = {"mime_type":"audio/wav","data_base64":base64.b64encode(buffer.getvalue()).decode()}
annotation = {"segments":[{"start_ms":0,"end_ms":10,"speaker_id":"speaker"}]}
user, _ = _decode_user_turn({"transcript":"caller", "audio":audio,
    "diarization_reference":annotation}, 0)
assistant, _ = _decode_assistant_turn({"transcript":"reply", "diarization":annotation}, 0)
print(json.dumps([user.content, assistant.content]))
"""
    )
    assert result == ["caller", "reply"]
