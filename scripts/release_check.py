"""Build the release artifacts and prove they work when installed (13-T2, 13-G1; 14-G1).

    python scripts/release_check.py --out DIR [--python PATH ...] [--plugin]

1. Builds the aibench, aibench-deepeval and aibench-ragas sdists and wheels into DIR/dist
   (each wheel is built from its sdist, so stale files in the working tree can't leak into it)
   and writes DIR/dist/SHA256SUMS.
2. For each interpreter (default: the one running this script), creates a clean venv,
   installs the aibench wheel from DIR/dist (dependencies from the package index) and runs
   the documented quickstart outside the repository: init, doctor, run, report, rescore
   without calling the application, and the conversation through `chat --send`. Then it
   runs the 100-case acceptance workflow (kill, resume, report, rescore) against the
   installed package.
3. With --plugin, creates a separate clean environment for each evaluator package and runs
   its real-package contract tests (deterministic local judges, no paid calls). DeepEval and
   Ragas are not co-installed because they own separate dependency environments.

Every step's command, exit code and expectation are written to DIR/release-check.json. The
script exits 1 if any step did not behave as documented. It publishes nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sqlite3
import subprocess
import sys
import time
import venv
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
BIN = "Scripts" if sys.platform == "win32" else "bin"
EXE = ".exe" if sys.platform == "win32" else ""
DEEPEVAL_VERSIONS = (
    "import importlib.metadata as m; "
    "print(m.version('aibench'), m.version('aibench-deepeval'), m.version('deepeval'))"
)
RAGAS_VERSIONS = (
    "import importlib.metadata as m; "
    "print(m.version('aibench'), m.version('aibench-ragas'), m.version('ragas'))"
)


@dataclass
class Step:
    name: str
    command: list[str]
    expected_exit: int
    exit_code: int | None = None
    seconds: float = 0.0
    checks: dict[str, bool] = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == self.expected_exit and all(self.checks.values())


class Checker:
    def __init__(self, out: Path) -> None:
        self.out = out
        self.steps: list[Step] = []

    def run(
        self,
        name: str,
        command: list[str],
        *,
        expected_exit: int = 0,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 900,
    ) -> tuple[Step, subprocess.CompletedProcess[str]]:
        step = Step(name, [str(c) for c in command], expected_exit)
        started = time.monotonic()
        result = subprocess.run(
            step.command,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        step.seconds = round(time.monotonic() - started, 1)
        step.exit_code = result.returncode
        slug = re.sub(r"[^A-Za-z0-9.-]+", "_", name).strip("_")[:60]
        log = self.out / "logs" / f"{len(self.steps):02d}-{slug}.txt"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(
            f"$ {' '.join(step.command)}\nexit={result.returncode}\n--- stdout\n"
            f"{result.stdout}\n--- stderr\n{result.stderr}",
            encoding="utf-8",
        )
        self.steps.append(step)
        mark = "ok  " if step.exit_code == expected_exit else "FAIL"
        print(f"{mark} {name}: exit {step.exit_code} (expected {expected_exit}), {step.seconds}s")
        return step, result


def build(checker: Checker, dist: Path) -> dict[str, str]:
    dist.mkdir(parents=True, exist_ok=True)
    for project in (REPO, REPO / "plugins" / "deepeval", REPO / "plugins" / "ragas"):
        checker.run(
            f"build {project.name}",
            [sys.executable, "-m", "build", "--outdir", str(dist), str(project)],
        )
    sums = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(dist.iterdir())
        if p.suffix in (".whl", ".gz")
    }
    (dist / "SHA256SUMS").write_text(
        "".join(f"{digest}  {name}\n" for name, digest in sums.items()), encoding="utf-8"
    )
    return sums


def make_venv(path: Path, python: str) -> Path:
    if python == sys.executable:
        venv.create(path, with_pip=True, clear=True)
    else:
        subprocess.run([python, "-m", "venv", "--clear", str(path)], check=True)
    return path / BIN / f"python{EXE}"


def wheel(dist: Path, name: str) -> Path:
    [found] = sorted(dist.glob(f"{name}-*.whl"))
    return found


def attempts(project: Path) -> int:
    with sqlite3.connect(project / ".aibench" / "aibench.db") as conn:
        return int(conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0])


def quickstart(checker: Checker, py: Path, work: Path, tag: str) -> None:
    """The documented quickstart (docs/quickstart.md), run by the installed package."""
    aibench = [str(py), "-m", "aibench"]
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    _, where = checker.run(
        f"{tag}: aibench imported from the venv",
        [str(py), "-c", "import aibench, sys; print(aibench.__file__); print(sys.version)"],
        env=env,
    )
    checker.steps[-1].checks["site-packages, not the repository"] = (
        "site-packages" in where.stdout and str(REPO / "src") not in where.stdout
    )
    step, version = checker.run(f"{tag}: aibench --version", [*aibench, "--version"], env=env)
    step.note = version.stdout.strip()
    work.mkdir(parents=True, exist_ok=True)
    checker.run(f"{tag}: init", [*aibench, "init", "support-bench"], cwd=work, env=env)
    project = work / "support-bench"
    checker.run(f"{tag}: doctor", [*aibench, "doctor"], cwd=project, env=env)

    # The quickstart has one application failure, so the run is incomplete: exit 3.
    step, result = checker.run(
        f"{tag}: run", [*aibench, "run", "--json"], cwd=project, env=env, expected_exit=3
    )
    data = json.loads(result.stdout) if result.stdout.strip().startswith("{") else {}
    gates = {g["gate_id"]: g for g in data.get("gates", [])}
    correct = gates.get("correct-answers", {})
    step.checks["correct-answers gate fails at 8/10"] = (
        correct.get("status") == "fail" and correct.get("passes") == 8
    )
    step.checks["answers-present gate passes"] = (
        gates.get("answers-present", {}).get("status") == "pass"
    )
    run_id = data.get("run_id", "missing")

    step, _ = checker.run(f"{tag}: report", [*aibench, "report", run_id], cwd=project, env=env)
    step.checks["report.html written"] = (
        project / ".aibench" / "reports" / run_id / "report.html"
    ).is_file()
    step, markdown = checker.run(
        f"{tag}: markdown report",
        [*aibench, "report", run_id, "--format", "markdown", "--out", "-"],
        cwd=project,
        env=env,
    )
    step.checks["shows support-004 evidence"] = "support-004" in markdown.stdout

    before = attempts(project)
    step, _ = checker.run(
        f"{tag}: rescore",
        [*aibench, "evaluate", run_id, "--plan", "plan.json", "--json"],
        cwd=project,
        env=env,
    )
    step.checks["no application call"] = attempts(project) == before

    checker.run(
        f"{tag}: chat /plan",
        [*aibench, "chat", "--new", "--objective", "answers are correct", "--send", "/plan"],
        cwd=project,
        env=env,
    )
    step, result = checker.run(
        f"{tag}: chat /run",
        [*aibench, "chat", "--send", "/run"],
        cwd=project,
        env=env,
        expected_exit=3,
    )
    step, result = checker.run(
        f"{tag}: chat /report", [*aibench, "chat", "--send", "/report"], cwd=project, env=env
    )
    step.checks["reports 8/10"] = "8/10" in result.stdout


def acceptance(checker: Checker, py: Path, out: Path, tag: str) -> None:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    step, _ = checker.run(
        f"{tag}: 100-case acceptance",
        [str(py), str(REPO / "examples" / "acceptance" / "run_acceptance.py"), "--out", str(out)],
        env=env,
    )
    summary_path = out / "summary.json"
    summary: dict[str, Any] = (
        json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    )
    quality = summary.get("quality", {})
    step.checks["failures are exactly the injected cases"] = bool(
        quality.get("failing_cases_equal_injected")
    )
    step.checks["reliability 1.0"] = summary.get("reliability", {}).get("value") == 1.0
    step.checks["rescore made 0 application calls"] = (
        summary.get("rescore", {}).get("application_calls_during_rescore") == 0
    )
    step.checks["nothing called while stopped"] = (
        summary.get("interruption", {}).get("calls_while_stopped") == 0
    )


def _plugin_environment(
    checker: Checker,
    dist: Path,
    out: Path,
    *,
    name: str,
    wheel_name: str,
    version_probe: str,
    python_env: str,
    tests: list[str],
) -> None:
    py = make_venv(out / f"venv-{name}", sys.executable)
    checker.run(
        f"{name}: install core and plugin wheels",
        [
            str(py),
            "-m",
            "pip",
            "install",
            "-q",
            str(wheel(dist, "aibench")),
            str(wheel(dist, wheel_name)),
        ],
    )
    step, result = checker.run(f"{name}: installed versions", [str(py), "-c", version_probe])
    step.note = result.stdout.strip()
    checker.run(
        f"{name}: real-package adapter contract tests",
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-rs",
            *tests,
        ],
        cwd=REPO,
        env={**os.environ, python_env: str(py)},
    )


def plugins(checker: Checker, dist: Path, out: Path) -> None:
    _plugin_environment(
        checker,
        dist,
        out,
        name="deepeval",
        wheel_name="aibench_deepeval",
        version_probe=DEEPEVAL_VERSIONS,
        python_env="AIBENCH_DEEPEVAL_PYTHON",
        tests=["tests/test_deepeval_adapter.py", "tests/test_worker_evaluator.py"],
    )
    _plugin_environment(
        checker,
        dist,
        out,
        name="ragas",
        wheel_name="aibench_ragas",
        version_probe=RAGAS_VERSIONS,
        python_env="AIBENCH_RAGAS_PYTHON",
        tests=["tests/test_ragas_adapter.py", "tests/test_worker_evaluator.py"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="Work and evidence directory.")
    parser.add_argument(
        "--python", action="append", default=None, help="Interpreter to install into (repeat)."
    )
    parser.add_argument(
        "--plugin", action="store_true", help="Also check the DeepEval and Ragas plugins."
    )
    parser.add_argument(
        "--no-acceptance", action="store_true", help="Skip the 100-case acceptance workflow."
    )
    args = parser.parse_args()
    out: Path = args.out.resolve()
    if out.is_relative_to(REPO):
        parser.error("--out must be outside the repository, so nothing there is picked up")
    out.mkdir(parents=True, exist_ok=True)
    checker = Checker(out)
    dist = out / "dist"
    sums = build(checker, dist)

    interpreters: list[str] = args.python or [sys.executable]
    environments = []
    for index, python in enumerate(interpreters):
        tag = f"py{index}"
        py = make_venv(out / f"venv-{tag}", python)
        _, version = checker.run(
            f"{tag}: interpreter",
            [str(py), "-c", "import platform, sys; print(platform.python_version(), sys.platform)"],
        )
        environments.append({"tag": tag, "python": version.stdout.strip()})
        checker.run(
            f"{tag}: install wheel",
            [str(py), "-m", "pip", "install", "-q", str(wheel(dist, "aibench"))],
        )
        quickstart(checker, py, out / f"demo-{tag}", tag)
        if not args.no_acceptance:
            acceptance(checker, py, out / f"acceptance-{tag}", tag)
    if args.plugin:
        plugins(checker, dist, out)

    failed = [s.name for s in checker.steps if not s.ok]
    summary = {
        "host": {"platform": platform.platform(), "machine": platform.machine()},
        "artifacts": sums,
        "environments": environments,
        "steps": [{**asdict(s), "ok": s.ok} for s in checker.steps],
        "failed": failed,
    }
    (out / "release-check.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\n{len(checker.steps) - len(failed)}/{len(checker.steps)} steps as documented")
    for name in failed:
        print(f"  not as documented: {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
