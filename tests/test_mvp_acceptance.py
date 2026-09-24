"""MVP acceptance (§17, 12-T1) on the 100-case fixture in `examples/acceptance/`.

§17: "The underlying workflow runs 100 fixture cases, identifies an injected RAG failure,
observes a missing-evidence warning on a black-box endpoint, resumes an interrupted safe
run, and rescores stored outputs without invoking the app again", and "a fresh user opens
aibench, explains a benchmark goal in natural language, answers a material clarification,
revises the draft, and starts a run. The user asks a question during execution,
pauses/resumes through slash controls, closes and restores the session without duplicate
execution, and discusses a failed case with evidence."

The application is a real HTTP service (`examples/acceptance/rag_service.py`) running in
this test process, so its per-question call counter survives an engine process being
killed. The engine runs as a real child process for the interruption. The conversation
uses the deterministic scripted provider: it proves the harness side of the dialogue, not
any live model's behaviour. (Hand-written vs generated plan equivalence, the last §17
clause, is `tests/test_plan_equivalence.py`.)
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.conversation.agent import ConversationAgent
from aibench.sessions.controller import SessionController
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from aibench.tui.commands import Commands
from tests.planning_support import GROUNDED, planning_inputs, registry_with
from tests.session_support import ScriptedProvider, call, patch_step, say, start_step

ACCEPTANCE = Path(__file__).resolve().parents[1] / "examples" / "acceptance"
cli = CliRunner()


def _load_service() -> Any:
    spec = importlib.util.spec_from_file_location("rag_service", ACCEPTANCE / "rag_service.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rag = _load_service()
ROWS = [json.loads(line) for line in (ACCEPTANCE / "rag100.jsonl").read_text().splitlines()]
INJECTED = {r["case_id"] for r in ROWS if r["metadata"]["injected"] == "retrieval_failure"}


@pytest.fixture
def service() -> Iterator[Any]:
    server = rag.RagService().start()
    try:
        yield server
    finally:
        server.stop()


def _project(tmp_path: Path, url: str) -> Path:
    """The acceptance fixture, pointed at this test's service port."""
    root = tmp_path / "acceptance"
    shutil.copytree(ACCEPTANCE, root, ignore=shutil.ignore_patterns("__pycache__", "*.py"))
    for name in ("rag.app.json", "blackbox.app.json"):
        path = root / name
        text = path.read_text(encoding="utf-8").replace("http://127.0.0.1:8766", url)
        path.write_text(text, encoding="utf-8")
    return root


def _json(output: str) -> dict[str, Any]:
    return json.loads(output)


def _report(root: Path, run_id: str) -> dict[str, Any]:
    result = cli.invoke(
        app, ["report", run_id, "--workspace", str(root), "--format", "json", "--out", "-"]
    )
    assert result.exit_code == 0, result.output
    return _json(result.stdout)


# --------------------------------------------------------------------------- the workflow


