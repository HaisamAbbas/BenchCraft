"""Read-only loopback dashboard integration tests."""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from rich.ansi import AnsiDecoder
from typer.testing import CliRunner

from aibench.cli import dashboard as dashboard_cli
from aibench.cli.dashboard import _DashboardServer
from aibench.cli.main import app
from aibench.core.models import RunManifest
from aibench.core.sessions import BenchmarkSession, DecisionRecord, SessionChoices
from aibench.services import dashboard as dashboard_service
from aibench.services.comparison import compare_runs
from aibench.services.dashboard import MAX_DASHBOARD_SOURCE_ROWS
from aibench.services.reports import build_report, report_facts
from aibench.sessions.store import SessionStore
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

runner = CliRunner()


def _seed_run(project_root: Path, run_id: str) -> None:
    db = Database.open_workspace(Workspace.at(project_root))
    storage = Storage(db)
    storage.commit_run(
        RunManifest(
            run_id=run_id,
            dataset_hash="sha256:dataset",
            application_hash="sha256:application",
            plan_hash="sha256:plan",
            environment={"PRIVATE_SECRET": "do-not-display"},
        )
    )
    db.close()


def _seed_session(project_root: Path) -> None:
    db = Database.open_workspace(Workspace.at(project_root))
    storage = Storage(db)
    choices = SessionChoices(
        application=str(project_root / "private app config.yaml"),
        dataset=str(project_root / "private cases.jsonl"),
    )
    session = BenchmarkSession(
        session_id="session-1",
        project_root=str(project_root),
        policy_path=str(project_root / "private policy.yaml"),
        revision=1,
        decision_id="session-1:d1",
    )
    decision = DecisionRecord(
        decision_id="session-1:d1",
        session_id=session.session_id,
        source_turn_id=None,
        source="user",
        revision=1,
        choices=choices,
        plan_file="plan.json",
        plan_hash="sha256:plan",
        executable=False,
        draft={},
    )
    SessionStore(storage).create_session(session, decision, ())
    db.close()


@contextmanager
def _server(project_root: Path) -> Iterator[str]:
    server = _DashboardServer(Workspace.at(project_root), 0)
    assert server.server_address[0] == "127.0.0.1"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(url: str) -> tuple[int, dict[str, object], dict[str, str]]:
    with urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read()), dict(response.headers.items())


def test_dashboard_serves_read_only_stored_runs_sessions_and_report_facts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(tmp_path, "r1")
    _seed_run(tmp_path, "r:colon")
    _seed_session(tmp_path)

    def reject_writer(*args: object, **kwargs: object) -> Database:
        raise AssertionError("dashboard must not open a writer connection")

    monkeypatch.setattr(Database, "open", reject_writer)
    with _server(tmp_path) as base:
        status, payload, headers = _get(base + "/api/runs?limit=10")
        assert status == 200
        assert {item["run_id"] for item in payload["data"]} == {"r1", "r:colon"}
        assert "environment" not in payload["data"][0]
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"

        _, sessions, _ = _get(base + "/api/sessions")
        assert sessions["data"][0]["session_id"] == "session-1"
        session_json = json.dumps(sessions)
        assert "private app config.yaml" not in session_json
        assert "private cases.jsonl" not in session_json
        assert "private policy.yaml" not in session_json

        _, report, _ = _get(base + "/api/runs/r1/report")
        assert report["data"]["run_id"] == "r1"
        assert "metrics" in report["data"]
        assert "do-not-display" not in json.dumps(report)

        _, encoded_report, _ = _get(base + "/api/runs/r%3Acolon/report")
        assert encoded_report["data"]["run_id"] == "r:colon"

        db = Database.open_readonly(Workspace.at(tmp_path).db_path)
        try:
            storage = Storage(db)
            expected_report = report_facts(
                build_report(
                    storage,
                    ArtifactStore(Workspace.at(tmp_path).artifacts_dir, create=False),
                    "r1",
                    include_content=False,
                ),
                evidence_limit=10,
            )
        finally:
            db.close()
        assert report["data"] == expected_report


