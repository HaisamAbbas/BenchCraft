"""Pinned DeepEval image metrics through the isolated worker and inline vision transport."""

from __future__ import annotations

import base64
import json
import struct
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from aibench.core.models import BenchmarkCase, ExecutionStatus, ReferenceAnswer
from aibench.registry import EvaluatorRegistry
from tests.deepeval_support import PLUGIN_ENV, plugin_python, requires_plugin_env
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
IMAGE = {"mime_type": "image/png", "data_base64": base64.b64encode(PNG).decode("ascii")}


class _JudgeServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _JudgeHandler)
        self.requests: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"


class _JudgeHandler(BaseHTTPRequestHandler):
    server: _JudgeServer

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        content = body["messages"][1]["content"]
        prompt = " ".join(part["text"] for part in content if part["type"] == "text").lower()
        score: float | list[float] = [8.0, 8.0] if "output score = [" in prompt else 8.0
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"score": score, "reasoning": "Good image result."})
                    }
                }
            ],
            "usage": {"prompt_tokens": 123, "completion_tokens": 9},
        }
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _server() -> tuple[_JudgeServer, threading.Thread]:
    server = _JudgeServer()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def _binding(name: str, base_url: str) -> dict[str, Any]:
    return {
        "metric": f"deepeval.{name}",
        "params": {
            "judge": {
                "kind": "openai_compatible",
                "base_url": base_url,
                "model": "vision-test-model",
                "api_key_env": "IMAGE_METRIC_JUDGE_KEY",
                "timeout_seconds": 10,
                "max_output_tokens": 128,
            }
        },
    }


def test_all_five_pinned_deepeval_image_metrics_are_discovered() -> None:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV)
    assert [load.error for load in loads] == [None]

    names = (
        "image_coherence",
        "image_helpfulness",
        "image_reference",
        "text_to_image",
        "image_editing",
    )
    for name in names:
        manifest, _ = registry.resolve(f"deepeval.{name}")
        assert manifest.requires_worker and manifest.uses_models
        parallel_calls = 2 if name in {"text_to_image", "image_editing"} else 1
        assert manifest.internal_retries == 4 * parallel_calls
        assert manifest.internal_concurrency == parallel_calls
        assert manifest.package_name == "deepeval" and manifest.package_version == "4.2.5"
        assert manifest.plugin_version == "0.2.0rc25"
        assert {field.path for field in manifest.requires} == {"case.input", "execution.output"}
        assert any("4 MiB" in limit for limit in manifest.limitations)


