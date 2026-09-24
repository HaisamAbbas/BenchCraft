"""openai/evals open-source bridge (17-T1), end to end with the real pinned `evals` package
in its isolated plugin environment and a real CLI application.

Live mode: `aibench openai-evals-oss run` drives the upstream eval; its completion function
asks the harness, which invokes the application once per sample and records it, then the
recorded outputs are replay-scored. Recorded mode: `openai_evals_oss.*` metrics score an
ordinary run's stored outputs, answering only exact, single requests.

Skipped when the plugin environment is not installed (see plugins/openai_evals_oss/README.md).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples" / "openai_evals"
PLUGIN_ENV = Path(
    os.environ.get("AIBENCH_OPENAI_EVALS_OSS_PYTHON")
    or REPO
    / "plugins"
    / "openai_evals_oss"
    / ".venv"
    / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
)
pytestmark = pytest.mark.skipif(
    not PLUGIN_ENV.is_file(),
    reason=f"openai/evals plugin environment not installed at {PLUGIN_ENV}",
)
cli = CliRunner()


def _project(tmp_path: Path, **policy: Any) -> dict[str, Path]:
    log = tmp_path / "quiz.log"
    (tmp_path / "quiz.app.json").write_text(
        json.dumps(
            {
                "application_id": "quiz",
                "runner": "cli",
                "target": "quiz_app.py",
                "transport": {
                    "kind": "cli",
                    "argv": [sys.executable, str(EXAMPLES / "quiz_app.py")],
                    "timeout_seconds": 30,
                    "env": {"QUIZ_LOG": str(log)},
                },
                "output_binding": {"output": "/output"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "policy.json").write_text(
        json.dumps(
            {
                "allow_trusted_local": True,
                "allowed_evaluators": ["native.*", "openai_evals_oss.*"],
                "allowed_plugin_environments": [str(PLUGIN_ENV)],
                **policy,
            }
        ),
        encoding="utf-8",
    )
    return {"app": tmp_path / "quiz.app.json", "policy": tmp_path / "policy.json", "log": log}


def _calls(log: Path) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _live(tmp_path: Path, paths: dict[str, Path], *extra: str, code: int = 0) -> Any:
    result = cli.invoke(
        app,
        [
            "openai-evals-oss", "run", str(paths["app"]),
            "--samples", str(EXAMPLES / "samples.jsonl"),
            "--plugin-env", str(PLUGIN_ENV),
            "--policy", str(paths["policy"]),
            "--workspace", str(tmp_path),
            *extra,
        ],
    )  # fmt: skip
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if "--json" in extra and code == 0 else result


def test_the_live_bridge_runs_the_upstream_eval_through_the_application(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    report = _live(tmp_path, paths, "--eval", "match", "--json")
    assert (report["samples"], report["executed"], report["refused"]) == (4, 4, {})
    assert report["mode"] == "delegated_suite"
    # The upstream Match verdicts, replay-scored from what the application returned.
    assert report["decisions"] == {
        "arith": "pass", "capital": "pass", "planet": "fail", "water": "pass",
    }  # fmt: skip
    assert report["live_and_replay_disagree"] == []
    # Exactly one application call per sample, with exactly the eval's request.
    calls = _calls(paths["log"])
    samples = [json.loads(line) for line in (EXAMPLES / "samples.jsonl").read_text().splitlines()]
    assert sorted(c["case_id"] for c in calls) == sorted(s["id"] for s in samples)
    assert {c["case_id"]: c["input"] for c in calls} == {s["id"]: s["input"] for s in samples}

    # The run is recorded as a delegated suite with the plugin's identity.
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    storage = Storage(Database.open_workspace(Workspace.at(tmp_path)))
    try:
        record = storage.get_run(report["run_id"])
        events = [e["event_type"] for e in storage.list_run_events(report["run_id"])]
    finally:
        storage.db.close()
    assert record is not None and record.status == "completed"
    parameters = record.manifest.parameters
    assert parameters["mode"] == "delegated_suite"
    assert parameters["suite"]["plugin"] == "aibench-openai-evals-oss"
    assert parameters["suite"]["upstream"] == "evals==3.0.1.post1"
    assert events.count("delegated_sample") == 4 and "scoring_pass_completed" in events


def test_a_few_shot_eval_is_bridged_with_the_prompt_it_actually_sends(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    few_shot = [json.loads(line) for line in (EXAMPLES / "few_shot.jsonl").read_text().splitlines()]
    params = json.dumps({"num_few_shot": 1, "few_shot": few_shot})
    report = _live(tmp_path, paths, "--eval", "match", "--params", params, "--json")
    assert report["executed"] == 4 and report["refused"] == {}
    assert list(report["decisions"].values()).count("pass") == 3
    arith = next(c for c in _calls(paths["log"]) if c["case_id"] == "arith")
    # The application received the expanded prompt: system, the few-shot turns, the question.
    assert [m["content"] for m in arith["input"]] == [
        "Answer briefly.", "What is 1+1?", "2", "What is 2+2?",
    ]  # fmt: skip


def test_recorded_replay_scores_a_normal_run_and_refuses_dynamic_requests(tmp_path: Path) -> None:
    paths = _project(tmp_path)
    samples = [json.loads(line) for line in (EXAMPLES / "samples.jsonl").read_text().splitlines()]
    (tmp_path / "data.jsonl").write_text(
        "".join(
            json.dumps(
                {"case_id": s["id"], "input": s["input"], "reference": {"answer": s["ideal"]}}
            )
            + "\n"
            for s in samples
        ),
        encoding="utf-8",
    )
    few_shot = [json.loads(line) for line in (EXAMPLES / "few_shot.jsonl").read_text().splitlines()]
    (tmp_path / "plan.json").write_text(
        json.dumps(
            {
                "plan_id": "quiz-replay",
                "dataset": "data.jsonl",
                "application": "quiz.app.json",
                "metrics": [
                    {"metric": "openai_evals_oss.match"},
                    {"metric": "openai_evals_oss.includes", "params": {"ignore_case": True}},
                    # The few-shot expansion changes the request: replay must refuse it.
                    {"metric": "openai_evals_oss.match",
                     "params": {"num_few_shot": 1, "few_shot": few_shot}},
                ],
                "plugin_environments": [{"python": str(PLUGIN_ENV)}],
            }
        ),
        encoding="utf-8",
    )  # fmt: skip
    run = cli.invoke(
        app,
        ["run", "--plan", str(tmp_path / "plan.json"), "--policy", str(paths["policy"]),
         "--workspace", str(tmp_path), "--json"],
    )  # fmt: skip
    assert run.exit_code == 3, run.output  # the refused evaluations are unhealthy work
    run_id = json.loads(run.stdout)["run_id"]
    assert len(_calls(paths["log"])) == 4  # the application ran once per case

    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    storage = Storage(Database.open_workspace(Workspace.at(tmp_path)))
    try:
        results = storage.list_metric_results(run_id)
    finally:
        storage.db.close()
    by_binding: dict[str, dict[str, Any]] = {}
    for r in results:
        params = r.provenance["binding"].get("params") or {}
        key = f"{r.metric_id}{'+fewshot' if 'few_shot' in params else ''}"
        by_binding.setdefault(key, {})[r.case_id] = r
    match = by_binding["openai_evals_oss.match"]
    assert {c: r.decision.value for c, r in match.items()} == {
        "arith": "pass", "capital": "pass", "planet": "fail", "water": "pass",
    }  # fmt: skip
    assert all(r.status.value == "ok" for r in by_binding["openai_evals_oss.includes"].values())
    refused = by_binding["openai_evals_oss.match+fewshot"]
    assert {r.status.value for r in refused.values()} == {"error"}
    assert all("unsupported_dynamic_request" in (r.reason or "") for r in refused.values())


def test_a_follow_up_request_is_refused_by_replay(tmp_path: Path) -> None:
    """One recorded answer cannot stand in for a second request (checked in the plugin
    environment against the adapter's replay completion function)."""
    code = (
        "from aibench_openai_evals_oss import upstream\n"
        "from aibench_openai_evals_oss.replay import replay_answer\n"
        "answer = replay_answer('Q?', 'A')\n"
        "print(answer('Q?', 0))\n"
        "try:\n    answer('Q?', 1)\nexcept upstream.RequestRefused as exc:\n    print(exc.reason)\n"
        "try:\n    answer('other', 0)\nexcept upstream.RequestRefused as exc:\n    print(exc.reason)\n"
    )
    out = subprocess.run(
        [str(PLUGIN_ENV), "-c", code], capture_output=True, text=True, timeout=120, check=True
    ).stdout.split()
    assert out == ["A", "unsupported_follow_up", "unsupported_dynamic_request"]


def test_unsupported_evals_and_unapproved_plugins_never_reach_the_application(
    tmp_path: Path,
) -> None:
    paths = _project(tmp_path)
    result = _live(tmp_path, paths, "--eval", "modelgraded", code=2)
    assert "openai_evals_oss.modelgraded" in result.output
    denied = _project(tmp_path, allowed_evaluators=["native.*"])
    result = _live(tmp_path, denied, "--eval", "match", code=4)
    assert "openai_evals_oss.match" in result.output
    assert _calls(paths["log"]) == []


def test_the_oss_plugin_imports_upstream_only_in_its_own_environment() -> None:
    import aibench.services.delegated  # noqa: F401 - the core side of the bridge

    assert "evals" not in sys.modules


def test_bridge_startup_failure_reaps_process_and_private_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aibench.services import delegated

    class FakeProcess:
        pid = 12346
        returncode: int | None = None
        waited = False
        killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.waited = True
            self.returncode = -9
            return self.returncode

    class FakeTree:
        contained = True

        def __init__(self, _: int) -> None:
            self.killed = False
            self.closed = False

        def kill(self) -> None:
            self.killed = True

        def close(self) -> None:
            self.closed = True

    process = FakeProcess()
    trees: list[FakeTree] = []

    async def spawn(*_: Any, **__: Any) -> FakeProcess:
        return process

    def make_tree(pid: int) -> FakeTree:
        tree = FakeTree(pid)
        trees.append(tree)
        return tree

    async def fail_startup(*_: Any, **__: Any) -> dict[str, Any]:
        raise TimeoutError("bridge handshake timed out")

    async def drain_nothing() -> None:
        return None

    monkeypatch.setattr(delegated.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(delegated, "ProcessTree", make_tree)
    bridge = delegated._Bridge(Path(sys.executable))
    workdir = bridge.workdir
    monkeypatch.setattr(bridge, "call", fail_startup)
    monkeypatch.setattr(bridge, "_drain_stderr", drain_nothing)

    with pytest.raises(TimeoutError, match="handshake timed out"):
        asyncio.run(bridge.__aenter__())

    assert len(trees) == 1 and trees[0].killed and trees[0].closed
    assert process.waited
    assert bridge._drain is None
    assert not workdir.exists()


def test_an_application_that_would_not_receive_the_whole_request_is_refused(
    tmp_path: Path,
) -> None:
    """An input binding that selects part of the input would answer a different question
    than the eval asked, so no live answer could count as exact (review finding)."""
    paths = _project(tmp_path)
    config = json.loads(paths["app"].read_text(encoding="utf-8"))
    config["input_binding"] = {"fields": {"/input": "/input/1/content"}}
    paths["app"].write_text(json.dumps(config), encoding="utf-8")
    result = _live(tmp_path, paths, "--eval", "match", code=2)
    assert "does not deliver the whole input unchanged" in " ".join(result.output.split())
    assert _calls(paths["log"]) == []
