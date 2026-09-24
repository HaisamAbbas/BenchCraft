"""Deterministic HTTP test app for multi-turn text episode integration tests.

This fixture models a support conversation and exposes a separate reset endpoint. It is a
local test world; it performs no real refund, exchange, or network effect.
"""

from __future__ import annotations

import copy
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


def _initial_state(seed: Any = None) -> dict[str, Any]:
    supplied = seed if isinstance(seed, dict) else {}
    return {
        "messages": [],
        "active_order": supplied.get("active_order"),
        "refund_eligible": bool(supplied.get("refund_eligible", False)),
        "exchange_denied": bool(supplied.get("exchange_denied", False)),
        "turn_count": 0,
    }


def make_server(port: int = 8769) -> ThreadingHTTPServer:
    lock = threading.Lock()
    state = _initial_state()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: Any) -> None:
            return

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            value = json.loads(raw.decode("utf-8")) if raw else {}
            return value if isinstance(value, dict) else {}

        def _send(self, status: int, value: dict[str, Any]) -> None:
            encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            nonlocal state
            try:
                body = self._body()
            except (ValueError, UnicodeDecodeError):
                self._send(400, {"error": "invalid JSON"})
                return
            if self.path == "/reset":
                with lock:
                    state = _initial_state(body.get("seed"))
                    snapshot = copy.deepcopy(state)
                self._send(200, {"reset": True, "world_state": snapshot})
                return
            if self.path != "/answer":
                self._send(404, {"error": "not found"})
                return
            message = body.get("input")
            if not isinstance(message, str):
                self._send(400, {"error": "input must be text"})
                return
            with lock:
                state["messages"].append(message)
                state["turn_count"] += 1
                combined = " ".join(state["messages"]).casefold()
                if "a17" in combined:
                    state["active_order"] = "A17"
                elif "b55" in combined:
                    state["active_order"] = "B55"
                if "refund" in combined and "two days" in combined:
                    state["refund_eligible"] = state["active_order"] == "A17"
                if "exchange" in combined and "45 days" in combined:
                    state["exchange_denied"] = state["active_order"] == "B55"
                if state["refund_eligible"]:
                    output = "Order A17 is within the refund window."
                elif state["exchange_denied"]:
                    output = "Order B55 is outside the exchange window."
                elif "refund" in message.casefold() or "exchange" in message.casefold():
                    output = "Please tell me when the order was purchased."
                else:
                    output = "I need the order and purchase details to check that."
                snapshot = copy.deepcopy(
                    {key: value for key, value in state.items() if key != "messages"}
                )
            self._send(200, {"output": output, "world_state": snapshot})

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


if __name__ == "__main__":
    server = make_server()
    print(f"multi-turn support test app listening on http://127.0.0.1:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
