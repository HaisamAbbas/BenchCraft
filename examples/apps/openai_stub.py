"""A local OpenAI-compatible chat-completions server for trying the `openai_compatible`
runner without a paid provider. Deterministic and standard library only. It is a stand-in
for an endpoint, not a model: it proves the protocol, not any model's quality.

POST /v1/chat/completions -> a chat.completion with content, usage and, when tools are
offered and the last user message asks to book, a `book_flight` tool call request.
GET  /v1/models           -> the one model it serves

    python openai_stub.py --port 8767
"""

from __future__ import annotations

import argparse
import json
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "stub-1"
ANSWERS = (
    ("refund", "Refunds are available within 30 days of purchase."),
    ("warranty", "All products carry a one-year limited warranty."),
)


def complete(request: dict[str, Any]) -> dict[str, Any]:
    messages = request.get("messages") or []
    user = next((m for m in reversed(messages) if m.get("role") == "user"), {})
    text = str(user.get("content", ""))
    lowered = text.lower()
    message: dict[str, Any] = {"role": "assistant", "content": None}
    finish = "stop"
    if request.get("tools") and "book" in lowered:
        message["tool_calls"] = [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "book_flight", "arguments": json.dumps({"flight": "BA117"})},
            }
        ]
        finish = "tool_calls"
    else:
        message["content"] = next(
            (reply for key, reply in ANSWERS if key in lowered), "I don't know."
        )
    prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
    completion_tokens = len(str(message["content"] or "").split())
    return {
        "id": "chatcmpl-stub",
        "object": "chat.completion",
        "model": request.get("model", MODEL),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


class StubServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int]) -> None:
        super().__init__(address, Handler)
        self.calls: Counter[str] = Counter()
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: StubServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": {"message": "not found"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "body must be JSON"}})
            return
        with self.server.lock:
            self.server.calls["chat"] += 1
            self.server.requests.append(
                {"body": request, "authorization": self.headers.get("Authorization")}
            )
        if not isinstance(request.get("messages"), list):
            self._send(400, {"error": {"message": "messages must be a list"}})
            return
        self._send(200, complete(request))


def make_server(host: str = "127.0.0.1", port: int = 8767) -> StubServer:
    return StubServer((host, port))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8767)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"serving on http://{args.host}:{server.server_port}/v1", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
