"""Streaming response observers used by HTTP-backed application runners."""

from __future__ import annotations

import codecs
import json
import math
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ResponseStreamResult:
    document: Any | None
    metrics: dict[str, Any]
    integrity: dict[str, Any]
    error: str | None


class ResponseStream(Protocol):
    accept: str

    def feed(self, chunk: bytes) -> None: ...

    def finish(self) -> ResponseStreamResult: ...


_MAX_INTER_TOKEN_INTERVALS = 100_000


def _nearest_rank(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(percentile / 100 * len(ordered))) - 1]


def _interval_summary(values: list[float], *, observed: int, truncated: bool) -> dict[str, Any]:
    p50 = _nearest_rank(values, 50)
    p95 = _nearest_rank(values, 95)
    p99 = _nearest_rank(values, 99)
    return {
        "samples": len(values),
        "observed_intervals": observed,
        "min_ms": round(min(values), 3) if values else None,
        "mean_ms": round(math.fsum(values) / len(values), 3) if values else None,
        "p50_ms": round(p50, 3) if p50 is not None else None,
        "p95_ms": round(p95, 3) if p95 is not None else None,
        "p99_ms": round(p99, 3) if p99 is not None else None,
        "max_ms": round(max(values), 3) if values else None,
        "samples_truncated": truncated,
    }


