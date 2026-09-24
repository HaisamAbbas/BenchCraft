"""A local stand-in for the hosted OpenAI Evals API, for contract tests (Prompt 17).

It serves the endpoints the official `openai` SDK calls (`/v1/evals`, `/v1/evals/{id}/runs`,
`.../output_items`, cancel) with the response shapes of the SDK's generated models, and
grades uploaded items with `string_check` locally. It is a test fixture: passing against
it shows the harness and the pinned SDK agree on the contract, not that the live service
behaves the same way.

Failure injection, for the harness's ambiguity and partial-result handling:
    server.inject["create_eval"|"create_run"] = "error_after_commit" | "drop_after_commit"
                                                | "reject" | "rate_limit"
    server.inject["create_eval"] = "garbage_after_commit"  # stored, then a 200 non-JSON
    server.inject["create_eval"] = "redirect"   # 307 to `server.redirect_to`
    server.duplicate_case = "case-id"   # the service returns two items for one case
    server.stop_after = 2               # the run fails after grading 2 items
    server.graded_before_cancel = 1     # items already graded when a cancel arrives
Every request is recorded in `server.requests` (method, path, JSON body, auth header).
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

_TEMPLATE = re.compile(r"\{\{\s*item\.(\w+)\s*\}\}")


class EvalsStub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0) -> None:
        super().__init__(("127.0.0.1", port), _Handler)
        self.lock = threading.Lock()
        self.evals: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.requests: list[dict[str, Any]] = []
        self.inject: dict[str, str] = {}
        self.duplicate_case: str | None = None
        self.stop_after: int | None = None
        self.graded_before_cancel = 1
        self.redirect_to = ""  # for inject[...] = "redirect"
        self.counter = 0

    def new_id(self, prefix: str) -> str:
        with self.lock:
            self.counter += 1
            return f"{prefix}_{self.counter:06d}"


def _grade(criterion: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    def fill(text: str) -> str:
        return _TEMPLATE.sub(lambda m: str(item.get(m.group(1), "")), text)

    kind = criterion["type"]
    if kind == "string_check":
        left, right = fill(criterion["input"]), fill(criterion["reference"])
        op = criterion["operation"]
        passed = {
            "eq": left == right,
            "ne": left != right,
            "like": right in left,
            "ilike": right.lower() in left.lower(),
        }[op]
        return {"name": criterion["name"], "passed": passed, "score": float(passed), "type": kind}
    return {"name": criterion["name"], "passed": False, "score": 0.0, "type": kind}


def _sample() -> dict[str, Any]:
    # Stored-output grading generates nothing: an empty sample with the model's shape.
    return {
        "error": {"code": "", "message": ""},
        "finish_reason": "",
        "input": [],
        "max_completion_tokens": 0,
        "model": "",
        "output": [],
        "seed": 0,
        "temperature": 0.0,
        "top_p": 1.0,
        "usage": {
            "cached_tokens": 0,
            "completion_tokens": 0,
            "prompt_tokens": 0,
            "total_tokens": 0,
        },
    }


class _Handler(BaseHTTPRequestHandler):
    server: EvalsStub
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(
        self, status: int, payload: dict[str, Any], headers: dict[str, str] | None = None
    ) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drop(self) -> None:
        self.close_connection = True
        self.connection.close()

    def _record(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else None
        entry = {
            "method": self.command,
            "path": urlsplit(self.path).path,
            "query": parse_qs(urlsplit(self.path).query),
            "body": body,
            "authorization": self.headers.get("Authorization"),
        }
        self.server.requests.append(entry)
        return entry

    def _injected(self, op: str) -> str | None:
        return self.server.inject.pop(op, None)

    def _page(self, items: list[dict[str, Any]], query: dict[str, list[str]]) -> dict[str, Any]:
        limit = int(query.get("limit", ["20"])[0])
        after = query.get("after", [None])[0]
        start = 0
        if after is not None:
            ids = [i["id"] for i in items]
            start = ids.index(after) + 1 if after in ids else len(items)
        page = items[start : start + limit]
        return {
            "object": "list",
            "data": page,
            "first_id": page[0]["id"] if page else None,
            "last_id": page[-1]["id"] if page else None,
            "has_more": start + limit < len(items),
        }

    def do_GET(self) -> None:
        entry = self._record()
        parts = entry["path"].strip("/").split("/")  # v1/evals/...
        stub = self.server
        if parts == ["v1", "evals"]:
            evals = sorted(stub.evals.values(), key=lambda e: e["created_at"], reverse=True)
            return self._send(200, self._page(evals, entry["query"]))
        if len(parts) == 4 and parts[3] == "runs":
            runs = [r for r in stub.runs.values() if r["eval_id"] == parts[2]]
            runs.sort(key=lambda r: r["created_at"], reverse=True)
            return self._send(200, self._page([r["public"] for r in runs], entry["query"]))
        if len(parts) == 5 and parts[3] == "runs":
            run = stub.runs.get(parts[4])
            if run is None:
                return self._send(404, {"error": {"message": "no such run"}})
            if run["public"]["status"] == "queued":
                run["public"]["status"] = "in_progress"
            elif run["public"]["status"] == "in_progress":
                self._finish(run)
            return self._send(200, run["public"])
        if len(parts) == 6 and parts[5] == "output_items":
            run = stub.runs.get(parts[4])
            if run is None:
                return self._send(404, {"error": {"message": "no such run"}})
            return self._send(200, self._page(run["visible"], entry["query"]))
        return self._send(404, {"error": {"message": f"no route {entry['path']}"}})

    def _finish(self, run: dict[str, Any]) -> None:
        graded = run["graded"]
        if self.server.stop_after is not None:
            run["visible"] = graded[: self.server.stop_after]
            run["public"]["status"] = "failed"
            run["public"]["error"] = {"code": "stub_failure", "message": "stopped early"}
        else:
            run["visible"] = graded
            run["public"]["status"] = "completed"
        self._counts(run)

    def _counts(self, run: dict[str, Any]) -> None:
        passed = sum(1 for i in run["visible"] if all(r["passed"] for r in i["results"]))
        run["public"]["result_counts"] = {
            "errored": 0,
            "failed": len(run["visible"]) - passed,
            "passed": passed,
            "total": len(run["items"]),
        }

    def do_POST(self) -> None:
        entry = self._record()
        body = entry["body"] or {}
        parts = entry["path"].strip("/").split("/")
        stub = self.server
        if parts == ["v1", "evals"]:
            injected = self._injected("create_eval")
            if injected == "reject":
                return self._send(400, {"error": {"message": "bad request (injected)"}})
            if injected == "rate_limit":
                return self._send(429, {"error": {"message": "slow down"}}, {"Retry-After": "1"})
            if injected == "redirect":
                return self._send(
                    307, {}, {"Location": f"{stub.redirect_to}{self.path}"}
                )  # the client must not follow it with the body
            eval_id = stub.new_id("eval")
            stub.evals[eval_id] = {
                "id": eval_id,
                "object": "eval",
                "created_at": int(time.time() * 1000) + stub.counter,
                "name": body.get("name", ""),
                "data_source_config": {**body["data_source_config"], "schema": {}},
                "testing_criteria": body["testing_criteria"],
                "metadata": body.get("metadata"),
            }
            if injected == "error_after_commit":
                return self._send(500, {"error": {"message": "internal error (injected)"}})
            if injected == "garbage_after_commit":
                return self._send_raw(200, b"<html>proxy error</html>", "text/html")
            if injected == "drop_after_commit":
                return self._drop()
            return self._send(200, stub.evals[eval_id])
        if len(parts) == 4 and parts[3] == "runs":
            evaluation = stub.evals.get(parts[2])
            if evaluation is None:
                return self._send(404, {"error": {"message": "no such eval"}})
            injected = self._injected("create_run")
            if injected == "reject":
                return self._send(400, {"error": {"message": "bad request (injected)"}})
            if injected == "rate_limit":
                return self._send(429, {"error": {"message": "slow down"}}, {"Retry-After": "1"})
            source = body["data_source"]
            if source.get("type") != "jsonl":
                return self._send(400, {"error": {"message": "the stub grades jsonl only"}})
            run_id = stub.new_id("evalrun")
            items = [entry_["item"] for entry_ in source["source"]["content"]]
            graded = []
            for index, item in enumerate(items):
                graded.append(self._item(run_id, parts[2], index, item, evaluation))
                if item.get("aibench_case_id") == stub.duplicate_case:
                    graded.append(self._item(run_id, parts[2], index, item, evaluation))
            public = {
                "id": run_id,
                "object": "eval.run",
                "created_at": int(time.time() * 1000) + stub.counter,
                "eval_id": parts[2],
                "name": body.get("name", ""),
                "model": "",
                "status": "queued",
                "data_source": source,
                "error": {"code": "", "message": ""},
                "metadata": body.get("metadata"),
                "per_model_usage": [],
                "per_testing_criteria_results": [],
                "report_url": f"http://127.0.0.1/evals/{parts[2]}/runs/{run_id}",
                "result_counts": {"errored": 0, "failed": 0, "passed": 0, "total": len(items)},
            }
            stub.runs[run_id] = {
                "eval_id": parts[2], "created_at": public["created_at"], "public": public,
                "items": items, "graded": graded, "visible": [],
            }  # fmt: skip
            if injected == "error_after_commit":
                return self._send(502, {"error": {"message": "bad gateway (injected)"}})
            if injected == "drop_after_commit":
                return self._drop()
            return self._send(200, public)
        if len(parts) == 5 and parts[3] == "runs":  # cancel
            run = stub.runs.get(parts[4])
            if run is None:
                return self._send(404, {"error": {"message": "no such run"}})
            run["visible"] = run["graded"][: stub.graded_before_cancel]
            run["public"]["status"] = "canceled"
            self._counts(run)
            return self._send(200, run["public"])
        return self._send(404, {"error": {"message": f"no route {entry['path']}"}})

    def _item(
        self,
        run_id: str,
        eval_id: str,
        index: int,
        item: dict[str, Any],
        evaluation: dict[str, Any],
    ) -> dict[str, Any]:
        results = [_grade(c, item) for c in evaluation["testing_criteria"]]
        return {
            "id": self.server.new_id("outputitem"),
            "object": "eval.run.output_item",
            "created_at": int(time.time()),
            "eval_id": eval_id,
            "run_id": run_id,
            "datasource_item_id": index,
            "datasource_item": item,
            "results": results,
            "sample": _sample(),
            "status": "pass" if all(r["passed"] for r in results) else "fail",
        }


def make_server(port: int = 0) -> EvalsStub:
    return EvalsStub(port)
