"""Run the 100-case MVP acceptance workflow through the real CLI and record the evidence.

    python examples/acceptance/run_acceptance.py --out ACCEPTANCE_DIR

Steps, all with the installed `aibench` command against a live `rag_service` on loopback:
1. `aibench run` in a child process, killed once 30 application calls have been made;
2. `aibench resume` to finish the run under its frozen plan;
3. `aibench report` (JSON and HTML) from stored facts;
4. `aibench evaluate` with another binding (stored outputs only).

`summary.json` records what was observed: per-case application calls, work completion
(reliability), the error taxonomy, cost accounting completeness, whether the failing cases
are exactly the injected ones, and whether rescoring called the application.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def _service() -> Any:
    spec = importlib.util.spec_from_file_location("rag_service", HERE / "rag_service.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _aibench(*args: str, check_codes: tuple[int, ...] = (0,)) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "aibench", *args], capture_output=True, text=True, check=False
    )
    if proc.returncode not in check_codes:
        raise SystemExit(f"aibench {' '.join(args)} exited {proc.returncode}: {proc.stderr}")
    return proc.returncode, proc.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="Directory for the evidence.")
    args = parser.parse_args()
    out: Path = args.out.resolve()
    if out.exists() and any(out.iterdir()):
        raise SystemExit(f"{out} is not empty; choose a new directory")
    rag = _service()
    project = out / "project"
    shutil.copytree(HERE, project, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    service = rag.RagService().start()
    started = time.monotonic()
    try:
        for name in ("rag.app.json", "blackbox.app.json"):
            path = project / name
            path.write_text(
                path.read_text(encoding="utf-8").replace("http://127.0.0.1:8766", service.url),
                encoding="utf-8",
            )
        rows = [json.loads(x) for x in (project / "rag100.jsonl").read_text().splitlines()]
        injected = {r["case_id"] for r in rows if r["metadata"]["injected"] != "none"}

        # 1. run, killed mid-way
        service.delay = 0.15
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "aibench",
                "run",
                "--plan",
                str(project / "plan.json"),
                "--workspace",
                str(project),
                "--json",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 120
        while service.total_calls() < 30:
            if child.poll() is not None:
                raise SystemExit("the run finished before it could be interrupted")
            if time.monotonic() > deadline:
                child.kill()
                raise SystemExit("the run made fewer than 30 calls in 120 s")
            time.sleep(0.02)
        child.kill()
        child.wait(timeout=30)
        calls_at_kill = service.total_calls()
        _, listed = _aibench("runs", "list", "--workspace", str(project), "--json")
        run_id = json.loads(listed)[0]["run_id"]
        time.sleep(1.0)
        calls_while_stopped = service.total_calls() - calls_at_kill

        # 2. resume
        service.delay = 0.0
        resume_code, resumed = _aibench(
            "resume", run_id, "--workspace", str(project), "--json", check_codes=(0, 1, 3)
        )
        outcome = json.loads(resumed)

        # 3. report
        _, report_text = _aibench(
            "report", run_id, "--workspace", str(project), "--format", "json", "--out", "-"
        )
        report = json.loads(report_text)
        _aibench("report", run_id, "--workspace", str(project), "--out", str(out / "report.html"))
        (out / "report.json").write_text(report_text, encoding="utf-8")

        # 4. rescore stored outputs
        calls_before_rescore = service.total_calls()
        _aibench(
            "evaluate",
            run_id,
            "--plan",
            str(project / "rescore.plan.json"),
            "--workspace",
            str(project),
            "--json",
        )
        rescore_calls = service.total_calls() - calls_before_rescore
        _, rescored_text = _aibench(
            "report", run_id, "--workspace", str(project), "--format", "json", "--out", "-"
        )
        rescored = json.loads(rescored_text)["scoring_passes"][-1]["metrics"][0]["summary"]
    finally:
        service.stop()

    engine = report["scoring_passes"][0]["metrics"][0]["summary"]
    application = report["application"]
    # Completed valid work / scheduled eligible work (§23), per kind. An evaluation that
    # was skipped because its execution failed is not completed work, whatever the work
    # item's state says; it is excluded from the eligible denominator instead.
    completed = application["completed"] + engine["completed"]
    scheduled = (application["planned"] or 0) + engine["eligible"] + engine["pending"]
    failed = {item["case_id"] for item in report["evidence"]["items"]}
    summary = {
        "run_id": run_id,
        "hardware": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
        },
        "workload": "100 cases, 1 exact-match binding, application concurrency 4, loopback HTTP",
        "wall_seconds": round(time.monotonic() - started, 1),
        "interruption": {
            "calls_at_kill": calls_at_kill,
            "calls_while_stopped": calls_while_stopped,
            "resume_exit_code": resume_code,
        },
        "application_calls": {
            "total": sum(service.calls.values()),
            "distinct_cases_called": len(service.calls),
            "max_calls_for_one_case": max(service.calls.values()),
            "cases_called_twice": sum(1 for n in service.calls.values() if n > 1),
        },
        "reliability": {
            "definition": (
                "(successful executions + completed evaluations) / (planned executions + "
                "evaluations with a usable execution)"
            ),
            "executions": [application["completed"], application["planned"]],
            "evaluations": [engine["completed"], engine["eligible"] + engine["pending"]],
            "completed": completed,
            "scheduled": scheduled,
            "value": round(completed / scheduled, 4) if scheduled else None,
            "error_taxonomy": {
                "application": report["application"]["error_kinds"],
                "evaluator": report["scoring_passes"][0]["metrics"][0]["evaluator_failures"],
                "work_items_needing_attention": len(report["work"]["needs_attention"]),
            },
            "application_attempts": application["attempts"],
            "uncommitted_dispatches": application["uncommitted_dispatches"],
        },
        "cost_accounting": {
            role: {
                k: report["cost"][role].get(k)
                for k in ("calls", "calls_with_known_cost", "accounting", "total_cost_usd")
            }
            for role in ("application", "evaluator")
        },
        "quality": {
            "selected": engine["selected"],
            "passes": engine["decisions"]["pass"],
            "fails": engine["decisions"]["fail"],
            "failing_cases_equal_injected": failed == injected,
            "gates": [(g["gate_id"], g["status"], g.get("reason")) for g in report["gates"]],
            "exit_code": outcome["exit_code"],
        },
        "rescore": {
            "application_calls_during_rescore": rescore_calls,
            "selected": rescored["selected"],
            "passes": rescored["decisions"]["pass"],
        },
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
