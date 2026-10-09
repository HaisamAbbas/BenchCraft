"""Helpers for engine tests: a real instrumented CLI application, plans and policies on disk,
and a workspace-backed run harness. The app appends one JSON line per invocation to a log
(case, attempt-within-case, start/end times), so tests can count real invocations and
measure in-flight concurrency."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from aibench.engine.compile import compile_plan
from aibench.engine.engine import RunController, RunOutcome
from aibench.security.policy import ExecutionPolicy
from aibench.services.runs import create_run, execute_run
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

APP = r"""
import json, os, sys, time, pathlib
log = pathlib.Path(os.environ["APP_LOG"])
attempts = log.with_suffix(".attempts")
request = json.load(sys.stdin)
case, text = request["case_id"], request["input"]
# Count earlier *started* attempts: a timed-out attempt is killed before it logs completion.
previous = attempts.read_text().split().count(case) if attempts.exists() else 0
with attempts.open("a") as f:
    f.write(case + " ")
start = time.time()
if text.startswith("slow"):
    time.sleep(float(text.split()[1]))
if text.startswith("flaky") and previous < int(text.split()[1]):
    time.sleep(30)  # time out on the first N attempts
if text.startswith("crash"):
    with log.open("a") as f:
        f.write(json.dumps({"case": case, "start": start, "end": time.time()}) + "\n")
    sys.exit(3)
with log.open("a") as f:
    f.write(json.dumps({"case": case, "start": start, "end": time.time()}) + "\n")
print(json.dumps({"output": "yes"}))
"""


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.log = tmp_path / "invocations.jsonl"
        (tmp_path / "app.py").write_text(APP, encoding="utf-8")
        self.workspace = Workspace.at(tmp_path / "project")
        self.workspace.ensure_directories()

    # ------------------------------------------------------------------ files

    def dataset(self, inputs: dict[str, str], name: str = "data.jsonl") -> str:
        lines = [
            json.dumps({"case_id": c, "input": t, "expected_output": "yes"})
            for c, t in inputs.items()
        ]
        (self.root / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return name

    def cli_app(self, timeout: float = 20.0, effects: str = "none") -> str:
        config = {
            "application_id": "instrumented",
            "runner": "cli",
            "target": "app.py",
            "effects": effects,
            "transport": {
                "kind": "cli",
                "argv": [sys.executable, "app.py"],
                "timeout_seconds": timeout,
                "env": {"APP_LOG": str(self.log)},
            },
        }
        (self.root / "app.json").write_text(json.dumps(config), encoding="utf-8")
        return "app.json"

    def plan(self, *, dataset: str, application: str, **fields: Any) -> Path:
        plan = {
            "plan_id": "test-plan",
            "dataset": dataset,
            "application": application,
            "metrics": [{"metric": "native.exact_match"}],
            **fields,
        }
        path = self.root / "plan.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    # ------------------------------------------------------------------ running

    def storage(self) -> tuple[Storage, ArtifactStore]:
        return Storage(Database.open_workspace(self.workspace)), ArtifactStore(
            self.workspace.artifacts_dir
        )

    def create(
        self,
        plan: Path,
        policy: ExecutionPolicy | None = None,
        trusted: bool = True,
        environ: dict[str, str] | None = None,
    ) -> str:
        compiled = compile_plan(plan, policy=policy or ExecutionPolicy(), trusted_local=trusted)
        storage, artifacts = self.storage()
        try:
            return create_run(
                compiled,
                storage=storage,
                artifacts=artifacts,
                granted_by="test",
                environ=environ,
            )
        finally:
            storage.db.close()

    def execute(
        self,
        run_id: str,
        controller: RunController | None = None,
        during: Any = None,
        environ: dict[str, str] | None = None,
    ) -> RunOutcome:
        """Run or resume `run_id` on a fresh connection (as a new process would)."""
        storage, artifacts = self.storage()

        async def go() -> RunOutcome:
            ctl = controller or RunController()
            task = asyncio.ensure_future(
                execute_run(
                    run_id,
                    storage=storage,
                    artifacts=artifacts,
                    controller=ctl,
                    environ=environ,
                )
            )
            if during is not None:
                await during(ctl, self)
            return await task

        try:
            return asyncio.run(go())
        finally:
            storage.db.close()

    def invocations(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        # Concurrent app processes append to one log; an interleaved append can leave a
        # blank line. A partial JSON line would still fail loudly.
        return [json.loads(line) for line in self.log.read_text().splitlines() if line.strip()]

    def count(self, case: str | None = None) -> int:
        return sum(1 for i in self.invocations() if case is None or i["case"] == case)

    async def wait_for_invocations(self, n: int, timeout: float = 30.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self.count() < n:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(f"only {self.count()} invocations after {timeout}s")
            await asyncio.sleep(0.02)


def max_overlap(invocations: list[dict[str, Any]]) -> int:
    events = sorted([(i["start"], 1) for i in invocations] + [(i["end"], -1) for i in invocations])
    current = best = 0
    for _, delta in events:
        current += delta
        best = max(best, current)
    return best
