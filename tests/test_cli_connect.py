from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.conversation.agent import ConversationAgent
from aibench.services.traces import import_traces
from aibench.sessions.controller import SessionController
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage
from tests.session_support import ScriptedProvider, call, say

runner = CliRunner()


def _dataset(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "input": "question",
                "reference": {"answer": "expected"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _args(project: Path, dataset: Path, *extra: str) -> list[str]:
    return [
        "connect",
        "http",
        "--project",
        str(project),
        "--url",
        "https://api.example.test/v1/query",
        "--dataset",
        str(dataset),
        "--app-id",
        "support-api",
        "--effects",
        "none",
        *extra,
    ]


def test_http_setup_creates_a_bounded_no_repository_project_without_network_calls(
    tmp_path: Path,
) -> None:
    project = tmp_path / "support-eval"
    dataset = _dataset(tmp_path / "cases.jsonl")
    result = runner.invoke(
        app,
        _args(
            project,
            dataset,
            "--authorize-origin",
            "https://api.example.test",
            "--bearer-secret-ref",
            "env:SUPPORT_API_TOKEN",
            "--max-calls",
            "7",
        ),
    )

    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["network_requests_during_setup"] == 0
    assert report["dataset_cases"] == 1
    assert report["max_application_calls"] == 7
    application = json.loads((project / "application.http.json").read_text(encoding="utf-8"))
    transport = application["transport"]
    assert transport["url"] == "https://api.example.test/v1/query"
    assert transport["secret_headers"]["Authorization"]["ref"] == "env:SUPPORT_API_TOKEN"
    assert application["input_binding"] == {"fields": {"/question": "/input"}}
    assert application["output_binding"] == {"output": "/answer"}
    policy = json.loads((project / "policy.json").read_text(encoding="utf-8"))
    assert policy["allowed_http_origins"] == ["https://api.example.test:443/"]
    assert policy["allowed_egress_origins"] == ["https://api.example.test:443/"]
    assert policy["allowed_secret_refs"] == ["env:SUPPORT_API_TOKEN"]
    assert policy["ceilings"]["max_application_calls"] == 7
    # Judged metrics make several calls per case: the judge ceiling is not the app's.
    assert policy["ceilings"]["max_evaluator_calls"] == 7 * 20
    # A judge that thinks first is slow: an hour stopped a 15-case run 2 evaluations short.
    assert policy["ceilings"]["max_wall_seconds"] == 4 * 3600
    assert "SUPPORT_API_TOKEN" not in json.dumps(report)


def test_remote_http_setup_requires_exact_origin_authorization(tmp_path: Path) -> None:
    project = tmp_path / "support-eval"
    result = runner.invoke(
        app,
        _args(project, _dataset(tmp_path / "cases.jsonl")),
    )
    assert result.exit_code == 2
    assert "--authorize-origin" in result.output
    assert "https://api.example.test:443/" in result.output
    assert not (project / "aibench.json").exists()


def test_loopback_setup_needs_no_remote_authorization(tmp_path: Path) -> None:
    project = tmp_path / "local-eval"
    result = runner.invoke(
        app,
        [
            "connect",
            "http",
            "--project",
            str(project),
            "--url",
            "http://127.0.0.1:8766/query",
            "--dataset",
            str(_dataset(tmp_path / "cases.jsonl")),
            "--effects",
            "none",
        ],
    )
    assert result.exit_code == 0, result.output
    policy = json.loads((project / "policy.json").read_text(encoding="utf-8"))
    assert policy["allowed_http_origins"] == ["http://127.0.0.1:8766/"]
    assert policy["allowed_egress_origins"] == ["http://127.0.0.1:8766/"]


def test_http_setup_rejects_url_credentials_queries_and_secret_literals(tmp_path: Path) -> None:
    project = tmp_path / "support-eval"
    dataset = _dataset(tmp_path / "cases.jsonl")
    base = _args(project, dataset, "--authorize-origin", "https://api.example.test")
    for endpoint, secret_ref, expected in (
        ("https://user:password@api.example.test/query", None, "credentials in the URL"),
        ("https://api.example.test/query?token=secret", None, "query strings and fragments"),
        ("https://api.example.test/query", "sk-literal-secret-value", "environment reference"),
    ):
        args = list(base)
        url_index = args.index("https://api.example.test/v1/query")
        args[url_index] = endpoint
        if secret_ref:
            args.extend(["--bearer-secret-ref", secret_ref])
        result = runner.invoke(app, args)
        assert result.exit_code == 2
        assert expected in result.output
        assert "sk-literal-secret-value" not in result.output
        assert not (project / "aibench.json").exists()


def test_http_setup_never_overwrites_existing_project_files(tmp_path: Path) -> None:
    project = tmp_path / "support-eval"
    dataset = _dataset(tmp_path / "cases.jsonl")
    args = _args(project, dataset, "--authorize-origin", "https://api.example.test")
    first = runner.invoke(app, args)
    assert first.exit_code == 0, first.output
    before = {name: (project / name).read_bytes() for name in ("aibench.json", "policy.json", "application.http.json")}

    second = runner.invoke(app, args)
    assert second.exit_code == 2
    assert "did not change existing files" in second.output
    assert before == {name: (project / name).read_bytes() for name in before}


def test_generated_http_project_runs_through_session_evidence_and_report_services(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            del fmt, args

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            requests.append(request)
            body = json.dumps({"answer": "expected"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    database = None
    try:
        project = tmp_path / "connected-api"
        project.mkdir()
        dataset = _dataset(project / "cases.jsonl")
        result = runner.invoke(
            app,
            [
                "connect",
                "http",
                "--project",
                str(project),
                "--url",
                f"http://127.0.0.1:{server.server_port}/query",
                "--dataset",
                str(dataset),
                "--app-id",
                "connected-api",
                "--effects",
                "none",
            ],
        )
        assert result.exit_code == 0, result.output
        (project / "rag.py").write_text("import chromadb\n", encoding="utf-8")
        configured_policy = json.loads((project / "policy.json").read_text(encoding="utf-8"))
        configured_policy["inspection_roots"] = [str(project)]
        (project / "policy.json").write_text(json.dumps(configured_policy), encoding="utf-8")
        workspace = Workspace.at(project)
        database = Database.open_workspace(workspace)
        storage = Storage(database)
        artifacts = ArtifactStore(workspace.artifacts_dir)
        controller = SessionController.create(
            storage=storage,
            artifacts=artifacts,
            workspace_root=project,
            project_root=project,
            application=project / "application.http.json",
            dataset=dataset,
            objectives=("correctness",),
            policy_path=project / "policy.json",
        )

        async def run() -> str:
            started = await controller.start_run(action_id="connected-api-run", expected_revision=1)
            completed = await controller.wait_for_run(started.run_id)
            assert completed is not None and completed.state.value == "completed"
            return started.run_id

        run_id = asyncio.run(run())
        assert requests == [{"question": "question"}]
        report = controller.report_facts(run_id)
        assert report["status"] == "completed"
        assert report["provenance"]["application_id"] == "connected-api"
        evidence = controller.case_evidence("case-1", run_id)
        assert evidence["executions"][0]["output"] == "expected"
        assert "reference" not in json.dumps(requests)

        trace_file = tmp_path / "http-trace.json"
        trace_file.write_text(
            json.dumps(
                {
                    "resourceSpans": [
                        {
                            "scopeSpans": [
                                {
                                    "spans": [
                                        {
                                            "traceId": "a" * 32,
                                            "spanId": "b" * 16,
                                            "name": "generation",
                                            "attributes": [
                                                {
                                                    "key": "gen_ai.usage.input_tokens",
                                                    "value": {"intValue": "9"},
                                                }
                                            ],
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        imported = import_traces(storage, artifacts, run_id, trace_file)
        assert imported["traces"] == 1
        provider = ScriptedProvider(
            [
                call("read_profile"),
                call("get_trace_evidence", run_id=run_id),
                say("HTTP-run repository findings and stored traces share this session run."),
            ]
        )
        asyncio.run(ConversationAgent(controller, provider).handle_message("What did this API run observe?"))
        briefing = json.dumps(provider.calls[-1])
        assert "rag.py" in briefing and "inferred" in briefing
        assert run_id in briefing and requests == [{"question": "question"}]
        assert "http-trace.json" not in briefing
        trace_results = [
            json.loads(message["content"])
            for message in provider.calls[-1]
            if message.get("role") == "tool"
        ]
        summary = next(item["trace_summary"] for item in trace_results if "trace_summary" in item)
        assert summary["usage"]["input_tokens"] == 9
        controller.storage.db.close()
        database = None
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        if database is not None:
            database.close()
