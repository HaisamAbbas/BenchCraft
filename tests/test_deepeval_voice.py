"""Bounded DeepEval voice metrics over recorded timestamped episodes."""

from __future__ import annotations

import base64
import io
import json
import struct
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from aibench.core.models import BenchmarkCase, ExecutionStatus
from aibench.registry import EvaluatorRegistry
from aibench.runners import HttpRunner
from aibench.services.execution import invoke_and_record
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env
from tests.runner_support import http_spec, run, serving
from tests.scoring_support import Seeded

pytestmark = requires_plugin_env

METRICS = (
    "deepeval.turn_taking_naturalness",
    "deepeval.agent_responsiveness",
    "deepeval.audio_integrity",
    "deepeval.voice_naturalness",
    "deepeval.speech_intelligibility",
    "deepeval.voice_consistency",
    "deepeval.voice_reliability",
)
SCORED_METRICS = (
    "deepeval.turn_taking_naturalness",
    "deepeval.voice_consistency",
    "deepeval.voice_reliability",
)


def _wav(*, duration_ms: int = 200, sample_rate: int = 16_000) -> bytes:
    frame_count = sample_rate * duration_ms // 1000
    samples = struct.pack("<h", 3500) * frame_count
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples)
    return buffer.getvalue()


WAV = _wav()
INLINE_WAV = {"mime_type": "audio/wav", "data_base64": base64.b64encode(WAV).decode()}


class _Application(ThreadingHTTPServer):
    def __init__(self, outputs: dict[str, Any]) -> None:
        self.outputs = outputs
        super().__init__(("127.0.0.1", 0), _ApplicationHandler)


class _ApplicationHandler(BaseHTTPRequestHandler):
    server: _Application

    def do_POST(self) -> None:
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        response = json.dumps({"output": self.server.outputs[request["case_id"]]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(response)))
        self.end_headers()
        self.wfile.write(response)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _case(case_id: str, *, start_ms: int) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        input={
            "transcript": f"Please help with request {case_id}.",
            "audio": INLINE_WAV,
            "audio_start_ms": start_ms,
        },
        fixtures=(),
        group_id="phone-call-1",
    )


def _output(transcript: str, *, start_ms: int) -> dict[str, Any]:
    return {
        "transcript": transcript,
        "audio": INLINE_WAV,
        "audio_start_ms": start_ms,
    }


def _binding(metric: str) -> dict[str, Any]:
    return {"metric": metric, "params": {}, "rule": {"comparator": ">=", "threshold": 0.5}}


def test_voice_metric_family_is_discovered_with_local_only_contracts() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]

    for metric in METRICS:
        manifest, _ = registry.resolve(metric)
        assert manifest.version == "1.0.0"
        assert manifest.plugin_version == "0.2.0rc22"
        assert manifest.package_version == "4.2.5"
        assert manifest.requires_worker
        assert not manifest.uses_models
        assert not manifest.network_destinations
        assert {item.path for item in manifest.requires} == {
            "case.group_id",
            "episode.turns",
            "execution.output",
        }


def test_voice_metrics_score_stored_wav_conversation_and_timestamps(
    tmp_path: Path,
) -> None:
    first = _case("turn-1", start_ms=0)
    second = _case("turn-2", start_ms=1300)
    outputs = {
        "turn-1": _output("I can help with that.", start_ms=600),
        "turn-2": _output("Your request is complete.", start_ms=1750),
    }
    seeded = Seeded(tmp_path)
    seeded.seed([first, second], [])

    with serving(_Application(outputs)) as base_url:
        spec = http_spec(f"{base_url}/voice", output_binding={"output": "/output"})
        for item in (first, second):

            async def invoke(case: BenchmarkCase = item) -> Any:
                async with HttpRunner(spec, base_dir=tmp_path) as runner:
                    return await invoke_and_record(
                        runner,
                        case,
                        storage=seeded.storage,
                        artifacts=seeded.artifacts,
                        run_id="run-1",
                    )

            assert run(invoke()).status is ExecutionStatus.OK

    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]
    report = seeded.score([_binding(metric) for metric in SCORED_METRICS], registry=registry)
    results = {(result.metric_id, result.case_id): result for result in report.results}

    assert len(results) == len(SCORED_METRICS) * 2
    for metric in SCORED_METRICS:
        assert results[(metric, "turn-2")].status is ExecutionStatus.OK, metric
        result = results[(metric, "turn-2")]
        assert result.value is not None and 0 <= result.value.value <= 1
        assert result.resources["model_calls"] == 0
        assert result.resources["cost"] == 0
    assert (
        results[("deepeval.voice_consistency", "turn-1")].status is ExecutionStatus.NOT_APPLICABLE
    )
    turn_taking = results[("deepeval.turn_taking_naturalness", "turn-2")]
    assert turn_taking.value is not None and turn_taking.value.value > 0.5
    assert turn_taking.raw_artifact_ref is not None


