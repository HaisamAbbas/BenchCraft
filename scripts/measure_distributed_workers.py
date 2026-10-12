"""Measure the coordinator API with an isolated CLI worker process.

This loopback-only probe exercises the real worker registration, polling, lease, heartbeat,
result and application HTTP paths. The worker runs in a separate process and has no
coordinator workspace. It reports invocation throughput, outcome failures, fixture 429s and
per-invocation latency. It is a one-machine process-isolation measurement, not a cross-host
capacity claim.

    uv run python scripts/measure_distributed_workers.py --cases 100 --capacity 4
    uv run python scripts/measure_distributed_workers.py --cases 100 --capacity 4 --rate 2 --burst 1

For an operator-selected cross-host probe, pass --remote-worker with an HTTPS coordinator
listener and certificate. The script prints instructions for the synthetic app and worker,
then waits for a worker intended to run on a separate host; it cannot verify host separation.
It never prints the shared token.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import ipaddress
import json
import math
import os
import platform
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from aibench.core.models import ExecutionStatus
from aibench.runners import load_application
from aibench.runners.base import InvocationContext
from aibench.runners.bindings import AppInputEnvelope
from aibench.security.endpoints import is_loopback
from aibench.services.distributed import MAX_ACTIVE_API_CONNECTIONS, CoordinatorRunner

ROOT = Path(__file__).resolve().parents[1]
_TOKEN_ENV = "AIBENCH_WORKER_TOKEN"
_APP_FIXTURE_PORT = 8770
_REMOTE_TOKEN_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip("[]").rstrip(".")
    normalized_lower = normalized.lower()
    return (
        normalized_lower == "localhost"
        or normalized_lower.endswith(".localhost")
        or is_loopback(normalized)
    )


def _is_unusable_advertise_host(host: str) -> bool:
    normalized = host.rstrip(".")
    if not normalized or _is_loopback_host(normalized):
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        try:
            ascii_host = normalized.encode("idna").decode("ascii")
        except UnicodeError:
            return True
        labels = ascii_host.split(".")
        return len(ascii_host) > 253 or any(
            not label
            or len(label) > 63
            or not label[0].isalnum()
            or not label[-1].isalnum()
            or any(
                not (character.isascii() and (character.isalnum() or character == "-"))
                for character in label
            )
            for label in labels
        )
    return (
        address.is_unspecified
        or address.is_multicast
        or address == ipaddress.IPv4Address("255.255.255.255")
    )


def _fixture_app_config(application_url: str, *, delay: float) -> dict[str, Any]:
    return {
        "application_id": "distributed-measurement-fixture",
        "runner": "http",
        "target": application_url,
        "revision": "local-rate-fixture-v1",
        "transport": {
            "kind": "http",
            "url": application_url,
            "timeout_seconds": max(30.0, delay + 5.0),
        },
        "input_binding": {"fields": {"/input": "/input"}},
        "output_binding": {"output": "/output"},
    }


def _load_fixture() -> Any:
    path = ROOT / "examples" / "apps" / "rate_limited_app.py"
    spec = importlib.util.spec_from_file_location("distributed_measurement_fixture", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load the local HTTP fixture")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = round((len(ordered) - 1) * quantile)
    return round(ordered[index], 4)


def _stop_worker_process(process: subprocess.Popen[bytes]) -> None:
    """Stop the worker if it is still running and reap it within a bounded wait."""
    if process.poll() is None:
        try:
            process.terminate()
        except OSError:
            if process.poll() is None:
                raise
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except OSError:
            if process.poll() is None:
                raise
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                raise


async def _measure(
    *, cases: int, capacity: int, delay: float, rate: float, burst: int, measurement_timeout: float
) -> dict[str, Any]:
    fixture = _load_fixture()
    scratch: Path | None = None
    application_server: Any = None
    server_thread: threading.Thread | None = None
    server_started = False
    worker_log: Any = None
    worker_process: subprocess.Popen[bytes] | None = None
    coordinator: CoordinatorRunner | None = None

    try:
        scratch = Path(tempfile.mkdtemp(prefix="aibench-distributed-measure-"))
        application_server = fixture.make_server(rate, burst=burst, delay=delay)

        # Terminating the long-lived worker closes its HTTP keep-alive sockets abruptly. Ignore
        # those expected fixture teardown resets so they do not look like benchmark failures.
        original_handle_error = application_server.handle_error

        def handle_fixture_error(request: Any, client_address: Any) -> None:
            error = sys.exc_info()[1]
            if isinstance(error, ConnectionResetError):
                return
            original_handle_error(request, client_address)

        application_server.handle_error = handle_fixture_error
        server_thread = threading.Thread(target=application_server.serve_forever, daemon=True)
        server_thread.start()
        server_started = True

        worker_root = scratch / "worker"
        worker_root.mkdir()
        application_url = f"http://127.0.0.1:{application_server.server_port}/"
        app_config = worker_root / "app.json"
        app_config.write_text(
            json.dumps(_fixture_app_config(application_url, delay=delay)), encoding="utf-8"
        )
        loaded = load_application(app_config)
        token = secrets.token_urlsafe(32)
        run_id = f"measure-{uuid.uuid4().hex}"
        worker_log_path = scratch / "worker.stderr.log"

        def worker_ready(details: dict[str, Any]) -> None:
            nonlocal worker_log, worker_process
            worker_log = worker_log_path.open("wb")
            environment = os.environ.copy()
            environment[_TOKEN_ENV] = token
            worker_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "aibench",
                    "worker",
                    "connect",
                    "--coordinator",
                    str(details["url"]),
                    "--app-config",
                    str(app_config),
                    "--capacity",
                    str(capacity),
                ],
                cwd=worker_root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=worker_log,
            )

        coordinator = CoordinatorRunner(
            loaded.spec,
            run_id=run_id,
            options={
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "advertise_url": None,
                "tls_cert": None,
                "tls_key": None,
                "workers": 1,
                "startup_timeout_seconds": 30,
                "token_env": _TOKEN_ENV,
                "trust_local_app": False,
            },
            token=token,
            on_ready=worker_ready,
        )
        await coordinator.prepare()
        started = time.perf_counter()
        latencies: list[float] = []

        async def invoke(index: int) -> Any:
            context = InvocationContext(
                run_id=run_id,
                case_id=f"case-{index:06d}",
                attempt_id=1,
                correlation_id=f"{run_id}-{index}",
            )
            call_started = time.perf_counter()
            outcome = await coordinator.invoke(
                AppInputEnvelope({"input": f"question-{index}"}), context
            )
            latencies.append(time.perf_counter() - call_started)
            return outcome

        outcomes = await asyncio.gather(*(invoke(index) for index in range(cases)))
        elapsed = time.perf_counter() - started
        successes = sum(outcome.status is ExecutionStatus.OK for outcome in outcomes)
        failures = cases - successes
        rejections = len(application_server.rejections)
        arrivals = len(application_server.arrivals)
        return {
            "machine": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "processor": platform.processor(),
                "logical_cpus": os.cpu_count(),
            },
            "measurement": {
                "mode": "one-machine, separate coordinator API and CLI worker process",
                "cases": cases,
                "worker_processes": 1,
                "worker_capacity": capacity,
                "fixture_delay_seconds": delay,
                "fixture_rate_per_second": rate,
                "fixture_burst": burst,
                "measurement_timeout_seconds": measurement_timeout,
                "coordinator_workspace_created": False,
                "worker_workspace_created": (worker_root / ".aibench").exists(),
                "warm_worker_invocations_per_second": round(cases / elapsed, 2)
                if elapsed
                else None,
                "elapsed_seconds": round(elapsed, 4),
                "outcomes_succeeded": successes,
                "outcomes_failed": failures,
                "outcome_failure_rate": round(failures / cases, 4) if cases else 0.0,
                "application_requests_accepted": arrivals,
                "application_http_429": rejections,
                "application_peak_concurrency": application_server.peak_active,
                "application_http_429_rate": round(rejections / (arrivals + rejections), 4)
                if arrivals + rejections
                else 0.0,
                "invocation_latency_seconds": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "max": round(max(latencies), 4) if latencies else None,
                },
            },
            "limitations": [
                "The coordinator and worker run on one host over loopback; external network latency is not measured.",
                "This invokes the coordinator runner directly and does not measure SQLite commits, evaluation, or full CLI run time.",
                "The local HTTP fixture is synthetic and does not establish real-provider throughput or quota behavior.",
                "The measurement deadline excludes bounded cleanup, which can add up to about 60 seconds.",
            ],
        }
    finally:
        try:
            if worker_process is not None:
                _stop_worker_process(worker_process)
        finally:
            try:
                if worker_log is not None:
                    worker_log.close()
            finally:
                try:
                    if coordinator is not None:
                        await coordinator.close()
                finally:
                    try:
                        if application_server is not None:
                            if server_started:
                                application_server.shutdown()
                            application_server.server_close()
                    finally:
                        try:
                            if server_thread is not None and server_started:
                                server_thread.join(timeout=2)
                        finally:
                            if scratch is not None:
                                shutil.rmtree(scratch, ignore_errors=True)


def _print_remote_worker_instructions(
    *,
    coordinator_url: str,
    app_config: Path,
    worker_count: int,
    capacity: int,
    rate: float,
    burst: int,
    delay: float,
    token_env: str,
    ca_bundle_on_worker: str | None,
) -> None:
    print(
        f"Coordinator API is ready and waiting for at least {worker_count} worker(s).",
        flush=True,
    )
    print(f"Copy this app config to each worker host: {app_config}", flush=True)
    print(
        "On the worker host, set the same coordinator token in its secret manager as "
        f"{token_env}; do not pass the token on the command line.",
        flush=True,
    )
    print(
        "On each worker host, start the local synthetic app fixture: "
        f"uv run python examples/apps/rate_limited_app.py --port {_APP_FIXTURE_PORT} "
        f"--rate {rate} --burst {burst} --delay {delay}",
        flush=True,
    )
    worker_args = [
        "python",
        "-m",
        "aibench",
        "worker",
        "connect",
        "--coordinator",
        coordinator_url,
        "--app-config",
        "worker-app.json",
        "--capacity",
        str(capacity),
        "--token-env",
        token_env,
    ]
    if ca_bundle_on_worker:
        worker_args.extend(("--ca-bundle", ca_bundle_on_worker))
    posix_command = shlex.join(worker_args)
    powershell_command = "& " + " ".join(
        "'" + argument.replace("'", "''") + "'" for argument in worker_args
    )
    print(
        f"Then start the worker on each worker host (start at least {worker_count}; POSIX shell):",
        flush=True,
    )
    print(posix_command, flush=True)
    print("PowerShell command:", flush=True)
    print(powershell_command, flush=True)
    print(
        "Waiting for workers; measurement starts after the minimum count connects. After the "
        "result is written, stop each worker with Ctrl+C or its service manager; workers retry "
        "when the coordinator is unavailable.",
        flush=True,
    )


async def _measure_external_worker(
    *,
    cases: int,
    worker_count: int,
    capacity: int,
    delay: float,
    rate: float,
    burst: int,
    measurement_timeout: float,
    startup_timeout: float,
    listen_host: str,
    listen_port: int,
    advertise_url: str | None,
    tls_cert: Path | None,
    tls_key: Path | None,
    token_env: str,
    app_config_out: Path,
    ca_bundle_on_worker: str | None,
    application_url: str | None = None,
    on_ready: Any = _print_remote_worker_instructions,
) -> dict[str, Any]:
    config_path = app_config_out.expanduser().resolve()
    if config_path.exists():
        raise FileExistsError(f"refusing to overwrite worker app config {config_path}")
    token = os.environ.get(token_env, "")
    if (
        not token.isascii()
        or len(token) < 32
        or len(token) > 4096
        or any(ord(character) < 33 or ord(character) > 126 for character in token)
    ):
        raise ValueError(f"{token_env} must contain 32 to 4096 visible ASCII characters")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    application_url = application_url or f"http://127.0.0.1:{_APP_FIXTURE_PORT}/"
    with config_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(_fixture_app_config(application_url, delay=delay), indent=2))
        stream.write("\n")
    loaded = load_application(config_path)
    run_id = f"remote-measure-{uuid.uuid4().hex}"
    registered_capacities: dict[str, int] = {}

    def collect_worker_event(name: str, payload: dict[str, Any]) -> None:
        if name == "distributed_worker_connected":
            registered_capacities[str(payload["worker_id"])] = int(payload["capacity"])

    coordinator = CoordinatorRunner(
        loaded.spec,
        run_id=run_id,
        options={
            "listen_host": listen_host,
            "listen_port": listen_port,
            "advertise_url": advertise_url,
            "tls_cert": str(tls_cert.expanduser().resolve()) if tls_cert else None,
            "tls_key": str(tls_key.expanduser().resolve()) if tls_key else None,
            "workers": worker_count,
            "startup_timeout_seconds": startup_timeout,
            "token_env": token_env,
            "trust_local_app": False,
        },
        token=token,
        event_sink=collect_worker_event,
        on_ready=lambda details: on_ready(
            coordinator_url=str(details["url"]),
            app_config=config_path,
            worker_count=worker_count,
            capacity=capacity,
            rate=rate,
            burst=burst,
            delay=delay,
            token_env=token_env,
            ca_bundle_on_worker=ca_bundle_on_worker,
        ),
    )
    try:
        await coordinator.prepare()
        started = time.perf_counter()
        latencies: list[float] = []

        async def invoke(index: int) -> Any:
            context = InvocationContext(
                run_id=run_id,
                case_id=f"case-{index:06d}",
                attempt_id=1,
                correlation_id=f"{run_id}-{index}",
            )
            call_started = time.perf_counter()
            outcome = await coordinator.invoke(
                AppInputEnvelope({"input": f"question-{index}"}), context
            )
            latencies.append(time.perf_counter() - call_started)
            return outcome

        outcomes = await asyncio.gather(*(invoke(index) for index in range(cases)))
        elapsed = time.perf_counter() - started
        successes = sum(outcome.status is ExecutionStatus.OK for outcome in outcomes)
        failures = cases - successes
        return {
            "machine": {
                "role": "coordinator",
                "platform": platform.platform(),
                "python": platform.python_version(),
                "processor": platform.processor(),
                "logical_cpus": os.cpu_count(),
            },
            "measurement": {
                "mode": (
                    "HTTPS coordinator API with an externally connected worker"
                    if tls_cert
                    else "HTTP loopback coordinator API with a separately started worker process"
                ),
                "host_separation": "operator-selected; not independently verified",
                "cases": cases,
                "minimum_worker_connections": worker_count,
                "unique_worker_ids_registered": len(registered_capacities),
                "registered_capacity_by_worker_id_at_latest_registration": dict(
                    sorted(registered_capacities.items())
                ),
                "registered_worker_connection_pool_budget": sum(
                    capacity + 4 for capacity in registered_capacities.values()
                ),
                "coordinator_active_api_connection_limit": MAX_ACTIVE_API_CONNECTIONS,
                "registered_worker_pool_budget_exceeds_coordinator_limit": (
                    sum(capacity + 4 for capacity in registered_capacities.values())
                    > MAX_ACTIVE_API_CONNECTIONS
                ),
                "fixture_delay_seconds": delay,
                "fixture_rate_per_second": rate,
                "fixture_burst": burst,
                "measurement_timeout_seconds": measurement_timeout,
                "worker_startup_timeout_seconds": startup_timeout,
                "coordinator_workspace_created": False,
                "worker_workspace_created": None,
                "application_http_429": None,
                "warm_worker_invocations_per_second": round(cases / elapsed, 2)
                if elapsed
                else None,
                "elapsed_seconds": round(elapsed, 4),
                "outcomes_succeeded": successes,
                "outcomes_failed": failures,
                "outcome_failure_rate": round(failures / cases, 4),
                "invocation_latency_seconds": {
                    "p50": _percentile(latencies, 0.50),
                    "p95": _percentile(latencies, 0.95),
                    "max": round(max(latencies), 4) if latencies else None,
                },
            },
            "limitations": [
                "The operator must start the CLI worker and synthetic app fixture on a separate host; host separation is not independently attested.",
                "The coordinator waits for at least the requested worker count but accepts additional registrations; reported IDs and capacities do not establish which workers contributed for the full timed interval.",
                "The worker HTTP client pool budget is capacity plus four connections per worker; excessive aggregate budgets can exceed the coordinator API's active-connection limit and cause retries.",
                "The report measures the coordinator API, worker polling, network round trips, and synthetic HTTP app together; it excludes workspace writes, scoring, and full CLI run time.",
                "HTTP 429 counts and application-side concurrency are not observed remotely; failed outcomes include transport and application failures.",
                "The configured fixture is synthetic and does not establish production-provider behavior.",
            ],
        }
    finally:
        await coordinator.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--capacity", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--rate", type=float, default=100_000.0)
    parser.add_argument("--burst", type=int, default=100_000)
    parser.add_argument(
        "--remote-worker",
        action="store_true",
        help="Measure an operator-started worker over the coordinator HTTPS API.",
    )
    parser.add_argument(
        "--worker-count",
        type=int,
        default=1,
        help="Minimum worker registrations required before measurement (remote mode; default: 1).",
    )
    parser.add_argument("--coordinator-listen-host")
    parser.add_argument("--coordinator-listen-port", type=int)
    parser.add_argument("--coordinator-advertise-url")
    parser.add_argument("--coordinator-tls-cert", type=Path)
    parser.add_argument("--coordinator-tls-key", type=Path)
    parser.add_argument("--worker-app-config-out", type=Path)
    parser.add_argument("--token-env", default=_TOKEN_ENV)
    parser.add_argument(
        "--worker-ca-bundle-on-worker",
        help="CA bundle path on the remote worker, for a private coordinator certificate.",
    )
    parser.add_argument("--worker-startup-timeout", type=float, default=300.0)
    parser.add_argument(
        "--measurement-timeout",
        type=float,
        default=600.0,
        help="Measurement deadline in seconds (default: 600; maximum: 3600; cleanup may add up to 60 seconds).",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.cases < 1 or args.cases > 10_000 or args.capacity < 1 or args.capacity > 64:
        parser.error("--cases must be between 1 and 10000 and --capacity between 1 and 64")
    if (
        not math.isfinite(args.delay)
        or not math.isfinite(args.rate)
        or args.delay < 0
        or args.delay > 300
        or args.rate <= 0
        or args.burst < 1
        or args.burst > 100_000
    ):
        parser.error(
            "--delay must be finite and between 0 and 300 seconds; --rate must be finite and "
            "positive; --burst must be between 1 and 100000"
        )
    if (
        not math.isfinite(args.measurement_timeout)
        or args.measurement_timeout < 1
        or args.measurement_timeout > 3600
    ):
        parser.error("--measurement-timeout must be finite and between 1 and 3600 seconds")
    remote_args = (
        args.coordinator_listen_host,
        args.coordinator_listen_port,
        args.coordinator_advertise_url,
        args.coordinator_tls_cert,
        args.coordinator_tls_key,
        args.worker_app_config_out,
        args.worker_ca_bundle_on_worker,
    )
    if args.remote_worker:
        if args.cases > 1_000:
            parser.error("--remote-worker limits --cases to 1000")
        if not 1 <= args.worker_count <= 64:
            parser.error("--worker-count must be between 1 and 64")
        requested_connection_budget = args.worker_count * (args.capacity + 4)
        if requested_connection_budget > MAX_ACTIVE_API_CONNECTIONS:
            parser.error(
                "requested worker HTTP connection-pool budget exceeds the coordinator limit "
                f"of {MAX_ACTIVE_API_CONNECTIONS}; reduce --worker-count or --capacity"
            )
        if not all(
            (
                args.coordinator_listen_host,
                args.coordinator_listen_port,
                args.coordinator_advertise_url,
                args.coordinator_tls_cert,
                args.coordinator_tls_key,
                args.worker_app_config_out,
            )
        ):
            parser.error(
                "--remote-worker requires coordinator listen host/port, advertise URL, TLS "
                "certificate/key, and --worker-app-config-out"
            )
        if _is_loopback_host(args.coordinator_listen_host):
            parser.error("--remote-worker requires a non-loopback coordinator listen host")
        if not 1 <= args.coordinator_listen_port <= 65_535:
            parser.error("--coordinator-listen-port must be between 1 and 65535")
        try:
            advertised = urlsplit(args.coordinator_advertise_url)
            advertised_port = advertised.port
        except ValueError as exc:
            parser.error(f"invalid --coordinator-advertise-url: {exc}")
        if (
            advertised.scheme != "https"
            or not advertised.hostname
            or _is_unusable_advertise_host(advertised.hostname)
            or any(
                ord(character) <= 32 or ord(character) == 127
                for character in args.coordinator_advertise_url
            )
            or advertised.username is not None
            or advertised.password is not None
            or advertised.query
            or advertised.fragment
            or advertised.path not in ("", "/")
        ):
            parser.error(
                "--coordinator-advertise-url must be a reachable HTTPS origin without "
                "whitespace, credentials, path, query, or fragment"
            )
        if advertised_port is None or advertised_port != args.coordinator_listen_port:
            parser.error("advertise URL port must equal --coordinator-listen-port")
        if not args.coordinator_tls_cert.is_file() or not args.coordinator_tls_key.is_file():
            parser.error("--coordinator-tls-cert and --coordinator-tls-key must exist")
        if not _REMOTE_TOKEN_ENV_RE.fullmatch(args.token_env):
            parser.error("--token-env must be a valid environment variable name")
        if args.worker_ca_bundle_on_worker is not None and (
            not args.worker_ca_bundle_on_worker.strip()
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in args.worker_ca_bundle_on_worker
            )
        ):
            parser.error("--worker-ca-bundle-on-worker must be a printable worker-local path")
        if not math.isfinite(args.worker_startup_timeout) or not (
            1 <= args.worker_startup_timeout <= 3600
        ):
            parser.error("--worker-startup-timeout must be finite and between 1 and 3600 seconds")
        if args.worker_startup_timeout > args.measurement_timeout:
            parser.error("--worker-startup-timeout cannot exceed --measurement-timeout")
        if args.worker_app_config_out.expanduser().resolve().exists():
            parser.error("--worker-app-config-out already exists; choose a new path")
        if (
            args.out
            and args.out.expanduser().resolve() == args.worker_app_config_out.expanduser().resolve()
        ):
            parser.error("--out and --worker-app-config-out must be different files")
        token = os.environ.get(args.token_env, "")
        if (
            not token.isascii()
            or len(token) < 32
            or len(token) > 4096
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
        ):
            parser.error(f"{args.token_env} must contain 32 to 4096 visible ASCII characters")
        measure = _measure_external_worker(
            cases=args.cases,
            worker_count=args.worker_count,
            capacity=args.capacity,
            delay=args.delay,
            rate=args.rate,
            burst=args.burst,
            measurement_timeout=args.measurement_timeout,
            startup_timeout=args.worker_startup_timeout,
            listen_host=args.coordinator_listen_host,
            listen_port=args.coordinator_listen_port,
            advertise_url=args.coordinator_advertise_url,
            tls_cert=args.coordinator_tls_cert,
            tls_key=args.coordinator_tls_key,
            token_env=args.token_env,
            app_config_out=args.worker_app_config_out,
            ca_bundle_on_worker=args.worker_ca_bundle_on_worker,
        )
    else:
        if any(value is not None for value in remote_args) or args.token_env != _TOKEN_ENV:
            parser.error("coordinator and remote-worker options require --remote-worker")
        if args.worker_count != 1:
            parser.error("--worker-count requires --remote-worker")
        if args.worker_startup_timeout != 300.0:
            parser.error("--worker-startup-timeout requires --remote-worker")
        measure = _measure(
            cases=args.cases,
            capacity=args.capacity,
            delay=args.delay,
            rate=args.rate,
            burst=args.burst,
            measurement_timeout=args.measurement_timeout,
        )
    result = asyncio.run(asyncio.wait_for(measure, timeout=args.measurement_timeout))
    document = {
        "tool": "scripts/measure_distributed_workers.py",
        "argv": sys.argv[1:],
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        **result,
    }
    rendered = json.dumps(document, indent=2, allow_nan=False) + "\n"
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
