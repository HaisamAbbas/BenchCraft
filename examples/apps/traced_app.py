"""An HTTP application that exports OpenTelemetry spans (16-T2 fixture).

For each `POST /answer` it appends one OTLP/JSON document (a JSON line) to the trace file:

    POST /answer                 root; carries the request's X-Request-ID
      agent.run                  gen_ai.usage.* = the total of the two calls below
        chat stub-1              gen_ai.usage.* for the first model call
        execute_tool lookup      gen_ai.tool.name = lookup
        chat stub-1              gen_ai.usage.* for the second model call

The agent span's usage repeats its children's, as some instrumentations do: an importer
that adds parent and child usage together double-counts. Two questions produce the partial
traces real exporters produce: one containing "sampled out" is exported unsampled, one
containing "lost root" is exported without its root span.

    python traced_app.py --port 8769 --trace-file traces.jsonl
"""

from __future__ import annotations

import argparse
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

CALLS = ((12, 5), (20, 9))  # (input, output) tokens of the two model calls


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    if isinstance(value, list):
        return {
            "key": key,
            "value": {"arrayValue": {"values": [{"stringValue": v} for v in value]}},
        }
    return {"key": key, "value": {"stringValue": str(value)}}


def spans_for(question: str, request_id: str) -> list[dict[str, Any]]:
    trace_id = secrets.token_hex(16)
    flags = 0 if "sampled out" in question else 1
    ids = {name: secrets.token_hex(8) for name in ("root", "agent", "chat1", "tool", "chat2")}

    def span(key: str, name: str, parent: str | None, attributes: list[dict[str, Any]]) -> dict:
        record = {
            "traceId": trace_id,
            "spanId": ids[key],
            "name": name,
            "flags": flags,
            "startTimeUnixNano": "1",
            "endTimeUnixNano": "2",
            "attributes": attributes,
        }
        if parent:
            record["parentSpanId"] = ids[parent]
        return record

    total_in = sum(i for i, _ in CALLS)
    total_out = sum(o for _, o in CALLS)
    spans = [
        span("root", "POST /answer", None, [_attr("http.request.header.x-request-id", [request_id])]),
        span("agent", "agent.run", "root", [
            _attr("gen_ai.usage.input_tokens", total_in),
            _attr("gen_ai.usage.output_tokens", total_out),
        ]),
        span("chat1", "chat stub-1", "agent", [
            _attr("gen_ai.request.model", "stub-1"),
            _attr("gen_ai.usage.input_tokens", CALLS[0][0]),
            _attr("gen_ai.usage.output_tokens", CALLS[0][1]),
        ]),
        span("tool", "execute_tool lookup", "agent", [
            _attr("gen_ai.operation.name", "execute_tool"), _attr("gen_ai.tool.name", "lookup"),
        ]),
        span("chat2", "chat stub-1", "agent", [
            _attr("gen_ai.request.model", "stub-1"),
            _attr("gen_ai.usage.input_tokens", CALLS[1][0]),
            _attr("gen_ai.usage.output_tokens", CALLS[1][1]),
        ]),
    ]  # fmt: skip
    if "lost root" in question:
        spans = spans[1:]
        spans[0]["attributes"].append(_attr("aibench.correlation_id", request_id))
    return spans


class TracedServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], trace_file: Path) -> None:
        super().__init__(address, Handler)
        self.trace_file = trace_file
        self.lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server: TracedServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        question = str(body.get("input", ""))
        request_id = self.headers.get("X-Request-ID", "")
        document = {
            "resourceSpans": [
                {
                    "resource": {"attributes": [_attr("service.name", "traced-app")]},
                    "scopeSpans": [{"spans": spans_for(question.lower(), request_id)}],
                }
            ]
        }
        with self.server.lock, self.server.trace_file.open("a", encoding="utf-8") as out:
            out.write(json.dumps(document) + "\n")
        reply = json.dumps({"answer": f"answered: {question}"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def make_server(trace_file: Path, host: str = "127.0.0.1", port: int = 8769) -> TracedServer:
    return TracedServer((host, port), trace_file)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    parser.add_argument("--trace-file", type=Path, default=Path("traces.jsonl"))
    args = parser.parse_args()
    server = make_server(args.trace_file, args.host, args.port)
    print(f"serving on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
