"""E2E-01 (with E2E-02 and E2E-03 checkpoints): the conversational benchmark loop through
the real `aibench chat` in a real terminal (22-T3, 22-G3).

What is real here:
- the `aibench` process in a Windows pseudo-console;
- its OpenAI-compatible provider path (HTTP, tool calls, streaming);
- the policy, session store, run engine and report;
- the HTTP RAG application (`examples/acceptance/rag_service.py`), whose per-question
  call counter is the side-effect counter.

The assistant model is the deterministic `examples/e2e/scripted_assistant.py`. It proves
the harness side of the dialogue, not any model's judgement.

With `AIBENCH_E2E_INSTALLED=1` the terminal runs whatever `aibench` this interpreter has
installed (the clean-install suite, `scripts/e2e_suite.py`). Otherwise it runs `src/`.
With `AIBENCH_E2E_EVIDENCE=DIR` the journey writes its run ID, case IDs and side-effect
counts to `DIR/e2e-01.json`.
"""

from __future__ import annotations

import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
ACCEPTANCE = REPO / "examples" / "acceptance"

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or importlib.util.find_spec("winpty") is None,
    reason="the terminal journey needs Windows ConPTY and the optional pywinpty test dependency",
)

# Starting the CLI on a loaded development machine takes 8-15 s; each turn renders a plan.
STARTUP_SECONDS = 90.0
TURN_SECONDS = 60.0


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rag = _load(ACCEPTANCE / "rag_service.py", "e2e_rag_service")
scripted = _load(REPO / "examples" / "e2e" / "scripted_assistant.py", "e2e_scripted_assistant")
ROWS = [json.loads(line) for line in (ACCEPTANCE / "rag100.jsonl").read_text().splitlines()]


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    if os.environ.get("AIBENCH_E2E_INSTALLED") != "1":
        env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env.pop("OPENAI_API_KEY", None)  # nothing in this journey may reach a paid provider
    return env


def _unwrapped(text: str) -> str:
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07", "", text)
    return "".join(plain.split())


class Terminal:
    def __init__(self, argv: list[str], cwd: Path) -> None:
        from winpty import PtyProcess

        self.process = PtyProcess.spawn(
            [sys.executable, "-m", "aibench", *argv],
            cwd=str(cwd),
            env=_environment(),
            dimensions=(50, 220),
        )
        self.observed = ""
        self.mark = 0
        self._chunks: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                self._chunks.put(self.process.read(4096))
            except Exception:  # noqa: BLE001 - EOF or teardown closes the PTY handle
                return

    def _pump(self, timeout: float) -> None:
        try:
            self.observed += self._chunks.get(timeout=timeout)
        except queue.Empty:
            pass

    def since_mark(self) -> str:
        return _unwrapped(self.observed[self.mark :])

    def expect(self, fragment: str, timeout: float = TURN_SECONDS) -> None:
        """Wait until `fragment` appears after the last `send` (wrapping ignored)."""
        wanted = "".join(fragment.split())
        deadline = time.monotonic() + timeout
        while wanted not in self.since_mark() and time.monotonic() < deadline:
            self._pump(0.1)
            if not self.process.isalive() and self._chunks.empty():
                break
        assert wanted in self.since_mark(), (
            f"terminal did not show {fragment!r}; last output: {self.observed[-3000:]}"
        )

    def send(self, line: str) -> None:
        self.mark = len(self.observed)
        self.process.write(line + "\r")

    def exit(self) -> None:
        self.send("/exit")
        deadline = time.monotonic() + 30
        while self.process.isalive() and time.monotonic() < deadline:
            self._pump(0.05)
        assert not self.process.isalive(), "aibench did not exit after /exit"

    def close(self) -> None:
        if self.process.isalive():
            self.process.terminate(force=True)
        self.process.close(force=True)


