"""MVP acceptance fixture (12-T1): a support RAG service over HTTP, standard library only.

POST /answer   {"question": str} -> {"answer": str, "retrieved": [{"doc_id", "text"}]}
POST /blackbox {"question": str} -> {"answer": str}           (no retrieval exposed)
GET  /health   -> {"status": "ok"}

Retrieval is keyword overlap over a 10-document corpus; the answer is the top document.
What `retrieved` reports is what the answer was built from, so it is real evidence.

Injected faults, so the acceptance run has something real to find:
- **Retrieval failures.** A question containing one of the `DECOYS` words scores the decoy's
  document higher than the right one, so the answer is wrong and `retrieved` shows why.
  `rag100.jsonl` marks those cases with `metadata.injected = "retrieval_failure"`.
- **Black-box endpoint.** `/blackbox` returns the same answers without retrieval, so any
  groundedness check must be reported as a gap (missing evidence), never scored.

Every POST is counted per question (`calls`), so a test can prove that resuming or
rescoring does not invoke the application again. `delay` slows every answer (seconds).

    python rag_service.py --port 8766
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CORPUS = {
    "refunds": "Refunds are available within 30 days of purchase with a receipt.",
    "warranty": "All products carry a one-year limited warranty against defects.",
    "shipping": "We ship to over 40 countries; delivery takes 5 to 10 days.",
    "hours": "Support is open Monday to Friday, 9am to 5pm UTC.",
    "password": "Reset your password from the sign-in page with Forgot password.",
    "cancel": "You can cancel a subscription at any time from Account settings.",
    "payment": "We accept Visa, Mastercard and PayPal for all orders.",
    "invoice": "Invoices are emailed after each payment and listed under Billing.",
    "privacy": "We never sell personal data; see the privacy policy for details.",
    "contact": "Email support@example.com to reach a person on our team.",
}
KEYWORDS = {
    "refunds": {"refund", "refunds", "money", "back", "return"},
    "warranty": {"warranty", "guarantee", "defect", "defective", "broken"},
    "shipping": {"ship", "shipping", "delivery", "deliver", "countries"},
    "hours": {"hours", "open", "monday", "friday", "weekend"},
    "password": {"password", "reset", "login", "sign"},
    "cancel": {"cancel", "subscription", "stop", "unsubscribe"},
    "payment": {"pay", "payment", "card", "paypal", "visa"},
    "invoice": {"invoice", "invoices", "receipt", "billing", "bill"},
    "privacy": {"privacy", "personal", "data", "sell"},
    "contact": {"contact", "email", "person", "human", "talk"},
}
# A decoy word adds weight to another document, outranking the right one.
DECOYS = {"abroad": "shipping", "urgently": "contact"}
_WORD = re.compile(r"[a-z]+")


def retrieve(question: str, k: int = 2) -> list[dict[str, str]]:
    words = _WORD.findall(question.lower())
    scores: Counter[str] = Counter()
    for word in words:
        for doc_id, keys in KEYWORDS.items():
            if word in keys:
                scores[doc_id] += 1
        if word in DECOYS:
            scores[DECOYS[word]] += 2
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return [{"doc_id": d, "text": CORPUS[d]} for d, s in ranked[:k] if s > 0]


def answer(question: str) -> dict[str, Any]:
    docs = retrieve(question)
    return {"answer": docs[0]["text"] if docs else "I don't know.", "retrieved": docs}


class RagService(ThreadingHTTPServer):
    """The service; `calls` counts POSTs per question, `delay` slows every answer."""

    daemon_threads = True

    def __init__(self, port: int = 0, delay: float = 0.0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.delay = delay
        self.calls: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"

    def handle_error(self, request: Any, client_address: Any) -> None:
        """A client that was killed mid-request (the acceptance test kills the engine)
        drops its connection; that is expected, not a service error."""
        import sys

        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)

    def total_calls(self) -> int:
        with self._lock:
            return sum(self.calls.values())

    def record(self, question: str) -> None:
        with self._lock:
            self.calls[question] += 1

    def start(self) -> RagService:
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: RagService

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
        self._send(200 if self.path == "/health" else 404, {"status": "ok"})

    def do_POST(self) -> None:
        if self.path not in ("/answer", "/blackbox"):
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            question = str(json.loads(self.rfile.read(length) or b"{}").get("question", ""))
        except (json.JSONDecodeError, AttributeError):
            self._send(400, {"error": "invalid JSON"})
            return
        self.server.record(question)
        if self.server.delay:
            time.sleep(self.server.delay)
        result = answer(question)
        if self.path == "/blackbox":
            result = {"answer": result["answer"]}
        self._send(200, result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--delay", type=float, default=0.0)
    args = parser.parse_args()
    service = RagService(args.port, args.delay)
    print(f"serving on {service.url}", flush=True)
    try:
        service.serve_forever()
    except KeyboardInterrupt:
        service.server_close()


if __name__ == "__main__":
    main()
