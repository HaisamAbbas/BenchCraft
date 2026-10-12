"""Measure the coordinator API with an isolated CLI worker process.

This loopback-only probe exercises the real worker registration, polling, lease, heartbeat,
result and application HTTP paths. The worker runs in a separate process and has no
coordinator workspace. It reports invocation throughput, outcome failures, fixture 429s and
per-invocation latency. It is a one-machine process-isolation measurement, not a cross-host
capacity claim.

    uv run python scripts/measure_distributed_workers.py --cases 100 --capacity 4
    uv run python scripts/measure_distributed_workers.py --cases 100 --capacity 4 --rate 2 --burst 1
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import platform
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from aibench.core.models import ExecutionStatus
from aibench.runners import load_application
from aibench.runners.base import InvocationContext
from aibench.runners.bindings import AppInputEnvelope
from aibench.services.distributed import CoordinatorRunner

ROOT = Path(__file__).resolve().parents[1]
_TOKEN_ENV = "AIBENCH_WORKER_TOKEN"


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
            json.dumps(
                {
                    "application_id": "distributed-measurement-fixture",
                    "runner": "http",
                    "target": application_url,
                    "revision": "local-rate-fixture-v1",
                    "transport": {
                        "kind": "http",
                        "url": application_url,
                        "timeout_seconds": 30,
                    },
                    "input_binding": {"fields": {"/input": "/input"}},
                    "output_binding": {"output": "/output"},
                }
            ),
            encoding="utf-8",
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=int, default=100)
    parser.add_argument("--capacity", type=int, default=4)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--rate", type=float, default=100_000.0)
    parser.add_argument("--burst", type=int, default=100_000)
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
    result = asyncio.run(
        asyncio.wait_for(
            _measure(
                cases=args.cases,
                capacity=args.capacity,
                delay=args.delay,
                rate=args.rate,
                burst=args.burst,
                measurement_timeout=args.measurement_timeout,
            ),
            timeout=args.measurement_timeout,
        )
    )
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
