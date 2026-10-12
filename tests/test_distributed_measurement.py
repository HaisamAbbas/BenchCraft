"""The G22.3 probe exercises a real CLI worker and cleans up failed measurements."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure_distributed_workers.py"


def _load_measurement_module() -> Any:
    spec = importlib.util.spec_from_file_location("distributed_measurement", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_distributed_measurement_runs_a_real_worker_process(tmp_path: Path) -> None:
    output = tmp_path / "measurement.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--cases",
            "8",
            "--capacity",
            "2",
            "--rate",
            "1000",
            "--burst",
            "1000",
            "--out",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    document = json.loads(output.read_text(encoding="utf-8"))
    measured = document["measurement"]
    assert measured["worker_processes"] == 1
    assert measured["worker_workspace_created"] is False
    [worker_runtime] = measured["worker_runtime_by_worker_id_at_latest_registration"].values()
    assert isinstance(worker_runtime["python"], str)
    assert measured["outcomes_succeeded"] == 8
    assert measured["outcomes_failed"] == 0
    assert measured["application_requests_accepted"] == 8
    assert measured["application_peak_concurrency"] <= measured["worker_capacity"]


@pytest.mark.parametrize(
    ("option", "value"),
    [("--rate", "NaN"), ("--delay", "Infinity"), ("--measurement-timeout", "Infinity")],
)
def test_distributed_measurement_rejects_nonfinite_values(option: str, value: str) -> None:
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--cases", "1", option, value],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode != 0
    assert "must be finite" in completed.stderr


def test_setup_failure_closes_fixture_thread_and_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_measurement_module()
    scratch_root = tmp_path / "measurement"
    threads: list[threading.Thread] = []

    def make_thread(*args: Any, **kwargs: Any) -> threading.Thread:
        thread = threading.Thread(*args, **kwargs)
        threads.append(thread)
        return thread

    def make_scratch(*, prefix: str) -> str:
        scratch_root.mkdir()
        return str(scratch_root)

    def fail_config_load(_path: Path) -> None:
        raise RuntimeError("injected config-load failure")

    monkeypatch.setattr(module, "threading", SimpleNamespace(Thread=make_thread))
    monkeypatch.setattr(module, "tempfile", SimpleNamespace(mkdtemp=make_scratch))
    monkeypatch.setattr(module, "load_application", fail_config_load)

    with pytest.raises(RuntimeError, match="injected config-load failure"):
        asyncio.run(
            module._measure(
                cases=1,
                capacity=1,
                delay=0,
                rate=10,
                burst=1,
                measurement_timeout=5,
            )
        )

    assert threads and all(not thread.is_alive() for thread in threads)
    assert not scratch_root.exists()


def test_worker_cleanup_skips_exited_process_and_handles_exit_race() -> None:
    module = _load_measurement_module()

    class ExitedWorker:
        def __init__(self) -> None:
            self.waited = False

        def poll(self) -> int | None:
            return 0

        def terminate(self) -> None:
            raise AssertionError("an exited worker must not be terminated")

        def wait(self, *, timeout: float) -> None:
            assert timeout == 5
            self.waited = True

    class WorkerExitingDuringTerminate(ExitedWorker):
        def __init__(self) -> None:
            super().__init__()
            self.poll_count = 0

        def poll(self) -> int | None:
            self.poll_count += 1
            return None if self.poll_count == 1 else 0

        def terminate(self) -> None:
            raise ProcessLookupError("worker exited between poll and terminate")

    already_exited = ExitedWorker()
    module._stop_worker_process(already_exited)
    assert already_exited.waited

    exiting = WorkerExitingDuringTerminate()
    module._stop_worker_process(exiting)
    assert exiting.waited


def test_remote_worker_instructions_use_custom_token_environment_variable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_measurement_module()
    module._print_remote_worker_instructions(
        coordinator_url="https://bench.example:8765",
        app_config=tmp_path / "worker app-config.json",
        worker_count=2,
        capacity=2,
        rate=100,
        burst=10,
        delay=0.02,
        token_env="BENCHCRAFT_TEST_WORKER_TOKEN",
        ca_bundle_on_worker="/etc/ssl/Bench Craft/benchcraft-ca.pem",
    )
    instructions = capsys.readouterr().out
    assert "--token-env BENCHCRAFT_TEST_WORKER_TOKEN" in instructions
    assert "--ca-bundle '/etc/ssl/Bench Craft/benchcraft-ca.pem'" in instructions
    assert "BenchCraft checkout at the coordinator's exact version" in instructions
    assert "uv run --locked python -m aibench worker connect" in instructions
    assert "--report-runtime" in instructions
    assert "--app-config 'worker app-config.json'" in instructions
    assert "keeping its filename" in instructions
    assert "at least 2; POSIX shell" in instructions
    assert "stop each worker with Ctrl+C" in instructions
    assert "workers retry when the coordinator is unavailable" in instructions
    assert "do not pass the token on the command line" in instructions


def test_fixture_http_timeout_exceeds_configured_response_delay() -> None:
    module = _load_measurement_module()
    config = module._fixture_app_config("http://127.0.0.1:8770/", delay=300)
    assert config["transport"]["timeout_seconds"] == 305


def test_remote_probe_rejects_worker_pool_budget_above_coordinator_limit() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--remote-worker",
            "--cases",
            "1",
            "--worker-count",
            "4",
            "--capacity",
            "33",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode != 0
    assert "worker HTTP connection-pool budget exceeds the coordinator limit of 128" in (
        completed.stderr
    )


def test_opt_in_probe_measures_a_separately_started_worker_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_measurement_module()
    fixture = module._load_fixture()
    application_server = fixture.make_server(1_000, burst=1_000, delay=0.001)
    server_thread = threading.Thread(target=application_server.serve_forever, daemon=True)
    server_thread.start()
    worker_root = tmp_path / "worker-host"
    worker_root.mkdir()
    worker_log = (tmp_path / "worker.stderr.log").open("wb")
    worker_processes: list[subprocess.Popen[bytes]] = []
    token = "remote-probe-test-token-that-is-long-enough-12345"
    monkeypatch.setenv("BENCHCRAFT_PROBE_TEST_TOKEN", token)

    def start_worker(**details: Any) -> None:
        for _ in range(details["worker_count"]):
            worker_processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "aibench",
                        "worker",
                        "connect",
                        "--coordinator",
                        details["coordinator_url"],
                        "--app-config",
                        str(details["app_config"]),
                        "--capacity",
                        str(details["capacity"]),
                        "--report-runtime",
                        "--token-env",
                        details["token_env"],
                    ],
                    cwd=worker_root,
                    env=os.environ.copy(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=worker_log,
                )
            )

    try:
        document = asyncio.run(
            asyncio.wait_for(
                module._measure_external_worker(
                    cases=6,
                    worker_count=2,
                    capacity=2,
                    delay=0.001,
                    rate=1_000,
                    burst=1_000,
                    measurement_timeout=40,
                    startup_timeout=25,
                    listen_host="127.0.0.1",
                    listen_port=0,
                    advertise_url=None,
                    tls_cert=None,
                    tls_key=None,
                    token_env="BENCHCRAFT_PROBE_TEST_TOKEN",
                    app_config_out=tmp_path / "worker-app.json",
                    ca_bundle_on_worker=None,
                    application_url=(f"http://127.0.0.1:{application_server.server_port}/"),
                    on_ready=start_worker,
                ),
                timeout=45,
            )
        )
        measured = document["measurement"]
        assert measured["mode"] == (
            "HTTP loopback coordinator API with a separately started worker process"
        )
        assert measured["host_separation"] == "operator-selected; not independently verified"
        assert measured["minimum_worker_connections"] == 2
        assert measured["unique_worker_ids_registered"] == 2
        capacities_by_worker_id = measured[
            "registered_capacity_by_worker_id_at_latest_registration"
        ]
        assert len(capacities_by_worker_id) == 2
        assert all(isinstance(worker_id, str) for worker_id in capacities_by_worker_id)
        assert sorted(capacities_by_worker_id.values()) == [2, 2]
        runtime_by_worker_id = measured["worker_runtime_by_worker_id_at_latest_registration"]
        assert set(runtime_by_worker_id) == set(capacities_by_worker_id)
        assert all(isinstance(runtime["python"], str) for runtime in runtime_by_worker_id.values())
        assert measured["registered_worker_connection_pool_budget"] == 12
        assert measured["coordinator_active_api_connection_limit"] == 128
        assert measured["registered_worker_pool_budget_exceeds_coordinator_limit"] is False
        assert measured["worker_workspace_created"] is None
        assert measured["outcomes_succeeded"] == 6
        assert measured["outcomes_failed"] == 0
        assert measured["application_http_429"] is None
        app_config = json.loads((tmp_path / "worker-app.json").read_text(encoding="utf-8"))
        assert app_config["target"] == (f"http://127.0.0.1:{application_server.server_port}/")
        assert token not in json.dumps(app_config)
        assert not (worker_root / ".aibench").exists()
    finally:
        for worker_process in worker_processes:
            module._stop_worker_process(worker_process)
        worker_log.close()
        application_server.shutdown()
        application_server.server_close()
        server_thread.join(timeout=2)


@pytest.mark.parametrize(
    "advertise_url",
    [
        "https://127.0.0.1:8765",
        "https://0.0.0.0:8765",
        "https://255.255.255.255:8765",
        "https://coordinator.localhost:8765",
    ],
)
def test_remote_probe_rejects_unreachable_advertise_hosts_before_starting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, advertise_url: str
) -> None:
    cert = tmp_path / "coordinator.crt"
    key = tmp_path / "coordinator.key"
    cert.write_text("placeholder", encoding="utf-8")
    key.write_text("placeholder", encoding="utf-8")
    output_config = tmp_path / "worker-app.json"
    token = "remote-probe-test-token-that-is-long-enough-12345"
    monkeypatch.setenv("BENCHCRAFT_PROBE_TEST_TOKEN", token)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--remote-worker",
            "--cases",
            "1",
            "--coordinator-listen-host",
            "0.0.0.0",
            "--coordinator-listen-port",
            "8765",
            "--coordinator-advertise-url",
            advertise_url,
            "--coordinator-tls-cert",
            str(cert),
            "--coordinator-tls-key",
            str(key),
            "--worker-app-config-out",
            str(output_config),
            "--token-env",
            "BENCHCRAFT_PROBE_TEST_TOKEN",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        env=os.environ.copy(),
    )
    assert completed.returncode != 0
    assert "reachable HTTPS origin" in completed.stderr
    assert token not in completed.stderr
    assert not output_config.exists()
