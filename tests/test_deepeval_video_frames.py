"""Timestamped sampled-frame evaluation through the isolated DeepEval worker."""

from __future__ import annotations

import base64
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import BenchmarkCase, ExecutionStatus, ReferenceAnswer
from aibench.registry import EvaluatorRegistry
from aibench.runners import HttpRunner
from aibench.services.execution import invoke_and_record
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env
from tests.runner_support import http_spec, run, serving
from tests.scoring_support import Seeded, execution

pytestmark = requires_plugin_env


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    chunk = kind + payload
    return (
        struct.pack(">I", len(payload)) + chunk + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    )


PNG = (
    b"\x89PNG\r\n\x1a\n"
    + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    + _png_chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    + _png_chunk(b"IEND", b"")
)
INLINE_PNG = {"mime_type": "image/png", "data_base64": base64.b64encode(PNG).decode()}


class _Judge(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _JudgeHandler)
        self.requests: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


class _JudgeHandler(BaseHTTPRequestHandler):
    server: _Judge

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        payload = {
            "choices": [
                {"message": {"content": '{"score":0.8,"reason":"The sequence supports it."}'}}
            ],
            "usage": {"prompt_tokens": 444, "completion_tokens": 18},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _Application(ThreadingHTTPServer):
    def __init__(self, frames: dict[str, Any]) -> None:
        self.frames = frames
        super().__init__(("127.0.0.1", 0), _ApplicationHandler)


class _ApplicationHandler(BaseHTTPRequestHandler):
    server: _Application

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({"output": self.server.frames}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _frame(timestamp_ms: int) -> dict[str, Any]:
    return {"timestamp_ms": timestamp_ms, "image": INLINE_PNG}


def _case(case_id: str, *, timestamps: tuple[int, ...] = (0, 1000)) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        input={
            "prompt": "Did the person enter and then leave the room?",
            "video": {"frames": [_frame(timestamp) for timestamp in timestamps]},
        },
        reference=ReferenceAnswer(
            answer="The person enters the room, then leaves.",
            context=("Unrequested reference context must not reach the judge.",),
        ),
        fixtures=(),
        metadata={"private": "unrequested metadata"},
    )


def _binding(base_url: str) -> dict[str, Any]:
    return {
        "metric": "deepeval.video_frames_g_eval",
        "params": {
            "judge": {
                "kind": "openai_compatible",
                "base_url": base_url,
                "model": "video-frames-test-model",
                "api_key_env": "VIDEO_FRAMES_JUDGE_KEY",
                "timeout_seconds": 10,
            },
            "criteria": "Score whether the output correctly describes the sampled event order.",
        },
        "rule": {"comparator": ">=", "threshold": 0.5},
    }


def _server() -> tuple[_Judge, threading.Thread]:
    server = _Judge()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_video_frames_metric_is_discovered_with_a_bounded_contract() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]
    manifest, _ = registry.resolve("deepeval.video_frames_g_eval")

    assert manifest.requires_worker and manifest.uses_models
    assert manifest.version == "1.0.0"
    assert manifest.plugin_version == "0.2.0rc20"
    assert manifest.package_name == "aibench-deepeval"
    assert manifest.package_version == "0.2.0rc20"
    assert manifest.internal_retries == 0
    assert {item.path for item in manifest.requires} == {
        "case.input",
        "case.reference.answer",
        "execution.output",
    }
    assert any("4 MiB" in item for item in manifest.limitations)


