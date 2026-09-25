"""The local capacity measurement (20-T1, 20-T4) runs the real CLI end to end and reports
what the application actually received, so its evidence describes the tested workload."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_capacity_measurement_records_the_run_it_actually_executed(tmp_path: Path) -> None:
    out = tmp_path / "capacity.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "measure_capacity.py"),
            "--cases",
            "6",
            "--scenario",
            "0.05:3",
            "--scenario",
            "0:1",
            "--scratch",
            str(tmp_path / "projects"),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    document = json.loads(out.read_text(encoding="utf-8"))
    assert document["machine"]["logical_cpus"]
    assert document["argv"][:2] == ["--cases", "6"]
    startup_cpu = document["cli_startup"]["process_cpu_seconds"]
    first, second = document["scenarios"]
    assert (first["application_concurrency"], second["application_concurrency"]) == (3, 1)
    for scenario in (first, second):
        assert scenario["state"] == "completed"
        assert scenario["counts"]["execution"] == {"succeeded": 6}
        assert scenario["counts"]["evaluation"] == {"succeeded": 6}
        # The server's own record agrees with the harness: every case invoked once, none
        # rejected, and never more in flight than the plan's application concurrency.
        assert scenario["server_requests"] == 6 and scenario["server_rejections"] == 0
        assert 1 <= scenario["server_peak_in_flight"] <= scenario["application_concurrency"]
        assert scenario["cases_per_second"] > 0
        # The whole run is accounted, so it costs at least what bare start-up costs, and
        # each scenario has its own memory peak (not one carried over from another child).
        assert scenario["process_cpu_seconds"] >= startup_cpu > 0
        assert scenario["peak_memory_mb"] > 0 and scenario["peak_memory_kind"]
        assert scenario["database_bytes"] > 0 and scenario["artifact_bytes"] > 0
    assert first["latency_bound_cases_per_second"] == 60.0
    assert second["latency_bound_cases_per_second"] is None
