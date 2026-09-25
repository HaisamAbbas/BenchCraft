"""A scripted assistant model behind an OpenAI-compatible endpoint (22-T3, E2E-01).

A deterministic stand-in for the chat model, so the installed `aibench chat` can be
driven end to end over its real provider path (HTTP, tool calls, streaming) without a
paid model. It proves the harness side of the dialogue, not any model's quality.

Each request consumes the next step of the script, in order:

    {"call": "propose_plan_patch", "args": {"expected_revision": "$revision", ...}}
    {"say": "Updated the draft. Run it?"}

Placeholders in arguments are filled from what the assistant was actually sent, like a
model reading its context:

- `$revision`: the latest draft revision reported by a tool result, else the one in the
  session-state briefing;
- `$question_id`: the latest question asked (`asked`) or `question_id` reported by a tool
  result, else the briefing's;
- `$run_id`: the latest `run_id` reported by a tool result or the briefing.

A request beyond the end of the script gets HTTP 500, so an unscripted extra model call
fails loudly. Every request is recorded (`requests`) for the caller's assertions.

    python scripted_assistant.py --script script.json --port 8790
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL = "scripted-1"


def _find(value: Any, key: str) -> Any:
    """The last value of `key` anywhere in a JSON value (depth-first, later wins)."""
    found = None
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key and not isinstance(v, (dict, list)):
                found = v
            inner = _find(v, key)
            if inner is not None:
                found = inner
    elif isinstance(value, list):
        for item in value:
            inner = _find(item, key)
            if inner is not None:
                found = inner
    return found


KEYS = {
    "$revision": ("revision",),
    "$question_id": ("asked", "question_id"),
    "$run_id": ("run_id",),
}


def _context_value(messages: list[dict[str, Any]], keys: tuple[str, ...]) -> Any:
    for message in reversed(messages):
        if message.get("role") != "tool":
            continue
        try:
            data = json.loads(message.get("content") or "null")
        except json.JSONDecodeError:
            continue
        for key in keys:
            value = _find(data, key)
            if value is not None:
                return value
    for message in messages:
        content = str(message.get("content") or "")
        if content.startswith("Session state"):
            briefing = json.loads(content.split("\n", 1)[1])
            for key in keys:
                value = _find(briefing, key)
                if value is not None:
                    return value
    return None


def _fill(value: Any, messages: list[dict[str, Any]]) -> Any:
    if isinstance(value, str) and value in KEYS:
        return _context_value(messages, KEYS[value])
    if isinstance(value, dict):
        return {k: _fill(v, messages) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, messages) for v in value]
    return value


class ScriptedAssistant(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, script: list[dict[str, Any]], port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.script = list(script)
        self.position = 0
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/v1"

    @property
    def remaining(self) -> int:
        return len(self.script) - self.position

    def next_message(self, request: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            self.requests.append(request)
            if self.position >= len(self.script):
                return None
            step = self.script[self.position]
            self.position += 1
        messages = request.get("messages") or []
        if "say" in step:
            return {"role": "assistant", "content": step["say"]}
        arguments = _fill(step.get("args", {}), messages)
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{self.position}",
                    "type": "function",
                    "function": {"name": step["call"], "arguments": json.dumps(arguments)},
                }
            ],
        }

    def start(self) -> ScriptedAssistant:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: ScriptedAssistant
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        payload = {"object": "list", "data": [{"id": MODEL, "object": "model"}]}
        self._send(200, json.dumps(payload).encode(), "application/json")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        message = self.server.next_message(request)
        if message is None:
            error = {"error": {"message": "the script has no step for this request"}}
            self._send(500, json.dumps(error).encode(), "application/json")
            return
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        if not request.get("stream"):
            completion = {
                "id": "chatcmpl-scripted",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                "usage": usage,
            }
            self._send(200, json.dumps(completion).encode(), "application/json")
            return
        # Server-sent events, in the chunk shape of the official SDK's ChatCompletionChunk.
        delta: dict[str, Any] = {"role": "assistant"}
        if message.get("content"):
            delta["content"] = message["content"]
        if message.get("tool_calls"):
            delta["tool_calls"] = [{"index": 0, **message["tool_calls"][0]}]
        chunks = [
            {"choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]},
            {"choices": [], "usage": usage},
        ]
        body = "".join(
            f"data: {json.dumps({'id': 'chatcmpl-scripted', 'model': MODEL, **c})}\n\n"
            for c in chunks
        )
        self._send(200, (body + "data: [DONE]\n\n").encode(), "text/event-stream")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--script", required=True, help="JSON file with a list of steps")
    parser.add_argument("--port", type=int, default=8790)
    args = parser.parse_args()
    with open(args.script, encoding="utf-8") as handle:
        server = ScriptedAssistant(json.load(handle), port=args.port)
    print(f"serving on {server.url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
