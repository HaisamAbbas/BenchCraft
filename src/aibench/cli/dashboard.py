"""A local, read-only browser view of stored BenchCraft runs and sessions."""

from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import ssl
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import typer

from aibench.cli.output import Console
from aibench.services.comparison import ComparisonError
from aibench.services.dashboard import (
    MAX_DASHBOARD_SOURCE_ROWS,
    DashboardEndpointNotFound,
    DashboardNotFound,
    DashboardWorkLimitExceeded,
    dashboard_compare,
    dashboard_run_report,
    list_dashboard_runs,
    list_dashboard_sessions,
)
from aibench.storage.db import Workspace

console = Console()
logger = logging.getLogger(__name__)
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TOKEN_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_BEARER_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,256}$")
_MAX_REJECTED_BODY_BYTES = 256 * 1024
_PAGE = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1">
    <title>BenchCraft dashboard</title>
    <style>
      :root { color-scheme: light dark; font: 16px system-ui, sans-serif; }
      body { max-width: 1100px; margin: 2rem auto; padding: 0 1rem; }
      h1, h2 { line-height: 1.2; }
      button, select { font: inherit; padding: .4rem .6rem; }
      button { cursor: pointer; }
      table { border-collapse: collapse; width: 100%; margin: 1rem 0 2rem; }
      th, td { text-align: left; padding: .55rem; border-bottom: 1px solid #8888; vertical-align: top; }
      .muted { opacity: .7; }
      #error { color: #c44; white-space: pre-wrap; }
      pre { max-height: 32rem; overflow: auto; padding: 1rem; background: #8882; }
      section { margin-top: 2rem; }
      .controls { display: flex; gap: .7rem; align-items: center; flex-wrap: wrap; }
    </style>
  </head>
  <body>
    <h1>BenchCraft dashboard</h1>
    <p class="muted">Read-only view. Evidence text and conversations are withheld.</p>
    <form id="auth" hidden autocomplete="off">
      <label>Dashboard token <input id="token" type="password" maxlength="256" autocomplete="off" required></label>
      <button type="submit">Connect</button>
    </form>
    <button id="refresh">Refresh</button>
    <span id="updated" class="muted"></span>
    <p id="error" role="alert"></p>
    <section>
      <h2>Runs</h2>
      <table>
        <thead><tr><th>Run</th><th>Status</th><th>Created</th><th>Tags / baselines</th><th>Evidence</th></tr></thead>
        <tbody id="runs"></tbody>
      </table>
    </section>
    <section>
      <h2>Sessions</h2>
      <table>
        <thead><tr><th>Session</th><th>Revision</th><th>Active run</th><th>Updated</th></tr></thead>
        <tbody id="sessions"></tbody>
      </table>
    </section>
    <section>
      <h2>Compare stored runs</h2>
      <p class="muted">Comparison reads committed results; it does not rerun an app or evaluator.</p>
      <div class="controls">
        <label>Baseline <select id="baseline"></select></label>
        <label>Candidate <select id="candidate"></select></label>
        <button id="compare">Compare</button>
      </div>
    </section>
    <section>
      <h2>Result</h2>
      <pre id="result" aria-live="polite">Choose a run's evidence or compare two runs.</pre>
    </section>
    <script>
      const byId = (id) => document.getElementById(id);
      const requiresToken = __REQUIRES_TOKEN__;
      let bearerToken = null;

      async function api(path) {
        const headers = { Accept: 'application/json' };
        if (requiresToken) {
          if (!bearerToken) {
            byId('auth').hidden = false;
            throw new Error('Enter the dashboard token, then select Connect.');
          }
          headers.Authorization = 'Bearer ' + bearerToken;
        }
        const response = await fetch(path, { headers });
        const body = await response.json();
        if (response.status === 401 && requiresToken) {
          bearerToken = null;
          byId('auth').hidden = false;
          byId('error').textContent = 'Dashboard token rejected. Enter the correct token and connect again.';
          throw new Error('Dashboard token rejected.');
        }
        if (!response.ok) throw new Error(body.error || 'Request failed');
        return body.data;
      }

      function cell(row, value) {
        const td = document.createElement('td');
        td.textContent = value == null ? '—' : String(value);
        row.append(td);
        return td;
      }

      function addOption(select, value, label) {
        const item = document.createElement('option');
        item.value = value;
        item.textContent = label;
        select.append(item);
      }

      async function showReport(runId) {
        byId('error').textContent = '';
        try {
          const report = await api('/api/runs/' + encodeURIComponent(runId) + '/report');
          byId('result').textContent = JSON.stringify(report, null, 2);
        } catch (error) {
          byId('error').textContent = error.message;
        }
      }

      async function compare() {
        byId('error').textContent = '';
        const baseline = byId('baseline').value;
        const current = byId('candidate').value;
        if (!baseline || !current || baseline === current) {
          byId('error').textContent = 'Choose two different runs.';
          return;
        }
        try {
          const query = new URLSearchParams({ baseline, current });
          const result = await api('/api/compare?' + query);
          byId('result').textContent = JSON.stringify(result, null, 2);
        } catch (error) {
          byId('error').textContent = error.message;
        }
      }

      async function refresh() {
        byId('error').textContent = '';
        byId('result').textContent = "Choose a run's evidence or compare two runs.";
        try {
          const [runs, sessions] = await Promise.all([
            api('/api/runs?limit=50'),
            api('/api/sessions?limit=50'),
          ]);
          const runBody = byId('runs');
          runBody.replaceChildren();
          for (const run of runs) {
            const row = document.createElement('tr');
            cell(row, run.run_id);
            cell(row, run.status);
            cell(row, run.created_at);
            cell(row, [...(run.tags || []), ...(run.baselines || [])].join(', '));
            const action = cell(row, '');
            const button = document.createElement('button');
            button.textContent = 'View evidence';
            button.addEventListener('click', () => showReport(run.run_id));
            action.append(button);
            runBody.append(row);
          }

          const sessionBody = byId('sessions');
          sessionBody.replaceChildren();
          for (const session of sessions) {
            const row = document.createElement('tr');
            cell(row, session.session_id);
            cell(row, session.revision);
            cell(row, session.active_run_id);
            cell(row, session.updated_at);
            sessionBody.append(row);
          }

          for (const select of [byId('baseline'), byId('candidate')]) {
            const previous = select.value;
            select.replaceChildren();
            for (const run of runs) {
              addOption(select, run.run_id, run.run_id + ' (' + run.status + ')');
            }
            if (runs.some((run) => run.run_id === previous)) select.value = previous;
          }
          byId('updated').textContent = 'Updated ' + new Date().toLocaleTimeString();
        } catch (error) {
          byId('error').textContent = error.message;
        }
      }

      byId('refresh').addEventListener('click', refresh);
      byId('compare').addEventListener('click', compare);
      byId('auth').addEventListener('submit', (event) => {
        event.preventDefault();
        const input = byId('token');
        if (!input.value) return;
        bearerToken = input.value;
        input.value = '';
        byId('auth').hidden = true;
        refresh();
      });
      refresh();
    </script>
  </body>
</html>""".encode()


class _DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    max_concurrent_requests = 4
    request_timeout_seconds = 10
    max_request_duration_seconds = 30

    def __init__(
        self,
        workspace: Workspace,
        port: int,
        *,
        host: str = "127.0.0.1",
        bearer_token: str | None = None,
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.workspace = workspace
        self.bearer_token = bearer_token
        self.scheme = "https" if ssl_context is not None else "http"
        self._request_slots = threading.BoundedSemaphore(self.max_concurrent_requests)
        self._request_timers: dict[Any, threading.Timer] = {}
        self._request_timers_lock = threading.Lock()
        super().__init__((host, port), _DashboardHandler)
        if ssl_context is not None:
            # Defer the TLS handshake to the bounded request thread rather than
            # blocking the accept loop on an incomplete client handshake.
            self.socket = ssl_context.wrap_socket(
                self.socket, server_side=True, do_handshake_on_connect=False
            )

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                body_info = self._read_busy_request_headers(request)
                body = json.dumps({"error": "dashboard is busy"}).encode("utf-8")
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Type: application/json; charset=utf-8\r\n"
                    + f"Content-Length: {len(body)}\r\n".encode("ascii")
                    + b"Cache-Control: no-store\r\n"
                    + b"X-Content-Type-Options: nosniff\r\n"
                    + b"Referrer-Policy: no-referrer\r\n"
                    + b"Connection: close\r\n\r\n"
                    + body
                )
                if body_info is not None:
                    self._discard_busy_request_body(request, *body_info)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            request.settimeout(self.request_timeout_seconds)
            timer = threading.Timer(
                self.max_request_duration_seconds, self._expire_request, args=(request,)
            )
            timer.daemon = True
            with self._request_timers_lock:
                self._request_timers[request] = timer
            timer.start()
            super().process_request(request, client_address)
        except BaseException:
            self._cancel_request_timer(request)
            self._request_slots.release()
            raise

    @staticmethod
    def _expire_request(request: Any) -> None:
        try:
            request.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _cancel_request_timer(self, request: Any) -> None:
        with self._request_timers_lock:
            timer = self._request_timers.pop(request, None)
        if timer is not None:
            timer.cancel()

    @staticmethod
    def _read_busy_request_headers(request: Any) -> tuple[int, int, bool] | None:
        deadline = time.monotonic() + 0.5
        max_header_bytes = 16 * 1024
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer and len(buffer) < max_header_bytes:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                request.settimeout(remaining)
                chunk = request.recv(min(4096, max_header_bytes - len(buffer)))
            except OSError:
                return None
            if not chunk:
                return None
            buffer.extend(chunk)

        header, separator, body = bytes(buffer).partition(b"\r\n\r\n")
        if not separator:
            return None
        content_length = 0
        chunked = False
        for line in header.split(b"\r\n")[1:]:
            name, colon, value = line.partition(b":")
            if not colon:
                continue
            field_name = name.strip().lower()
            if field_name == b"content-length":
                try:
                    content_length = max(0, int(value.strip()))
                except ValueError:
                    content_length = 0
            elif field_name == b"transfer-encoding":
                chunked = b"chunked" in value.lower()
        return len(body), content_length, chunked

    @staticmethod
    def _discard_busy_request_body(
        request: Any, buffered_bytes: int, content_length: int, chunked: bool
    ) -> None:
        max_body_bytes = _MAX_REJECTED_BODY_BYTES
        body_remaining = max(0, max_body_bytes - buffered_bytes)
        if not chunked:
            if not 0 < content_length <= max_body_bytes:
                return
            body_remaining = min(body_remaining, max(0, content_length - buffered_bytes))
        if body_remaining <= 0:
            return
        deadline = time.monotonic() + 0.5
        while body_remaining > 0:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                request.settimeout(remaining)
                chunk = request.recv(min(4096, body_remaining))
            except OSError:
                return
            if not chunk:
                return
            body_remaining -= len(chunk)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._cancel_request_timer(request)
            self._request_slots.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        # TLS handshake exceptions can contain untrusted connection details.
        error = sys.exc_info()[0]
        logger.warning("dashboard connection failed (%s)", error.__name__ if error else "unknown")


class _DashboardHandler(BaseHTTPRequestHandler):
    server: _DashboardServer

    def do_GET(self) -> None:
        if not self._is_allowed_origin():
            self._json(403, {"error": "dashboard requests must use the serving origin"})
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            page = _PAGE.replace(
                b"const requiresToken = __REQUIRES_TOKEN__;",
                f"const requiresToken = {'true' if self.server.bearer_token else 'false'};".encode(
                    "ascii"
                ),
            )
            self._send(200, page, "text/html; charset=utf-8")
            return
        if not parsed.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        if self.server.bearer_token is not None and not self._has_valid_bearer_token():
            self._json(
                401,
                {"error": "a valid dashboard bearer token is required"},
                authenticate='Bearer realm="BenchCraft dashboard"',
            )
            return
        try:
            params = self._query(parsed.query)
            result = self._route(parsed.path, params)
            self._json(200, {"data": result})
        except DashboardNotFound:
            self._json(404, {"error": "stored run not found"})
        except DashboardEndpointNotFound:
            self._json(404, {"error": "not found"})
        except DashboardWorkLimitExceeded:
            self._json(
                413,
                {
                    "error": f"report and comparison requests are limited to {MAX_DASHBOARD_SOURCE_ROWS} source rows per run"
                },
            )
        except (ValueError, ComparisonError):
            self._json(400, {"error": "invalid dashboard request or incompatible runs"})
        # Exception strings can include workspace paths or stored user data.
        except Exception as exc:  # noqa: BLE001
            logger.error("dashboard request failed (%s)", type(exc).__name__)
            self._json(500, {"error": "stored workspace data could not be read"})

    def do_POST(self) -> None:
        self._reject_write()

    def do_PUT(self) -> None:
        self._reject_write()

    def do_DELETE(self) -> None:
        self._reject_write()

    def do_PATCH(self) -> None:
        self._reject_write()

    def _reject_write(self) -> None:
        # Flush the rejection before touching the body. Drain only a bounded body
        # prefix so compliant clients can read 405 without letting writes tie up a
        # slot indefinitely or force an unbounded read.
        self.close_connection = True
        self._json(405, {"error": "dashboard is read-only"}, allow="GET")
        self.wfile.flush()

        transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
        if "chunked" in transfer_encoding:
            self._discard_body_until(lambda deadline: self._discard_chunked_body(deadline))
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return
        if not 0 < content_length <= _MAX_REJECTED_BODY_BYTES:
            return
        self._discard_body_until(
            lambda deadline: self._discard_request_bytes(content_length, deadline)
        )

    def _discard_body_until(self, discard: Any) -> None:
        duration = 0.5
        deadline = time.monotonic() + duration
        timer = threading.Timer(duration, self._shutdown_read, args=(self.connection,))
        timer.daemon = True
        timer.start()
        try:
            discard(deadline)
        finally:
            timer.cancel()

    @staticmethod
    def _shutdown_read(connection: Any) -> None:
        try:
            connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def _discard_request_bytes(self, byte_count: int, deadline: float) -> bool:
        remaining = byte_count
        while remaining > 0:
            time_left = deadline - time.monotonic()
            if time_left <= 0:
                return False
            self.connection.settimeout(min(time_left, 0.5))
            try:
                chunk = self.rfile.read(min(4096, remaining))
            except OSError:
                return False
            if not chunk:
                return False
            remaining -= len(chunk)
        return True

    def _discard_chunked_body(self, deadline: float) -> None:
        consumed = 0
        while consumed < _MAX_REJECTED_BODY_BYTES:
            time_left = deadline - time.monotonic()
            if time_left <= 0:
                return
            self.connection.settimeout(min(time_left, 0.5))
            try:
                line = self.rfile.readline(min(1024, _MAX_REJECTED_BODY_BYTES - consumed + 1))
                consumed += len(line)
                if not line.endswith(b"\r\n"):
                    return
                size_text = line.split(b";", 1)[0].strip()
                chunk_size = int(size_text, 16)
                if chunk_size == 0:
                    while consumed < _MAX_REJECTED_BODY_BYTES:
                        trailer = self.rfile.readline(
                            min(1024, _MAX_REJECTED_BODY_BYTES - consumed + 1)
                        )
                        consumed += len(trailer)
                        if not trailer.endswith(b"\r\n"):
                            return
                        if trailer == b"\r\n":
                            return
                    return
                if chunk_size + 2 > _MAX_REJECTED_BODY_BYTES - consumed:
                    return
                chunk = self.rfile.read(chunk_size + 2)
                consumed += len(chunk)
                if len(chunk) != chunk_size + 2 or not chunk.endswith(b"\r\n"):
                    return
            except (OSError, ValueError):
                return

    def _route(self, path: str, params: dict[str, str]) -> Any:
        if path == "/api/runs":
            self._only(params, {"limit", "offset"})
            return list_dashboard_runs(
                self.server.workspace,
                limit=self._integer(params, "limit", default=50, minimum=1, maximum=100),
                offset=self._integer(params, "offset", default=0, minimum=0, maximum=2**63 - 1),
            )
        if path == "/api/sessions":
            self._only(params, {"limit"})
            return list_dashboard_sessions(
                self.server.workspace,
                limit=self._integer(params, "limit", default=50, minimum=1, maximum=100),
            )
        report_path = re.fullmatch(r"/api/runs/([^/]{1,384})/report", path)
        if report_path:
            self._only(params, set())
            try:
                run_id = unquote(report_path.group(1), errors="strict")
            except UnicodeDecodeError as exc:
                raise DashboardEndpointNotFound(path) from exc
            if not self._valid_run_id(run_id):
                raise DashboardEndpointNotFound(path)
            return dashboard_run_report(self.server.workspace, run_id)
        if path == "/api/compare":
            self._only(params, {"baseline", "current"})
            baseline = params.get("baseline", "")
            current = params.get("current", "")
            if not self._valid_run_id(baseline) or not self._valid_run_id(current):
                raise ValueError("two valid run IDs are required")
            if baseline == current:
                raise ValueError("runs must differ")
            return dashboard_compare(self.server.workspace, baseline, current)
        raise DashboardEndpointNotFound(path)

    def _query(self, raw: str) -> dict[str, str]:
        values = parse_qs(raw, keep_blank_values=True, strict_parsing=bool(raw), max_num_fields=4)
        if any(len(items) != 1 for items in values.values()):
            raise ValueError("query parameter repeated")
        return {key: items[0] for key, items in values.items()}

    @staticmethod
    def _only(params: dict[str, str], allowed: set[str]) -> None:
        if not params.keys() <= allowed:
            raise ValueError("unknown query parameter")

    @staticmethod
    def _integer(
        params: dict[str, str], key: str, *, default: int, minimum: int, maximum: int
    ) -> int:
        try:
            value = int(params.get(key, str(default)))
        except ValueError as exc:
            raise ValueError(f"{key} must be an integer") from exc
        if not minimum <= value <= maximum:
            raise ValueError(f"{key} is out of range")
        return value

    @staticmethod
    def _valid_run_id(value: str) -> bool:
        return bool(_RUN_ID.fullmatch(value))

    def _is_allowed_origin(self) -> bool:
        host = self.headers.get("Host", "").lower()
        if not host or any(character in host for character in "\r\n/\\@"):
            return False
        if self.server.bearer_token is None:
            bound_host = str(self.server.server_address[0])
            bound_host_header = (
                f"[{bound_host}]:{self.server.server_port}"
                if ":" in bound_host
                else f"{bound_host}:{self.server.server_port}"
            )
            expected_hosts = {
                bound_host_header,
                f"localhost:{self.server.server_port}",
            }
            if host not in expected_hosts:
                return False
        origin = self.headers.get("Origin")
        return origin is None or origin.lower() == f"{self.server.scheme}://{host}"

    def _has_valid_bearer_token(self) -> bool:
        authorization = self.headers.get("Authorization", "")
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "bearer":
            return False
        expected = self.server.bearer_token
        if expected is None:
            return True
        return hmac.compare_digest(token.encode("utf-8"), expected.encode("ascii"))

    def _json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        allow: str | None = None,
        authenticate: str | None = None,
    ) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.error("dashboard response serialization failed (%s)", type(exc).__name__)
            status = 500
            body = b'{"error":"stored workspace data could not be read"}'
        self._send(
            status,
            body,
            "application/json; charset=utf-8",
            allow=allow,
            authenticate=authenticate,
        )

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        allow: str | None = None,
        authenticate: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        if allow is not None:
            self.send_header("Allow", allow)
        if authenticate is not None:
            self.send_header("WWW-Authenticate", authenticate)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Request paths and query strings may contain workspace identifiers.
        return


def serve_dashboard(
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    port: int = typer.Option(
        8765, "--port", min=0, max=65535, help="Listening port (0 chooses one)."
    ),
    bind: str = typer.Option(
        "127.0.0.1",
        "--bind",
        help="Bind address (loopback by default; remote binds require TLS and a bearer token).",
    ),
    token_env: str | None = typer.Option(
        None,
        "--token-env",
        help="Environment variable containing the remote dashboard token (32+ URL-safe characters).",
    ),
    tls_cert: Path | None = typer.Option(  # noqa: B008
        None, "--tls-cert", help="TLS certificate PEM file (required for remote binds)."
    ),
    tls_key: Path | None = typer.Option(  # noqa: B008
        None, "--tls-key", help="TLS private key PEM file (required for remote binds)."
    ),
    no_open: bool = typer.Option(
        False, "--no-open", help="Print the URL without opening a browser."
    ),
) -> None:
    """Serve stored runs and sessions; no benchmark work is started."""
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.is_file():
        console.print(f"No BenchCraft workspace at {ws.root}", style="red")
        raise typer.Exit(code=2)
    try:
        bearer_token, tls_context = _remote_security(bind, token_env, tls_cert, tls_key)
        server = _DashboardServer(
            ws,
            port,
            host=bind,
            bearer_token=bearer_token,
            ssl_context=tls_context,
        )
    except (OSError, ssl.SSLError, ValueError) as exc:
        console.print(f"Could not start dashboard: {exc}", style="red")
        raise typer.Exit(code=2) from exc
    scheme = "https" if tls_context is not None else "http"
    url_host = f"[{bind}]" if ":" in bind else bind
    url = f"{scheme}://{url_host}:{server.server_port}/"
    if bearer_token is None:
        console.print(f"Dashboard listening at {url} (loopback only; press Ctrl+C to stop)")
    else:
        console.print(
            f"Dashboard bound at {url} (TLS and bearer authentication enabled; "
            "connect using a hostname covered by the certificate; press Ctrl+C to stop)"
        )
    try:
        if not no_open and bearer_token is None:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _is_loopback_bind(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        # Hostnames other than localhost can resolve to a non-loopback address;
        # treat them as remote and require the complete secure configuration.
        return False


def _remote_security(
    host: str,
    token_env: str | None,
    tls_cert: Path | None,
    tls_key: Path | None,
) -> tuple[str | None, ssl.SSLContext | None]:
    if _is_loopback_bind(host):
        if token_env is not None or tls_cert is not None or tls_key is not None:
            raise ValueError("--token-env, --tls-cert, and --tls-key are only for remote binds")
        return None, None
    if token_env is None or tls_cert is None or tls_key is None:
        raise ValueError("remote dashboard binds require --token-env, --tls-cert, and --tls-key")
    if not _TOKEN_ENV_NAME.fullmatch(token_env):
        raise ValueError("--token-env must be a valid environment variable name")
    token = os.environ.get(token_env)
    if token is None or not _BEARER_TOKEN.fullmatch(token):
        raise ValueError(
            f"{token_env} must contain 32 to 256 URL-safe token characters; "
            "generate one with Python secrets.token_urlsafe(32)"
        )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(tls_cert, tls_key)
    return token, context