def test_dashboard_comparison_uses_stored_runs_on_request(tmp_path: Path) -> None:
    _seed_run(tmp_path, "base")
    _seed_run(tmp_path, "candidate")
    with _server(tmp_path) as base:
        status, payload, _ = _get(base + "/api/compare?baseline=base&current=candidate")
    assert status == 200
    assert payload["data"]["baseline_run_id"] == "base"
    assert payload["data"]["current_run_id"] == "candidate"

    db = Database.open_readonly(Workspace.at(tmp_path).db_path)
    try:
        expected = compare_runs(
            Storage(db),
            ArtifactStore(Workspace.at(tmp_path).artifacts_dir, create=False),
            "base",
            "candidate",
            bootstrap_replicates=2_000,
        )
    finally:
        db.close()
    assert payload["data"] == expected


def test_dashboard_rejects_writes_invalid_queries_and_cross_origin_hosts(tmp_path: Path) -> None:
    _seed_run(tmp_path, "r1")
    with _server(tmp_path) as base:
        with pytest.raises(HTTPError) as method_error:
            urlopen(Request(base + "/api/runs", data=b"{}", method="POST"), timeout=5)
        assert method_error.value.code == 405
        assert method_error.value.headers["Allow"] == "GET"

        with pytest.raises(HTTPError) as patch_error:
            urlopen(Request(base + "/api/runs", data=b"{}", method="PATCH"), timeout=5)
        assert patch_error.value.code == 405
        assert patch_error.value.headers["Allow"] == "GET"

        large_write = Request(base + "/api/runs", data=b"x" * (128 * 1024), method="POST")
        with pytest.raises(HTTPError) as large_write_error:
            urlopen(large_write, timeout=5)
        assert large_write_error.value.code == 405
        assert "read-only" in large_write_error.value.read().decode("utf-8")

        address = base.removeprefix("http://")
        with socket.create_connection(
            (address.split(":", 1)[0], int(address.rsplit(":", 1)[1])), timeout=5
        ) as client:
            client.sendall(
                (
                    "PATCH /api/runs HTTP/1.1\r\n"
                    f"Host: {address}\r\n"
                    "Transfer-Encoding: chunked\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii")
                + b"5\r\nhello\r\n0\r\n\r\n"
            )
            response = bytearray()
            while True:
                try:
                    chunk = client.recv(4096)
                except ConnectionResetError:
                    break
                if not chunk:
                    break
                response.extend(chunk)
        assert response.startswith(b"HTTP/1.0 405 Method Not Allowed\r\n")
        assert b"read-only" in response

        with pytest.raises(HTTPError) as query_error:
            urlopen(base + "/api/runs?limit=1000", timeout=5)
        assert query_error.value.code == 400

        request = Request(base + "/api/runs", headers={"Host": "attacker.example"})
        with pytest.raises(HTTPError) as host_error:
            urlopen(request, timeout=5)
        assert host_error.value.code == 403

        cross_origin = Request(
            base + "/api/runs",
            headers={"Origin": "https://attacker.example"},
        )
        with pytest.raises(HTTPError) as origin_error:
            urlopen(cross_origin, timeout=5)
        assert origin_error.value.code == 403

        with pytest.raises(HTTPError) as route_error:
            urlopen(base + "/api/unknown", timeout=5)
        assert route_error.value.code == 404


def test_dashboard_bounds_report_work_by_run_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(tmp_path, "large")
    _seed_run(tmp_path, "other")

    def over_limit(self: Storage, run_id: str, dataset_hash: str, *, limit: int) -> int:
        assert dataset_hash == "sha256:dataset"
        assert limit == MAX_DASHBOARD_SOURCE_ROWS
        assert run_id in {"large", "other"}
        return MAX_DASHBOARD_SOURCE_ROWS + 1 if run_id == "large" else 0

    monkeypatch.setattr(Storage, "count_dashboard_source_rows", over_limit)
    with _server(tmp_path) as base:
        with pytest.raises(HTTPError) as report_error:
            urlopen(base + "/api/runs/large/report", timeout=5)
        with pytest.raises(HTTPError) as compare_error:
            urlopen(base + "/api/compare?baseline=large&current=other", timeout=5)
    assert report_error.value.code == 413
    assert str(MAX_DASHBOARD_SOURCE_ROWS) in report_error.value.read().decode("utf-8")
    assert compare_error.value.code == 413


