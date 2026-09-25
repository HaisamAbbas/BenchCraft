"""The deterministic offline E2E suite, E2E-01 to E2E-08, from a clean install (22-T3).

    python scripts/e2e_suite.py --out DIR

1. Builds the aibench sdist, then the wheel from that sdist, into DIR/dist. Stale files in
   the working tree cannot leak into the wheel.
2. Creates a clean venv. It installs the wheel and the test runner, with dependency
   versions constrained by `requirements-dev.lock.txt` and taken from the package index.
3. Checks that `aibench` imports from the venv's site-packages, not from `src/`.
4. Runs each journey's tests with that interpreter. `AIBENCH_E2E_INSTALLED=1` makes the
   terminal journey start the installed `aibench`.
5. Writes DIR/e2e-suite.json: environment, per-journey test outcomes, and the journey
   evidence (run IDs, case IDs, side-effect counts).

Everything runs locally against fixture applications and a scripted assistant model. No
paid provider, remote service or credential is used. The script publishes nothing, and
exits 1 unless every selected test passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import venv
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BIN = "Scripts" if sys.platform == "win32" else "bin"
EXE = ".exe" if sys.platform == "win32" else ""

# Journey -> (what it proves, the tests that exercise it). The definitions are recorded in
# docs/engineering/ticket-test-matrix.md.
JOURNEYS: dict[str, tuple[str, list[str]]] = {
    "E2E-01": (
        (
            "Conversational loop from the first message to an evidence-linked report and "
            "follow-up discussion, inside the CLI"
        ),
        [
            "tests/test_e2e_cli_journey.py::test_a_user_benchmarks_an_app_from_the_first_message_to_evidence_in_the_cli",
            "tests/test_mvp_acceptance.py::test_a_fresh_user_completes_the_conversational_acceptance_journey",
        ],
    ),
    "E2E-02": (
        "Steering a live run: questions, pause, resume, cancel",
        [
            "tests/test_engine.py::test_pause_stops_new_dispatch_until_resumed",
            "tests/test_engine.py::test_cancel_stops_dispatch_and_cancels_in_flight_work",
            "tests/test_parallel_execution.py::test_throttled_work_can_still_be_paused_and_cancelled_promptly",
            "tests/test_conversation_hardening.py::test_controls_work_during_a_provider_outage_and_stay_separate_from_replies",
        ],
    ),
    "E2E-03": (
        "Interruption and recovery without duplicate application side effects",
        [
            "tests/test_mvp_acceptance.py::test_the_100_case_workflow_survives_a_kill_finds_the_injected_failures_and_rescores_offline",
            "tests/test_session_recovery.py::test_reopening_after_a_kill_shows_the_real_state_and_restarts_nothing",
            "tests/test_session_recovery.py::test_retrying_a_start_after_a_kill_never_starts_a_second_run",
            "tests/test_engine.py::test_interrupt_then_resume_completes_without_duplicates",
            "tests/test_engine_faults.py::test_crash_at_evaluation_commit_boundary_does_not_duplicate_results",
        ],
    ),
    "E2E-04": (
        "Stored-output rescoring never invokes the application",
        [
            "tests/test_cli_run.py::test_interrupted_run_resumes_through_the_cli_and_rescoring_never_invokes",
            "tests/test_engine.py::test_manual_plan_runs_end_to_end_and_saved_executions_rescore",
        ],
    ),
    "E2E-05": (
        "Invalid or denied plans make zero application and evaluator calls",
        [
            "tests/test_engine_policy.py::test_denied_actions_dispatch_nothing",
            "tests/test_engine_policy.py::test_denied_http_target_and_secret_never_receive_a_request",
            "tests/test_cli_run.py::test_example_plan_runs_under_the_dev_policy_and_is_denied_by_default",
            "tests/test_cli_plan.py::test_plan_validate_reports_invalid_and_denied_plans",
        ],
    ),
    "E2E-06": (
        "Reference isolation, and missing evidence as a gap rather than a score",
        [
            "tests/test_cli_runner.py::test_sentinel_reference_never_reaches_stdin_env_argv_or_captures",
            "tests/test_conversation.py::test_the_assistant_never_receives_reference_answers",
            "tests/test_mvp_acceptance.py::test_a_black_box_endpoint_yields_a_missing_evidence_gap_not_a_score",
            "tests/test_deepeval_adapter.py::test_missing_retrieval_is_never_filled_from_reference_context",
        ],
    ),
    "E2E-07": (
        (
            "Headless and interactive entry points share services; manual and generated "
            "plans measure identically"
        ),
        [
            "tests/test_plan_equivalence.py::test_manual_template_and_model_plans_measure_identically",
            "tests/test_tui.py::test_slash_commands_use_real_session_services_and_cover_the_contract",
            "tests/test_tui.py::test_explicit_run_starts_unpresented_validated_draft_with_a_preview",
            "tests/test_cli_chat.py::test_non_tty_chat_json_is_machine_readable_and_can_resume",
        ],
    ),
    "E2E-08": (
        (
            "A fresh repository-aware conversation sees bounded source evidence, runs the "
            "validated application, and reports stored results without duplicate calls"
        ),
        [
            "tests/test_e2e_repository_conversation.py::test_fresh_repository_inspection_runs_and_reports_with_evidence",
        ],
    ),
}


def _run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(argv), flush=True)
    return subprocess.run(argv, text=True, capture_output=True, check=False, **kwargs)


def _require(result: subprocess.CompletedProcess[str], what: str) -> None:
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-4000:] + result.stderr[-4000:])
        raise SystemExit(f"{what} failed with exit code {result.returncode}")


def _lock_pins(names: tuple[str, ...]) -> list[str]:
    pins = []
    for line in (REPO / "requirements-dev.lock.txt").read_text(encoding="utf-8").splitlines():
        requirement = line.split(";")[0].strip()
        name = re.split(r"[=<>!~ ]", requirement, maxsplit=1)[0].lower()
        if name in names and "==" in requirement:
            pins.append(requirement)
    return pins


def _outcomes(junit: Path) -> dict[str, dict[str, Any]]:
    outcomes: dict[str, dict[str, Any]] = {}
    for case in ET.parse(junit).getroot().iter("testcase"):
        file = case.get("classname", "").replace(".", "/") + ".py"
        # A parametrized test is one node here; it passes only if every instance passed.
        node = f"{file}::{case.get('name', '').split('[')[0]}"
        status = "passed"
        detail = ""
        for tag in ("failure", "error", "skipped"):
            element = case.find(tag)
            if element is not None:
                status = {"failure": "failed", "error": "error", "skipped": "skipped"}[tag]
                detail = (element.get("message") or "")[:500]
        entry = outcomes.setdefault(node, {"status": "passed", "seconds": 0.0, "detail": ""})
        entry["seconds"] = round(entry["seconds"] + float(case.get("time", 0)), 3)
        entry["instances"] = entry.get("instances", 0) + 1
        if status != "passed" and entry["status"] in ("passed", "skipped"):
            entry["status"], entry["detail"] = status, detail
    return outcomes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--journey", action="append", help="run only these journeys")
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    selected = {k: v for k, v in JOURNEYS.items() if not args.journey or k in args.journey}

    dist = out / "dist"
    _require(_run([sys.executable, "-m", "build", "--outdir", str(dist), str(REPO)]), "build")
    [wheel] = sorted(dist.glob("aibench-*.whl"))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()

    env_dir = out / "venv"
    venv.create(env_dir, with_pip=True, clear=True)
    python = str(env_dir / BIN / f"python{EXE}")
    constraints = ["--constraint", str(REPO / "requirements-dev.lock.txt")]
    _require(_run([python, "-m", "pip", "install", "-q", str(wheel), *constraints]), "install")
    test_deps = _lock_pins(("pytest", "pywinpty") if sys.platform == "win32" else ("pytest",))
    _require(_run([python, "-m", "pip", "install", "-q", *test_deps]), "install test runner")
    located = _run(
        [python, "-c", "import aibench; print(aibench.__file__, aibench.__version__)"], cwd=REPO
    )
    _require(located, "import check")
    aibench_file, version = located.stdout.split()
    if "site-packages" not in aibench_file:
        raise SystemExit(f"aibench was imported from {aibench_file}, not the clean install")

    evidence_dir = out / "evidence"
    env = {
        **os.environ,
        "AIBENCH_E2E_INSTALLED": "1",
        "AIBENCH_E2E_EVIDENCE": str(evidence_dir),
        "PYTHONPATH": "",
    }
    env.pop("OPENAI_API_KEY", None)
    nodes = sorted({n for _, tests in selected.values() for n in tests})
    junit = out / "e2e-junit.xml"
    started = time.time()
    run = _run(
        [
            python,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-rs",
            f"--junitxml={junit}",
            *nodes,
        ],
        cwd=REPO,
        env=env,
    )
    (out / "pytest-output.txt").write_text(run.stdout + run.stderr, encoding="utf-8")
    outcomes = _outcomes(junit) if junit.exists() else {}

    journeys = {}
    for key, (claim, tests) in selected.items():
        results = {t: outcomes.get(t, {"status": "not run"}) for t in tests}
        journeys[key] = {
            "claim": claim,
            "status": "passed"
            if all(r["status"] == "passed" for r in results.values())
            else "failed",
            "tests": results,
        }
    document = {
        "suite": "scripts/e2e_suite.py",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(started)),
        "seconds": round(time.time() - started, 1),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "aibench_version": version,
            "aibench_imported_from": aibench_file,
            "wheel": wheel.name,
            "wheel_sha256": digest,
        },
        "pytest_exit_code": run.returncode,
        "journeys": journeys,
        "evidence": {
            p.stem: json.loads(p.read_text(encoding="utf-8"))
            for p in sorted(evidence_dir.glob("*.json"))
        },
    }
    (out / "e2e-suite.json").write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    for key, journey in journeys.items():
        print(f"{key}: {journey['status']}")
    return (
        0 if run.returncode == 0 and all(j["status"] == "passed" for j in journeys.values()) else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
