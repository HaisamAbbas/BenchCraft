"""Bounded audio evaluator contract against the isolated DeepEval plugin worker."""

from __future__ import annotations

import base64
import json
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import (
    BenchmarkCase,
    ExecutionStatus,
    ReferenceAnswer,
)
from aibench.registry import EvaluatorRegistry
from aibench.runners import HttpRunner
from aibench.services.execution import invoke_and_record
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env
from tests.runner_support import http_spec, run, serving
from tests.scoring_support import Seeded, execution

pytestmark = requires_plugin_env


def _wav(*, byte_rate: int = 32_000, block_align: int = 2, pcm: bytes = b"\x00\x00") -> bytes:
    fmt = struct.pack("<IHHIIHH", 16, 1, 1, 16_000, byte_rate, block_align, 16)
    chunks = b"fmt " + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


WAV = _wav()
INLINE_WAV = {"mime_type": "audio/wav", "data_base64": base64.b64encode(WAV).decode()}


class _AudioJudge(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _AudioJudgeHandler)
        self.requests: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


class _AudioJudgeHandler(BaseHTTPRequestHandler):
    server: _AudioJudge

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        payload = {
            "choices": [
                {"message": {"content": '{"score":0.85,"reason":"The response matches."}'}}
            ],
            "usage": {"prompt_tokens": 211, "completion_tokens": 13},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _AudioApplication(ThreadingHTTPServer):
    def __init__(self, audio: dict[str, str]) -> None:
        self.audio = audio
        super().__init__(("127.0.0.1", 0), _AudioApplicationHandler)


class _AudioApplicationHandler(BaseHTTPRequestHandler):
    server: _AudioApplication

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({"output": self.server.audio}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _case(case_id: str) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        input={"prompt": "Describe the spoken greeting.", "audio": INLINE_WAV},
        reference=ReferenceAnswer(answer="The speaker says hello."),
        fixtures=(),
        metadata={"private": "unrequested metadata"},
    )


def _binding(base_url: str) -> dict[str, Any]:
    return {
        "metric": "deepeval.audio_g_eval",
        "params": {
            "judge": {
                "kind": "openai_compatible",
                "base_url": base_url,
                "model": "audio-test-model",
                "api_key_env": "AUDIO_JUDGE_KEY",
                "timeout_seconds": 10,
            },
            "criteria": "Score whether the output accurately conveys the spoken greeting.",
        },
        "rule": {"comparator": ">=", "threshold": 0.8},
    }


def _server() -> tuple[_AudioJudge, threading.Thread]:
    server = _AudioJudge()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_audio_metric_is_discovered_with_a_bounded_inline_contract() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]
    manifest, _ = registry.resolve("deepeval.audio_g_eval")

    assert manifest.requires_worker and manifest.uses_models
    assert manifest.version == "1.0.0"
    assert manifest.plugin_version == "0.2.0rc22"
    assert manifest.package_name == "aibench-deepeval"
    assert manifest.package_version == "0.2.0rc22"
    assert manifest.internal_retries == 0
    assert {item.path for item in manifest.requires} == {
        "case.input",
        "case.reference.answer",
        "execution.output",
    }
    assert any("4 MiB" in item for item in manifest.limitations)


def test_audio_decoder_rejects_urls_bad_signatures_and_oversized_data() -> None:
    valid_wav = base64.b64encode(WAV).decode()
    spoof_wav = base64.b64encode(b"RIFF\x04\x00\x00\x00WAVE").decode()
    bad_byte_rate_wav = base64.b64encode(_wav(byte_rate=0)).decode()
    bad_block_align_wav = base64.b64encode(
        _wav(byte_rate=64_000, block_align=4, pcm=bytes(4))
    ).decode()
    mp3_frame = b"\xff\xfb\x90\x64" + bytes(413)
    short_mp3 = base64.b64encode(mp3_frame[:4]).decode()
    valid_mp3 = base64.b64encode(mp3_frame).decode()
    tagged_mp3 = base64.b64encode(b"ID3\x04\x00\x00\x00\x00\x00\x00" + mp3_frame).decode()
    code = """
import json
from aibench_deepeval.audio import MAX_AUDIO_BYTES, decode_case_audio, decode_execution_output
bad = [
    {"url": "https://169.254.169.254/latest/meta-data"},
    {"mime_type": "audio/wav", "data_base64": "bm90IGF1ZGlv"},
    {"mime_type": "audio/wav", "data_base64": "__SPOOF_WAV__"},
    {"mime_type": "audio/wav", "data_base64": "__BAD_BYTE_RATE_WAV__"},
    {"mime_type": "audio/wav", "data_base64": "__BAD_BLOCK_ALIGN_WAV__"},
    {"mime_type": "audio/mpeg", "data_base64": "__SHORT_MP3__"},
    {"mime_type": "audio/aac", "data_base64": "AAAA"},
    {"mime_type": "audio/wav", "data_base64": "A" * (((MAX_AUDIO_BYTES + 2) // 3) * 4 + 4)},
]
errors = []
for item in bad:
    try:
        decode_execution_output(item)
    except (TypeError, ValueError) as exc:
        errors.append(str(exc).split(":", 1)[0])
    else:
        errors.append("accepted")
try:
    decode_case_audio({"audio": {"mime_type": "audio/mpeg", "data_base64": "bm90IG1wMw=="}})
except ValueError as exc:
    case_error = str(exc).split(":", 1)[0]
else:
    case_error = "accepted"
valid_formats = []
for encoded, mime_type in (("__VALID_WAV__", "audio/wav"), ("__VALID_MP3__", "audio/mpeg"), ("__TAGGED_MP3__", "audio/mpeg")):
    valid_formats.append(decode_execution_output({"mime_type": mime_type, "data_base64": encoded})[0])
print(json.dumps({"outputs": errors, "case": case_error, "valid_formats": valid_formats}))
"""
    code = (
        code.replace("__VALID_WAV__", valid_wav)
        .replace("__VALID_MP3__", valid_mp3)
        .replace("__TAGGED_MP3__", tagged_mp3)
        .replace("__SPOOF_WAV__", spoof_wav)
        .replace("__BAD_BYTE_RATE_WAV__", bad_byte_rate_wav)
        .replace("__BAD_BLOCK_ALIGN_WAV__", bad_block_align_wav)
        .replace("__SHORT_MP3__", short_mp3)
    )
    result = plugin_python(code)
    assert result == {
        "outputs": [
            "invalid",
            "invalid",
            "invalid",
            "invalid",
            "invalid",
            "invalid",
            "unsupported",
            "oversized",
        ],
        "case": "invalid",
        "valid_formats": ["wav", "mp3", "mp3"],
    }