def test_dashboard_bounds_legacy_results_without_work_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(tmp_path, "legacy-result")
    _seed_run(tmp_path, "legacy-trace")
    db = Database.open_workspace(Workspace.at(tmp_path))
    db.connection.execute(
        """INSERT INTO metric_results
           (result_id, run_id, case_id, metric_id, status, decision, content_hash, data,
            committed_at)
           VALUES ('legacy-result', 'legacy-result', 'case-1', 'metric-1', 'ok', 'pass',
                   'sha256:result', '{}', '2026-10-12T00:00:00Z')"""
    )
    db.connection.execute(
        """INSERT INTO trace_observations
           (import_id, run_id, trace_id, execution_id, complete, data, created_at)
           VALUES ('import-1', 'legacy-trace', 'trace-1', NULL, 1, '{}',
                   '2026-10-12T00:00:00Z')"""
    )
    db.close()
    monkeypatch.setattr(dashboard_service, "MAX_DASHBOARD_SOURCE_ROWS", 0)

    with _server(tmp_path) as base:
        for run_id in ("legacy-result", "legacy-trace"):
            with pytest.raises(HTTPError) as error:
                urlopen(base + f"/api/runs/{run_id}/report", timeout=5)
            assert error.value.code == 413


def test_dashboard_reads_preflight_and_report_from_one_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(tmp_path, "snapshot")
    monkeypatch.setattr(dashboard_service, "MAX_DASHBOARD_SOURCE_ROWS", 0)
    count_rows = Storage.count_dashboard_source_rows
    list_traces = Storage.list_trace_observations
    committed = False
    trace_counts_during_report: list[int] = []

    def observe_report_traces(storage: Storage, run_id: str) -> list[dict[str, object]]:
        traces = list_traces(storage, run_id)
        trace_counts_during_report.append(len(traces))
        return traces

    def add_trace_after_preflight(
        storage: Storage, run_id: str, dataset_hash: str, *, limit: int
    ) -> int:
        nonlocal committed
        count = count_rows(storage, run_id, dataset_hash, limit=limit)
        if not committed:
            writer = Database.open(Workspace.at(tmp_path).db_path)
            writer.connection.execute(
                """INSERT INTO trace_observations
                   (import_id, run_id, trace_id, execution_id, complete, data, created_at)
                   VALUES ('later-import', 'snapshot', 'later-trace', NULL, 1, ?,
                           '2026-10-12T00:00:00Z')""",
                (
                    json.dumps(
                        {
                            "raw_artifact_id": "later-import:raw",
                            "partial_reasons": [],
                            "execution_id": None,
                            "complete": True,
                            "tools": [],
                        }
                    ),
                ),
            )
            writer.close()
            committed = True
        return count

    monkeypatch.setattr(Storage, "count_dashboard_source_rows", add_trace_after_preflight)
    monkeypatch.setattr(Storage, "list_trace_observations", observe_report_traces)
    with _server(tmp_path) as base:
        status, report, _ = _get(base + "/api/runs/snapshot/report")

    assert status == 200
    assert committed
    assert "data" in report
    assert trace_counts_during_report == [0]
    monkeypatch.setattr(Storage, "list_trace_observations", list_traces)
    db = Database.open_readonly(Workspace.at(tmp_path).db_path)
    try:
        assert len(Storage(db).list_trace_observations("snapshot")) == 1
    finally:
        db.close()


