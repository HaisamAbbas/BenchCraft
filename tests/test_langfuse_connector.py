"""Langfuse connector (17-T3, 17-G4), end to end through the CLI: a dataset imported from a
local Langfuse stand-in, run against a traced HTTP application, its traces imported and its
recorded results exported back as scores. The round trip must preserve provenance: every
exported score names the Langfuse dataset item and trace the case came from.

The stand-in follows the public API's documented shapes (langfuse==4.15.6 generated
client); it is not a live deployment, and no live call is made (none is authorized).
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.connectors.langfuse import ConnectorError, LangfuseClient, LangfuseConfig

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples" / "langfuse"
cli = CliRunner()
QUESTIONS = {
    "item-arith": ("What is 2+2?", "4"),
    "item-capital": ("What is the capital of France?", "Paris"),
    "item-planet": ("What is the largest planet?", "Jupiter"),
    "item-water": ("What is the chemical symbol for water?", "H2O"),
}


def _load(path: Path) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(f"example_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def langfuse() -> Iterator[Any]:
    stub_module = _load(EXAMPLES / "langfuse_stub.py")
    server = stub_module.make_server()
    server.add_dataset(
        "quiz",
        [{"id": item_id, "input": [{"role": "user", "content": q}], "expectedOutput": a}
         for item_id, (q, a) in QUESTIONS.items()]
        + [{"id": "item-old", "input": "retired question", "expectedOutput": "x",
            "status": "ARCHIVED"}],
    )  # fmt: skip
    threading.Thread(target=server.serve_forever, daemon=True).start()
    server.keys = (stub_module.PUBLIC_KEY, stub_module.SECRET_KEY)
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def project(
    tmp_path: Path, langfuse: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[dict[str, Any]]:
    host = f"http://127.0.0.1:{langfuse.server_port}"
    monkeypatch.setenv("TEST_LF_PUBLIC", langfuse.keys[0])
    monkeypatch.setenv("TEST_LF_SECRET", langfuse.keys[1])
    quiz = _load(EXAMPLES / "traced_quiz_app.py").make_server(host)
    threading.Thread(target=quiz.serve_forever, daemon=True).start()
    policy = {
        "allowed_egress_origins": [host],
        "allowed_secret_refs": ["env:TEST_LF_PUBLIC", "env:TEST_LF_SECRET"],
        "allowed_http_origins": [f"http://127.0.0.1:{quiz.server_port}"],
    }
    (tmp_path / "policy.json").write_text(json.dumps(policy), encoding="utf-8")
    (tmp_path / "quiz.app.json").write_text(
        json.dumps({
            "application_id": "traced-quiz", "runner": "http",
            "target": f"http://127.0.0.1:{quiz.server_port}/answer",
            "transport": {"kind": "http", "url": f"http://127.0.0.1:{quiz.server_port}/answer"},
            "input_binding": {"fields": {"/input": "/input"}},
            "output_binding": {"output": "/answer"},
        }),
        encoding="utf-8",
    )  # fmt: skip
    try:
        yield {"root": tmp_path, "host": host, "policy": tmp_path / "policy.json",
               "policy_data": policy}  # fmt: skip
    finally:
        quiz.shutdown()
        quiz.server_close()


def _lf(p: dict[str, Any], *args: str, code: int = 0, workspace: bool = True) -> Any:
    command = ["langfuse", *args, "--host", p["host"], "--policy", str(p["policy"]),
               "--public-key", "env:TEST_LF_PUBLIC", "--secret-key", "env:TEST_LF_SECRET",
               "--json"]  # fmt: skip
    if workspace:
        command += ["--workspace", str(p["root"])]
    result = cli.invoke(app, command)
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if code == 0 else result


def _import_and_run(p: dict[str, Any]) -> tuple[dict[str, Any], str]:
    imported = _lf(p, "import-dataset", "quiz", "--out", str(p["root"] / "quiz.jsonl"),
                   workspace=False)  # fmt: skip
    (p["root"] / "plan.json").write_text(
        json.dumps({"plan_id": "quiz", "dataset": "quiz.jsonl", "application": "quiz.app.json",
                    "metrics": [{"metric": "native.exact_match"}]}),
        encoding="utf-8",
    )  # fmt: skip
    run = cli.invoke(
        app,
        ["run", "--plan", str(p["root"] / "plan.json"), "--policy", str(p["policy"]),
         "--workspace", str(p["root"]), "--json"],
    )  # fmt: skip
    assert run.exit_code in (0, 1), run.output
    return imported, json.loads(run.stdout)["run_id"]


def _storage(p: dict[str, Any]) -> Any:
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    return Storage(Database.open_workspace(Workspace.at(p["root"])))


def test_a_round_trip_preserves_provenance(
    project: dict[str, Any], langfuse: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aibench.connectors.langfuse.PAGE_LIMIT", 2)  # page-numbered paging
    imported, run_id = _import_and_run(project)
    assert (imported["items"], imported["imported"]) == (5, 4)
    assert imported["skipped"] == {"archived": 1}
    pages = [r for r in langfuse.requests if r["path"] == "/api/public/dataset-items"]
    assert [r["query"]["page"] for r in pages] == [["1"], ["2"], ["3"]]
    rows = [json.loads(line) for line in (project["root"] / "quiz.jsonl").read_text().splitlines()]
    capital = next(r for r in rows if r["case_id"] == "item-capital")
    assert capital["extensions"]["langfuse.dataset_item"]["id"] == "item-capital"
    assert capital["extensions"]["langfuse.dataset_item"]["updatedAt"] == "2026-09-01T10:00:00.000Z"
    assert capital["reference"] == {"answer": "Paris"}
    assert "items/item-capital@2026-09-01" in capital["provenance"]["source_refs"][0]

    traces = _lf(project, "import-traces", run_id)
    assert (traces["executions"], traces["traces"], traces["without_trace"]) == (4, 3, 1)
    assert traces["partial"] == 0
    storage = _storage(project)
    try:
        observations = storage.list_trace_observations(run_id)
        executions = {e.execution_id: e for e in storage.list_execution_attempts(run_id)}
    finally:
        storage.db.close()
    # Usage from the generation only, the agent span above it contributes nothing.
    usage = {executions[o["execution_id"]].case_id: o["usage"] for o in observations}
    assert usage["item-arith"]["input_tokens"] == 12 and usage["item-arith"]["output_tokens"] == 1
    assert all(o["normalization"] == "langfuse-observations/1" for o in observations)

    exported = _lf(project, "export-scores", run_id)
    assert (exported["planned"], exported["created"], exported["failed"]) == (3, 3, {})
    assert exported["skipped"] == {"no_langfuse_trace": 1}  # the untraced water case
    storage = _storage(project)
    try:
        results = {r.case_id: r for r in storage.list_metric_results(run_id)}
    finally:
        storage.db.close()
    by_case = {e.case_id: e for e in executions.values()}
    for score in langfuse.scores.values():
        meta = score["metadata"]
        case_id = meta["aibench_case_id"]
        # The score sits on the trace of the execution that produced the result, and names
        # the Langfuse dataset item the case was imported from.
        assert score["subject"]["id"] == by_case[case_id].correlation_id
        assert meta["langfuse_dataset_item_id"] == case_id
        assert meta["aibench_result_id"] == results[case_id].result_id
        assert meta["aibench_run_id"] == run_id
        assert meta["aibench_metric"] == "native.exact_match@1.0.0"
        assert score["value"] is (results[case_id].decision.value == "pass")
    assert {s["metadata"]["aibench_case_id"] for s in langfuse.scores.values()} == {
        "item-arith", "item-capital", "item-planet",
    }  # fmt: skip

    # Exporting again creates nothing: each score is found by its ID and matches.
    again = _lf(project, "export-scores", run_id)
    assert (again["created"], again["already_present"], again["conflicts"]) == (0, 3, [])
    assert len(langfuse.scores) == 3
    # A score changed in Langfuse is reported as a conflict, never overwritten.
    changed = next(iter(langfuse.scores.values()))
    changed["value"] = not changed["value"]
    third = _lf(project, "export-scores", run_id)
    assert third["conflicts"] == [changed["id"]] and third["created"] == 0


def test_a_score_whose_creation_reply_was_lost_is_confirmed_by_read_back(
    project: dict[str, Any], langfuse: Any
) -> None:
    _, run_id = _import_and_run(project)
    _lf(project, "import-traces", run_id)
    langfuse.inject["create_score"] = "drop_after_commit"
    exported = _lf(project, "export-scores", run_id)
    assert exported["created"] == 3 and exported["failed"] == {}
    creates = [
        r for r in langfuse.requests if r["method"] == "POST" and r["path"] == "/api/public/scores"
    ]
    assert len(creates) == 3  # the lost reply was not answered by sending again


def test_nothing_is_sent_to_an_unapproved_host(project: dict[str, Any], langfuse: Any) -> None:
    project["policy"].write_text(
        json.dumps({**project["policy_data"], "allowed_egress_origins": []}), encoding="utf-8"
    )
    refused = _lf(project, "import-dataset", "quiz", "--out", str(project["root"] / "x.jsonl"),
                  code=4, workspace=False)  # fmt: skip
    assert "allowed_egress_origins" in refused.output
    project["policy"].write_text(
        json.dumps({**project["policy_data"], "allowed_secret_refs": ["env:TEST_LF_PUBLIC"]}),
        encoding="utf-8",
    )
    refused = _lf(project, "import-dataset", "quiz", "--out", str(project["root"] / "x.jsonl"),
                  code=4, workspace=False)  # fmt: skip
    assert "env:TEST_LF_SECRET" in refused.output
    assert langfuse.requests == []


def test_import_and_export_never_compute_a_metric(project: dict[str, Any], langfuse: Any) -> None:
    """Import/export is data movement: results come only from the harness's scoring passes,
    and results that were not evaluated are never exported as zeros."""
    _, run_id = _import_and_run(project)
    storage = _storage(project)
    try:
        before = {r.result_id for r in storage.list_metric_results(run_id)}
    finally:
        storage.db.close()
    _lf(project, "import-traces", run_id)
    _lf(project, "export-scores", run_id)
    storage = _storage(project)
    try:
        after = {r.result_id for r in storage.list_metric_results(run_id)}
    finally:
        storage.db.close()
    assert after == before


def test_the_live_status_is_explicit() -> None:
    result = cli.invoke(app, ["langfuse", "status", "--json"])
    assert result.exit_code == 0
    status = json.loads(result.stdout)
    assert "not verified against a live Langfuse deployment" in status["live_verification"]
    assert status["modes"] == ["import-dataset", "import-traces", "export-scores"]


@pytest.mark.parametrize("status", [200, 500])
def test_langfuse_success_and_error_bodies_stop_at_the_response_limit(
    status: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("aibench.connectors.langfuse.MAX_RESPONSE_BYTES", 100)

    class Chunks(httpx.SyncByteStream):
        def __init__(self) -> None:
            self.consumed = 0

        def __iter__(self):
            for _ in range(10):
                self.consumed += 100
                yield b"x" * 100

        def close(self) -> None:
            pass

    stream = Chunks()
    transport = httpx.MockTransport(
        lambda request: httpx.Response(status, stream=stream, request=request)
    )
    client = LangfuseClient(
        LangfuseConfig(
            host="https://langfuse.example",
            public_key="env:PUBLIC",
            secret_key="env:SECRET",
        ),
        {"PUBLIC": "public", "SECRET": "secret"},
        transport=transport,
    )
    try:
        with pytest.raises(ConnectorError, match="response over 100 bytes"):
            client._request("GET", "api/public/v2/datasets/quiz")
    finally:
        client.close()

    assert 100 < stream.consumed < 1_000


def test_langfuse_advertises_only_bounded_content_encodings() -> None:
    accept_encodings: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        accept_encodings.append(request.headers.get("Accept-Encoding"))
        return httpx.Response(200, json={"data": []}, request=request)

    client = LangfuseClient(
        LangfuseConfig(
            host="https://langfuse.example",
            public_key="env:PUBLIC",
            secret_key="env:SECRET",
        ),
        {"PUBLIC": "public", "SECRET": "secret"},
        transport=httpx.MockTransport(respond),
    )
    try:
        client._request("GET", "api/public/v2/datasets/quiz")
    finally:
        client.close()

    assert accept_encodings == ["gzip, deflate"]


def test_a_rescored_run_exports_one_score_per_trace_and_metric(
    project: dict[str, Any], langfuse: Any
) -> None:
    """Every scoring pass is stored; only the latest (or a chosen one) is exported, so a
    trace never gets two same-named scores (review finding)."""
    _, run_id = _import_and_run(project)
    _lf(project, "import-traces", run_id)
    (project["root"] / "metrics.json").write_text(
        json.dumps({"metrics": [{"metric": "native.exact_match"}]}), encoding="utf-8"
    )
    rescored = cli.invoke(
        app,
        ["score", run_id, "--metrics", str(project["root"] / "metrics.json"),
         "--workspace", str(project["root"])],
    )  # fmt: skip
    assert rescored.exit_code == 0, rescored.output
    exported = _lf(project, "export-scores", run_id)
    assert exported["planned"] == 3 and exported["created"] == 3
    traces = [s["subject"]["id"] for s in langfuse.scores.values()]
    assert len(traces) == len(set(traces)) == 3
    storage = _storage(project)
    try:
        latest = next(
            e["payload"]["scoring_id"] for e in reversed(storage.list_run_events(run_id))
            if e["event_type"] == "scoring_pass"
        )  # fmt: skip
    finally:
        storage.db.close()
    assert {s["metadata"]["aibench_scoring_id"] for s in langfuse.scores.values()} == {latest}


def test_scores_go_only_to_the_host_the_data_came_from(
    project: dict[str, Any], langfuse: Any
) -> None:
    """Items and traces imported from one Langfuse host are never scored on another
    (review finding)."""
    _, run_id = _import_and_run(project)
    _lf(project, "import-traces", run_id)
    other = project["host"].replace("127.0.0.1", "localhost")
    project["policy"].write_text(
        json.dumps({**project["policy_data"],
                    "allowed_egress_origins": [project["host"], other]}),
        encoding="utf-8",
    )  # fmt: skip
    command = ["langfuse", "export-scores", run_id, "--host", other, "--policy",
               str(project["policy"]), "--public-key", "env:TEST_LF_PUBLIC",
               "--secret-key", "env:TEST_LF_SECRET", "--workspace", str(project["root"]),
               "--json"]  # fmt: skip
    result = cli.invoke(app, command)
    assert result.exit_code == 0, result.output
    exported = json.loads(result.stdout)
    assert exported["planned"] == 0 and langfuse.scores == {}
    assert exported["skipped"] == {"case_from_another_langfuse_host": 4}


def test_one_call_traced_by_two_sources_is_counted_once() -> None:
    """An OTLP export and a Langfuse import of the same execution must not double its
    usage in the report (review finding)."""
    from aibench.services.traces import traces_summary

    def row(trace_id: str, tokens: int) -> dict[str, Any]:
        return {"import_id": "i", "trace_id": trace_id, "execution_id": "exec-1",
                "complete": True, "partial_reasons": [], "raw_artifact_ids": ["a"],
                "tools": [], "usage": {"input_tokens": tokens, "output_tokens": 0,
                                       "total_tokens": tokens,
                                       "aggregate_spans_excluded": []}}  # fmt: skip

    class Store:
        def list_trace_observations(self, run_id: str) -> list[dict[str, Any]]:
            return [row("otel-trace", 12), row("langfuse:otel-trace", 12)]

    summary = traces_summary(Store(), "run")  # type: ignore[arg-type]
    assert summary is not None
    assert summary["usage"]["total_tokens"] == 12
    assert summary["usage"]["duplicate_traces_excluded"] == 1