def test_voice_contract_rejects_remote_audio_bad_timestamps_and_oversized_clips() -> None:
    result = plugin_python(
        """
import base64, io, json, struct, wave
from aibench_deepeval.voice import _decode_user_turn, _decode_assistant_turn, _voice_test_case
from deepeval.metrics.voice._detectors import detect_responsiveness
buf = io.BytesIO()
with wave.open(buf, 'wb') as out:
    out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(struct.pack('<h', 1000) * 100)
valid = {"mime_type": "audio/wav", "data_base64": base64.b64encode(buf.getvalue()).decode()}
mp3 = {"mime_type": "audio/mpeg", "data_base64": base64.b64encode(b"\\xff\\xfb\\x90\\x64" + bytes(413)).decode()}
checks = [
    _decode_user_turn({"transcript":"hello", "audio":{"url":"https://example.invalid/a.wav"}, "audio_start_ms":0}, 0),
    _decode_user_turn({"transcript":"hello", "audio":valid, "audio_start_ms":True}, 0),
    _decode_user_turn({"transcript":"hello", "audio":mp3, "audio_start_ms":0}, 0),
    _decode_assistant_turn({"transcript":"hi", "audio":{"url":"https://example.invalid/a.wav"}, "audio_start_ms":100}, 0),
    _decode_assistant_turn({"transcript":"hi", "audio":valid, "latency_ms":10}, 0),
    _decode_assistant_turn({"transcript":"hi", "audio":valid, "interrupted":True}, 0),
]
large_buf = io.BytesIO()
with wave.open(large_buf, 'wb') as out:
    out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(struct.pack('<h', 1000) * (16000 * 20))
large = {"mime_type":"audio/wav", "data_base64":base64.b64encode(large_buf.getvalue()).decode()}
checks.append(_decode_user_turn({"transcript":"hello", "audio":large, "audio_start_ms":0}, 0))
near_cap_buf = io.BytesIO()
with wave.open(near_cap_buf, 'wb') as out:
    out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000); out.writeframes(struct.pack('<h', 1000) * ((512 * 1024 - 44) // 2))
near_cap = {"mime_type":"audio/wav", "data_base64":base64.b64encode(near_cap_buf.getvalue()).decode()}
turns = [{"input":{"transcript":"hello", "audio":near_cap}, "output":"ok"} for _ in range(9)]
aggregate_case, aggregate_bytes, aggregate_error = _voice_test_case(turns)
valid_turns = [{"input":{"transcript":"hello", "audio":valid}, "output":{"transcript":"ok", "audio":valid}} for _ in range(2)]
conversation, _, conversation_error = _voice_test_case(valid_turns)
responsiveness = detect_responsiveness(conversation) if conversation is not None else None
missing_audio, _, missing_audio_error = _voice_test_case([{"input":{"transcript":"hello", "audio":valid}, "output":"ok"}])
missing_audio_report = detect_responsiveness(missing_audio) if missing_audio is not None else None
print(json.dumps({
    "checks":[value if isinstance(value, str) else "accepted" for value in checks],
    "aggregate_error":aggregate_error,
    "aggregate_bytes":aggregate_bytes,
    "conversation_error":conversation_error,
    "turn_roles":[turn.role for turn in conversation.turns] if conversation is not None else None,
    "reprompt_events":responsiveness.events if responsiveness is not None else None,
    "missing_audio_error":missing_audio_error,
    "missing_audio_score":missing_audio_report.score if missing_audio_report is not None else None,
    "missing_audio_events":missing_audio_report.events if missing_audio_report is not None else None,
}))
"""
    )
    assert result == {
        "checks": [
            "invalid:episode_turn_0_user_audio_shape",
            "invalid:episode_turn_0_user_audio_start_ms",
            "unsupported:episode_turn_0_user_audio_format",
            "invalid:episode_turn_0_assistant_audio_shape",
            "invalid:episode.turn.0.assistant_shape",
            "invalid:episode.turn.0.assistant_shape",
            "oversized:episode_turn_0_user_audio",
        ],
        "aggregate_error": "oversized:episode.audio",
        "aggregate_bytes": 4_718_592,
        "conversation_error": None,
        "turn_roles": ["user", "assistant", "user", "assistant"],
        "reprompt_events": [],
        "missing_audio_error": None,
        "missing_audio_score": 0.0,
        "missing_audio_events": [{"type": "assistant_audio_missing", "turn": 1, "critical": True}],
    }
