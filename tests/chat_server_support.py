"""A local HTTP server speaking the documented Chat Completions subset, including
streamed replies (server-sent events). Local integration only: it checks our request
shape and parsing, not compatibility with any live service."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

Reply = tuple[int, Any]  # (status, JSON payload | bytes | SSE chunk list)


class ChatServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, replies: list[Reply]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.replies = replies
        self.requests: list[dict[str, Any]] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class Stream(list[dict[str, Any]]):
    """Chunks to send as a server-sent event stream, then `data: [DONE]` unless
    `done=False`."""

    def __init__(self, chunks: list[dict[str, Any]], *, done: bool = True) -> None:
        super().__init__(chunks)
        self.done = done


class _Handler(BaseHTTPRequestHandler):
    server: ChatServer

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization"), "body": body}
        )
        status, payload = self.server.replies.pop(0)
        if isinstance(payload, Stream):
            raw = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in payload)
            if payload.done:
                raw += b"data: [DONE]\n\n"
            content_type = "text/event-stream"
        else:
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            content_type = "application/json"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        pass


@contextmanager
def chat_server(replies: list[Reply]) -> Iterator[ChatServer]:
    server = ChatServer(replies)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def chunk(
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    delta: dict[str, Any] = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    data: dict[str, Any] = {
        "id": "chatcmpl-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "local",
        "choices": [] if usage is not None else [{"index": 0, "delta": delta}],
    }
    if usage is not None:
        data["usage"] = usage
    return data


def text_stream(*fragments: str) -> Stream:
    """A streamed reply made of text fragments, with usage in the final chunk."""
    return Stream(
        [chunk(f) for f in fragments] + [chunk(usage={"prompt_tokens": 7, "completion_tokens": 3})]
    )


def tool_stream(name: str, arguments: dict[str, Any], call_id: str = "call-1") -> Stream:
    """A streamed tool call whose arguments arrive in two fragments."""
    text = json.dumps(arguments)
    half = len(text) // 2
    return Stream(
        [
            chunk(
                tool_calls=[
                    {
                        "index": 0,
                        "id": call_id,
                        "type": "function",
                        "function": {"name": name, "arguments": text[:half]},
                    }
                ]
            ),
            chunk(tool_calls=[{"index": 0, "function": {"arguments": text[half:]}}]),
            chunk(usage={"prompt_tokens": 9, "completion_tokens": 4}),
        ]
    )