def test_generated_audio_survives_http_output_binding_and_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise app JSON, output-pointer extraction, durable storage, and isolated scoring."""
    monkeypatch.setenv("AIBENCH_AUDIO_TEST_KEY", "audio-secret-value")
    item = _case("http-generated-audio")
    text_item = _case("text-output")
    remote_item = _case("remote-output")
    bad_case_audio = _case("bad-case-audio").model_copy(
        update={
            "input": {
                "prompt": "What does the speaker say?",
                "audio": {"url": "https://example.invalid/audio.wav"},
            }
        }
    )
    seeded = Seeded(tmp_path)
    seeded.seed(
        [item, text_item, remote_item, bad_case_audio],
        [
            execution("text-output", "The speaker says hello."),
            execution("remote-output", {"url": "https://169.254.169.254/latest/meta-data"}),
            execution("bad-case-audio", "The speaker says hello."),
        ],
    )
    app_server = _AudioApplication(INLINE_WAV)
    with serving(app_server) as base_url:
        spec = http_spec(f"{base_url}/speak", output_binding={"output": "/output"})

        async def invoke() -> Any:
            async with HttpRunner(spec, base_dir=tmp_path) as runner:
                return await invoke_and_record(
                    runner,
                    item,
                    storage=seeded.storage,
                    artifacts=seeded.artifacts,
                    run_id="run-1",
                )

        recorded = run(invoke())

    assert recorded.status is ExecutionStatus.OK
    assert recorded.output == INLINE_WAV
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV, secret_env={"AUDIO_JUDGE_KEY": "env:AIBENCH_AUDIO_TEST_KEY"}
    )
    assert [load.error for load in loads] == [None]
    judge, thread = _server()
    try:
        report = seeded.score([_binding(judge.base_url)], registry=registry)
    finally:
        judge.shutdown()
        judge.server_close()
        thread.join(timeout=5)

    results = {result.case_id: result for result in report.results}
    result = results["http-generated-audio"]
    assert result.status is ExecutionStatus.OK
    assert result.value is not None and result.value.value == 0.85
    assert result.resources["model_calls"] == 1
    assert result.resources["tokens"] == {"input": 211, "output": 13}
    assert result.resources["cost"] is None
    assert results["text-output"].status is ExecutionStatus.OK
    assert results["remote-output"].status is ExecutionStatus.NOT_APPLICABLE
    assert results["bad-case-audio"].status is ExecutionStatus.NOT_APPLICABLE
    assert len(judge.requests) == 2
    request = judge.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["auth"] == "Bearer audio-secret-value"
    content = request["body"]["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert "The speaker says hello." in content[0]["text"]
    assert "unrequested metadata" not in content[0]["text"]
    assert content[2] == {
        "type": "input_audio",
        "input_audio": {"data": INLINE_WAV["data_base64"], "format": "wav"},
    }
    assert content[3] == {"type": "text", "text": "Application-generated output audio:"}
    assert content[4] == {
        "type": "input_audio",
        "input_audio": {"data": INLINE_WAV["data_base64"], "format": "wav"},
    }
    assert "data_base64" not in content[0]["text"]
    raw = seeded.artifacts.read_bytes(seeded.storage.get_artifact(result.raw_artifact_ref))
    assert b"audio-secret-value" not in raw

    text_request = next(
        request
        for request in judge.requests
        if json.loads(request["body"]["messages"][1]["content"][0]["text"])["application_output"][
            "kind"
        ]
        == "text"
    )
    text_content = text_request["body"]["messages"][1]["content"]
    assert sum(part["type"] == "input_audio" for part in text_content) == 1