def test_dashboard_request_timeout_releases_stalled_slots(tmp_path: Path) -> None:
    server = _DashboardServer(Workspace.at(tmp_path), 0)
    server.request_timeout_seconds = 0.2
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    clients: list[socket.socket] = []
    try:
        for _ in range(server.max_concurrent_requests):
            clients.append(socket.create_connection(server.server_address, timeout=2))
        acquired = 0
        for _ in range(server.max_concurrent_requests):
            assert server._request_slots.acquire(timeout=3)
            acquired += 1
        for _ in range(acquired):
            server._request_slots.release()
        # On a slow runner (macOS CI) the test can take the slots before the stalled
        # handlers do; they then take them afterwards, and a request in that window is
        # refused as busy until their timeout releases them. Released is what matters.
        deadline = time.monotonic() + 5
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
                    assert response.status == 200
                break
            except HTTPError as exc:
                if exc.code != 503 or time.monotonic() > deadline:
                    raise
                time.sleep(0.05)
    finally:
        for client in clients:
            client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_absolute_deadline_releases_slow_drip_request_slots(
    tmp_path: Path,
) -> None:
    server = _DashboardServer(Workspace.at(tmp_path), 0)
    server.max_request_duration_seconds = 0.4
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(server.server_address, timeout=2)
    client.settimeout(2)
    try:
        host = f"127.0.0.1:{server.server_port}"
        for fragment in (
            b"GET / HTTP/1.1\r\n",
            f"Host: {host}\r\n".encode("ascii"),
            b"X-Slow: first",
            b"-second\r\n",
        ):
            try:
                client.sendall(fragment)
            except OSError:
                break
            time.sleep(0.15)

        acquired = 0
        for _ in range(server.max_concurrent_requests):
            assert server._request_slots.acquire(timeout=3)
            acquired += 1
        for _ in range(acquired):
            server._request_slots.release()

        with urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5) as response:
            assert response.status == 200
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_logs_server_error_type_without_stored_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _seed_run(tmp_path, "r1")

    def fail(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError("private/path-and-record-value")

    monkeypatch.setattr(dashboard_cli, "list_dashboard_runs", fail)
    with _server(tmp_path) as base, pytest.raises(HTTPError) as error:
        urlopen(base + "/api/runs", timeout=5)
    assert error.value.code == 500
    assert "RuntimeError" in caplog.text
    assert "private/path-and-record-value" not in caplog.text


def test_dashboard_rejects_excess_concurrent_requests(tmp_path: Path) -> None:
    server = _DashboardServer(Workspace.at(tmp_path), 0)
    for _ in range(server.max_concurrent_requests):
        assert server._request_slots.acquire(blocking=False)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5)
        assert error.value.code == 503
        assert error.value.headers["Content-Type"] == "application/json; charset=utf-8"
        assert json.loads(error.value.read()) == {"error": "dashboard is busy"}

        host = f"127.0.0.1:{server.server_port}"
        body = b"x" * (512 * 1024)
        requests = (
            (
                (
                    f"POST / HTTP/1.1\r\nHost: {host}\r\nContent-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii"),
                body,
            ),
            (
                (
                    f"PATCH / HTTP/1.1\r\nHost: {host}\r\nTransfer-Encoding: chunked\r\n"
                    "Connection: close\r\n\r\n"
                ).encode("ascii"),
                f"{len(body):x}\r\n".encode("ascii") + body + b"\r\n0\r\n\r\n",
            ),
        )
        for request_headers, request_body in requests:
            with socket.create_connection(server.server_address, timeout=5) as client:
                client.settimeout(5)
                client.sendall(request_headers)
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    try:
                        chunk = client.recv(4096)
                    except (ConnectionAbortedError, ConnectionResetError):
                        break
                    if not chunk:
                        break
                    response.extend(chunk)
                headers, separator, response_body = bytes(response).partition(b"\r\n\r\n")
                assert separator
                content_length = next(
                    int(line.partition(b":")[2].strip())
                    for line in headers.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                )
                while len(response_body) < content_length:
                    try:
                        chunk = client.recv(4096)
                    except (ConnectionAbortedError, ConnectionResetError):
                        break
                    if not chunk:
                        break
                    response.extend(chunk)
                    _, _, response_body = bytes(response).partition(b"\r\n\r\n")
                if request_body:
                    try:
                        client.sendall(request_body)
                    except OSError:
                        # The oversized content-length request can already be closed.
                        pass
            headers, separator, response_body = bytes(response).partition(b"\r\n\r\n")
            assert separator
            assert headers.startswith(b"HTTP/1.1 503 Service Unavailable\r\n")
            assert b"Content-Type: application/json; charset=utf-8" in headers
            assert json.loads(response_body) == {"error": "dashboard is busy"}
    finally:
        for _ in range(server.max_concurrent_requests):
            server._request_slots.release()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_page_uses_text_nodes_and_has_no_external_assets(tmp_path: Path) -> None:
    with _server(tmp_path) as base, urlopen(base + "/", timeout=5) as response:
        assert response.status == 200
        assert "text/html" in response.headers["Content-Type"]
        html = response.read().decode("utf-8")
        csp = response.headers["Content-Security-Policy"]
    assert "textContent" in html
    assert "innerHTML" not in html
    assert "https://" not in html
    assert "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp


def test_dashboard_cli_requires_existing_workspace_and_exposes_help(tmp_path: Path) -> None:
    missing = runner.invoke(app, ["dashboard", "--workspace", str(tmp_path), "--no-open"])
    assert missing.exit_code == 2
    assert "No BenchCraft workspace" in missing.stdout

    help_result = runner.invoke(app, ["dashboard", "--help"])
    assert help_result.exit_code == 0
    visible_help = "\n".join(segment.plain for segment in AnsiDecoder().decode(help_result.output))
    assert "loopback" in visible_help
    assert "--no-open" in visible_help