def test_the_100_case_workflow_survives_a_kill_finds_the_injected_failures_and_rescores_offline(
    tmp_path: Path, service: Any
) -> None:
    root = _project(tmp_path, service.url)
    service.delay = 0.15  # long enough to kill the engine mid-run

    # 1. Run in a real child process and kill it while the application is being called.
    child = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "aibench",
            "run",
            "--plan",
            str(root / "plan.json"),
            "--workspace",
            str(root),
            "--json",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    deadline = time.monotonic() + 120
    while service.total_calls() < 30 and time.monotonic() < deadline:
        assert child.poll() is None, "the run ended before it could be interrupted"
        time.sleep(0.02)
    child.kill()
    child.wait(timeout=30)
    interrupted_at = service.total_calls()
    assert 30 <= interrupted_at < 100

    [run] = _json(cli.invoke(app, ["runs", "list", "--workspace", str(root), "--json"]).stdout)
    run_id = run["run_id"]
    status = _json(
        cli.invoke(app, ["runs", "status", run_id, "--workspace", str(root), "--json"]).stdout
    )
    assert status["status"] == "running"  # what a killed process leaves behind
    time.sleep(1.0)
    assert service.total_calls() == interrupted_at  # nothing continued on its own

    # 2. Resume: the rest runs; nothing already committed runs again.
    service.delay = 0.0
    resumed = cli.invoke(app, ["resume", run_id, "--workspace", str(root), "--json"])
    outcome = _json(resumed.stdout)
    assert outcome["state"] == "completed"
    assert outcome["counts"]["execution"] == {"succeeded": 100}
    assert outcome["counts"]["evaluation"] == {"succeeded": 100}
    # Effect-free work in flight at the kill may be dispatched once more (ADR 0005);
    # at most the plan's application concurrency (4) such calls.
    assert 100 <= service.total_calls() <= 104
    assert set(service.calls) == {r["input"] for r in ROWS}
    assert max(service.calls.values()) <= 2
    # Complete, but the release gate fails on exactly the injected failures: exit code 1.
    assert resumed.exit_code == 1 == outcome["exit_code"], resumed.output
    assert outcome["outcome"]["gates_failed"] == ["correct-answers"]

    # 3. The report finds the injected RAG failures, with the retrieval that explains them.
    report = _report(root, run_id)
    [metric] = report["scoring_passes"][0]["metrics"]
    summary = metric["summary"]
    assert summary["selected"] == summary["completed"] == 100
    assert summary["decisions"]["pass"] == 92 and summary["decisions"]["fail"] == 8
    items = report["evidence"]["items"]
    assert {i["case_id"] for i in items} == INJECTED
    for item in items:
        topic = next(r["metadata"]["topic"] for r in ROWS if r["case_id"] == item["case_id"])
        retrieved = item["execution"]["retrieved_context_excerpts"]
        assert rag.CORPUS[topic] not in retrieved[:1]  # the right passage was not used
    assert report["cost"]["application"]["accounting"] == "unknown"  # the app reports none
    # Every call the application received is accounted for, including the in-flight calls
    # lost with the killed process (no attempt was recorded for them). Recovery counts work
    # that was marked running, which can be just before its request went out, so the report
    # is an upper bound: never fewer calls than the application saw.
    assert report["application"]["attempts"] == 100
    uncommitted = report["application"]["uncommitted_dispatches"]
    assert service.total_calls() - 100 <= uncommitted <= 4  # at most the concurrency
    assert report["cost"]["application"]["calls"] == 100 + uncommitted >= service.total_calls()
    assert report["run"]["approved_by"] == "policy"

    # 4. Rescore the stored outputs with another binding: the application is not called.
    calls_before = service.total_calls()
    rescored = cli.invoke(
        app,
        [
            "evaluate",
            run_id,
            "--plan",
            str(root / "rescore.plan.json"),
            "--workspace",
            str(root),
            "--json",
        ],
    )
    assert rescored.exit_code == 0, rescored.output
    assert service.total_calls() == calls_before
    engine, rescore = _report(root, run_id)["scoring_passes"]
    [rescored_metric] = rescore["metrics"]
    assert rescored_metric["profile"]["params"] == {
        "case_sensitive": False,
        "collapse_whitespace": True,
    }
    assert rescored_metric["summary"]["selected"] == 100
    assert rescored_metric["summary"]["decisions"]["pass"] == 92
    assert engine["metrics"][0]["summary"] == summary  # the engine pass is untouched


def test_a_black_box_endpoint_yields_a_missing_evidence_gap_not_a_score(
    tmp_path: Path, service: Any
) -> None:
    root = _project(tmp_path, service.url)
    described = cli.invoke(app, ["app", "describe", str(root / "blackbox.app.json"), "--json"])
    assert described.exit_code == 0, described.output
    assert _json(described.stdout)["observable"]["retrieved_context"] == "unknown"

    # With a groundedness judge in the catalog, the black-box app gets a gap; the same
    # objective on the instrumented app gets the metric (the control).
    registry = registry_with(GROUNDED)
    from aibench.planning.planner import plan_with_template
    from aibench.security.policy import ExecutionPolicy

    policy = ExecutionPolicy(
        allowed_evaluators=("native.*", "fixture.*"), allow_model_evaluators=True
    )
    results = {}
    for name in ("blackbox.app.json", "rag.app.json"):
        inputs = planning_inputs(
            root,
            root / name,
            root / "rag100.jsonl",
            ["unsupported claims"],
            policy=policy,
            registry=registry,
        )
        results[name] = plan_with_template(inputs).proposal
    blackbox, instrumented = results["blackbox.app.json"], results["rag.app.json"]
    assert not blackbox.metrics
    [gap] = blackbox.gaps
    assert "retrieved_context" in gap.reason or "retriev" in gap.reason.lower()
    assert [m.metric.split("@")[0] for m in instrumented.metrics] == ["fixture.grounded"]
    assert service.total_calls() == 0  # planning never calls the application


