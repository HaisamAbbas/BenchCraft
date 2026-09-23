"""Example HTTP RAG application: keyword-overlap retrieval over a small in-memory corpus.

POST /answer {"question": str, "top_k": int} ->
    {"answer": str, "retrieved": [{"doc_id": str, "text": str, "score": int}, ...]}
GET  /health -> {"status": "ok"}

The documents in `retrieved` are the ones actually used to build the answer, so the
retrieval observation is real, not a copy of any reference context. The app reports no
token usage or cost, because it has none.

    python http_rag_app.py --port 8765
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CORPUS = (
    ("refund-policy", "Refunds may be requested within 30 days of purchase with a receipt."),
    ("warranty", "All products carry a one-year limited warranty against manufacturing defects."),
    ("shipping", "We ship to over 40 countries. International delivery takes 5 to 10 days."),
    ("support-hours", "Support is available Monday to Friday, 9am to 5pm UTC."),
)
_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "a", "an", "is", "your", "you", "do", "what", "how", "to", "of", "for"}


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOPWORDS}


def retrieve(question: str, top_k: int) -> list[dict[str, Any]]:
    query = _terms(question)
    scored = []
    for doc_id, text in CORPUS:
        doc_terms = _terms(text)
        # Prefix overlap so "refund" matches "refunds" without a stemmer.
        score = sum(1 for q in query if any(d.startswith(q) or q.startswith(d) for d in doc_terms))
        if score:
            scored.append({"doc_id": doc_id, "text": text, "score": score})
    scored.sort(key=lambda d: (-d["score"], d["doc_id"]))
    return scored[:top_k]


def answer(question: str, top_k: int) -> dict[str, Any]:
    docs = retrieve(question, top_k)
    reply = docs[0]["text"] if docs else "I could not find that in our documentation."
    return {"answer": reply, "retrieved": docs}


class RagHandler(BaseHTTPRequestHandler):
    server_version = "aibench-rag-example/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # keep test output quiet
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/answer":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "body must be JSON"})
            return
        question = request.get("question")
        top_k = request.get("top_k", 2)
        if not isinstance(question, str) or not isinstance(top_k, int) or top_k < 1:
            self._send(400, {"error": "expected string 'question' and positive integer 'top_k'"})
            return
        self._send(200, answer(question, top_k))


def make_server(host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), RagHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print(f"serving on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