def _dashboard_tls_context() -> tuple[ssl.SSLContext, ssl.SSLContext, Path, Path]:
    fixture = Path(__file__).parent / "fixtures" / "dashboard_tls"
    certificate = fixture / "cert.pem"
    private_key = fixture / "key.pem"
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    server_context.load_cert_chain(certificate, private_key)
    client_context = ssl.create_default_context(cafile=str(certificate))
    return server_context, client_context, certificate, private_key


def test_dashboard_remote_mode_requires_tls_and_token_and_authenticates_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_run(tmp_path, "remote-run")
    server_context, client_context, certificate, private_key = _dashboard_tls_context()
    token = "a" * 42
    monkeypatch.setenv("BENCHCRAFT_DASHBOARD_TEST_TOKEN", token)

    with pytest.raises(ValueError, match="require --token-env"):
        dashboard_cli._remote_security("0.0.0.0", None, None, None)
    with pytest.raises(ValueError, match="32 to 256"):
        monkeypatch.setenv("BENCHCRAFT_DASHBOARD_TEST_TOKEN", "too-short")
        dashboard_cli._remote_security(
            "0.0.0.0",
            "BENCHCRAFT_DASHBOARD_TEST_TOKEN",
            certificate,
            private_key,
        )
    monkeypatch.setenv("BENCHCRAFT_DASHBOARD_TEST_TOKEN", token)
    configured_token, configured_tls = dashboard_cli._remote_security(
        "0.0.0.0",
        "BENCHCRAFT_DASHBOARD_TEST_TOKEN",
        certificate,
        private_key,
    )
    assert configured_token == token
    assert configured_tls is not None
    assert configured_tls.minimum_version is ssl.TLSVersion.TLSv1_2
    with pytest.raises(ValueError, match="only for remote binds"):
        dashboard_cli._remote_security("127.0.0.1", "BENCHCRAFT_DASHBOARD_TEST_TOKEN", None, None)

    server = _DashboardServer(
        Workspace.at(tmp_path),
        0,
        host="0.0.0.0",
        bearer_token=token,
        ssl_context=server_context,
    )
    assert server.server_address[0] == "0.0.0.0"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"https://127.0.0.1:{server.server_port}"
    stalled_tls_client: socket.socket | None = None
    try:
        stalled_tls_client = socket.create_connection(("127.0.0.1", server.server_port), timeout=5)
        # An incomplete handshake must occupy only a bounded worker, not stall accepts.
        time.sleep(0.05)
        with urlopen(base + "/", context=client_context, timeout=5) as page_response:
            page = page_response.read().decode("utf-8")
        assert "const requiresToken = true;" in page
        assert 'id="token" type="password"' in page
        assert "sessionStorage" not in page
        assert "window.prompt" not in page
        assert token not in page

        with pytest.raises(HTTPError) as missing:
            urlopen(base + "/api/runs", context=client_context, timeout=5)
        assert missing.value.code == 401
        assert missing.value.headers["WWW-Authenticate"] == 'Bearer realm="BenchCraft dashboard"'

        wrong_token = Request(base + "/api/runs", headers={"Authorization": "Bearer " + "b" * 42})
        with pytest.raises(HTTPError) as rejected:
            urlopen(wrong_token, context=client_context, timeout=5)
        assert rejected.value.code == 401

        cross_origin = Request(
            base + "/api/runs",
            headers={
                "Authorization": "Bearer " + token,
                "Origin": "https://attacker.example",
            },
        )
        with pytest.raises(HTTPError) as origin_error:
            urlopen(cross_origin, context=client_context, timeout=5)
        assert origin_error.value.code == 403

        valid_token = Request(base + "/api/runs", headers={"Authorization": "Bearer " + token})
        with urlopen(valid_token, context=client_context, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["data"][0]["run_id"] == "remote-run"
    finally:
        if stalled_tls_client is not None:
            stalled_tls_client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_dashboard_remote_cli_fails_closed_without_complete_secure_configuration(
    tmp_path: Path,
) -> None:
    _seed_run(tmp_path, "r1")
    result = runner.invoke(
        app,
        ["dashboard", "--workspace", str(tmp_path), "--bind", "0.0.0.0", "--no-open"],
    )
    assert result.exit_code == 2
    assert "remote dashboard binds require --token-env" in result.stdout
    assert "--tls-cert" in result.stdout
    assert "--tls-key" in result.stdout
