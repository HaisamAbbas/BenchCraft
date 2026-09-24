"""A local stand-in for Langfuse's public API, for connector tests (Prompt 17).

It serves the v4 endpoints the connector uses, with the field names of the generated
client in `langfuse==4.15.6`: datasets, dataset items (page-numbered), v2 observations
(cursor-paged), score creation and v3 score reads. It checks HTTP Basic auth. A test
fixture only: passing against it shows the connector follows the documented contract, not
that a live deployment behaves the same way.

Test hooks (not Langfuse endpoints): `POST /_stub/observations` lets a fixture application
record the observations its instrumentation would send; `server.inject["create_score"] =
"drop_after_commit"` stores a score and then drops the connection. Every request is kept
in `server.requests`.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

PUBLIC_KEY = "pk-lf-stub"
SECRET_KEY = "sk-lf-stub-secret"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())


class LangfuseStub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.lock = threading.Lock()
        self.datasets: dict[str, dict[str, Any]] = {}
        self.items: list[dict[str, Any]] = []
        self.observations: list[dict[str, Any]] = []
        self.scores: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.inject: dict[str, str] = {}

    def add_dataset(self, name: str, items: list[dict[str, Any]]) -> None:
        dataset_id = f"ds-{len(self.datasets) + 1}"
        self.datasets[name] = {
            "id": dataset_id, "name": name, "description": None, "metadata": {},
            "projectId": "proj-1", "createdAt": _now(), "updatedAt": _now(),
        }  # fmt: skip
        for item in items:
            self.items.append(
                {
                    "status": "ACTIVE",
                    "metadata": None,
                    "sourceTraceId": None,
                    "sourceObservationId": None,
                    "createdAt": _now(),
                    "updatedAt": "2026-09-01T10:00:00.000Z",
                    "mediaReferences": [],
                    **item,
                    "datasetId": dataset_id,
                    "datasetName": name,
                }
            )


class _Handler(BaseHTTPRequestHandler):
    server: LangfuseStub
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()
        return self.headers.get("Authorization") == f"Basic {expected}"

    def _read(self) -> tuple[str, dict[str, list[str]], Any]:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        self.server.requests.append(
            {
                "method": self.command,
                "path": parts.path,
                "query": parse_qs(parts.query),
                "body": body,
            }
        )
        return parts.path, parse_qs(parts.query), body

    def do_GET(self) -> None:
        path, query, _ = self._read()
        if not self._authorized():
            return self._send(401, {"message": "unauthorized"})
        stub = self.server
        if path.startswith("/api/public/v2/datasets/"):
            dataset = stub.datasets.get(unquote(path.rsplit("/", 1)[1]))
            return (
                self._send(200, dataset) if dataset else self._send(404, {"message": "no dataset"})
            )
        if path == "/api/public/dataset-items":
            name = query.get("datasetName", [""])[0]
            page, limit = int(query.get("page", ["1"])[0]), int(query.get("limit", ["50"])[0])
            items = [i for i in stub.items if i["datasetName"] == name]
            total_pages = max(1, -(-len(items) // limit))
            return self._send(
                200,
                {
                    "data": items[(page - 1) * limit : page * limit],
                    "meta": {"page": page, "limit": limit, "totalItems": len(items),
                             "totalPages": total_pages},
                },
            )  # fmt: skip
        if path == "/api/public/v2/observations":
            trace_id = query.get("traceId", [None])[0]
            limit = int(query.get("limit", ["50"])[0])
            offset = int(query.get("cursor", ["0"])[0])
            found = [o for o in stub.observations if o["traceId"] == trace_id]
            page = found[offset : offset + limit]
            more = offset + limit < len(found)
            return self._send(
                200, {"data": page, "meta": {"cursor": str(offset + limit) if more else None}}
            )
        if path == "/api/public/v3/scores":
            wanted = query.get("id", [None])[0]
            data = [s for s in stub.scores.values() if wanted is None or s["id"] == wanted]
            return self._send(200, {"data": data, "meta": {"limit": 50, "cursor": None}})
        return self._send(404, {"message": f"no route {path}"})

    def do_POST(self) -> None:
        path, _, body = self._read()
        stub = self.server
        if path == "/_stub/observations":  # test hook: the app's instrumentation
            with stub.lock:
                stub.observations.extend(body)
            return self._send(200, {"ok": True})
        if not self._authorized():
            return self._send(401, {"message": "unauthorized"})
        if path == "/api/public/scores":
            if not isinstance(body, dict) or "name" not in body or "value" not in body:
                return self._send(400, {"message": "name and value are required"})
            score_id = body.get("id") or f"score-{len(stub.scores) + 1}"
            value = body["value"]
            if body.get("dataType") == "BOOLEAN":
                value = bool(value)
            stub.scores[score_id] = {
                "id": score_id,
                "projectId": "proj-1",
                "name": body["name"],
                "source": "API",
                "timestamp": _now(),
                "environment": body.get("environment") or "default",
                "createdAt": _now(),
                "updatedAt": _now(),
                "comment": body.get("comment"),
                "metadata": body.get("metadata"),
                "dataType": body.get("dataType") or "NUMERIC",
                "value": value,
                "subject": {"kind": "trace", "id": body.get("traceId")},
            }
            if stub.inject.pop("create_score", None) == "drop_after_commit":
                self.close_connection = True
                self.connection.close()
                return None
            return self._send(200, {"id": score_id})
        return self._send(404, {"message": f"no route {path}"})


def make_server(port: int = 0) -> LangfuseStub:
    return LangfuseStub(port)