# --------------------------------------------------------------------------- the conversation


def test_a_fresh_user_completes_the_conversational_acceptance_journey(
    tmp_path: Path, service: Any
) -> None:
    root = _project(tmp_path, service.url)
    workspace = Workspace.at(root)
    workspace.ensure_directories()

    def open_controller(session_id: str | None = None) -> SessionController:
        storage = Storage(Database.open_workspace(workspace))
        artifacts = ArtifactStore(workspace.artifacts_dir)
        if session_id is not None:
            return SessionController(
                session_id, storage=storage, artifacts=artifacts, workspace_root=workspace.root
            )
        return SessionController.create(
            storage=storage,
            artifacts=artifacts,
            workspace_root=workspace.root,
            project_root=root,
            application=root / "rag.app.json",
            dataset=root / "rag100.jsonl",
        )

    ctl = open_controller()
    provider = ScriptedProvider(
        [
            # 1. the goal, in the user's words; one material clarification
            patch_step("check that answers are correct", add_objectives=["answers are correct"]),
            call(
                "ask_user",
                prompt="Run all 100 cases, or start with a smaller pilot?",
                required_fields=["selection"],
                choices=["all 100", "a pilot"],
            ),
            say("I added a correctness check. Run all 100 cases, or start with a pilot?"),
        ]
    )
    agent = ConversationAgent(ctl, provider)

    async def journey() -> dict[str, Any]:
        seen: dict[str, Any] = {}
        seen["goal"] = await agent.handle_message(
            "Benchmark the support RAG app and check that answers are correct."
        )
        [question] = ctl.store.questions(ctl.session_id, status="open")
        # 2. the answer revises the draft (a 20-case pilot), and the revision is shown
        provider.add(
            patch_step("Start with the first 20 cases", limit=20, answers=[question.question_id]),
            call("show_plan"),
            say("Updated the draft to the first 20 cases. Run it?"),
        )
        seen["revision"] = await agent.handle_message("Start with the first 20 cases.")
        # 3. run it; the application is slow, so the run is live for a while
        service.delay = 0.25
        provider.add(start_step("Run it"), say("Started the pilot."))
        seen["start"] = await agent.handle_message("Run it.")
        run_id = seen["start"].actions[0]["run_id"]
        # 4. a question during execution: explained, and the run is untouched
        await asyncio.sleep(0.5)
        provider.add(
            call("explain_metric", metric="native.exact_match"),
            say("Exact match compares the answer with the reviewed reference answer."),
        )
        before = ctl.run_condition(run_id)["condition"]
        seen["question"] = await agent.handle_message("What does exact match measure?")
        seen["during"] = (before, ctl.run_condition(run_id)["condition"])
        # 5. pause and resume through slash controls
        commands = Commands(ctl)
        seen["pause"] = await commands.run("/pause")
        await asyncio.sleep(0.6)
        paused_calls = service.total_calls()
        await asyncio.sleep(0.6)
        seen["paused_calls"] = (paused_calls, service.total_calls())
        seen["paused_status"] = (await commands.run("/status")).data
        service.delay = 0.0
        seen["resume"] = await commands.run("/resume")
        finished = await ctl.wait_for_run(run_id)
        seen["finished"] = finished.state.value if finished else None
        seen["run_id"] = run_id
        await ctl.close()
        return seen

    seen = asyncio.run(journey())
    run_id = seen["run_id"]
    try:
        assert seen["goal"].decisions and seen["goal"].questions
        assert seen["revision"].decisions and seen["revision"].presented_draft is not None
        assert seen["revision"].presented_draft["revision"] == 3
        assert seen["start"].actions[0]["state"] == "done"
        assert seen["question"].actions == [] and seen["question"].explained
        assert seen["during"] == ("running_here", "running_here")
        assert seen["pause"].data["state"] == "done"
        # paused: in-flight work finishes, nothing new is dispatched
        assert seen["paused_calls"][0] == seen["paused_calls"][1]
        assert seen["paused_status"]["status"] in ("paused", "pausing")
        assert seen["resume"].data["state"] == "done"
        assert seen["finished"] == "completed"
        assert service.total_calls() == 20  # the pilot: 20 cases, each exactly once
    finally:
        ctl.storage.db.close()

    # 6. close and restore: the session comes back with the real state; nothing restarts
    reopened = open_controller(seen_id := ctl.session_id)
    try:
        reconciled = reopened.reconcile()
        [condition] = [r for r in reconciled["runs"] if r["run_id"] == run_id]
        assert condition["condition"] == "completed"
        time.sleep(0.5)
        assert service.total_calls() == 20
        # 7. discuss a failed case with evidence (the first 20 hold two injected failures)
        provider2 = ScriptedProvider(
            [
                call("list_failures"),
                call("get_case_evidence", case_id="rag-003"),
                say(
                    "2 of 20 cases failed. Hypothesis, based on rag-003: the decoy word "
                    "'abroad' pulled retrieval to the shipping passage."
                ),
            ]
        )
        discussed = asyncio.run(
            ConversationAgent(reopened, provider2).handle_message(
                "Show me the failures and explain one."
            )
        )
        failures = reopened.failures(run_id)
        assert {f["case_id"] for f in failures["metric_failures"]} == {"rag-003", "rag-015"}
        assert {r["tool"] for r in discussed.results} == {"list_failures", "get_case_evidence"}
        assert discussed.unverified_numbers == []  # "2" and "20" trace to list_failures
        assert discussed.actions == []
        assert reopened.session_id == seen_id
    finally:
        reopened.storage.db.close()


