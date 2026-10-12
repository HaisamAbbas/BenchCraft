"""The G22.3 probe exercises a real CLI worker and cleans up failed measurements."""

from __future__ import annotations

import asyncio
import importlib.util
import json
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
