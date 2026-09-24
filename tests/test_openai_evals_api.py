"""Hosted OpenAI Evals API bridge (17-T2, 17-G1..G3), end to end: a real recorded run of a
CLI application, the real pinned `openai` SDK in the plugin's isolated environment, and a
local stand-in for the service (`examples/openai_evals/evals_api_stub.py`) that records
every request and injects failures.

The stand-in proves the harness and the pinned SDK agree on the request and response
contract, and exercises failure handling. It is not evidence about the live service; no
live call is made (none is authorized).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples" / "openai_evals"
_BIN = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
API_ENV = Path(
    os.environ.get("AIBENCH_OPENAI_EVALS_API_PYTHON")
    or REPO / "plugins" / "openai_evals_api" / ".venv" / _BIN
)
OSS_ENV = Path(
    os.environ.get("AIBENCH_OPENAI_EVALS_OSS_PYTHON")
    or REPO / "plugins" / "openai_evals_oss" / ".venv" / _BIN
)
pytestmark = pytest.mark.skipif(
    not API_ENV.is_file(), reason=f"Evals API plugin environment not installed at {API_ENV}"
)
cli = CliRunner()
KEY = "sk-test-aibench-evals-key-0123456789"
CRITERION = {
    "type": "string_check",
    "name": "mentions_reference",
    "input": "{{item.output}}",
    "operation": "ilike",
    "reference": "{{item.reference}}",
}
EXPECTED = {"arith": "pass", "capital": "pass", "planet": "fail", "water": "pass"}


def _load(path: Path) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stub() -> Iterator[Any]:
    server = _load(EXAMPLES / "evals_api_stub.py").make_server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def project(tmp_path: Path, stub: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A recorded run of the quiz application, and a policy approving the stand-in."""
    monkeypatch.setenv("AIBENCH_TEST_OPENAI_KEY", KEY)
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
    (tmp_path / "quiz.app.json").write_text(
        json.dumps({
            "application_id": "quiz", "runner": "cli", "target": "quiz_app.py",
            "transport": {"kind": "cli", "argv": [sys.executable, str(EXAMPLES / "quiz_app.py")]},
            "output_binding": {"output": "/output"},
        }),
        encoding="utf-8",
    )  # fmt: skip
    (tmp_path / "plan.json").write_text(
        json.dumps({"plan_id": "quiz", "dataset": "data.jsonl", "application": "quiz.app.json",
                    "metrics": [{"metric": "native.exact_match"}]}),
        encoding="utf-8",
    )  # fmt: skip
    base_url = f"http://127.0.0.1:{stub.server_port}/v1"
    policy = {
        "allow_trusted_local": True,
        "allowed_evaluators": ["native.*", "openai_evals_api.*"],
        "allowed_plugin_environments": [str(API_ENV)],
        "allowed_egress_origins": [base_url],
        "allowed_secret_refs": ["env:AIBENCH_TEST_OPENAI_KEY"],
    }
    (tmp_path / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    run = cli.invoke(
        app,
        ["run", "--plan", str(tmp_path / "plan.json"), "--policy", str(tmp_path / "policy.json"),
         "--workspace", str(tmp_path), "--json"],
    )  # fmt: skip
    assert run.exit_code in (0, 1), run.output
    criteria = tmp_path / "criteria.json"
    criteria.write_text(json.dumps([CRITERION]), encoding="utf-8")
    return {
        "root": tmp_path,
        "run_id": json.loads(run.stdout)["run_id"],
        "base_url": base_url,
        "policy": tmp_path / "policy.json",
        "policy_data": policy,
        "criteria": criteria,
    }


def _cli(p: dict[str, Any], *args: str, code: int = 0) -> Any:
    result = cli.invoke(app, ["openai-evals-api", *args, "--workspace", str(p["root"]), "--json"])
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if code == 0 else result


def _submit(p: dict[str, Any], *extra: str, code: int = 0) -> Any:
    return _cli(
        p, "submit", p["run_id"],
        "--criteria", str(p["criteria"]),
        "--plugin-env", str(API_ENV),
        "--policy", str(p["policy"]),
        "--base-url", p["base_url"],
        "--api-key", "env:AIBENCH_TEST_OPENAI_KEY",
        *extra, code=code,
    )  # fmt: skip


def _posts(stub: Any, suffix: str) -> list[dict[str, Any]]:
    return [r for r in stub.requests if r["method"] == "POST" and r["path"].endswith(suffix)]


def _results(p: dict[str, Any]) -> list[Any]:
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    storage = Storage(Database.open_workspace(Workspace.at(p["root"])))
    try:
        return [
            r for r in storage.list_metric_results(p["run_id"])
            if r.metric_id == "openai_evals_api.criterion"
        ]  # fmt: skip
    finally:
        storage.db.close()


def _until_terminal(p: dict[str, Any], job_id: str) -> dict[str, Any]:
    for _ in range(5):
        job = _cli(p, "status", job_id, "--policy", str(p["policy"]))
        if job["state"] in ("completed", "failed", "canceled"):
            return job
    raise AssertionError(job)


def test_submit_poll_fetch_grades_recorded_outputs_once_per_case(
    project: dict[str, Any], stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aibench.services.remote_jobs.FETCH_PAGE_SIZE", 3)  # two pages
    job = _submit(project)
    assert job["state"] == "submitted" and job["remote"]["run_id"].startswith("evalrun_")
    # What left the machine: exactly the recorded outputs, as a jsonl data source.
    [create_run] = _posts(stub, "/runs")
    source = create_run["body"]["data_source"]
    assert source["type"] == "jsonl" and source["source"]["type"] == "file_content"
    items = {c["item"]["aibench_case_id"]: c["item"] for c in source["source"]["content"]}
    assert items["capital"]["output"] == "Paris is the capital of France."
    assert items["planet"] == {**items["planet"], "output": "Saturn", "reference": "Jupiter"}
    assert create_run["authorization"] == f"Bearer {KEY}"
    assert create_run["body"]["metadata"]["aibench_job"] == job["job_id"]

    assert _until_terminal(project, job["job_id"])["state"] == "completed"
    fetched = _cli(project, "fetch", job["job_id"], "--policy", str(project["policy"]))
    assert fetched["state"] == "imported"
    assert fetched["mapping"] == {
        "output_items": 4, "mapped": 4, "missing": [], "unknown_case_ids": [],
        "duplicate_items_ignored": 0, "conflicting_cases": [],
    }  # fmt: skip
    pages = [r for r in stub.requests if r["path"].endswith("/output_items")]
    assert len(pages) == 2 and pages[1]["query"]["after"]  # cursor pagination followed
    results = _results(project)
    assert {r.case_id: r.decision.value for r in results} == EXPECTED
    assert all(r.scoring_id == f"remote-{job['job_id']}" for r in results)

    # Fetching again imports nothing new; the key was never stored.
    _cli(project, "fetch", job["job_id"], "--policy", str(project["policy"]))
    assert len(_results(project)) == 4
    db = (project["root"] / ".aibench" / "aibench.db").read_bytes()
    assert KEY.encode() not in db
    assert all(KEY.encode() not in f.read_bytes()
               for f in (project["root"] / ".aibench" / "artifacts").rglob("*") if f.is_file())  # fmt: skip


@pytest.mark.parametrize("where", ["create_eval", "create_run"])
@pytest.mark.parametrize("failure", ["error_after_commit", "drop_after_commit"])
def test_an_ambiguous_submission_is_reconciled_not_resent(
    project: dict[str, Any], stub: Any, where: str, failure: str
) -> None:
    stub.inject[where] = failure  # the service committed it, then the reply was lost
    job = _submit(project)
    kind = where.removeprefix("create_")
    assert job["state"] == f"{kind}_unknown"
    resumed = _cli(project, "resume", job["job_id"], "--policy", str(project["policy"]))
    assert resumed["state"] == "submitted"
    assert any(h["event"] == "reconciled" and h["kind"] == kind for h in resumed["history"])
    # Exactly one eval and one run exist remotely: nothing was sent twice.
    assert len(stub.evals) == 1 and len(stub.runs) == 1
    assert len(_posts(stub, "/evals")) == 1 and len(_posts(stub, "/runs")) == 1


def test_an_unknown_submission_that_is_not_found_is_resent_only_on_request(
    project: dict[str, Any], stub: Any
) -> None:
    stub.inject["create_run"] = "error_after_commit"
    job = _submit(project)
    stub.runs.clear()  # this time the service had not kept it
    still = _cli(project, "resume", job["job_id"], "--policy", str(project["policy"]))
    assert still["state"] == "run_unknown"
    assert still["history"][-1]["event"] == "reconcile_not_found"
    assert len(_posts(stub, "/runs")) == 1  # not resent on its own
    resent = _cli(project, "resume", job["job_id"], "--policy", str(project["policy"]), "--resend")
    assert resent["state"] == "submitted"
    assert [h["event"] for h in resent["history"]][-3:] == [
        "resend_accepted", "sending", "run_created",
    ]  # fmt: skip
    assert len(_posts(stub, "/runs")) == 2
    # A second job for the same grading of the same outputs needs explicit consent.
    duplicate = _submit(project, code=2)
    assert "--allow-duplicate" in duplicate.output


def test_partial_failed_and_cancelled_runs_keep_the_lost_coverage(
    project: dict[str, Any], stub: Any
) -> None:
    stub.stop_after = 2
    job = _submit(project)
    assert _until_terminal(project, job["job_id"])["state"] == "failed"
    fetched = _cli(project, "fetch", job["job_id"], "--policy", str(project["policy"]))
    assert fetched["mapping"]["mapped"] == 2 and len(fetched["mapping"]["missing"]) == 2
    results = _results(project)
    assert len(results) == 4  # every submitted case has a result
    skipped = [r for r in results if r.status.value == "skipped"]
    assert len(skipped) == 2 and all("remote_missing: run failed" in r.reason for r in skipped)

    stub.stop_after = None
    second = _submit(project, "--allow-duplicate")
    cancelled = _cli(project, "cancel", second["job_id"], "--policy", str(project["policy"]))
    assert cancelled["state"] == "canceled"
    fetched = _cli(project, "fetch", second["job_id"], "--policy", str(project["policy"]))
    assert fetched["mapping"]["mapped"] == 1 and len(fetched["mapping"]["missing"]) == 3


def test_remote_items_map_to_cases_without_loss_or_duplication(
    project: dict[str, Any], stub: Any
) -> None:
    stub.duplicate_case = "capital"  # the service returns two results for one case
    job = _submit(project)
    _until_terminal(project, job["job_id"])
    fetched = _cli(project, "fetch", job["job_id"], "--policy", str(project["policy"]))
    assert fetched["mapping"]["conflicting_cases"] == ["capital"]
    assert fetched["mapping"]["mapped"] == 3
    by_case = {r.case_id: r for r in _results(project)}
    assert len(by_case) == 4 and by_case["capital"].status.value == "error"
    assert "remote_conflict" in by_case["capital"].reason
    assert {c: r.decision.value for c, r in by_case.items() if c != "capital"} == {
        c: d for c, d in EXPECTED.items() if c != "capital"
    }


def test_the_map_of_remote_items_flags_unknown_and_repeated_items() -> None:
    from aibench.services.remote_jobs import map_output_items

    def item(item_id: str, case: str) -> dict[str, Any]:
        return {"id": item_id, "datasource_item": {"aibench_case_id": case}, "results": []}

    mapping = map_output_items(
        [item("o1", "a"), item("o1", "a"), item("o2", "stranger"), item("o3", "b")],
        ["a", "b", "c"],
    )
    assert mapping.summary() == {
        "output_items": 3, "mapped": 2, "missing": ["c"], "unknown_case_ids": ["stranger"],
        "duplicate_items_ignored": 1, "conflicting_cases": [],
    }  # fmt: skip


def test_stored_output_scoring_never_asks_the_service_to_generate(
    project: dict[str, Any], stub: Any
) -> None:
    """17-G2: a grader that reads a generated sample, or a generating data source, is
    refused before anything is sent."""
    project["criteria"].write_text(
        json.dumps([{**CRITERION, "input": "{{sample.output_text}}"}]), encoding="utf-8"
    )
    refused = _submit(project, code=4)
    assert "sample.output_text" in refused.output
    project["criteria"].write_text(
        json.dumps([{"type": "label_model", "name": "judge", "model": "gpt-x",
                     "input": [{"role": "user", "content": "{{item.output}}"}],
                     "labels": ["good", "bad"], "passing_labels": ["good"]}]),
        encoding="utf-8",
    )  # fmt: skip
    refused = _submit(project, code=4)
    assert "allow_model_evaluators" in refused.output
    assert stub.requests == []  # nothing left the machine
    code = (
        "from aibench_openai_evals_api import contract\n"
        "for kind in ('completions', 'responses'):\n"
        "    try:\n"
        "        contract.check_run_request({'data_source': {'type': kind}})\n"
        "    except contract.ContractError as exc:\n"
        "        print('refused', kind)\n"
    )
    out = subprocess.run([str(API_ENV), "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.split("\n")[:2] == ["refused completions", "refused responses"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"allowed_egress_origins": []}, "allowed_egress_origins"),
        ({"allowed_secret_refs": []}, "secret env:AIBENCH_TEST_OPENAI_KEY"),
        ({"allowed_plugin_environments": []}, "plugin"),
        ({"allowed_evaluators": ["native.*"]}, "openai_evals_api.criterion"),
    ],
)
def test_egress_needs_every_permission_first(
    project: dict[str, Any], stub: Any, change: dict[str, Any], message: str
) -> None:
    project["policy"].write_text(json.dumps({**project["policy_data"], **change}), encoding="utf-8")
    refused = _submit(project, code=4)
    assert message in refused.output
    assert stub.requests == []


def test_a_remote_job_metric_cannot_be_bound_in_a_plan(project: dict[str, Any]) -> None:
    plan = json.loads((project["root"] / "plan.json").read_text())
    plan["metrics"] = [{"metric": "openai_evals_api.criterion", "params": {"criterion": CRITERION}}]
    plan["plugin_environments"] = [{"python": str(API_ENV)}]
    (project["root"] / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    result = cli.invoke(
        app,
        ["run", "--plan", str(project["root"] / "plan.json"), "--policy", str(project["policy"]),
         "--workspace", str(project["root"]), "--json"],
    )  # fmt: skip
    assert result.exit_code != 0
    assert "remote job" in result.output


@pytest.mark.skipif(not OSS_ENV.is_file(), reason="needs both OpenAI plugin environments")
def test_the_oss_and_hosted_bridges_are_separate_plugins() -> None:
    """17-G1: separate plugin identities, dependencies and capabilities."""
    from aibench.registry.discovery import discover_plugins, environment_site_paths, load_manifests

    found = {}
    for python in (OSS_ENV, API_ENV):
        [plugin] = [
            p for p in discover_plugins(environment_site_paths(python))
            if p.distribution.startswith("aibench-openai-evals")
        ]  # fmt: skip
        found[plugin.distribution] = (plugin, load_manifests(plugin, python=python).manifests)
    assert set(found) == {"aibench-openai-evals-oss", "aibench-openai-evals-api"}
    oss = found["aibench-openai-evals-oss"][1]
    api = found["aibench-openai-evals-api"][1]
    assert {m.plugin_id for m in oss} == {"aibench-openai-evals-oss"}
    assert {m.plugin_id for m in api} == {"aibench-openai-evals-api"}
    assert {(m.package_name, m.package_version) for m in oss} == {("evals", "3.0.1.post1")}
    assert {(m.package_name, m.package_version) for m in api} == {("openai", "3.19.2")}
    assert {m.consumes for m in oss} == {"recorded_outputs"}
    assert {m.consumes for m in api} == {"remote_job"}
    assert all(not m.network_destinations for m in oss)
    assert all(m.network_destinations for m in api)
    # Each environment holds only its own upstream: no hosted SDK plugin in the OSS env,
    # and no openai/evals framework in the hosted one.
    probe = "import importlib.util,sys;print(importlib.util.find_spec(sys.argv[1]) is not None)"
    assert subprocess.run([str(API_ENV), "-c", probe, "evals"], capture_output=True,
                          text=True, check=True).stdout.strip() == "False"  # fmt: skip


def test_a_redirect_is_never_followed_with_the_uploaded_data(
    project: dict[str, Any], stub: Any
) -> None:
    """The SDK follows redirects by default; a redirect from the approved origin must not
    carry the request body to an origin the policy never approved (review finding)."""
    other = _load(EXAMPLES / "evals_api_stub.py").make_server()
    threading.Thread(target=other.serve_forever, daemon=True).start()
    try:
        stub.redirect_to = f"http://127.0.0.1:{other.server_port}"
        stub.inject["create_eval"] = "redirect"
        job = _submit(project)
        assert job["state"] == "rejected"
        assert other.requests == []  # nothing reached the unapproved origin
    finally:
        other.shutdown()
        other.server_close()


def test_an_unreadable_success_reply_is_ambiguous_not_rejected(
    project: dict[str, Any], stub: Any
) -> None:
    """The service created the eval, but the reply was not JSON: that is an unknown
    outcome to reconcile, not a rejection that reopens a duplicate submit (review
    finding)."""
    stub.inject["create_eval"] = "garbage_after_commit"
    job = _submit(project)
    assert job["state"] == "eval_unknown"
    resumed = _cli(project, "resume", job["job_id"], "--policy", str(project["policy"]))
    assert resumed["state"] == "submitted"
    assert len(stub.evals) == 1 and len(_posts(stub, "/evals")) == 1


def test_an_interruption_after_sending_is_reconciled_on_resume(
    project: dict[str, Any], stub: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The harness dies after the create request left but before it recorded the reply:
    resuming must look for it remotely, never send it again (review finding)."""
    import asyncio

    from aibench.engine.compile import load_policy
    from aibench.services import remote_jobs
    from aibench.storage.artifacts import ArtifactStore
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    real_call = remote_jobs._Worker.call

    async def interrupted(self: Any, message: Any, timeout: float = 300) -> Any:
        reply = await real_call(self, message, timeout)
        if message.get("op") == "create_eval":
            raise KeyboardInterrupt  # after the service answered, before it was recorded
        return reply

    monkeypatch.setattr(remote_jobs._Worker, "call", interrupted)
    ws = Workspace.at(project["root"])
    storage = Storage(Database.open_workspace(ws))
    config = remote_jobs.RemoteConfig(API_ENV, project["base_url"], "env:AIBENCH_TEST_OPENAI_KEY")
    try:
        with pytest.raises(KeyboardInterrupt):
            asyncio.run(
                remote_jobs.submit_job(
                    storage, ArtifactStore(ws.artifacts_dir), project["run_id"], [CRITERION],
                    config=config, policy=load_policy(project["policy"]),
                )
            )  # fmt: skip
        [job] = storage.list_remote_jobs(project["run_id"])
    finally:
        storage.db.close()
    assert job["state"] == "eval_unknown"
    monkeypatch.setattr(remote_jobs._Worker, "call", real_call)
    resumed = _cli(project, "resume", job["job_id"], "--policy", str(project["policy"]))
    assert resumed["state"] == "submitted"
    assert len(_posts(stub, "/evals")) == 1  # found by the job's ID, not sent again


def test_a_run_that_can_still_change_is_not_graded(project: dict[str, Any], stub: Any) -> None:
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    storage = Storage(Database.open_workspace(Workspace.at(project["root"])))
    try:
        storage.update_run_status(project["run_id"], "interrupted")
    finally:
        storage.db.close()
    refused = _submit(project, code=2)
    assert "interrupted" in refused.output
    assert stub.requests == []


def test_results_attach_to_the_executions_that_were_uploaded(
    project: dict[str, Any], stub: Any
) -> None:
    job = _submit(project)
    _until_terminal(project, job["job_id"])
    _cli(project, "fetch", job["job_id"], "--policy", str(project["policy"]))
    stored = _cli(project, "jobs")["jobs"][0]
    by_case = {r.case_id: r for r in _results(project)}
    for case_id, upload in stored["uploads"].items():
        assert by_case[case_id].execution_id == upload["execution_id"]


def test_plan_analysis_refuses_a_remote_job_metric(project: dict[str, Any]) -> None:
    """Not only `aibench run`: plan validation and chat drafts use the same analysis."""
    from aibench.engine.compile import PlanInvalid, compile_plan, load_policy

    plan = json.loads((project["root"] / "plan.json").read_text())
    plan["metrics"] = [{"metric": "openai_evals_api.criterion", "params": {"criterion": CRITERION}}]
    plan["plugin_environments"] = [{"python": str(API_ENV)}]
    path = project["root"] / "remote-plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    with pytest.raises(PlanInvalid, match="remote job"):
        compile_plan(path, policy=load_policy(project["policy"]))


def test_remote_worker_startup_failure_reaps_process_and_private_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aibench.services import remote_jobs

    class FakeProcess:
        pid = 12345
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
        raise TimeoutError("worker handshake timed out")

    monkeypatch.setattr(remote_jobs.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(remote_jobs, "ProcessTree", make_tree)
    worker = remote_jobs._Worker(
        remote_jobs.RemoteConfig(Path(sys.executable), api_key="env:TEST_KEY"),
        {"TEST_KEY": "test-secret"},
    )
    workdir = worker.workdir
    monkeypatch.setattr(worker, "call", fail_startup)

    with pytest.raises(TimeoutError, match="handshake timed out"):
        asyncio.run(worker.__aenter__())

    assert len(trees) == 1 and trees[0].killed and trees[0].closed
    assert process.waited
    assert not workdir.exists()
