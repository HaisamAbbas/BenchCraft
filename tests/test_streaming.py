from __future__ import annotations

import json
import time

from aibench.core.models import ExecutionResult, ExecutionStatus
from aibench.runners import streaming as streaming_module
from aibench.runners.streaming import ChatCompletionSSE
from aibench.services.performance import stream_performance_summary


def _event(value: dict[str, object]) -> bytes:
    return b"data: " + json.dumps(value, ensure_ascii=False).encode("utf-8") + b"\r\n\r\n"


def test_chat_completion_sse_measures_content_deltas_and_rebuilds_response() -> None:
    stream = ChatCompletionSSE(expected_choices=1, request_started=time.perf_counter() - 0.01)
    chunks = [
        _event(
            {
                "id": "chat-1",
                "model": "test",
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            }
        ),
        _event({"choices": [{"index": 0, "delta": {"content": "café "}, "finish_reason": None}]}),
        _event({"choices": [{"index": 0, "delta": {"content": "answer"}, "finish_reason": None}]}),
        _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
        _event({"choices": [], "usage": {"completion_tokens": 2, "total_tokens": 5}}),
        b"data: [DONE]\r\n\r\n",
    ]
    stream.feed(chunks[0])
    split = chunks[1].index("é".encode()) + 1
    stream.feed(chunks[1][:split])
    time.sleep(0.005)
    stream.feed(chunks[1][split:])
    time.sleep(0.005)
    for chunk in chunks[2:]:
        stream.feed(chunk)
        time.sleep(0.001)

    result = stream.finish()

    assert result.error is None
    assert result.document["choices"][0]["message"]["content"] == "café answer"
    assert result.document["usage"]["completion_tokens"] == 2
    assert result.metrics["time_to_first_token_ms"] >= 10
    assert result.metrics["inter_token_latency_ms"]["samples"] == 1
    assert result.metrics["output_tokens_per_second"] > 0
    assert result.integrity["complete"] is True


