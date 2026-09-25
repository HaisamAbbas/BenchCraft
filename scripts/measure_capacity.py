"""Measure local run capacity with the real CLI (20-T1, 20-T4).

Each scenario builds a fresh project and executes `aibench run --json` in its own process
against `examples/apps/rate_limited_app.py`, served from this process with a quota far
above the offered load. The app's response delay stands in for application latency. For
every scenario the script records:

- the engine's elapsed time and the process wall time (CLI start-up included);
- the harness process's CPU time and peak memory;
- system-wide CPU utilisation during the run, so contention from other work is visible;
- the peak number of requests the server saw at once;
- the workspace database and artifact sizes.

It measures this machine and this workload only. It is not a capacity claim for other
hardware, other applications or evaluators that call a model.

    python scripts/measure_capacity.py --out docs/engineering/evidence/20/capacity.json
    python scripts/measure_capacity.py --cases 20 --scenario 0:4   # a quick check
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent

# (application delay in seconds, application concurrency)
DEFAULT_SCENARIOS = [(0.0, 1), (0.0, 16), (0.0, 64), (0.25, 64), (1.0, 16), (1.0, 64)]


def _load_app() -> Any:
    spec = importlib.util.spec_from_file_location(
        "rate_limited_app", ROOT / "examples" / "apps" / "rate_limited_app.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- process accounting


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

    @property
    def seconds(self) -> float:
        return ((self.high << 32) | self.low) / 1e7


class _BasicAccounting(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", ctypes.c_uint32),
        ("TotalProcesses", ctypes.c_uint32),
        ("ActiveProcesses", ctypes.c_uint32),
        ("TotalTerminatedProcesses", ctypes.c_uint32),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
        ("IoCounters", ctypes.c_uint64 * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


_CREATE_SUSPENDED = 0x4


def _system_times() -> tuple[float, float] | None:
    """(busy, total) CPU seconds summed over all processors, or None off Windows."""
    if sys.platform != "win32":
        return None
    idle, kernel, user = _FileTime(), _FileTime(), _FileTime()
    ctypes.windll.kernel32.GetSystemTimes(
        ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
    )
    total = kernel.seconds + user.seconds  # kernel time includes idle time
    return total - idle.seconds, total


def _run_measured(argv: list[str], cwd: Path) -> dict[str, Any]:
    before = _system_times()
    started = time.perf_counter()
    if sys.platform == "win32":
        # A venv's python.exe is a launcher that starts the real interpreter as a child, so
        # the whole process tree is accounted through a job object. The process starts
        # suspended and joins the job before it can start anything.
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        job = kernel32.CreateJobObjectW(None, None)
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_SUSPENDED,
        )
        handle = ctypes.c_void_p(int(proc._handle))  # type: ignore[attr-defined]
        if not job or not kernel32.AssignProcessToJobObject(ctypes.c_void_p(job), handle):
            proc.kill()
            raise OSError(f"could not account the process in a job object: {ctypes.GetLastError()}")
        if ctypes.windll.ntdll.NtResumeProcess(handle) != 0:
            proc.kill()
            raise OSError("could not resume the suspended process")
        stdout, stderr = proc.communicate()
        accounting, limits = _BasicAccounting(), _ExtendedLimits()
        for info_class, info in ((1, accounting), (9, limits)):
            if not kernel32.QueryInformationJobObject(
                ctypes.c_void_p(job), info_class, ctypes.byref(info), ctypes.sizeof(info), None
            ):
                raise OSError(f"job object query {info_class} failed: {ctypes.GetLastError()}")
        kernel32.CloseHandle(ctypes.c_void_p(job))
        cpu = (accounting.TotalUserTime + accounting.TotalKernelTime) / 1e7
        peak = int(limits.PeakProcessMemoryUsed)
        memory_kind = "peak private commit of any process in the tree (job object)"
    else:
        # os.wait4 reports this child's own usage. RUSAGE_CHILDREN would carry the largest
        # earlier child's peak into every later scenario.
        proc = subprocess.Popen(argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        outputs: dict[str, bytes] = {}
        readers = [
            threading.Thread(target=lambda n=n, s=s: outputs.__setitem__(n, s.read()))
            for n, s in (("stdout", proc.stdout), ("stderr", proc.stderr))
        ]
        for reader in readers:
            reader.start()
        _, status, usage = os.wait4(proc.pid, 0)
        for reader in readers:
            reader.join()
        proc.returncode = os.waitstatus_to_exitcode(status)
        stdout, stderr = outputs["stdout"], outputs["stderr"]
        cpu = usage.ru_utime + usage.ru_stime
        peak = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        memory_kind = "max resident set size of the process (wait4)"
    wall = time.perf_counter() - started
    after = _system_times()
    system_busy = None
    if before and after and after[1] > before[1]:
        system_busy = round((after[0] - before[0]) / (after[1] - before[1]), 3)
    return {
        "exit_code": proc.returncode,
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace")[-2000:],
        "wall_seconds": round(wall, 3),
        "process_cpu_seconds": round(cpu, 3),
        "peak_memory_mb": round(peak / 2**20, 1),  # MiB
        "peak_memory_kind": memory_kind,
        "system_cpu_busy_fraction": system_busy,
    }


# ---------------------------------------------------------------- scenarios


def _project(root: Path, cases: int, port: int, concurrency: int) -> None:
    lines = [
        json.dumps({"case_id": f"c{i:06d}", "input": f"q{i}", "expected_output": f"ok: q{i}"})
        for i in range(cases)
    ]
    (root / "data.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    app = {
        "application_id": "capacity-mock",
        "runner": "http",
        "target": "capacity-mock",
        "transport": {"kind": "http", "url": f"http://127.0.0.1:{port}/", "timeout_seconds": 60},
        "input_binding": {"fields": {"/input": "/input"}},
    }
    (root / "app.json").write_text(json.dumps(app), encoding="utf-8")
    plan = {
        "plan_id": "capacity",
        "dataset": "data.jsonl",
        "application": "app.json",
        "metrics": [{"metric": "native.exact_match"}],
        "concurrency": {"application": concurrency, "evaluation": 4},
    }
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    policy = {
        "allowed_applications": ["capacity-mock"],
        "allowed_http_origins": [f"http://127.0.0.1:{port}"],
    }
    (root / "policy.json").write_text(json.dumps(policy), encoding="utf-8")


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.exists() else 0


def run_scenario(
    delay: float, concurrency: int, cases: int, scratch: Path, startup_cpu: float = 0.0
) -> dict[str, Any]:
    app = _load_app()
    server = app.make_server(1e9, burst=10**9, delay=delay)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        root = Path(tempfile.mkdtemp(prefix=f"cap-{delay:g}-{concurrency}-", dir=scratch))
        _project(root, cases, server.server_port, concurrency)
        argv = [
            sys.executable,
            "-m",
            "aibench",
            "run",
            "--plan",
            "plan.json",
            "--policy",
            "policy.json",
            "--workspace",
            ".",
            "--json",
        ]
        measured = _run_measured(argv, root)
    finally:
        server.shutdown()
        server.server_close()
    result: dict[str, Any] = {
        "application_delay_seconds": delay,
        "application_concurrency": concurrency,
        "evaluation_concurrency": 4,
        "cases": cases,
        "exit_code": measured["exit_code"],
        "wall_seconds": measured["wall_seconds"],
        "process_cpu_seconds": measured["process_cpu_seconds"],
        "peak_memory_mb": measured["peak_memory_mb"],
        "peak_memory_kind": measured["peak_memory_kind"],
        "system_cpu_busy_fraction": measured["system_cpu_busy_fraction"],
        "server_requests": len(server.arrivals),
        "server_rejections": len(server.rejections),
        "server_peak_in_flight": server.peak_active,
        "database_bytes": _size(root / ".aibench" / "aibench.db")
        + _size(root / ".aibench" / "aibench.db-wal"),
        "artifact_bytes": _size(root / ".aibench" / "artifacts"),
    }
    try:
        report = json.loads(measured["stdout"])
    except json.JSONDecodeError:
        result["error"] = measured["stderr"] or measured["stdout"][-2000:]
        return result
    elapsed = report["budget"]["elapsed_seconds"]
    result.update(
        {
            "state": report["state"],
            "counts": report["counts"],
            "engine_elapsed_seconds": elapsed,
            "cases_per_second": round(cases / elapsed, 2) if elapsed else None,
            # What the application latency alone would allow at this concurrency.
            "latency_bound_cases_per_second": round(concurrency / delay, 2) if delay else None,
            # Net of a separately measured `aibench --version` start-up only: the run's own
            # fixed work (compile, migrations, run creation) stays in, so compare case counts
            # for the marginal cost.
            "harness_cpu_ms_per_case": round(
                1000 * max(measured["process_cpu_seconds"] - startup_cpu, 0.0) / cases, 2
            ),
            "engine_cpu_fraction_of_wall": round(
                max(measured["process_cpu_seconds"] - startup_cpu, 0.0) / elapsed, 3
            )
            if elapsed
            else None,
        }
    )
    return result


def _machine() -> dict[str, Any]:
    info: dict[str, Any] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "processor": platform.processor(),
        "logical_cpus": os.cpu_count(),
    }
    if sys.platform == "win32":

        class _Memory(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_uint32),
                ("dwMemoryLoad", ctypes.c_uint32),
                ("ullTotalPhys", ctypes.c_uint64),
                ("ullAvailPhys", ctypes.c_uint64),
                ("ullTotalPageFile", ctypes.c_uint64),
                ("ullAvailPageFile", ctypes.c_uint64),
                ("ullTotalVirtual", ctypes.c_uint64),
                ("ullAvailVirtual", ctypes.c_uint64),
                ("ullAvailExtendedVirtual", ctypes.c_uint64),
            ]

        memory = _Memory()
        memory.dwLength = ctypes.sizeof(memory)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
        info["physical_memory_gb"] = round(memory.ullTotalPhys / 2**30, 1)
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cases", type=int, default=300)
    parser.add_argument(
        "--scenario",
        action="append",
        default=[],
        metavar="DELAY:CONCURRENCY",
        help="repeatable; default: the built-in set",
    )
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--scratch", type=Path, help="where projects are created (default: temp)")
    args = parser.parse_args()
    scenarios = [
        (float(d), int(c)) for d, c in (s.split(":") for s in args.scenario)
    ] or DEFAULT_SCENARIOS
    scratch = args.scratch or Path(tempfile.mkdtemp(prefix="aibench-capacity-"))
    scratch.mkdir(parents=True, exist_ok=True)

    started = _run_measured([sys.executable, "-m", "aibench", "--version"], ROOT)
    results = []
    for delay, concurrency in scenarios:
        for repetition in range(args.repeat):
            result = run_scenario(
                delay, concurrency, args.cases, scratch, started["process_cpu_seconds"]
            )
            result["repetition"] = repetition
            results.append(result)
            print(
                json.dumps(
                    {
                        k: result.get(k)
                        for k in (
                            "application_delay_seconds",
                            "application_concurrency",
                            "cases",
                            "state",
                            "engine_elapsed_seconds",
                            "cases_per_second",
                            "latency_bound_cases_per_second",
                            "harness_cpu_ms_per_case",
                            "engine_cpu_fraction_of_wall",
                            "server_peak_in_flight",
                            "system_cpu_busy_fraction",
                        )
                    }
                ),
                flush=True,
            )
    document = {
        "tool": "scripts/measure_capacity.py",
        "argv": sys.argv[1:],
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "machine": _machine(),
        "cli_startup": {
            k: started[k]
            for k in ("wall_seconds", "process_cpu_seconds", "system_cpu_busy_fraction")
        },
        "workload": {
            "application": "examples/apps/rate_limited_app.py over loopback HTTP; quota far above load",
            "metric": "native.exact_match (deterministic, in-process)",
            "evaluation_concurrency": 4,
        },
        "scenarios": results,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return 0 if all(r.get("state") == "completed" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