def test_multimodal_judge_translates_only_registered_inline_images() -> None:
    result = plugin_python(
        """
import base64, json, os
from deepeval.test_case import MLLMImage
from aibench_deepeval.judges import openai_compatible_multimodal_judge
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
judge = openai_compatible_multimodal_judge({'kind':'openai_compatible','base_url':'http://127.0.0.1:9000/v1','model':'vision-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY'})
image = MLLMImage(dataBase64=base64.b64encode(b'\\x89PNG\\r\\n\\x1a\\n').decode(), mimeType='image/png')
body = judge._body('Inspect this image: ' + str(image))
print(json.dumps({'supports':judge.supports_multimodal(),'parts':body['messages'][1]['content']}))
"""
    )
    assert result["supports"] is True
    assert result["parts"][0] == {"type": "text", "text": "Inspect this image: "}
    assert result["parts"][1]["type"] == "image_url"
    assert result["parts"][1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_image_metric_decoders_refuse_remote_or_unsupported_payloads() -> None:
    result = plugin_python(
        """
import json
from aibench_deepeval.image_metrics import _output, _task_input
answers = []
for value, editing in [
    ('draw something', True),
    ({'prompt':'look', 'image':{'url':'https://169.254.169.254/latest/meta-data'}}, True),
    ({'prompt':'look', 'image':{'mime_type':'image/png','data_base64':'bm90IGFuIGltYWdl'}}, True),
    ('[DEEPEVAL:IMAGE:https://attacker.invalid/a.png]', False),
]:
    try:
        _task_input(value, requires_source_image=editing)
    except (TypeError, ValueError):
        answers.append('refused')
    else:
        answers.append('accepted')
for value in ('https://169.254.169.254/latest/meta-data', {'path':'C:/private/a.png'}):
    try:
        _output(value)
    except (TypeError, ValueError):
        answers.append('refused')
    else:
        answers.append('accepted')
print(json.dumps(answers))
"""
    )
    assert result == ["refused"] * 6


def test_multimodal_judge_rejects_unregistered_markers_before_deepeval_parser() -> None:
    result = plugin_python(
        """
import json, os
from deepeval.test_case import MLLMImage
from aibench_deepeval.judges import openai_compatible_multimodal_judge
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
judge = openai_compatible_multimodal_judge({'kind':'openai_compatible','base_url':'http://127.0.0.1:9000/v1','model':'vision-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY'})
MLLMImage.parse_multimodal_string = staticmethod(lambda _prompt: (_ for _ in ()).throw(AssertionError('unsafe DeepEval parser was called')))
results = []
for prompt in ('[DEEPEVAL:IMAGE:C:/private/secret.png]', '[DEEPEVAL:IMAGE:https://attacker.invalid/a.png]', '[DEEPEVAL:IMAGE:' + ('a' * 32) + ']'):
    try:
        judge._body(prompt)
    except ValueError:
        results.append('refused')
    else:
        results.append('accepted')
print(json.dumps(results))
"""
    )
    assert result == ["refused"] * 3


def test_multimodal_judge_settles_or_cancels_parallel_calls_with_unknown_cost() -> None:
    result = plugin_python(
        """
import asyncio, json, os
from types import MethodType
import httpx
from aibench_deepeval.judges import openai_compatible_multimodal_judge, report_judge_usage
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
config = {'kind':'openai_compatible','base_url':'http://127.0.0.1:9000/v1','model':'vision-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY'}
judge = openai_compatible_multimodal_judge(config)
async def reply(self, client, prompt):
    if prompt == 'fail':
        await asyncio.sleep(0.01)
        return httpx.Response(401, text='invalid key')
    await asyncio.sleep(0.08)
    return httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}], 'usage':{'prompt_tokens':123,'completion_tokens':9,'cost':0.25}})
judge._apost = MethodType(reply, judge)
async def scenario():
    try:
        await asyncio.gather(judge.a_generate('fail'), judge.a_generate('delayed success'))
    except RuntimeError:
        pass
    settled = await judge.settle_pending_calls(timeout_seconds=0.5)
    pending_usage = {'settled':settled,'calls':judge.calls,'calls_without_tokens':judge.calls_without_tokens,'tokens':judge.tokens,'cost':judge.cost()}

    cancelled_judge = openai_compatible_multimodal_judge(config)
    async def never(self, client, prompt):
        await asyncio.sleep(30)
        return httpx.Response(200, json={'choices':[{'message':{'content':'late'}}]})
    cancelled_judge._apost = MethodType(never, cancelled_judge)
    task = asyncio.create_task(cancelled_judge.a_generate('pending'))
    await asyncio.sleep(0.01)
    cancelled = await cancelled_judge.settle_pending_calls(timeout_seconds=0.01)
    await asyncio.gather(task, return_exceptions=True)
    class Context:
        def __init__(self):
            self.usage = []

        def report_usage(self, **usage):
            self.usage.append(usage)
    context = Context()
    report_judge_usage(context, None, cancelled_judge)
    cancelled_usage = {'settled':cancelled,'cancelled':task.cancelled(),'calls_without_tokens':cancelled_judge.calls_without_tokens,'cost':cancelled_judge.cost(),'reported':context.usage}

    parent_cancelled_judge = openai_compatible_multimodal_judge(config)
    success_finished = asyncio.Event()
    async def mixed(self, client, prompt):
        if prompt == 'pending':
            await asyncio.sleep(30)
            raise AssertionError('cancelled request unexpectedly finished')
        await asyncio.sleep(0.01)
        success_finished.set()
        return httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}], 'usage':{'prompt_tokens':123,'completion_tokens':9,'cost':0.25}})
    parent_cancelled_judge._apost = MethodType(mixed, parent_cancelled_judge)
    async def gathered_requests():
        await asyncio.gather(parent_cancelled_judge.a_generate('success'), parent_cancelled_judge.a_generate('pending'))
    parent = asyncio.create_task(gathered_requests())
    await success_finished.wait()
    await asyncio.sleep(0.01)
    parent.cancel()
    try:
        await parent
    except asyncio.CancelledError:
        pass
    await parent_cancelled_judge.settle_pending_calls()
    parent_context = Context()
    report_judge_usage(parent_context, None, parent_cancelled_judge)
    parent_cancelled_usage = {'cancelled':parent.cancelled(),'uncertain_calls':parent_cancelled_judge.uncertain_calls,'calls_without_tokens':parent_cancelled_judge.calls_without_tokens,'tokens':parent_cancelled_judge.tokens,'cost':parent_cancelled_judge.cost(),'reported':parent_context.usage}

    queued_config = {**config, 'max_concurrent_calls':1}
    queued_judge = openai_compatible_multimodal_judge(queued_config)
    request_started = asyncio.Event()
    async def blocked(self, client, prompt):
        request_started.set()
        await asyncio.sleep(30)
        raise AssertionError('cancelled request unexpectedly finished')
    queued_judge._apost = MethodType(blocked, queued_judge)
    async def queued_requests():
        await asyncio.gather(queued_judge.a_generate('active'), queued_judge.a_generate('queued'))
    queued_parent = asyncio.create_task(queued_requests())
    await request_started.wait()
    await asyncio.sleep(0.01)
    queued_parent.cancel()
    try:
        await queued_parent
    except asyncio.CancelledError:
        pass
    await queued_judge.settle_pending_calls()
    queued_context = Context()
    report_judge_usage(queued_context, None, queued_judge)
    queued_usage = {'cancelled':queued_parent.cancelled(),'uncertain_calls':queued_judge.uncertain_calls,'calls_without_tokens':queued_judge.calls_without_tokens,'cost':queued_judge.cost(),'reported':queued_context.usage}

    teardown_judge = openai_compatible_multimodal_judge(config)
    teardown_started = asyncio.Event()
    from contextlib import asynccontextmanager
    class BlockingGate:
        @asynccontextmanager
        async def slot(self):
            yield type('Slot', (), {'refused':False})()
            teardown_started.set()
            await asyncio.sleep(30)
    teardown_judge._gate = BlockingGate()
    async def response_before_teardown(self, client, prompt):
        return httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}], 'usage':{'prompt_tokens':123,'completion_tokens':9,'cost':0.25}})
    teardown_judge._apost = MethodType(response_before_teardown, teardown_judge)
    teardown_task = asyncio.create_task(teardown_judge.a_generate('response received'))
    await asyncio.wait_for(teardown_started.wait(), 2)
    teardown_task.cancel()
    try:
        await teardown_task
    except asyncio.CancelledError:
        pass
    await teardown_judge.settle_pending_calls()
    teardown_context = Context()
    report_judge_usage(teardown_context, None, teardown_judge)
    teardown_usage = {'cancelled':teardown_task.cancelled(),'uncertain_calls':teardown_judge.uncertain_calls,'calls_without_tokens':teardown_judge.calls_without_tokens,'cost':teardown_judge.cost(),'reported':teardown_context.usage}

    transient_judge = openai_compatible_multimodal_judge(config)
    transient_teardown_started = asyncio.Event()
    class TransientBlockingGate:
        @asynccontextmanager
        async def slot(self):
            try:
                yield type('Slot', (), {'refused':False})()
            finally:
                transient_teardown_started.set()
                await asyncio.sleep(30)
    transient_judge._gate = TransientBlockingGate()
    async def timeout_response(self, client, prompt):
        raise httpx.ReadTimeout('provider request timed out')
    transient_judge._apost = MethodType(timeout_response, transient_judge)
    transient_task = asyncio.create_task(transient_judge.a_generate('transient response'))
    await asyncio.wait_for(transient_teardown_started.wait(), 2)
    transient_task.cancel()
    try:
        await transient_task
    except asyncio.CancelledError:
        pass
    transient_context = Context()
    report_judge_usage(transient_context, None, transient_judge)
    transient_usage = {'cancelled':transient_task.cancelled(),'uncertain_calls':transient_judge.uncertain_calls,'calls_without_tokens':transient_judge.calls_without_tokens,'cost':transient_judge.cost(),'reported':transient_context.usage}
    return {'pending':pending_usage,'cancelled':cancelled_usage,'parent_cancelled':parent_cancelled_usage,'queued_cancelled':queued_usage,'teardown_cancelled':teardown_usage,'transient_cancelled':transient_usage}
print(json.dumps(asyncio.run(scenario())))
"""
    )
    assert result["pending"] == {
        "settled": True,
        "calls": 2,
        "calls_without_tokens": 1,
        "tokens": {"input": 123, "output": 9},
        "cost": None,
    }
    assert result["cancelled"] == {
        "settled": False,
        "cancelled": True,
        "calls_without_tokens": 1,
        "cost": None,
        "reported": [
            {
                "provider": "vision-test",
                "calls": None,
                "tokens": {},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }
    assert result["parent_cancelled"] == {
        "cancelled": True,
        "uncertain_calls": 1,
        "calls_without_tokens": 1,
        "tokens": {"input": 123, "output": 9},
        "cost": None,
        "reported": [
            {
                "provider": "vision-test",
                "calls": None,
                "tokens": {"input": 123, "output": 9},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }
    assert result["queued_cancelled"] == {
        "cancelled": True,
        "uncertain_calls": 1,
        "calls_without_tokens": 1,
        "cost": None,
        "reported": [
            {
                "provider": "vision-test",
                "calls": None,
                "tokens": {},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }
    assert result["teardown_cancelled"] == {
        "cancelled": True,
        "uncertain_calls": 1,
        "calls_without_tokens": 1,
        "cost": None,
        "reported": [
            {
                "provider": "vision-test",
                "calls": None,
                "tokens": {},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }
    assert result["transient_cancelled"] == {
        "cancelled": True,
        "uncertain_calls": 1,
        "calls_without_tokens": 1,
        "cost": None,
        "reported": [
            {
                "provider": "vision-test",
                "calls": None,
                "tokens": {},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }


def test_multimodal_judge_marks_dispatched_write_error_cost_unknown() -> None:
    result = plugin_python(
        """
import asyncio, json, os
from types import MethodType
import httpx
from aibench_deepeval.judges import openai_compatible_multimodal_judge, report_judge_usage
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
config = {'kind':'openai_compatible','base_url':'http://127.0.0.1:9000/v1','model':'vision-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY'}
judge = openai_compatible_multimodal_judge(config)
dispatched = asyncio.Event()
async def reply(self, client, prompt):
    if prompt == 'write-error':
        dispatched.set()
        raise httpx.WriteError('connection dropped while writing request')
    await dispatched.wait()
    await asyncio.sleep(0.01)
    return httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}], 'usage':{'prompt_tokens':123,'completion_tokens':9,'cost':0.25}})
judge._apost = MethodType(reply, judge)
class Context:
    def __init__(self):
        self.usage = []
    def report_usage(self, **usage):
        self.usage.append(usage)
async def scenario():
    outcomes = await asyncio.gather(
        judge.a_generate('write-error'),
        judge.a_generate('successful sibling'),
        return_exceptions=True,
    )
    context = Context()
    report_judge_usage(context, None, judge)
    return {
        'outcomes':[type(outcome).__name__ if isinstance(outcome, BaseException) else outcome for outcome in outcomes],
        'uncertain_calls':judge.uncertain_calls,
        'calls_without_tokens':judge.calls_without_tokens,
        'tokens':judge.tokens,
        'cost':judge.cost(),
        'reported':context.usage,
    }
print(json.dumps(asyncio.run(scenario())))
"""
    )
    assert result == {
        "outcomes": ["WriteError", "ok"],
        "uncertain_calls": 1,
        "calls_without_tokens": 1,
        "tokens": {"input": 123, "output": 9},
        "cost": None,
        "reported": [
            {
                "provider": "vision-test",
                "calls": None,
                "tokens": {"input": 123, "output": 9},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }


def test_multimodal_judge_accounts_request_error_during_gate_teardown() -> None:
    result = plugin_python(
        """
import asyncio, json, os
from contextlib import asynccontextmanager
from types import MethodType
import httpx
from aibench_deepeval.judges import openai_compatible_multimodal_judge, report_judge_usage
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
config = {'kind':'openai_compatible','base_url':'http://127.0.0.1:9000/v1','model':'vision-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY'}
judge = openai_compatible_multimodal_judge(config)
teardown_started = asyncio.Event()
class BlockingGate:
    @asynccontextmanager
    async def slot(self):
        try:
            yield type('Slot', (), {'refused':False})()
        finally:
            teardown_started.set()
            await asyncio.sleep(30)
judge._gate = BlockingGate()
async def broken_response(self, client, prompt):
    raise httpx.DecodingError('response body could not be decoded')
judge._apost = MethodType(broken_response, judge)
class Context:
    def __init__(self):
        self.usage = []
    def report_usage(self, **usage):
        self.usage.append(usage)
async def scenario():
    task = asyncio.create_task(judge.a_generate('response decode error'))
    await asyncio.wait_for(teardown_started.wait(), 2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    context = Context()
    report_judge_usage(context, None, judge)
    return {
        'cancelled':task.cancelled(),
        'uncertain_calls':judge.uncertain_calls,
        'calls_without_tokens':judge.calls_without_tokens,
        'cost':judge.cost(),
        'reported':context.usage,
    }
print(json.dumps(asyncio.run(scenario())))
"""
    )
    assert result == {
        "cancelled": True,
        "uncertain_calls": 1,
        "calls_without_tokens": 1,
        "cost": None,
        "reported": [
            {
                "provider": "vision-test",
                "calls": None,
                "tokens": {},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }


def test_retryable_http_response_counts_call_but_keeps_cost_unknown() -> None:
    result = plugin_python(
        """
import asyncio, json, os
from types import MethodType
import httpx
from aibench_deepeval.judges import openai_compatible_judge, report_judge_usage
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
judge = openai_compatible_judge({'kind':'openai_compatible','base_url':'http://127.0.0.1:9000/v1','model':'retry-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY','retry_wait_seconds':0})
responses = [
    httpx.Response(503, json={'usage':{'cost':0.2}}),
    httpx.Response(200, json={'choices':[{'message':{'content':'ok'}}], 'usage':{'prompt_tokens':5,'completion_tokens':2,'cost':0.3}}),
]
async def reply(self, client, prompt):
    return responses.pop(0)
judge._apost = MethodType(reply, judge)
asyncio.run(judge.a_generate('retry once'))
class Context:
    usage = []
    def report_usage(self, **usage):
        self.usage.append(usage)
context = Context()
report_judge_usage(context, None, judge)
print(json.dumps({'calls':judge.calls,'calls_without_tokens':judge.calls_without_tokens,'cost':judge.cost(),'usage':context.usage}))
"""
    )
    assert result == {
        "calls": 2,
        "calls_without_tokens": 1,
        "cost": None,
        "usage": [
            {
                "provider": "retry-test",
                "calls": 2,
                "tokens": {"input": 5, "output": 2},
                "cost": None,
                "calls_without_tokens": 1,
            }
        ],
    }


def test_multimodal_judge_caps_provider_response_and_marks_error_cost_unknown() -> None:
    result = plugin_python(
        """
import asyncio, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from aibench_deepeval.judges import openai_compatible_judge
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.rfile.read(int(self.headers['Content-Length']))
        body = b'x' * (1024 * 1024 + 1)
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *_args):
        return
server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
judge = openai_compatible_judge({'kind':'openai_compatible','base_url':f'http://127.0.0.1:{server.server_port}/v1','model':'vision-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY','timeout_seconds':2})
async def run():
    try:
        await judge.a_generate('bounded response test')
    except RuntimeError as exc:
        return {'error':type(exc).__name__,'calls':judge.calls,'cost':judge.cost()}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
print(json.dumps(asyncio.run(run())))
"""
    )
    assert result == {"error": "_JudgeResponseTooLarge", "calls": 1, "cost": None}


def test_malformed_provider_usage_cannot_corrupt_judge_totals() -> None:
    result = plugin_python(
        """
import json, os
import httpx
from aibench_deepeval.judges import openai_compatible_judge
os.environ['AIBENCH_IMAGE_TEST_KEY'] = 'placeholder'
config = {'kind':'openai_compatible','base_url':'http://127.0.0.1:9000/v1','model':'usage-test','api_key_env':'AIBENCH_IMAGE_TEST_KEY','price_per_million_tokens':{'input':1,'output':1}}
cases = [
    {'usage':{'cost':-1,'prompt_tokens':20,'completion_tokens':10}},
    {'usage':{'cost':float('nan'),'prompt_tokens':20,'completion_tokens':10}},
    {'usage':{'prompt_tokens':True,'completion_tokens':-10}},
    {'usage':{'prompt_tokens':10**400,'completion_tokens':0}},
    {'usage':[]},
]
results = []
for extra in cases:
    judge = openai_compatible_judge(config)
    payload = {'choices':[{'message':{'content':'ok'}}], **extra}
    response = httpx.Response(200, content=json.dumps(payload).encode())
    try:
        judge._parse(response, None)
        error = None
    except Exception as exc:
        error = type(exc).__name__
    results.append({'error':error,'calls':judge.calls,'calls_without_tokens':judge.calls_without_tokens,'tokens':judge.tokens,'cost':judge.cost()})

judge = openai_compatible_judge(config)
try:
    judge._parse(httpx.Response(200, content=b'{bad json'), None)
except Exception as exc:
    results.append({'error':type(exc).__name__,'calls':judge.calls,'calls_without_tokens':judge.calls_without_tokens,'tokens':judge.tokens,'cost':judge.cost()})
print(json.dumps(results))
"""
    )
    assert len(result) == 6
    for usage in result:
        assert usage["calls"] == 1
        assert usage["cost"] is None
    assert [usage["calls_without_tokens"] for usage in result] == [0, 0, 1, 1, 1, 1]
    assert result[0]["tokens"] == {"input": 20, "output": 10}
    assert result[1]["tokens"] == {"input": 20, "output": 10}
    assert result[2]["tokens"] == {}
    assert result[3]["tokens"] == {"output": 0}
    assert result[4]["error"] is None
    assert result[5]["error"] == "JSONDecodeError"


def test_upstream_metrics_score_generated_images_and_reject_unsafe_evidence(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AIBENCH_IMAGE_TEST_KEY", "test-key-not-sent-as-content")
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV,
        secret_env={"IMAGE_METRIC_JUDGE_KEY": "env:AIBENCH_IMAGE_TEST_KEY"},
    )
    assert [load.error for load in loads] == [None]

    cases = [
        BenchmarkCase(
            case_id="text-image",
            input="Draw a bright red bicycle beside a tree.",
            reference=ReferenceAnswer(answer="unused by these metrics"),
        ),
        BenchmarkCase(
            case_id="edit-image",
            input={"prompt": "Turn the red bicycle blue.", "image": IMAGE},
            reference=ReferenceAnswer(answer="unused by these metrics"),
        ),
    ]
    outputs = {
        "text-image": {"text": "A bicycle beside a tree.", "image": IMAGE},
        "edit-image": {"text": "The bicycle is blue.", "image": IMAGE},
    }
    seeded = Seeded(tmp_path)
    seeded.seed(cases, [execution(case.case_id, outputs[case.case_id]) for case in cases])
    server, thread = _server()
    try:
        bindings = [
            _binding(name, server.base_url)
            for name in (
                "image_coherence",
                "image_helpfulness",
                "image_reference",
                "text_to_image",
                "image_editing",
            )
        ]
        report = seeded.score(bindings, registry=registry, timeout_seconds=60)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    outcomes = {(item.case_id, item.metric_id.rsplit(".", 1)[-1]): item for item in report.results}
    for name in ("image_coherence", "image_helpfulness", "image_reference", "text_to_image"):
        outcome = outcomes[("text-image", name)]
        assert outcome.status is ExecutionStatus.OK, outcome.reason
        assert outcome.value is not None and outcome.value.value == pytest.approx(0.8)
    edited = outcomes[("edit-image", "image_editing")]
    assert edited.status is ExecutionStatus.OK and edited.value is not None
    assert edited.value.value == pytest.approx(0.8)
    assert server.requests
    for request in server.requests:
        assert request["path"] == "/v1/chat/completions"
        assert request["auth"] == "Bearer test-key-not-sent-as-content"
        images = [
            part["image_url"]["url"]
            for part in request["body"]["messages"][1]["content"]
            if part["type"] == "image_url"
        ]
        assert images and all(image.startswith("data:image/png;base64,") for image in images)
        assert "test-key-not-sent-as-content" not in json.dumps(request["body"])
