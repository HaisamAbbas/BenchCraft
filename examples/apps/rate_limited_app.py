"""A rate-limited HTTP application for load tests (16-T4 fixture).

It enforces its own quota: at most `rate` requests per second (a token bucket of `burst`)
and answers "429 Too Many Requests" with a `Retry-After` header beyond it. It records every
arrival time, the peak number of requests it served at once, and how many it rejected, so
a test can check a client's behaviour against what the server actually saw.

    python rate_limited_app.py --port 8770 --rate 20 --burst 2 --delay 0.05
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class RateLimitedServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        *,
        rate: float,
        burst: int = 1,
        delay: float = 0.0,
        retry_after: float = 1.0,
    ) -> None:
        super().__init__(address, Handler)
        self.rate, self.burst, self.delay, self.retry_after = rate, burst, delay, retry_after
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.lock = threading.Lock()
        self.arrivals: list[float] = []  # accepted requests
        self.rejections: list[float] = []  # 429 responses
        self.active = 0
        self.peak_active = 0

    def admit(self) -> bool:
        with self.lock:
            now = time.monotonic()
            self.tokens = min(float(self.burst), self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens < 1.0:
                self.rejections.append(now)
                return False
            self.tokens -= 1.0
            self.arrivals.append(now)
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            return True

    def done(self) -> None:
        with self.lock:
            self.active -= 1


class Handler(BaseHTTPRequestHandler):
    server: RateLimitedServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any], headers: dict[str, str]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        for name, value in {"Content-Type": "application/json", **headers}.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if not self.server.admit():
            self._send(
                429,
                {"error": "rate limit exceeded"},
                {"Retry-After": f"{self.server.retry_after:g}"},
            )
            return
        try:
            time.sleep(self.server.delay)
            self._send(200, {"output": f"ok: {body.get('input', '')}"}, {})
        finally:
            self.server.done()


def make_server(
    rate: float, *, burst: int = 1, delay: float = 0.0, retry_after: float = 1.0, port: int = 0
) -> RateLimitedServer:
    return RateLimitedServer(
        ("127.0.0.1", port), rate=rate, burst=burst, delay=delay, retry_after=retry_after
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8770)
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--burst", type=int, default=2)
    parser.add_argument("--delay", type=float, default=0.05)
    args = parser.parse_args()
    server = make_server(args.rate, burst=args.burst, delay=args.delay, port=args.port)
    print(f"serving on http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
