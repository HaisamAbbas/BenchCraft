"""The assistant's model calls wait out a rate limit or a busy server instead of stopping
the reply ("The assistant stopped before finishing: HTTP 429 ... overloaded", from a free
endpoint). Only requests that produced nothing are retried, so a reply is never repeated;
an error retrying cannot fix (a wrong key) fails at once."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from aibench.planning.openai_provider import OpenAICompatibleConfig, OpenAICompatibleProvider
from aibench.planning.planner import PlannerError
from tests.chat_server_support import chat_server, chunk, text_stream

LIMITED = (429, {"error": {"code": "1305", "message": "overloaded"}})
COMPLETION = {
    "choices": [{"message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def _provider(base_url: str, **kwargs: Any) -> OpenAICompatibleProvider:
    config = OpenAICompatibleConfig(
        base_url=base_url, model="glm-test", retry_wait_seconds=0, **kwargs
    )
    return OpenAICompatibleProvider(config)


def test_a_streamed_reply_survives_rate_limits_and_is_not_repeated() -> None:
    seen: list[str] = []
    replies = [LIMITED, (503, b"busy"), (200, text_stream("Hel", "lo"))]
    with chat_server(replies) as server:
        provider = _provider(server.base_url)
        try:
            reply = provider.complete_stream([{"role": "user", "content": "hi"}], [], seen.append)
        finally:
            provider.close()
        assert len(server.requests) == 3
    assert reply.text == "Hello" and "".join(seen) == "Hello"  # streamed once, not three times


def test_a_plain_reply_survives_a_rate_limit() -> None:
    with chat_server([LIMITED, (200, COMPLETION)]) as server:
        provider = _provider(server.base_url)
        try:
            reply = provider.complete([{"role": "user", "content": "hi"}], [])
        finally:
            provider.close()
        assert len(server.requests) == 2
    assert reply.text == "hello"


def test_it_gives_up_after_four_attempts_with_the_servers_error() -> None:
    for streamed in (False, True):
        with chat_server([LIMITED] * 5) as server:
            provider = _provider(server.base_url)
            try:
                with pytest.raises(PlannerError, match="HTTP 429"):
                    if streamed:
                        provider.complete_stream(
                            [{"role": "user", "content": "hi"}], [], lambda _t: None
                        )
                    else:
                        provider.complete([{"role": "user", "content": "hi"}], [])
            finally:
                provider.close()
            assert len(server.requests) == 4, streamed  # four attempts, then the error


def test_a_wrong_key_or_bad_request_is_not_retried() -> None:
    for status in (400, 401, 404):
        with chat_server([(status, {"error": "no"}), (200, COMPLETION)]) as server:
            provider = _provider(server.base_url)
            try:
                with pytest.raises(PlannerError, match=f"HTTP {status}"):
                    provider.complete([{"role": "user", "content": "hi"}], [])
            finally:
                provider.close()
            assert len(server.requests) == 1, status


# OpenRouter's wording when the provider it routed to failed (seen from DeepSeek V4 Flash).
UPSTREAM_FAILED = (
    400,
    {
        "error": {
            "message": "Provider returned error",
            "code": 400,
            "metadata": {
                "raw": '{"error":{"code":"invalid_request_error","message":"The request was '
                "rejected. Possible causes: input exceeds the model's maximum context length, "
                'or the request contains invalid parameters."}}',
                "provider_name": "SailResearch",
            },
        }
    },
)


def test_a_failure_of_the_provider_openrouter_routed_to_is_asked_again() -> None:
    """The assistant stopped with HTTP 400 "Provider returned error"; the same request went
    through a minute later. A 400 of the routed-to provider is retried; any other 400 is not
    (the test above)."""
    for streamed in (False, True):
        replies = [UPSTREAM_FAILED, (200, text_stream("hi") if streamed else COMPLETION)]
        with chat_server(replies) as server:
            provider = _provider(server.base_url)
            try:
                if streamed:
                    reply = provider.complete_stream(
                        [{"role": "user", "content": "hi"}], [], lambda _t: None
                    )
                else:
                    reply = provider.complete([{"role": "user", "content": "hi"}], [])
            finally:
                provider.close()
            assert len(server.requests) == 2, streamed
        assert reply.text in ("hi", "hello")


def test_a_dropped_connection_is_retried_but_a_stream_cut_after_text_is_not() -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=COMPLETION)

    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:9/v1", model="m", retry_wait_seconds=0),
        transport=httpx.MockTransport(flaky),
    )
    try:
        assert provider.complete([{"role": "user", "content": "hi"}], []).text == "hello"
        assert calls["n"] == 2
    finally:
        provider.close()

    class Cut(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            yield b"data: " + json.dumps(chunk("partial")).encode() + b"\n\n"
            raise httpx.ReadError("connection reset")

    starts = {"n": 0}

    def cut(request: httpx.Request) -> httpx.Response:
        starts["n"] += 1
        return httpx.Response(200, stream=Cut())

    seen: list[str] = []
    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:9/v1", model="m", retry_wait_seconds=0),
        transport=httpx.MockTransport(cut),
    )
    try:
        with pytest.raises(PlannerError, match="ReadError"):
            provider.complete_stream([{"role": "user", "content": "hi"}], [], seen.append)
    finally:
        provider.close()
    assert starts["n"] == 1  # text had streamed: retrying would have repeated it
    assert "".join(seen) == "partial"


def test_retry_after_and_the_wait_are_bounded() -> None:
    provider = _provider("https://example.test/v1")
    slow = OpenAICompatibleProvider(
        OpenAICompatibleConfig(base_url="https://example.test/v1", model="m")
    )
    try:
        assert provider._pause(0, 429, None) == 0  # retry_wait_seconds=0 in tests
        assert provider._pause(0, 429, "3") == 3
        assert provider._pause(0, 429, "9999") == 15  # never a long stall
        assert provider._pause(0, 429, "Wed, 21 Oct 2026 07:28:00 GMT") == 0
        assert provider._pause(3, 429, None) is None  # the fourth attempt is the last
        assert provider._pause(0, 401, None) is None and provider._pause(0, 200, None) is None
        assert provider._pause(0, None, None) == 0  # a failed connection is retried
        assert [slow._pause(n, 503, None) for n in range(3)] == [2, 4, 8]  # doubles
    finally:
        provider.close()
        slow.close()
