"""Image-first evaluator contract against the real isolated DeepEval plugin worker."""

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

from aibench.core.models import (
    BenchmarkCase,
    Decision,
    ExecutionStatus,
    MetricBinding,
    ReferenceAnswer,
)
from aibench.registry import BindingValidationError, EvaluatorRegistry
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


class _VisionJudge(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _VisionHandler)
        self.requests: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


class _VisionHandler(BaseHTTPRequestHandler):
    server: _VisionJudge

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        message_content = body["messages"][1]["content"][0]["text"]
        failed = "trigger HTTP error" in message_content
        payload = {
            "choices": [
                {"message": {"content": '{"score":0.9,"reason":"The answer matches the image."}'}}
            ],
            "usage": {"prompt_tokens": 321, "completion_tokens": 17},
        }
        data = json.dumps(payload).encode()
        self.send_response(503 if failed else 200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _ImageApplication(ThreadingHTTPServer):
    def __init__(self, image: dict[str, str]) -> None:
        self.image = image
        super().__init__(("127.0.0.1", 0), _ImageApplicationHandler)


class _ImageApplicationHandler(BaseHTTPRequestHandler):
    server: _ImageApplication

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        body = json.dumps({"output": self.server.image}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _server() -> tuple[_VisionJudge, threading.Thread]:
    server = _VisionJudge()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _vision_case(case_id: str, image: dict[str, Any]) -> BenchmarkCase:
    return BenchmarkCase(
        case_id=case_id,
        input={"prompt": "Name the object.", "image": image},
        reference=ReferenceAnswer(
            answer="A red bicycle.",
            context=("This reference context must not reach the vision judge.",),
        ),
        fixtures=(),
        metadata={"private": "unrequested metadata"},
    )


def _binding(base_url: str) -> dict[str, Any]:
    return {
        "metric": "deepeval.vision_g_eval",
        "params": {
            "judge": {
                "kind": "openai_compatible",
                "base_url": base_url,
                "model": "vision-test-model",
                "api_key_env": "VISION_JUDGE_KEY",
                "timeout_seconds": 10,
            },
            "criteria": "Score whether the output correctly identifies the pictured object.",
        },
        "rule": {"comparator": ">=", "threshold": 0.95},
    }


def test_vision_metric_is_discovered_with_an_explicit_inline_image_contract() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]
    manifest, _ = registry.resolve("deepeval.vision_g_eval")

    assert manifest.requires_worker and manifest.uses_models
    assert manifest.version == "1.1.0"
    assert manifest.plugin_version == "0.2.0rc18"
    assert manifest.package_name == "aibench-deepeval"
    assert manifest.package_version == "0.2.0rc18"
    assert manifest.internal_retries == 0
    assert {item.path for item in manifest.requires} == {
        "case.input",
        "case.reference.answer",
        "execution.output",
    }
    assert any("4 MiB" in item for item in manifest.limitations)


def test_vision_binding_refuses_non_compatible_judges() -> None:
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(PLUGIN_ENV)
    with pytest.raises(BindingValidationError, match="openai_compatible"):
        registry.validate(
            [
                MetricBinding(
                    metric="deepeval.vision_g_eval",
                    params={
                        "judge": {"kind": "python_factory", "factory": "x:y"},
                        "criteria": "Check correctness.",
                    },
                )
            ]
        )


def test_vision_endpoint_and_image_validation_reject_unsafe_inputs() -> None:
    result = plugin_python(
        """
import json
from aibench_deepeval.vision import MAX_IMAGE_BYTES, decode_case_image, validate_base_url
from aibench_deepeval.vision import decode_execution_output
values = [
    "https://api.example/v1",
    "http://127.0.0.1:9000/v1",
    "https://user:secret@api.example/v1",
    "http://api.example/v1",
    "https://10.0.0.7/v1",
    "https://api.example/v1/%2e%2e/private",
    "https://api.example/v1?",
]
answers = []
for value in values:
    try:
        validate_base_url(value)
    except ValueError:
        answers.append(False)
    else:
        answers.append(True)
image_errors = []
for encoded in ("%%%", "A" * (((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4)):
    try:
        decode_case_image({"image": {"mime_type": "image/png", "data_base64": encoded}})
    except ValueError as exc:
        image_errors.append(str(exc).split(":", 1)[0])
    else:
        image_errors.append("accepted")
output_errors = []
for output in (
    {"url": "https://169.254.169.254/latest/meta-data"},
    {"mime_type": "image/png", "data_base64": "bm90IGFuIGltYWdl"},
    {"mime_type": "image/png", "data_base64": "A" * (((MAX_IMAGE_BYTES + 2) // 3) * 4 + 4)},
):
    try:
        decode_execution_output(output)
    except ValueError as exc:
        output_errors.append(str(exc).split(":", 1)[0])
    else:
        output_errors.append("accepted")
print(json.dumps({"endpoints": answers, "images": image_errors, "outputs": output_errors}))
"""
    )
    assert result == {
        "endpoints": [True, True, False, False, False, False, False],
        "images": ["invalid", "oversized"],
        "outputs": ["invalid", "invalid", "oversized"],
    }


def test_vision_worker_sends_only_declared_fields_and_signature_checked_inline_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIBENCH_VISION_TEST_KEY", "vision-secret-value")
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV, secret_env={"VISION_JUDGE_KEY": "env:AIBENCH_VISION_TEST_KEY"}
    )
    assert [load.error for load in loads] == [None]

    good_image = {"mime_type": "image/png", "data_base64": base64.b64encode(PNG).decode()}
    cases = [
        _vision_case("good", good_image),
        _vision_case("provider_error", good_image),
        _vision_case("generated_image", good_image),
        _vision_case("remote_output", good_image),
        _vision_case("wrong_output_signature", good_image),
        _vision_case("url", {"url": "https://169.254.169.254/latest/meta-data"}),
        _vision_case(
            "wrong_signature",
            {"mime_type": "image/png", "data_base64": base64.b64encode(b"not an image").decode()},
        ),
    ]
    outputs: dict[str, Any] = {
        "good": "The picture shows a red bicycle.",
        "provider_error": "trigger HTTP error",
        "generated_image": good_image,
        "remote_output": {"url": "https://169.254.169.254/latest/meta-data"},
        "wrong_output_signature": {
            "mime_type": "image/png",
            "data_base64": base64.b64encode(b"not an image").decode(),
        },
    }
    seeded = Seeded(tmp_path)
    seeded.seed(
        cases,
        [
            execution(item.case_id, outputs.get(item.case_id, "The picture shows a red bicycle."))
            for item in cases
        ],
    )
    server, thread = _server()
    try:
        report = seeded.score(
            [_binding(server.base_url)],
            registry=registry,
            timeout_seconds=20,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    results = {result.case_id: result for result in report.results}
    assert results["good"].status is ExecutionStatus.OK
    assert results["good"].value is not None and results["good"].value.value == 0.9
    assert results["good"].decision is Decision.FAIL
    assert results["provider_error"].status is ExecutionStatus.ERROR
    assert results["provider_error"].resources["model_calls"] == 1
    assert results["provider_error"].resources["cost"] is None
    assert results["generated_image"].status is ExecutionStatus.OK
    assert results["generated_image"].value is not None
    assert results["generated_image"].value.value == 0.9
    assert results["remote_output"].status is ExecutionStatus.NOT_APPLICABLE
    assert results["wrong_output_signature"].status is ExecutionStatus.NOT_APPLICABLE
    assert results["url"].status is ExecutionStatus.NOT_APPLICABLE
    assert results["wrong_signature"].status is ExecutionStatus.NOT_APPLICABLE
    assert len(server.requests) == 3

    request = next(
        request
        for request in server.requests
        if any(
            part.get("text") == "Application-generated output image:"
            for part in request["body"]["messages"][1]["content"]
        )
    )
    assert request["path"] == "/v1/chat/completions"
    assert request["auth"] == "Bearer vision-secret-value"
    body = request["body"]
    content = body["messages"][1]["content"]
    assert content[0]["type"] == "text"
    assert "A red bicycle." in content[0]["text"]
    assert "This reference context must not reach" not in content[0]["text"]
    assert "unrequested metadata" not in content[0]["text"]
    image_url = content[2]["image_url"]["url"]
    assert image_url == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert content[3] == {"type": "text", "text": "Application-generated output image:"}
    output_image_url = content[4]["image_url"]["url"]
    assert output_image_url == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert "data_base64" not in content[0]["text"]

    result = results["good"]
    assert result.resources["model_calls"] == 1
    assert result.resources["tokens"] == {"input": 321, "output": 17}
    assert result.resources["cost"] is None
    raw = seeded.artifacts.read_bytes(seeded.storage.get_artifact(result.raw_artifact_ref))
    assert b"vision-secret-value" not in raw


def test_generated_image_survives_http_output_binding_and_scores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise application JSON, output-pointer extraction, durable storage, and scoring."""
    monkeypatch.setenv("AIBENCH_VISION_TEST_KEY", "vision-secret-value")
    image = {"mime_type": "image/png", "data_base64": base64.b64encode(PNG).decode()}
    item = _vision_case("http-generated-image", image)
    seeded = Seeded(tmp_path)
    seeded.seed([item], [])
    app_server = _ImageApplication(image)
    with serving(app_server) as base_url:
        spec = http_spec(f"{base_url}/generate", output_binding={"output": "/output"})

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
    assert recorded.output["mime_type"] == "image/png"
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV, secret_env={"VISION_JUDGE_KEY": "env:AIBENCH_VISION_TEST_KEY"}
    )
    assert [load.error for load in loads] == [None]
    judge, thread = _server()
    try:
        report = seeded.score([_binding(judge.base_url)], registry=registry)
    finally:
        judge.shutdown()
        judge.server_close()
        thread.join(timeout=5)

    [result] = report.results
    assert result.status is ExecutionStatus.OK
    assert result.value is not None and result.value.value == 0.9
    content = judge.requests[0]["body"]["messages"][1]["content"]
    image_parts = [part for part in content if part["type"] == "image_url"]
    assert len(image_parts) == 2
    assert all(
        part["image_url"]["url"] == "data:image/png;base64," + image["data_base64"]
        for part in image_parts
    )