class ChatCompletionSSE:
    """Accumulate OpenAI-compatible chat-completion SSE events and client timings.

    Inter-token observations are receive-time intervals between content-bearing SSE delta
    events. Providers may put more than one tokenizer token in a delta, and network buffering
    can deliver multiple deltas together, so they are explicitly transport-level observations.
    """

    accept = "text/event-stream"

    def __init__(self, *, expected_choices: int, request_started: float | None = None) -> None:
        self.expected_choices = expected_choices
        self.request_started = (
            request_started if request_started is not None else time.perf_counter()
        )
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._at_stream_start = True
        self._buffer = ""
        self._event_data: list[str] = []
        self._choices: dict[int, dict[str, Any]] = {}
        self._tool_calls: dict[int, dict[int, dict[str, Any]]] = {}
        self._metadata: dict[str, Any] = {}
        self._usage: dict[str, Any] | None = None
        self._errors: list[str] = []
        self._data_events = 0
        self._done_seen = False
        self._done_at: float | None = None
        self._content_times: list[float] = []
        self._last_content_time: float | None = None
        self._observed_intervals = 0
        self._intervals_truncated = False
        self._result: ResponseStreamResult | None = None

    def feed(self, chunk: bytes) -> None:
        if self._result is not None:
            return
        decoded = self._decoder.decode(chunk)
        if decoded:
            if self._at_stream_start:
                decoded = decoded.removeprefix("\ufeff")
                self._at_stream_start = False
            self._buffer += decoded
        self._drain(received_at=time.perf_counter())

    def _drain(self, *, received_at: float, final: bool = False) -> None:
        """Process complete SSE lines, supporting LF, CRLF, and CR delimiters."""
        text = self._buffer
        start = 0
        while start < len(text):
            positions = [
                position
                for position in (text.find("\r", start), text.find("\n", start))
                if position >= 0
            ]
            if not positions:
                break
            position = min(positions)
            if self._buffer[position] == "\r":
                if position + 1 == len(text) and not final:
                    break  # defer CRLF versus CR until the next received chunk
                width = 2 if text[position + 1 : position + 2] == "\n" else 1
            else:
                width = 1
            line = text[start:position]
            start = position + width
            self._line(line, received_at=received_at)
        self._buffer = text[start:]
        if final and self._buffer:
            self._line(self._buffer, received_at=received_at)
            self._buffer = ""

    def _line(self, line: str, *, received_at: float) -> None:
        if line == "":
            self._dispatch_event(received_at=received_at)
            return
        if line.startswith(":"):
            return
        field, separator, value = line.partition(":")
        if field != "data":
            return
        if separator and value.startswith(" "):
            value = value[1:]
        self._event_data.append(value)

    def _dispatch_event(self, *, received_at: float) -> None:
        if not self._event_data:
            return
        data = "\n".join(self._event_data)
        self._event_data.clear()
        if data == "[DONE]":
            if self._done_seen:
                self._add_error("event_after_done_marker")
                return
            self._done_seen = True
            self._done_at = received_at
            return
        if self._done_seen:
            self._add_error("event_after_done_marker")
            return
        self._data_events += 1
        try:
            event = json.loads(data)
        except (json.JSONDecodeError, ValueError):
            self._add_error("malformed_json_event")
            return
        if not isinstance(event, Mapping):
            self._add_error("non_object_event")
            return
        for key in ("id", "created", "model"):
            value = event.get(key)
            if value is not None:
                self._metadata[key] = value
        choices = event.get("choices", [])
        if not isinstance(choices, list):
            self._add_error("choices_not_a_list")
            return
        usage = event.get("usage")
        if usage is not None:
            if isinstance(usage, Mapping):
                self._usage = dict(usage)
            else:
                self._add_error("usage_not_an_object")
        for raw_choice in choices:
            self._choice(raw_choice, received_at=received_at)

    def _choice(self, raw: Any, *, received_at: float) -> None:
        if not isinstance(raw, Mapping):
            self._add_error("choice_not_an_object")
            return
        index = raw.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            self._add_error("choice_index_invalid")
            return
        if index >= self.expected_choices:
            self._add_error("choice_index_out_of_range")
            return
        choice = self._choices.setdefault(
            index,
            {
                "index": index,
                "message": {"role": "assistant", "content": None},
                "finish_reason": None,
            },
        )
        if choice["finish_reason"] is not None:
            self._add_error("choice_event_after_finish_reason")
            return
        delta = raw.get("delta", {})
        if not isinstance(delta, Mapping):
            self._add_error("choice_delta_not_an_object")
            return
        message = choice["message"]
        role = delta.get("role")
        if isinstance(role, str):
            message["role"] = role
        content = delta.get("content")
        if content is not None:
            if not isinstance(content, str):
                self._add_error("content_delta_not_text")
            else:
                message["content"] = (message["content"] or "") + content
                if content:
                    self._content_event(received_at=received_at)
        refusal = delta.get("refusal")
        if isinstance(refusal, str):
            message["refusal"] = message.get("refusal", "") + refusal
        tool_calls = delta.get("tool_calls")
        if tool_calls is not None:
            self._tool_call_deltas(index, tool_calls)
        function_call = delta.get("function_call")
        if function_call is not None:
            if not isinstance(function_call, Mapping):
                self._add_error("function_call_delta_not_an_object")
            else:
                accumulated = message.setdefault("function_call", {"name": "", "arguments": ""})
                self._append_string(accumulated, function_call, "name")
                self._append_string(accumulated, function_call, "arguments")
        finish_reason = raw.get("finish_reason")
        if finish_reason is not None:
            if isinstance(finish_reason, str) and finish_reason:
                choice["finish_reason"] = finish_reason
            else:
                self._add_error("finish_reason_invalid")

    def _tool_call_deltas(self, choice_index: int, raw_calls: Any) -> None:
        if not isinstance(raw_calls, list):
            self._add_error("tool_calls_delta_not_a_list")
            return
        calls = self._tool_calls.setdefault(choice_index, {})
        for raw in raw_calls:
            if not isinstance(raw, Mapping):
                self._add_error("tool_call_delta_not_an_object")
                continue
            index = raw.get("index")
            if isinstance(index, bool) or not isinstance(index, int) or index < 0:
                self._add_error("tool_call_index_invalid")
                continue
            call = calls.setdefault(
                index,
                {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
            )
            self._append_string(call, raw, "id")
            self._append_string(call, raw, "type")
            function = raw.get("function")
            if function is not None:
                if not isinstance(function, Mapping):
                    self._add_error("tool_function_delta_not_an_object")
                else:
                    self._append_string(call["function"], function, "name")
                    self._append_string(call["function"], function, "arguments")

    def _append_string(self, target: dict[str, Any], source: Mapping[str, Any], key: str) -> None:
        value = source.get(key)
        if value is None:
            return
        if not isinstance(value, str):
            self._add_error(f"{key}_delta_not_text")
            return
        target[key] = target.get(key, "") + value

    def _content_event(self, *, received_at: float) -> None:
        now = received_at
        if self._last_content_time is not None:
            self._observed_intervals += 1
            if len(self._content_times) - 1 < _MAX_INTER_TOKEN_INTERVALS:
                self._content_times.append(now)
            else:
                self._intervals_truncated = True
        else:
            self._content_times.append(now)
        self._last_content_time = now

    def _add_error(self, error: str) -> None:
        if error not in self._errors and len(self._errors) < 8:
            self._errors.append(error)

    def finish(self) -> ResponseStreamResult:
        if self._result is not None:
            return self._result
        finished = time.perf_counter()
        decoded = self._decoder.decode(b"", final=True)
        if decoded:
            if self._at_stream_start:
                decoded = decoded.removeprefix("\ufeff")
                self._at_stream_start = False
            self._buffer += decoded
        self._drain(received_at=finished, final=True)
        self._dispatch_event(received_at=finished)
        choices = sorted(self._choices.values(), key=lambda item: item["index"])
        for choice in choices:
            calls = self._tool_calls.get(choice["index"])
            if calls:
                choice["message"]["tool_calls"] = [calls[key] for key in sorted(calls)]
        finish_reasons = sum(choice["finish_reason"] is not None for choice in choices)
        expected = self.expected_choices
        complete = (
            self._done_seen
            and self._data_events > 0
            and expected > 0
            and len(choices) == expected
            and finish_reasons == expected
            and not self._errors
        )
        integrity = {
            "complete": complete,
            "done_marker_seen": self._done_seen,
            "expected_choices": expected,
            "received_choices": len(choices),
            "choices_with_finish_reason": finish_reasons,
            "errors": list(self._errors),
        }
        document: dict[str, Any] = {
            "object": "chat.completion",
            **self._metadata,
            "choices": choices,
        }
        if self._usage is not None:
            document["usage"] = self._usage
        intervals = [
            (right - left) * 1000
            for left, right in zip(self._content_times, self._content_times[1:])
        ]
        generation_end = self._done_at if self._done_at is not None else finished
        first_content = self._content_times[0] if self._content_times else None
        ttft = (first_content - self.request_started) * 1000 if first_content is not None else None
        generation_ms = (
            (generation_end - first_content) * 1000 if first_content is not None else None
        )
        tokens = self._usage.get("completion_tokens") if self._usage is not None else None
        if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
            tokens = None
        tokens_per_second = None
        if (
            complete
            and tokens is not None
            and tokens > 0
            and generation_ms is not None
            and generation_ms > 0
        ):
            try:
                rate = tokens * 1000 / generation_ms
            except OverflowError:
                pass
            else:
                if math.isfinite(rate):
                    tokens_per_second = rate
        metrics = {
            "time_to_first_token_ms": round(ttft, 3) if ttft is not None and ttft >= 0 else None,
            "generation_ms": (
                round(generation_ms, 3)
                if generation_ms is not None and generation_ms >= 0
                else None
            ),
            "output_tokens": tokens,
            "output_tokens_per_second": (
                round(tokens_per_second, 3) if tokens_per_second is not None else None
            ),
            "inter_token_latency_ms": _interval_summary(
                intervals,
                observed=self._observed_intervals,
                truncated=self._intervals_truncated,
            ),
            "integrity": integrity,
        }
        error = None
        if self._errors:
            error = "stream contains malformed chat-completion events"
        elif not complete:
            error = "stream ended before a complete chat-completion response"
        self._result = ResponseStreamResult(document, metrics, integrity, error)
        return self._result
