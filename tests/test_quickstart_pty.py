"""11-G3 / 11-T2 in a real terminal: the quickstart run and its report through the
interactive chat on a Windows ConPTY (the same services as `aibench report`)."""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("winpty") is None,
    reason="real ConPTY smoke requires Windows and the optional pywinpty test dependency",
)


def test_the_quickstart_runs_and_reports_in_a_real_terminal(tmp_path: Path) -> None:
    from winpty import PtyProcess

    project = tmp_path / "quickstart"
    assert CliRunner().invoke(app, ["init", str(project)]).exit_code == 0
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
    process = PtyProcess.spawn(
        [sys.executable, "-m", "aibench", "chat", "--new", "--objective", "answers are correct"],
        cwd=str(project),
        env=env,
        dimensions=(50, 200),
    )
    chunks: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        while True:
            try:
                chunks.put(process.read(4096))
            except Exception:  # noqa: BLE001 - EOF or teardown closes the PTY handle
                return

    threading.Thread(target=read_output, daemon=True).start()
    observed = ""

    def expect(fragment: str, timeout: float = 30.0) -> None:
        nonlocal observed
        deadline = time.monotonic() + timeout
        while fragment not in observed and time.monotonic() < deadline:
            try:
                observed += chunks.get(timeout=0.1)
            except queue.Empty:
                if not process.isalive():
                    break
        assert fragment in observed, f"terminal did not display {fragment!r}: {observed[-3000:]}"

    try:
        expect("Type /help for commands")
        process.write("/plan\r")
        expect("ready to run")
        process.write("/run\r")
        expect("completed: executions 10/10", timeout=90)
        process.write("/report\r")
        expect("report for run")
        expect("pass 8/10 (80.0%) of selected")
        expect("wrote html")
        process.write("/exit\r")
        deadline = time.monotonic() + 15
        while process.isalive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process.isalive(), "aibench did not exit after /exit"
    finally:
        if process.isalive():
            process.terminate(force=True)
        process.close(force=True)

    # the terminal's /report wrote the same stored-fact report the command renders
    [run_dir] = (project / ".aibench" / "reports").iterdir()
    stored = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    headless = CliRunner().invoke(
        app,
        ["report", run_dir.name, "--workspace", str(project), "--format", "json", "--out", "-"],
    )
    fresh = json.loads(headless.output)
    for document in (stored, fresh):
        document.pop("generated_at")
    assert stored == fresh