def _aibench(*argv: str, cwd: Path) -> Any:
    completed = subprocess.run(
        [sys.executable, "-m", "aibench", *argv],
        cwd=cwd,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert completed.returncode in (0, 1), completed.stderr
    return json.loads(completed.stdout)


@pytest.fixture
def service() -> Iterator[Any]:
    server = rag.RagService().start()
    try:
        yield server
    finally:
        server.stop()


SCRIPT = [
    # turn 1: the goal in the user's words, then one material clarification
    {
        "call": "propose_plan_patch",
        "args": {
            "expected_revision": "$revision",
            "user_quote": "check that answers are correct",
            "patch": {"add_objectives": ["answers are correct"]},
        },
    },
    {"call": "get_evaluation_opportunities", "args": {}},
    {
        "call": "ask_user",
        "args": {
            "prompt": "Run all 100 cases, or start with a smaller pilot?",
            "required_fields": ["selection"],
            "choices": ["all 100", "a pilot"],
        },
    },
    {"say": "I added a correctness check. Run all 100 cases, or start with a pilot?"},
    # turn 2: the answer resolves scope and authorizes the bounded run
    {
        "call": "propose_plan_patch",
        "args": {
            "expected_revision": "$revision",
            "user_quote": "Start with the first 20 cases",
            "patch": {"limit": 20, "answers": ["$question_id"]},
        },
    },
    {"call": "get_evaluation_opportunities", "args": {}},
    {"call": "show_plan", "args": {}},
    {
        "call": "request_action",
        "args": {
            "action": "start_run",
            "user_quote": "Start with the first 20 cases",
            "expected_revision": "$revision",
        },
    },
    {"say": "Started the 20-case pilot."},
    # turn 3: a question while it runs
    {"call": "explain_metric", "args": {"metric": "native.exact_match"}},
    {"say": "Exact match compares each answer with the reviewed reference answer."},
    # turn 4: after it finished, discuss a failure with evidence
    {"call": "list_failures", "args": {}},
    {"call": "get_case_evidence", "args": {"case_id": "rag-003"}},
    {
        "say": "Hypothesis: retrieval in rag-003 may have followed the decoy word 'abroad'; "
        "the stored case evidence identifies the affected result but does not prove why."
    },
    # turn 5: rescore the stored run; never repeat application calls
    {"call": "rescore_run", "args": {"user_quote": "Please rescore this run."}},
    {"say": "Rescored the stored executions; the application was not invoked."},
]


def test_a_user_benchmarks_an_app_from_the_first_message_to_evidence_in_the_cli(
    tmp_path: Path, service: Any
) -> None:
    project = tmp_path / "project"
    shutil.copytree(ACCEPTANCE, project, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    for name in ("rag.app.json", "blackbox.app.json"):
        path = project / name
        path.write_text(
            path.read_text(encoding="utf-8").replace("http://127.0.0.1:8766", service.url),
            encoding="utf-8",
        )
    (project / "aibench.json").write_text(
        json.dumps({"application_target": "rag.app.json", "dataset_path": "rag100.jsonl"}),
        encoding="utf-8",
    )
    assistant = scripted.ScriptedAssistant(SCRIPT).start()
    (project / "provider.json").write_text(
        json.dumps({"kind": "openai_compatible", "base_url": assistant.url, "model": "scripted-1"}),
        encoding="utf-8",
    )
    service.delay = 0.5  # keep the pilot live long enough to talk to it and pause it
    evidence: dict[str, Any] = {"journey": "E2E-01", "checkpoints": {}}
    terminal = Terminal(["chat", "--new", "--provider-config", "provider.json"], project)
    try:
        terminal.expect("Type /help for commands", timeout=STARTUP_SECONDS)
        # 1. first message: a plan draft and one focused question
        terminal.send("Benchmark the support RAG app and check that answers are correct.")
        terminal.expect("Run all 100 cases, or start with a pilot?")
        # 2. the answer resolves scope and starts the requested bounded evaluation
        terminal.send("Start with the first 20 cases.")
        terminal.expect("Started the 20-case pilot.")
        # E2E-02: a question during the run is answered; the run is not touched
        terminal.send("What does exact match measure?")
        terminal.expect("Exact match compares each answer with the reviewed reference answer.")
        # E2E-02: /pause stops dispatch; in-flight work may finish, nothing new starts.
        # Pause once the engine is really dispatching (its start-up is not instant).
        deadline = time.monotonic() + 60
        while service.total_calls() == 0 and time.monotonic() < deadline:
            time.sleep(0.1)
        terminal.send("/pause")
        terminal.expect("paus")
        time.sleep(1.5)  # an in-flight 0.5 s request drains
        paused_at = service.total_calls()
        time.sleep(2.0)
        evidence["checkpoints"]["paused_calls"] = [paused_at, service.total_calls()]
        assert service.total_calls() == paused_at
        assert 0 < paused_at < 20, terminal.observed[-4000:]
        service.delay = 0.0
        terminal.send("/resume")
        terminal.expect("completed: executions 20/20", timeout=120)
        # 5. follow-up discussion grounded in stored evidence
        terminal.send("Show me the failures and explain one.")
        terminal.expect("Hypothesis: retrieval in rag-003")
        terminal.send("Please rescore this run.")
        terminal.expect("Rescored the stored executions; the application was not invoked.")
        assert service.total_calls() == 20
        # 6. the evidence-linked report, from the same session
        terminal.send("/report")
        terminal.expect("report for run")
        terminal.expect("wrote html")
        terminal.exit()
    finally:
        terminal.close()
        assistant.stop()

    # The script was followed exactly: no unscripted or missing assistant calls.
    assert assistant.remaining == 0
    assert len(assistant.requests) == len(SCRIPT)
    # The assistant never received a reference answer (Golden isolation, E2E-06).
    sent = json.dumps(assistant.requests)
    references = {r["expected_output"] for r in ROWS[:20] if r.get("expected_output")}
    assert references and not any(json.dumps(ref)[1:-1] in sent for ref in references)

    # Stored facts, read back through headless commands (the same services, E2E-07).
    [session] = _aibench("sessions", "list", "--json", cwd=project)
    [run] = _aibench("runs", "list", "--workspace", ".", "--json", cwd=project)
    run_id = run["run_id"]
    report = _aibench(
        "report", run_id, "--workspace", ".", "--format", "json", "--out", "-", cwd=project
    )
    failures = _aibench(
        "chat", "--resume", session["session_id"], "--send", "/failures", "--json", cwd=project
    )["data"]
    failed_cases = sorted({f["case_id"] for f in failures["metric_failures"]})
    injected = sorted(
        r["case_id"] for r in ROWS[:20] if r["metadata"]["injected"] == "retrieval_failure"
    )
    assert failed_cases == injected == ["rag-003", "rag-015"]
    first_20 = [r["case_id"] for r in ROWS[:20]]
    per_case = {q: service.calls[q] for q in (r["input"] for r in ROWS[:20])}
    assert all(count == 1 for count in per_case.values())  # each case exactly once
    assert service.total_calls() == 20

    # E2E-03: close and reopen the session; the real state comes back, nothing reruns
    terminal = Terminal(["chat", "--resume", session["session_id"]], project)
    try:
        terminal.expect("Type /help for commands", timeout=STARTUP_SECONDS)
        terminal.send("/status")
        terminal.expect("completed")
        time.sleep(1.0)
        terminal.exit()
    finally:
        terminal.close()
    assert service.total_calls() == 20

    evidence.update(
        {
            "session_id": session["session_id"],
            "run_id": run_id,
            "case_ids": first_20,
            "application_calls_total": service.total_calls(),
            "application_calls_per_case_max": max(per_case.values()),
            "assistant_requests": len(assistant.requests),
            "failed_case_ids": failed_cases,
            "report_work": report["work"],
            "report_metrics": [
                {k: m[k] for k in m if k in ("label", "selected", "completed", "decisions")}
                for p in report["scoring_passes"]
                for m in p["metrics"]
            ],
        }
    )
    target = os.environ.get("AIBENCH_E2E_EVIDENCE")
    if target:
        Path(target).mkdir(parents=True, exist_ok=True)
        (Path(target) / "e2e-01.json").write_text(json.dumps(evidence, indent=2), "utf-8")