# --------------------------------------------------------------------------- workload


@pytest.mark.skipif(
    os.environ.get("AIBENCH_WORKLOAD_TESTS") != "1",
    reason="about 4 minutes; set AIBENCH_WORKLOAD_TESTS=1 to run the 1,000-case workload",
)
def test_a_larger_cheap_workload_stays_bounded_and_resumes(tmp_path: Path, service: Any) -> None:
    """§23 "a larger cheap/mock workload for bounded-memory and resume behavior": 1,000
    cases over loopback HTTP, interrupted part-way and resumed. Records the traced peak of
    Python allocations; this is one workload on one machine, not a scalability claim."""
    import tracemalloc

    from aibench.engine.compile import compile_plan
    from aibench.engine.engine import RunController
    from aibench.security.policy import ExecutionPolicy
    from aibench.services.runs import create_run, execute_run

    root = _project(tmp_path, service.url)
    # 1,000 distinct questions; the numeric suffix does not change retrieval
    rows = [
        {**ROWS[i % 100], "case_id": f"load-{i:04d}", "input": f"{ROWS[i % 100]['input']} {i}"}
        for i in range(1000)
    ]
    (root / "load.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    plan = json.loads((root / "plan.json").read_text())
    plan.update(
        plan_id="load-1000",
        dataset="load.jsonl",
        concurrency={"application": 16, "evaluation": 16},
        budgets={"max_application_calls": 1100, "max_evaluator_calls": 1100},
        gates=[],
    )
    (root / "load.plan.json").write_text(json.dumps(plan))
    workspace = Workspace.at(root)
    workspace.ensure_directories()
    compiled = compile_plan(root / "load.plan.json", policy=ExecutionPolicy())
    storage = Storage(Database.open_workspace(workspace))
    artifacts = ArtifactStore(workspace.artifacts_dir)
    try:
        run_id = create_run(compiled, storage=storage, artifacts=artifacts, granted_by="test")

        async def interrupted() -> Any:
            ctl = RunController()
            task = asyncio.ensure_future(
                execute_run(run_id, storage=storage, artifacts=artifacts, controller=ctl)
            )
            while service.total_calls() < 400:
                await asyncio.sleep(0.01)
            ctl.request("interrupt")
            return await task

        tracemalloc.start()
        started = time.monotonic()
        first = asyncio.run(interrupted())
        second = asyncio.run(execute_run(run_id, storage=storage, artifacts=artifacts))
        elapsed = time.monotonic() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    finally:
        storage.db.close()
    assert first.state.value == "interrupted"
    assert second.state.value == "completed"
    assert second.counts["execution"] == {"succeeded": 1000}
    assert second.counts["evaluation"] == {"succeeded": 1000}
    assert len(service.calls) == 1000 and max(service.calls.values()) <= 2
    # bounded: the whole workload's traced peak stays well under what holding every
    # request, response and result in memory at once would take
    assert peak < 150 * 1024 * 1024, f"peak traced allocations {peak / 2**20:.1f} MiB"
    print(f"\n1,000-case workload: {elapsed:.1f}s, peak traced {peak / 2**20:.1f} MiB")
