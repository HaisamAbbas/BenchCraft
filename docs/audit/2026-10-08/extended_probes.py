"""Additional offline boundary reproductions; run after audit_probes.py."""
from __future__ import annotations

import asyncio
import json
import math
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import audit_probes as audit


def main() -> None:
    from aibench.core.models import ApplicationSpec, BenchmarkCase, ExecutionResult, HttpTransport
    from aibench.datasets.ingest import ingest_dataset
    from aibench.datasets.normalize import normalize_case
    from aibench.engine.retry import classify_execution
    from aibench.runners.base import InvocationContext
    from aibench.runners.bindings import AppInputEnvelope
    from aibench.runners.http_runner import HttpRunner
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    audit.RESULTS = json.loads((audit.OUT / "probes.json").read_text(encoding="utf-8"))
    audit.SCRATCH = Path(audit.RESULTS["environment"]["scratch"])
    demo = audit.SCRATCH / "demo"
    healthy_id = audit.RESULTS["commands"]["run_healthy"]["json"]["run_id"]

    # Correct the original audit-only evaluator fixture, then reproduce genuine errors.
    custom = demo / "failing_evaluator.py"
    source = custom.read_text(encoding="utf-8").replace('value_kind="boolean", aggregation=', 'value_kind="boolean", direction="higher", scope="case", aggregation=')
    custom.write_text(source, encoding="utf-8")
    audit.cli("score_evaluator_errors", ["score", healthy_id, "--metrics", "failure.metrics.json", "--custom-evaluator", str(custom), "--trust-local-code", "--json"], demo)

    boolean_results = []
    for raw in (False, "false", "False", "0", 0, [], {}):
        result = normalize_case({"case_id": "fixture-boundary", "input": "Q", "fixtures": [{"name": "hidden", "content": "AUDIT_ONLY_HIDDEN_SENTINEL", "app_visible": raw}]}, line=1, occurrence_index=1)
        boolean_results.append({"specified_app_visible": raw, "normalized": result.case.fixtures[0].app_visible, "app_input": result.case.application_input_projection()})
    audit.probe("fixture_visibility_coercion", boolean_results)
    echo = demo / "echo_fixtures.py"
    echo.write_text('import json,sys\np=json.load(sys.stdin)\nprint(json.dumps({"output":p["fixtures"]}))\n', encoding="utf-8")
    audit.write(demo / "echo.app.json", {"application_id": "audit-echo", "runner": "cli", "target": "echo_fixtures.py", "effects": "none", "transport": {"kind": "cli", "argv": [audit.sys.executable, "echo_fixtures.py"]}})
    (demo / "fixture-leak.jsonl").write_text(json.dumps({"case_id": "fixture-boundary", "input": "Q", "fixtures": [{"name": "hidden", "content": "AUDIT_ONLY_HIDDEN_SENTINEL", "app_visible": "false"}]}) + "\n", encoding="utf-8")
    audit.cli("fixture_leak_e2e", ["app", "smoke", "echo.app.json", "--dataset", "fixture-leak.jsonl", "--trust-local-app", "--json"], demo)

    nan_path = demo / "nan.jsonl"
    nan_path.write_text('{"case_id":"nan-input","input":NaN}\n', encoding="utf-8")
    result = ingest_dataset(nan_path)
    audit.probe("nonfinite_dataset_input", {"accepted_valid": result.is_valid, "in_memory_nan": math.isnan(result.cases[0].input), "serialized_case": result.cases[0].model_dump(mode="json")})
    audit.cli("nan_dataset_validation", ["dataset", "validate", str(nan_path), "--json"], demo)

    # All credentials here are deliberately fictitious; no account key is read.
    sentinel = "sk-AUDIT_FAKE_CREDENTIAL_1234567890"
    fake_trace = demo / "secret.trace.json"
    audit.write(fake_trace, {"resourceSpans": [{"scopeSpans": [{"spans": [{"traceId": "a" * 32, "spanId": "b" * 16, "name": "offline-audit", "attributes": [{"key": "http.request.header.authorization", "value": {"stringValue": "Bearer " + sentinel}}]}]}]}]})
    imported = audit.cli("trace_secret_import", ["traces", "import", healthy_id, str(fake_trace), "--json"], demo)
    storage = Storage(Database.open_workspace(Workspace.at(demo)))
    try:
        raw_id = imported["json"]["raw_artifact_id"]
        artifact = storage.get_artifact(raw_id)
        audit.probe("trace_secret_at_rest", {"credential_persisted": sentinel.encode() in Path(artifact.uri).read_bytes(), "artifact_redaction_label": artifact.redaction.value})
    finally:
        storage.db.close()
    bad_trace = demo / "malformed.trace.json"
    audit.write(bad_trace, {"resourceSpans": [None]})
    audit.cli("malformed_trace_shape", ["traces", "import", healthy_id, str(bad_trace), "--json"], demo)

    # JSON report's category aggregate should receive the same redaction as other formats.
    category_custom = demo / "category_evaluator.py"
    category_custom.write_text('''from aibench.core.models import EvaluatorManifest
from aibench.evaluators.protocol import Evaluator, EvaluationOutcome
class Category(Evaluator):
    manifest = EvaluatorManifest(evaluator_id="audit.category", version="1.0.0", plugin_id="audit", plugin_version="1.0.0", description="Synthetic redaction probe", value_kind="category", direction="none", scope="case", aggregation="category_counts")
    async def evaluate(self, view, ctx):
        return EvaluationOutcome.ok("category", "sk-AUDIT_FAKE_CREDENTIAL_1234567890")
EVALUATORS = (Category,)
''', encoding="utf-8")
    audit.write(demo / "category.metrics.json", {"metrics": [{"metric": "audit.category"}]})
    audit.cli("score_secret_category", ["score", healthy_id, "--metrics", "category.metrics.json", "--custom-evaluator", str(category_custom), "--trust-local-code", "--json"], demo)
    json_report = audit.cli("report_secret_category_json", ["report", healthy_id, "--format", "json", "--out", "-"], demo)
    md_report = audit.cli("report_secret_category_markdown", ["report", healthy_id, "--format", "markdown", "--out", "-"], demo)
    audit.probe("json_report_redaction", {"sentinel_in_json": sentinel in json_report["stdout"], "sentinel_in_markdown": sentinel in md_report["stdout"]})

    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            requests.append(self.path)
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/slow")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                time.sleep(1.5)
                try:
                    self.send_response(200)
                    self.send_header("Content-Length", "15")
                    self.end_headers()
                    self.wfile.write(b'{"output":"ok"}')
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def redirect_probe():
        url = f"http://127.0.0.1:{server.server_port}/redirect"
        spec = ApplicationSpec(application_id="audit-redirect", runner="http", target=url, effects="reversible", transport=HttpTransport(url=url, follow_redirects=True, timeout_seconds=0.4, connect_timeout_seconds=0.4))
        runner = HttpRunner(spec, base_dir=demo)
        async with runner:
            outcome = await runner.invoke(AppInputEnvelope.from_case(BenchmarkCase(case_id="redirect", input="Q")), InvocationContext(run_id="audit-redirect", case_id="redirect"))
        record = ExecutionResult(execution_id="audit-redirect:redirect", run_id="audit-redirect", case_id="redirect", status=outcome.status, effect_state=outcome.effect_state, error_kind=outcome.error_kind, observation_completeness=outcome.completeness)
        audit.probe("redirect_effect_state", {"requests": requests, "status": outcome.status.value, "error_kind": outcome.error_kind.value, "effect_state": outcome.effect_state.value, "work_verdict": classify_execution(record).final_state.value})
    try:
        asyncio.run(redirect_probe())
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
