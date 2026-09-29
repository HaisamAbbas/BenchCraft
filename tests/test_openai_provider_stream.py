"""Streamed replies from the OpenAI-compatible provider (09-T2), against a local server
sending the documented chunk shape (`ChatCompletionChunk`). Local integration only; no
live service was called."""

from __future__ import annotations

import re

import pytest

from aibench.planning.openai_provider import OpenAICompatibleConfig, OpenAICompatibleProvider
from aibench.planning.planner import PlannerError
from tests.chat_server_support import Stream, chat_server, chunk, text_stream, tool_stream


def _provider(base_url: str) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(OpenAICompatibleConfig(base_url=base_url, model="local"))


def test_text_fragments_arrive_in_order_and_usage_comes_from_the_last_chunk() -> None:
    with chat_server([(200, text_stream("Exact match ", "compares ", "answers."))]) as server:
        provider = _provider(server.base_url)
        seen: list[str] = []
        reply = provider.complete_stream([{"role": "user", "content": "hi"}], [], seen.append)
        provider.close()
    assert seen == ["Exact match ", "compares ", "answers."]
    assert reply.text == "Exact match compares answers."
    assert (reply.prompt_tokens, reply.completion_tokens) == (7, 3)
    body = server.requests[0]["body"]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}


def test_tool_call_fragments_are_joined_by_index() -> None:
    arguments = {"metric": "native.exact_match"}
    with chat_server([(200, tool_stream("explain_metric", arguments))]) as server:
        provider = _provider(server.base_url)
        reply = provider.complete_stream([], [], lambda _: None)
        provider.close()
    (call,) = reply.tool_calls
    assert (call.call_id, call.name, call.arguments) == (
        "call-1",
        "explain_metric",
        '{"metric": "native.exact_match"}',
    )
    assert reply.text is None


def test_streamed_text_redacts_a_secret_even_when_split_between_chunks() -> None:
    secret = "sk-streamed-secret-123456"
    with chat_server([(200, text_stream("echo: sk-streamed-", "secret-123456 :done"))]) as server:
        config = OpenAICompatibleConfig(
            base_url=server.base_url, model="local", api_key="env:PLANNER_KEY"
        )
        provider = OpenAICompatibleProvider(config, environ={"PLANNER_KEY": secret})
        seen: list[str] = []
        try:
            reply = provider.complete_stream([], [], seen.append)
        finally:
            provider.close()

    assert secret not in "".join(seen)
    assert secret not in (reply.text or "")
    assert "<redacted:env:PLANNER_KEY>" in "".join(seen)


def test_streamed_tool_arguments_are_redacted_after_fragment_assembly() -> None:
    secret = "sk-streamed-secret-123456"
    with chat_server(
        [
            (
                200,
                Stream(
                    [
                        chunk(
                            tool_calls=[
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "function": {"name": "inspect", "arguments": '{"value":"sk-'},
                                }
                            ]
                        ),
                        chunk(
                            tool_calls=[
                                {
                                    "index": 0,
                                    "function": {"arguments": 'streamed-secret-123456"}'},
                                }
                            ]
                        ),
                    ]
                ),
            )
        ]
    ) as server:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                base_url=server.base_url, model="local", api_key="env:PLANNER_KEY"
            ),
            environ={"PLANNER_KEY": secret},
        )
        try:
            reply = provider.complete_stream([], [], lambda _: None)
        finally:
            provider.close()

    assert secret not in reply.tool_calls[0].arguments
    assert "<redacted:env:PLANNER_KEY>" in reply.tool_calls[0].arguments


@pytest.mark.parametrize(
    ("reply", "problem"),
    [
        ((200, Stream([chunk("partial")], done=False)), "ended before [DONE]"),
        ((200, Stream([{"choices": "nope"}])), "choices is not a list"),
        ((200, Stream([chunk(tool_calls=[{"function": {"name": "x"}}])])), "malformed tool call"),
        # 429 and 5xx are retried (tests/test_provider_retries.py); a bad request is not.
        ((400, {"error": "boom"}), "HTTP 400"),
    ],
)
def test_malformed_or_failed_streams_are_planner_errors(
    reply: tuple[int, object], problem: str
) -> None:
    with chat_server([reply]) as server:
        provider = _provider(server.base_url)
        with pytest.raises(PlannerError, match=re.escape(problem)):
            provider.complete_stream([], [], lambda _: None)
        provider.close()
