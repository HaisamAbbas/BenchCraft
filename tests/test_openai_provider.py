"""The OpenAI-compatible planner provider against a local HTTP server that speaks the
documented Chat Completions subset (07-T3).

Local integration only: these tests check our request shape, response parsing, secret
handling and egress policy — not compatibility with any live service (no live provider was
called; see reports/07.md)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from aibench.planning.openai_provider import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
    provider_denials,
)
from aibench.planning.planner import PlannerError, plan_with_model
from aibench.security.policy import ExecutionPolicy
from tests.planning_support import planning_inputs, write_app, write_dataset

KEY = "sk-test-SECRET-value-123"


class ChatServer(ThreadingHTTPServer):
    def __init__(self, replies: list[tuple[int, Any]]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.replies = replies
        self.requests: list[dict[str, Any]] = []


class _Handler(BaseHTTPRequestHandler):
    server: ChatServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(
            {
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "accept_encoding": self.headers.get("Accept-Encoding"),
                "body": body,
            }
        )
        status, payload = self.server.replies.pop(0)
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def chat_server(replies: list[tuple[int, Any]]) -> Iterator[ChatServer]:
    server = ChatServer(replies)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def completion(
    tool_calls: list[dict[str, Any]] | None = None, content: str | None = None
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 321, "completion_tokens": 45, "total_tokens": 366},
    }


def tool_call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


def _provider(server: ChatServer, **fields: Any) -> OpenAICompatibleProvider:
    host, port = server.server_address[:2]
    config = OpenAICompatibleConfig(
        base_url=f"http://{host}:{port}/v1",
        model="planner-model",
        api_key="env:PLANNER_KEY",
        **fields,
    )
    return OpenAICompatibleProvider(config, environ={"PLANNER_KEY": KEY})


def test_request_shape_auth_and_tool_call_parsing() -> None:
    reply = completion([tool_call("list_evaluators", {})])
    with chat_server([(200, reply)]) as server:
        provider = _provider(server)
        result = provider.complete(
            [{"role": "user", "content": "hi"}],
            [{"type": "function", "function": {"name": "list_evaluators", "parameters": {}}}],
        )
        provider.close()
    [request] = server.requests
    assert request["path"] == "/v1/chat/completions"
    assert request["auth"] == f"Bearer {KEY}"
    assert request["accept_encoding"] == "gzip, deflate"
    body = request["body"]
    assert (body["model"], body["tool_choice"], body["temperature"], body["seed"]) == (
        "planner-model",
        "auto",
        0.0,
        0,
    )
    assert body["tools"][0]["function"]["name"] == "list_evaluators"
    assert result.tool_calls[0].name == "list_evaluators" and result.tool_calls[0].arguments == "{}"
    assert (result.prompt_tokens, result.completion_tokens) == (321, 45)


def test_errors_are_planner_errors_with_the_key_redacted() -> None:
    with chat_server([(401, {"error": {"message": f"bad key {KEY}"}})]) as server:
        provider = _provider(server)
        with pytest.raises(PlannerError) as info:
            provider.complete([], [])
        provider.close()
    assert "HTTP 401" in str(info.value)
    assert KEY not in str(info.value) and "<redacted:env:PLANNER_KEY>" in str(info.value)


def test_malformed_and_oversized_responses_are_rejected() -> None:
    with chat_server([(200, b"not json"), (200, {"choices": []})]) as server:
        provider = _provider(server)
        for _ in range(2):
            with pytest.raises(PlannerError, match="unexpected chat completion response"):
                provider.complete([], [])
        provider.close()
    big = completion(content="x" * 2_100_000)
    with chat_server([(200, big)]) as server:
        provider = _provider(server)
        with pytest.raises(PlannerError, match="exceeds 2000000 bytes"):
            provider.complete([], [])
        provider.close()


def test_egress_policy_for_the_planner_endpoint() -> None:
    remote = OpenAICompatibleConfig(
        base_url="https://api.example.com/v1", model="m", api_key="env:K"
    )
    endpoint = (
        "planner endpoint https://api.example.com:443/ is not an approved destination (the "
        "planning briefing would leave this machine); add it to allowed_planner_origins"
    )
    assert provider_denials(remote, ExecutionPolicy()) == [
        endpoint,
        "secret env:K is not allowed by the policy",
    ]
    allowed = ExecutionPolicy(
        allowed_planner_origins=("https://api.example.com",), allowed_secret_refs=("env:K",)
    )
    assert provider_denials(remote, allowed) == []
    creds = OpenAICompatibleConfig(base_url="https://user:pw@api.example.com/v1", model="m")
    assert provider_denials(creds, allowed) == [
        "planner base_url must not contain credentials; use api_key"
    ]
    local = OpenAICompatibleConfig(base_url="http://127.0.0.1:8080/v1", model="m")
    assert provider_denials(local, ExecutionPolicy()) == []


def test_the_bounded_loop_runs_over_the_real_provider(tmp_path: Path) -> None:
    app = write_app(tmp_path)
    dataset = write_dataset(tmp_path, [{"case_id": "a", "input": "q", "expected_output": "x"}])
    inputs = planning_inputs(tmp_path, app, dataset, ["wrong answers"])
    proposal = {
        "objectives": [
            {"objective_id": "o1", "text": "wrong answers", "concepts": ["correctness"]}
        ],
        "metrics": [
            {
                "metric": "native.exact_match@1.0.0",
                "objective_ids": ["o1"],
                "rationale": "references exist",
            }
        ],
    }
    replies = [
        (200, completion([tool_call("summarize_dataset", {})])),
        (200, completion([tool_call("write_plan_draft", proposal, "call_2")])),
    ]
    with chat_server(replies) as server:
        provider = _provider(server)
        outcome = plan_with_model(inputs, provider)
        provider.close()
    assert outcome.provenance.fallback_reason is None and outcome.validation.executable
    assert outcome.provenance.prompt_tokens == 642
    second = server.requests[1]["body"]["messages"]
    assert second[-2]["tool_calls"][0]["id"] == "call_1"
    assert second[-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": second[-1]["content"],
    }