def test_video_frame_decoder_rejects_unbounded_or_unordered_data() -> None:
    result = plugin_python(
        """
import base64, json
from aibench_deepeval.video_frames import decode_case_frames, decode_execution_output
image = {"mime_type": "image/png", "data_base64": base64.b64encode(b"\\x89PNG\\r\\n\\x1a\\n" + bytes(8)).decode()}
def frame(t): return {"timestamp_ms": t, "image": image}
checks = [
    ("one", {"frames": [frame(0)]}),
    ("duplicate", {"frames": [frame(0), frame(0)]}),
    ("decreasing", {"frames": [frame(10), frame(9)]}),
    ("boolean", {"frames": [frame(0), {"timestamp_ms": True, "image": image}]}),
    ("too_many", {"frames": [frame(i) for i in range(9)]}),
    ("bad_shape", {"frames": [frame(0), {**frame(1), "url": "https://example.invalid"}]}),
]
errors = []
for name, value in checks:
    try: decode_execution_output(value)
    except (TypeError, ValueError) as exc: errors.append((name, str(exc).split(":", 1)[0]))
    else: errors.append((name, "accepted"))
too_big = {"mime_type": "image/png", "data_base64": base64.b64encode(b"\\x89PNG\\r\\n\\x1a\\n" + bytes(1024 * 1024)).decode()}
try: decode_execution_output({"frames": [frame(0), frame(1), {"timestamp_ms": 2, "image": too_big}]})
except ValueError as exc: oversize = str(exc).split(":", 1)[0]
else: oversize = "accepted"
large_image = {"mime_type": "image/png", "data_base64": base64.b64encode(b"\\x89PNG\\r\\n\\x1a\\n" + bytes(900 * 1024)).decode()}
large_sequence = {"frames": [{"timestamp_ms": i, "image": large_image} for i in range(5)]}
try: decode_execution_output(large_sequence)
except ValueError as exc: aggregate = str(exc).split(":", 1)[0]
else: aggregate = "accepted"
case_large_image = {"mime_type": "image/png", "data_base64": base64.b64encode(b"\\x89PNG\\r\\n\\x1a\\n" + bytes(300 * 1024)).decode()}
case_sequence = {"video": {"frames": [{"timestamp_ms": i, "image": case_large_image} for i in range(2)]}}
try: decode_case_frames(case_sequence)
except ValueError as exc: case_aggregate = str(exc).split(":", 1)[0]
else: case_aggregate = "accepted"
valid = decode_case_frames({"video": {"frames": [frame(1), frame(2)]}})
print(json.dumps({"errors": errors, "oversize": oversize, "aggregate": aggregate, "case_aggregate": case_aggregate, "timestamps": [f[0] for f in valid]}))
"""
    )
    assert result == {
        "errors": [
            ["one", "invalid"],
            ["duplicate", "invalid"],
            ["decreasing", "invalid"],
            ["boolean", "invalid"],
            ["too_many", "invalid"],
            ["bad_shape", "invalid"],
        ],
        "oversize": "oversized",
        "aggregate": "oversized",
        "case_aggregate": "oversized",
        "timestamps": [1, 2],
    }


def test_timestamped_frames_survive_http_storage_and_ordered_judge_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIBENCH_VIDEO_FRAMES_TEST_KEY", "video-frames-secret")
    case = _case("generated-frames")
    text_case = _case("text-output")
    bad_case = _case("bad-timestamps", timestamps=(1000, 500))
    remote_output_case = _case("remote-output")
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case, text_case, bad_case, remote_output_case],
        [
            execution("text-output", "The person enters and later leaves."),
            execution("bad-timestamps", "The person enters and later leaves."),
            execution(
                "remote-output",
                {"frames": [{"timestamp_ms": 0, "url": "https://example.invalid/a.png"}]},
            ),
        ],
    )
    output_frames = {"frames": [_frame(250), _frame(1250)]}
    app_server = _Application(output_frames)
    with serving(app_server) as base_url:
        spec = http_spec(f"{base_url}/analyze", output_binding={"output": "/output"})

        async def invoke() -> Any:
            async with HttpRunner(spec, base_dir=tmp_path) as runner:
                return await invoke_and_record(
                    runner,
                    case,
                    storage=seeded.storage,
                    artifacts=seeded.artifacts,
                    run_id="run-1",
                )

        recorded = run(invoke())

    assert recorded.status is ExecutionStatus.OK
    assert [frame["timestamp_ms"] for frame in recorded.output["frames"]] == [250, 1250]
    assert all(
        frame["image"]["data_base64"] == INLINE_PNG["data_base64"]
        for frame in recorded.output["frames"]
    )
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV, secret_env={"VIDEO_FRAMES_JUDGE_KEY": "env:AIBENCH_VIDEO_FRAMES_TEST_KEY"}
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
    assert results["generated-frames"].status is ExecutionStatus.OK
    assert results["generated-frames"].value is not None
    assert results["generated-frames"].value.value == 0.8
    assert results["generated-frames"].resources["model_calls"] == 1
    assert results["generated-frames"].resources["tokens"] == {"input": 444, "output": 18}
    assert results["generated-frames"].resources["cost"] is None
    assert results["text-output"].status is ExecutionStatus.OK
    assert results["bad-timestamps"].status is ExecutionStatus.NOT_APPLICABLE
    assert results["remote-output"].status is ExecutionStatus.NOT_APPLICABLE
    assert len(judge.requests) == 2

    request = judge.requests[0]
    assert request["path"] == "/v1/chat/completions"
    assert request["auth"] == "Bearer video-frames-secret"
    content = request["body"]["messages"][1]["content"]
    assert "Unrequested reference context" not in content[0]["text"]
    assert "unrequested metadata" not in content[0]["text"]
    assert "do not infer unsampled frames" in content[0]["text"]
    labels = [part["text"] for part in content if part["type"] == "text"]
    assert labels == [
        content[0]["text"],
        "Case frame at 0 ms:",
        "Case frame at 1000 ms:",
        "Application output frame at 250 ms:",
        "Application output frame at 1250 ms:",
    ]
    image_parts = [part for part in content if part["type"] == "image_url"]
    assert len(image_parts) == 4
    assert all(
        part["image_url"]["url"] == f"data:image/png;base64,{INLINE_PNG['data_base64']}"
        for part in image_parts
    )
    raw = seeded.artifacts.read_bytes(
        seeded.storage.get_artifact(results["generated-frames"].raw_artifact_ref)
    )
    assert b"video-frames-secret" not in raw
