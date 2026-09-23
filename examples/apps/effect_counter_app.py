"""Example effectful HTTP application that counts the side effects it performs.

POST /book  {"destination": str} -> performs the effect (increments the counter), then
            responds {"output": "Booked ...", "booking_id": n}. With --respond-delay the
            response is sent only after the delay, *after* the effect already happened —
            exactly the case where a client timeout cannot prove nothing occurred.
GET  /effects -> {"count": n}
POST /reset   -> sets the counter back to 0
GET  /health  -> {"status": "ok"}

It never books anything real: the "effect" is an in-memory counter.

    python effect_counter_app.py --port 8766 --respond-delay 0
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class EffectCounterServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], *, respond_delay: float = 0.0) -> None:
        super().__init__(address, EffectHandler)
        self.respond_delay = respond_delay
        self.count = 0
        self.lock = threading.Lock()
        # Every request as received (headers and body), so tests can audit exactly what
        # the harness sent.
        self.received: list[dict[str, Any]] = []


class EffectHandler(BaseHTTPRequestHandler):
    server: EffectCounterServer

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with self.server.lock:
            self.server.received.append(
                {"path": self.path, "headers": dict(self.headers.items()), "body": body}
            )
        return body

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
        elif self.path == "/effects":
            with self.server.lock:
                self._send(200, {"count": self.server.count})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        body = self._read_body()
        if self.path == "/reset":
            with self.server.lock:
                self.server.count = 0
            self._send(200, {"status": "reset"})
            return
        if self.path != "/book":
            self._send(404, {"error": "not found"})
            return
        try:
            request = json.loads(body or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "body must be JSON"})
            return
        with self.server.lock:
            self.server.count += 1  # the effect happens before any response is sent
            booking_id = self.server.count
        if self.server.respond_delay:
            time.sleep(self.server.respond_delay)
        destination = request.get("destination", "unknown")
        try:
            self._send(200, {"output": f"Booked trip to {destination}.", "booking_id": booking_id})
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up waiting; the effect still happened


def make_server(
    host: str = "127.0.0.1", port: int = 8766, *, respond_delay: float = 0.0
) -> EffectCounterServer:
    return EffectCounterServer((host, port), respond_delay=respond_delay)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--respond-delay", type=float, default=0.0)
    args = parser.parse_args()
    server = make_server(args.host, args.port, respond_delay=args.respond_delay)
    print(f"serving on http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
