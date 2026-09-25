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


# Starting a fresh interpreter, opening the workspace and drafting the first plan took
# 13.6 s on the development machine at 100% CPU (other agents' work), past a 12 s wait.
STARTUP_SECONDS = 45.0


class _Terminal:
    """Bare `aibench` in a real pseudo-console, read on a background thread."""

    def __init__(self, project: Path, dimensions: tuple[int, int] = (40, 120)) -> None:
        from winpty import PtyProcess

        source_root = Path(__file__).resolve().parents[1] / "src"
        env = os.environ.copy()
        env["PYTHONPATH"] = str(source_root) + os.pathsep + env.get("PYTHONPATH", "")
        self.process = PtyProcess.spawn(
            [sys.executable, "-m", "aibench"], cwd=str(project), env=env, dimensions=dimensions
        )
        self.observed = ""
        self._chunks: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                self._chunks.put(self.process.read(2048))
            except EOFError:
                return
            except Exception:  # noqa: BLE001 - process teardown closes the PTY handle
                return

    def send(self, line: str) -> None:
        self.process.write(line + "\r")

    def expect(self, fragment: str, timeout: float = 12.0) -> None:
        deadline = time.monotonic() + timeout
        while not self.shows(fragment) and time.monotonic() < deadline:
            try:
                self.observed += self._chunks.get(timeout=0.1)
            except queue.Empty:
                if not self.process.isalive():
                    break
        assert self.shows(fragment), (
            f"terminal did not display {fragment!r}: {self.observed[-2000:]}"
        )

    def shows(self, fragment: str) -> bool:
        """Whether `fragment` was displayed, even if the terminal wrapped it (a word wrap
        drops the space at the break, so whitespace is ignored)."""
        return fragment in self.observed or "".join(fragment.split()) in _unwrapped(self.observed)

    def exit(self) -> None:
        self.send("/exit")
        deadline = time.monotonic() + 12
        while self.process.isalive() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not self.process.isalive(), "aibench did not exit after /exit"

    def close(self) -> None:
        if self.process.isalive():
            self.process.terminate(force=True)
        self.process.close(force=True)


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    application = write_app(project)
    dataset = write_dataset(project, [{"case_id": "one", "input": "hello"}])
    (project / "aibench.json").write_text(
        json.dumps({"application_target": str(application), "dataset_path": str(dataset)}),
        encoding="utf-8",
    )
    return project


def test_bare_aibench_opens_chat_and_accepts_terminal_controls(tmp_path: Path) -> None:
    terminal = _Terminal(_project(tmp_path))
    try:
        terminal.expect("Type /help for commands", timeout=STARTUP_SECONDS)
        terminal.send("/help")
        terminal.expect("Anything else is a message")
        terminal.exit()
    finally:
        terminal.close()


def test_themes_switch_is_saved_and_shown_on_the_next_launch(tmp_path: Path) -> None:
    project = _project(tmp_path)
    terminal = _Terminal(project)
    try:
        terminal.expect("Type /help for commands", timeout=STARTUP_SECONDS)
        assert terminal.shows("theme crimson")  # red by default
        assert terminal.shows("BenchCraft v")
        terminal.send("/themes ocean")
        terminal.expect("theme ocean")
        terminal.exit()
    finally:
        terminal.close()
    saved = json.loads((project / ".aibench" / "ui.json").read_text(encoding="utf-8"))
    assert saved == {"theme": "ocean"}

    reopened = _Terminal(project)
    try:
        reopened.expect("Resume which session?", timeout=STARTUP_SECONDS)
        reopened.send("1")
        reopened.expect("Type /help for commands", timeout=STARTUP_SECONDS)
        assert reopened.shows("theme ocean")
        reopened.exit()
    finally:
        reopened.close()


def test_resizing_the_terminal_keeps_the_chat_working(tmp_path: Path) -> None:
    """§23 terminal resizing (13-T1): shrink and grow the console while the chat is open.
    Input keeps working, a line longer than the narrow width is read whole, and nothing
    crashes."""
    terminal = _Terminal(_project(tmp_path))
    try:
        terminal.expect("Type /help for commands", timeout=STARTUP_SECONDS)
        terminal.process.setwinsize(12, 40)
        assert terminal.process.getwinsize() == (12, 40)
        terminal.send("/resized" + "x" * 70)  # wraps at 40 columns
        terminal.expect("type /help")  # the unknown-command reply
        assert terminal.shows("unknown command /resized" + "x" * 70)  # read whole
        terminal.process.setwinsize(50, 200)
        terminal.send("/help")
        terminal.expect("Anything else is a message")
        terminal.process.setwinsize(8, 20)  # too small for the toolbar
        terminal.send("/status")  # still accepted and answered
        terminal.expect("this session has not started a run")
        assert "Traceback" not in terminal.observed
        terminal.exit()
    finally:
        terminal.close()


def _unwrapped(text: str) -> str:
    """Terminal output without escape sequences or any whitespace."""
    import re

    text = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07", "", text)
    return "".join(text.split())
