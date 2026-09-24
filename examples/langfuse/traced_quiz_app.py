"""A quiz HTTP application instrumented the way a Langfuse-traced app would be (Prompt 17).

It answers from a fixed table and, for each request, records a trace in the Langfuse
stand-in under trace ID = the request's `X-Request-ID`: an agent span (no usage) with a
model generation beneath it (with token usage). It sends no trace for the water question,
so an execution without a trace is exercised too.

    make_server(langfuse_url)   # POST /answer {"input": ...} -> {"answer": ...}
"""

from __future__ import annotations

import json
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ANSWERS = {
    "What is 2+2?": ("4", 12, 1),
    "What is the capital of France?": ("Paris", 15, 2),
    "What is the largest planet?": ("Saturn", 14, 1),  # wrong on purpose
    "What is the chemical symbol for water?": ("H2O", 16, 2),
}
UNTRACED = "What is the chemical symbol for water?"


class QuizServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, langfuse_url: str, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.langfuse_url = langfuse_url.rstrip("/")


class _Handler(BaseHTTPRequestHandler):
    server: QuizServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        question = request.get("input")
        question = question[-1]["content"] if isinstance(question, list) else str(question)
        answer, prompt_tokens, completion_tokens = ANSWERS.get(question, ("I don't know.", 5, 3))
        trace_id = self.headers.get("X-Request-ID")
        if trace_id and question != UNTRACED:
            self._trace(trace_id, prompt_tokens, completion_tokens)
        body = json.dumps({"answer": answer}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _trace(self, trace_id: str, prompt_tokens: int, completion_tokens: int) -> None:
        observations = [
            {"id": f"{trace_id[:8]}-agent", "traceId": trace_id, "type": "SPAN",
             "name": "quiz-agent", "parentObservationId": None, "level": "DEFAULT"},
            {"id": f"{trace_id[:8]}-gen", "traceId": trace_id, "type": "GENERATION",
             "name": "answer", "parentObservationId": f"{trace_id[:8]}-agent",
             "level": "DEFAULT", "model": "quiz-model",
             "usageDetails": {"input": prompt_tokens, "output": completion_tokens,
                              "total": prompt_tokens + completion_tokens}},
        ]  # fmt: skip
        request = urllib.request.Request(
            f"{self.server.langfuse_url}/_stub/observations",
            data=json.dumps(observations).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=10).read()


def make_server(langfuse_url: str, port: int = 0) -> QuizServer:
    return QuizServer(langfuse_url, port)
