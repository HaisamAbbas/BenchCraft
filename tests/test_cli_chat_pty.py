"""09-T1: a real Windows ConPTY smoke of bare `aibench` in a terminal."""

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

from tests.planning_support import write_app, write_dataset

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("winpty") is None,
    reason="real ConPTY smoke requires Windows and the optional pywinpty test dependency",
)


def test_bare_aibench_opens_chat_and_accepts_terminal_controls(tmp_path: Path) -> None:
    from winpty import PtyProcess

    project = tmp_path / "project"
    project.mkdir()
    application = write_app(project)
    dataset = write_dataset(project, [{"case_id": "one", "input": "hello"}])
    (project / "aibench.json").write_text(
        json.dumps(
            {
                "application_target": str(application),
                "dataset_path": str(dataset),
            }
        ),
        encoding="utf-8",
    )
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
    process = PtyProcess.spawn(
        [sys.executable, "-m", "aibench"],
        cwd=str(project),
        env=env,
        dimensions=(40, 120),
    )
    chunks: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        while True:
            try:
                chunks.put(process.read(2048))
            except EOFError:
                return
            except Exception:  # noqa: BLE001 - process teardown closes the PTY handle
                return

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    observed = ""

    def expect(fragment: str, timeout: float = 12.0) -> None:
        nonlocal observed
        deadline = time.monotonic() + timeout
        while fragment not in observed and time.monotonic() < deadline:
            try:
                observed += chunks.get(timeout=0.1)
            except queue.Empty:
                if not process.isalive():
                    break
        assert fragment in observed, f"terminal did not display {fragment!r}: {observed[-2000:]}"

    try:
        expect("Type /help for commands")
        process.write("/help\r")
        expect("Anything else is a message")
        process.write("/exit\r")
        deadline = time.monotonic() + 12
        while process.isalive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not process.isalive(), "aibench did not exit after /exit"
    finally:
        if process.isalive():
            process.terminate(force=True)
        process.close(force=True)