def test_chat_completion_sse_reassembles_incremental_tool_calls() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(
        _event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": '{"city":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            }
        )
    )
    stream.feed(
        _event(
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [{"index": 0, "function": {"arguments": '"Lahore"}'}}]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            }
        )
    )
    stream.feed(b"data: [DONE]\n\n")

    result = stream.finish()

    tool_call = result.document["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "call_1"
    assert tool_call["function"] == {"name": "lookup", "arguments": '{"city":"Lahore"}'}
    assert result.integrity["complete"] is True


def test_chat_completion_sse_rejects_missing_completion_markers() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(
        _event({"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]})
    )
    stream.feed(_event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}))

    result = stream.finish()

    assert result.error == "stream ended before a complete chat-completion response"
    assert result.integrity["done_marker_seen"] is False
    assert result.metrics["output_tokens_per_second"] is None


def test_chat_completion_sse_marks_malformed_events_incomplete() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(b"data: {broken json}\n\n")
    stream.feed(b"data: [DONE]\n\n")

    result = stream.finish()

    assert result.error == "stream contains malformed chat-completion events"
    assert result.integrity["errors"] == ["malformed_json_event"]


def test_chat_completion_sse_ignores_a_utf8_bom_before_the_first_event() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(b"\xef")
    stream.feed(
        b"\xbb\xbf"
        + _event(
            {"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]}
        )
        + b"data: [DONE]\n\n"
    )

    result = stream.finish()

    assert result.error is None
    assert result.document["choices"][0]["message"]["content"] == "hello"


def test_chat_completion_sse_rejects_events_after_the_done_marker() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(
        _event(
            {"choices": [{"index": 0, "delta": {"content": "complete"}, "finish_reason": "stop"}]}
        )
        + b"data: [DONE]\n\n"
        + _event({"choices": [{"index": 0, "delta": {"content": "late"}}]})
    )

    result = stream.finish()

    assert result.integrity["complete"] is False
    assert result.integrity["errors"] == ["event_after_done_marker"]
    assert result.document["choices"][0]["message"]["content"] == "complete"


def test_chat_completion_sse_rejects_content_after_a_choice_finishes() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(
        _event({"choices": [{"index": 0, "delta": {"content": "complete"}, "finish_reason": None}]})
        + _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        + _event({"choices": [{"index": 0, "delta": {"content": "late"}}]})
        + b"data: [DONE]\n\n"
    )

    result = stream.finish()

    assert result.integrity["complete"] is False
    assert result.integrity["errors"] == ["choice_event_after_finish_reason"]
    assert result.document["choices"][0]["message"]["content"] == "complete"


def test_chat_completion_sse_rejects_choice_indexes_outside_requested_count() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(
        _event(
            {
                "choices": [
                    {"index": 9, "delta": {"content": "wrong choice"}, "finish_reason": "stop"}
                ]
            }
        )
        + b"data: [DONE]\n\n"
    )

    result = stream.finish()

    assert result.integrity["complete"] is False
    assert result.integrity["errors"] == ["choice_index_out_of_range"]


def test_chat_completion_sse_accepts_cr_only_line_endings() -> None:
    stream = ChatCompletionSSE(expected_choices=1)
    event = json.dumps(
        {"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]}
    )
    stream.feed(f"data: {event}\r\rdata: [DONE]\r\r".encode())

    result = stream.finish()

    assert result.error is None
    assert result.document["choices"][0]["message"]["content"] == "hello"


def test_chat_completion_sse_uses_receive_time_not_parser_time_for_deltas(monkeypatch) -> None:
    monkeypatch.setattr(streaming_module.time, "perf_counter", lambda: 1.0)
    stream = ChatCompletionSSE(expected_choices=1)
    stream.feed(
        _event({"choices": [{"index": 0, "delta": {"content": "one"}}]})
        + _event({"choices": [{"index": 0, "delta": {"content": "two"}}]})
        + _event({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        + b"data: [DONE]\n\n"
    )

    result = stream.finish()

    assert result.error is None
    assert result.metrics["inter_token_latency_ms"]["mean_ms"] == 0


def test_chat_completion_sse_ignores_unrepresentable_token_rate(monkeypatch) -> None:
    ticks = iter((1.0, 2.0, 3.0, 4.0))
    monkeypatch.setattr(streaming_module.time, "perf_counter", lambda: next(ticks))
    stream = ChatCompletionSSE(expected_choices=1, request_started=0)
    stream.feed(
        _event({"choices": [{"index": 0, "delta": {"content": "hello"}, "finish_reason": "stop"}]})
    )
    stream.feed(_event({"choices": [], "usage": {"completion_tokens": 10**1000}}))
    stream.feed(b"data: [DONE]\n\n")

    result = stream.finish()

    assert result.integrity["complete"] is True
    assert result.metrics["output_tokens"] == 10**1000
    assert result.metrics["output_tokens_per_second"] is None


def test_chat_completion_sse_bounds_inter_token_samples(monkeypatch) -> None:
    monkeypatch.setattr(streaming_module, "_MAX_INTER_TOKEN_INTERVALS", 1)
    stream = ChatCompletionSSE(expected_choices=1)
    for text in ("one", "two", "three", "four"):
        stream.feed(
            _event({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})
        )

    summary = stream.finish().metrics["inter_token_latency_ms"]

    assert summary["samples"] == 1
    assert summary["observed_intervals"] == 3
    assert summary["samples_truncated"] is True


def test_stream_performance_summary_keeps_measured_and_warmup_phases_separate() -> None:
    measured = ExecutionResult(
        execution_id="measured",
        run_id="run",
        case_id="case",
        status=ExecutionStatus.OK,
        timing={
            "streaming": {
                "time_to_first_token_ms": 20,
                "output_tokens": 10,
                "output_tokens_per_second": 40,
                "inter_token_latency_ms": {
                    "samples": 2,
                    "observed_intervals": 3,
                    "mean_ms": 10,
                    "p95_ms": 15,
                    "samples_truncated": True,
                },
                "integrity": {"complete": True},
            }
        },
    )
    failed = ExecutionResult(
        execution_id="failed",
        run_id="run",
        case_id="failed-case",
        status=ExecutionStatus.ERROR,
        timing={
            "streaming": {
                "time_to_first_token_ms": None,
                "output_tokens": None,
                "output_tokens_per_second": None,
                "inter_token_latency_ms": {"samples": 0, "observed_intervals": 0},
                "integrity": {"complete": False},
            }
        },
    )
    warmup = ExecutionResult(
        execution_id="warmup",
        run_id="run",
        case_id="case",
        warmup=True,
        status=ExecutionStatus.OK,
        timing={
            "streaming": {
                "time_to_first_token_ms": 5,
                "output_tokens": 2,
                "output_tokens_per_second": 20,
                "inter_token_latency_ms": {"samples": 0, "observed_intervals": 0},
                "integrity": {"complete": True},
            }
        },
    )

    measured_summary = stream_performance_summary([measured, failed, warmup], warmup=False)
    warmup_summary = stream_performance_summary([measured, failed, warmup], warmup=True)

    assert measured_summary["requests"] == 2
    assert measured_summary["integrity"] == {"complete": 1, "incomplete": 1, "unknown": 0}
    assert measured_summary["time_to_first_token_ms"]["missing_measurements"] == 1
    assert measured_summary["inter_token_latency_ms"]["observed_intervals"] == 3
    assert measured_summary["inter_token_latency_ms"]["summarized_intervals"] == 2
    assert measured_summary["output_tokens"]["total"] == 10
    assert measured_summary["output_tokens_per_second"]["p50"] == 40
    assert "p50_ms" not in measured_summary["output_tokens_per_second"]
    assert measured_summary["output_tokens_per_second"]["missing_measurements"] == 1
    assert warmup_summary["requests"] == 1
    assert warmup_summary["time_to_first_token_ms"]["p50_ms"] == 5


def test_stream_performance_summary_excludes_invalid_interval_means() -> None:
    execution = ExecutionResult(
        execution_id="execution",
        run_id="run",
        case_id="case",
        status=ExecutionStatus.OK,
        timing={
            "streaming": {
                "inter_token_latency_ms": {
                    "samples": 2,
                    "observed_intervals": 2,
                    "mean_ms": "invalid",
                }
            }
        },
    )

    summary = stream_performance_summary([execution], warmup=False)

    assert summary["inter_token_latency_ms"]["observed_intervals"] == 2
    assert summary["inter_token_latency_ms"]["summarized_intervals"] == 0
    assert summary["inter_token_latency_ms"]["mean_ms"] is None
