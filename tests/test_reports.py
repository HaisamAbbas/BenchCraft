"""Evidence reports (11-T1, 11-G1, 11-G2): built from stored facts only, numbers that
reconcile with the stored records and their denominators, visible missing accounting,
escaped hostile evidence, partial snapshots, rescoring passes and release gates.

Runs use a real CLI application (a subprocess) and the real engine; nothing is mocked
except where a test proves that the application and evaluators are *not* needed."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import Decision, EvaluationResult, ExecutionStatus, MetricValue
from aibench.engine.compile import compile_plan
from aibench.engine.engine import RunController
from aibench.reporting.render import _retry_inclusive_text, _throughput_text, _value_summary, render
from aibench.security.policy import ExecutionPolicy
from aibench.services.reports import build_report, percentile, report_facts
from aibench.services.runs import create_run, execute_run, run_budget
from aibench.services.scoring import select_final_executions
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

# Echoes its input as the answer; "crash" exits 3; "slow N" sleeps N seconds first.
ECHO_APP = r"""
import json, pathlib, sys, time
request = json.load(sys.stdin)
text = request["input"]
log = pathlib.Path(__file__).with_name("calls.log")
with log.open("a", encoding="utf-8") as f:
    f.write(request["case_id"] + "\n")
if text.startswith("slow"):
    time.sleep(float(text.split()[1]))
if text.startswith("crash"):
    sys.exit(3)
print(json.dumps({"answer": text, "retrieved": ["passage about " + text[:20]]}))
"""


class Project:
    def __init__(self, root: Path, rows: list[dict[str, Any]], **plan: Any) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "app.py").write_text(ECHO_APP, encoding="utf-8")
        (root / "app.json").write_text(
            json.dumps(
                {
                    "application_id": "echo",
                    "runner": "cli",
                    "target": "app.py",
                    "transport": {"kind": "cli", "argv": [sys.executable, "app.py"]},
                    "output_binding": {"output": "/answer", "retrieved_context": "/retrieved"},
                }
            ),
            encoding="utf-8",
        )
        (root / "data.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        self.plan_path = root / "plan.json"
        self.write_plan(**plan)
        self.workspace = Workspace.at(root)
        self.workspace.ensure_directories()

    def write_plan(self, **fields: Any) -> None:
        plan = {
            "plan_id": "report-test",
            "dataset": "data.jsonl",
            "application": "app.json",
            "metrics": [{"metric": "native.exact_match"}],
            "retry": {"max_attempts": 1},
            **fields,
        }
        self.plan_path.write_text(json.dumps(plan), encoding="utf-8")

    def storage(self) -> tuple[Storage, ArtifactStore]:
        return Storage(Database.open_workspace(self.workspace)), ArtifactStore(
            self.workspace.artifacts_dir
        )

    def calls(self) -> int:
        log = self.root / "calls.log"
        return len(log.read_text(encoding="utf-8").split()) if log.exists() else 0

    def run(self, controller: RunController | None = None, during: Any = None) -> str:
        compiled = compile_plan(self.plan_path, policy=ExecutionPolicy(), trusted_local=True)
        storage, artifacts = self.storage()
        try:
            run_id = create_run(compiled, storage=storage, artifacts=artifacts, granted_by="test")

            async def go() -> None:
                ctl = controller or RunController()
                task = asyncio.ensure_future(
                    execute_run(run_id, storage=storage, artifacts=artifacts, controller=ctl)
                )
                if during is not None:
                    await during(ctl)
                await task

            asyncio.run(go())
            return run_id
        finally:
            storage.db.close()

    def report(self, run_id: str, **kwargs: Any) -> dict[str, Any]:
        storage, artifacts = self.storage()
        try:
            return build_report(storage, artifacts, run_id, **kwargs)
        finally:
            storage.db.close()


def test_scalar_text_report_labels_both_mean_denominators() -> None:
    assert _value_summary(
        {
            "value_summary": {
                "n": 3,
                "case_count": 2,
                "mean": 0.5,
                "min": 0.0,
                "max": 1.0,
                "case_mean_min": 0.0,
                "case_mean_max": 1.0,
                "repetition_weighted_mean": 0.333333,
            }
        }
    ) == (
        "case-macro mean 0.5 (case means 0 to 1) across 2 completed case(s); "
        "repetition-weighted mean 0.333333 across 3 completed repetition(s)"
    )


def _rows(*cases: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [{"case_id": c, "input": i, "expected_output": e} for c, i, e in cases]


MIXED = _rows(
    ("pass-1", "yes", "yes"),
    ("pass-2", "sure", "sure"),
    ("fail-1", "no", "yes"),
    ("crash-1", "crash now", "yes"),
)


def _without_timestamp(report: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in report.items() if k != "generated_at"}


# --------------------------------------------------------------------------- 11-G2


def test_every_number_reconciles_with_the_stored_records(tmp_path: Path) -> None:
    project = Project(
        tmp_path,
        MIXED,
        metrics=[
            {"metric": "native.exact_match"},
            {
                "metric": "native.json_schema",
                "params": {"schema": {"type": "string"}, "parse_text": False},
            },
        ],
    )
    run_id = project.run()
    report = project.report(run_id)
    storage, _ = project.storage()
    try:
        results = storage.list_metric_results(run_id)
        attempts = storage.list_execution_attempts(run_id)
    finally:
        storage.db.close()

    [engine] = report["scoring_passes"]
    assert engine["kind"] == "engine" and len(engine["metrics"]) == 2
    for metric in engine["metrics"]:
        s = metric["summary"]
        mine = [r for r in results if r.binding_hash == metric["binding_hash"]]
        # every planned item lands in exactly one bucket
        assert s["selected"] == 4 == len(mine)
        assert (
            s["completed"]
            + s["errors"]
            + s["cancelled"]
            + s["not_applicable"]
            + s["unavailable"]
            + s["pending"]
        ) == s["selected"]
        # counts copied from storage, recounted independently here
        assert s["completed"] == sum(r.status is ExecutionStatus.OK for r in mine)
        assert s["unavailable"] == sum(r.status is ExecutionStatus.SKIPPED for r in mine) == 1
        for decision in Decision:
            assert s["decisions"][decision.value] == sum(r.decision is decision for r in mine)
    exact = engine["metrics"][0]["summary"]
    assert exact["decisions"]["pass"] == 2 and exact["decisions"]["fail"] == 1
    assert exact["value_summary"] == {
        "true": 2,
        "false": 1,
        "rate": 0.666667,
        "denominator": "completed",
    }

    finals = select_final_executions(attempts)
    application = report["application"]
    assert application["planned"] == application["recorded"] == len(finals) == 4
    assert application["completed"] == 3 and application["failed"] == 1
    assert application["error_kinds"] == {"nonzero_exit": 1}
    walls = [e.timing["wall_ms"] for e in finals if e.status is ExecutionStatus.OK]
    latency = application["latency"]
    assert latency["successful_requests"] == 3
    assert latency["p50_ms"] == percentile(walls, 50) and latency["p95_ms"] == max(walls)
    assert latency["p99_ms"] == max(walls)
    assert latency["mean_ms"] == pytest.approx(sum(walls) / len(walls), abs=0.001)
    assert latency["stddev_ms"] >= 0 and latency["iqr_ms"] >= 0
    assert latency["retry_inclusive"]["successful_requests"] == 3
    assert latency["throughput"]["successful_requests"] == 3
    assert latency["throughput"]["wall_seconds"] > 0
    assert latency["warmup"]["samples"] == 0
    assert latency["excluded_failures"] == 1 and latency["excluded_timeouts"] == 0

    # the application reports no cost: unknown, never $0; model-free evaluators: complete
    assert report["cost"]["application"]["accounting"] == "unknown"
    assert report["cost"]["application"]["total_cost_usd"] is None
    assert report["cost"]["application"]["calls_with_unknown_cost"] == 4
    evaluator = report["cost"]["evaluator"]
    assert evaluator["accounting"] == "complete" and evaluator["total_cost_usd"] == 0.0
    assert evaluator["calls"] == 6  # 2 metrics x 3 usable outputs; skipped items call nothing

    # app failure vs evaluator failure: the crash is unavailable, not an evaluator error
    assert engine["metrics"][0]["evaluator_failures"] == {}
    assert [i["case_id"] for i in report["evidence"]["items"]] == ["crash-1", "fail-1"]

    markdown = render(report, "markdown")
    html = render(report, "html")
    for text in (markdown, html):
        assert "2/4 = 50.0%" in text  # passes / selected
        assert "3/4 = 75.0%" in text  # completed / selected
        assert "true 2/3 = 66.7% of completed" in text
        assert "unknown: no call reported its cost (4 calls)" in text


def test_performance_render_labels_unknown_recovery_phase_as_incomplete() -> None:
    assert _throughput_text({"phase_attribution_complete": False}) == (
        "unavailable (recovered dispatch phase is unknown)"
    )
    assert _retry_inclusive_text({"phase_attribution_complete": False}) == (
        "unavailable (recovered dispatch phase is unknown)"
    )


def test_missing_accounting_is_never_totalled() -> None:
    from aibench.services.reports import _cost_block

    partial = _cost_block(4, 1.5, 1)
    assert partial["accounting"] == "partial" and partial["total_cost_usd"] is None
    assert partial["known_cost_usd"] == 1.5 and partial["calls_with_known_cost"] == 3
    text = render_usd(partial)
    assert text.startswith("at least USD 1.5 observed; accounting partial (3/4 = 75.0%")
    assert _cost_block(0, 0.0, 0)["accounting"] == "no_calls"
    assert _cost_block(2, 0.2, 0)["total_cost_usd"] == 0.2


def render_usd(block: dict[str, Any]) -> str:
    from aibench.reporting.render import _usd

    return _usd(block)


def test_percentiles_are_observed_values_by_nearest_rank() -> None:
    values = [5.0, 1.0, 3.0, 2.0, 4.0]
    assert percentile(values, 50) == 3.0
    assert percentile(values, 95) == 5.0
    assert percentile([7.0], 95) == 7.0
    assert percentile([], 50) is None


# --------------------------------------------------------------------------- 11-G1


def test_reports_regenerate_without_invoking_the_app_or_any_evaluator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = Project(tmp_path, MIXED)
    run_id = project.run()
    first = project.report(run_id)
    calls = project.calls()

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("a report must not create a runner or load evaluators")

    from aibench import registry, runners
    from aibench.services import runs

    monkeypatch.setattr(runners, "create_runner", forbidden)
    monkeypatch.setattr(runs, "create_runner", forbidden)
    monkeypatch.setattr(registry.EvaluatorRegistry, "with_native", forbidden)
    monkeypatch.setattr(registry.EvaluatorRegistry, "load_plugin_environment", forbidden)
    (tmp_path / "app.py").unlink()  # the application is gone entirely

    again = project.report(run_id)
    assert _without_timestamp(again) == _without_timestamp(first)
    assert project.calls() == calls
    for fmt in ("json", "markdown", "html"):
        assert render(again, fmt)

    workspace = str(tmp_path)
    result = CliRunner().invoke(app, ["report", run_id, "--workspace", workspace, "--json"])
    assert result.exit_code == 0, result.output
    written = Path(json.loads(result.output)["path"])
    assert written == tmp_path / ".aibench" / "reports" / run_id / "report.html"
    assert "<!DOCTYPE html>" in written.read_text(encoding="utf-8")


def test_a_partial_snapshot_keeps_pending_work_in_every_denominator(tmp_path: Path) -> None:
    rows = _rows(("a", "yes", "yes"), ("b", "slow 30", "yes"), ("c", "yes", "yes"))
    # a and c run beside the slow b, so the snapshot never waits on b's 30 s sleep
    project = Project(
        tmp_path,
        rows,
        gates=[{"gate_id": "g", "binding": 0, "min_pass_rate": 0.5}],
        concurrency={"application": 3, "evaluation": 3},
    )
    snapshot: dict[str, Any] = {}

    async def during(ctl: RunController) -> None:
        for _ in range(600):
            await asyncio.sleep(0.05)
            storage, artifacts = project.storage()
            try:
                if len(storage.list_metric_results(storage.list_runs()[0].manifest.run_id)) >= 2:
                    run_id = storage.list_runs()[0].manifest.run_id
                    snapshot["report"] = build_report(storage, artifacts, run_id)
                    break
            finally:
                storage.db.close()
        ctl.request("cancel")

    run_id = project.run(during=during)
    live = snapshot["report"]
    assert live["run"]["provisional"] is True and live["run"]["finished"] is False
    [metric] = live["scoring_passes"][0]["metrics"]
    s = metric["summary"]
    assert s["selected"] == 3 and s["pending"] == 1 and s["completed"] == 2
    assert s["completed_coverage"] == round(2 / 3, 6)  # not 2/2
    [gate] = live["gates"]
    assert gate["status"] == "undecided" and "partial snapshot" in gate["reason"]
    assert live["outcome"]["gates_undecided"] == ["g"]
    assert "Partial snapshot" in render(live, "html")

    final = project.report(run_id)
    assert final["run"]["status"] == "cancelled" and final["run"]["partial"] is True
    assert final["run"]["provisional"] is False
    assert final["notes"][0].startswith("Partial results: the run ended cancelled")
    assert "Partial results." in render(final, "markdown")


# --------------------------------------------------------------------------- 11-T1 safety


_REPORT_TAGS = {
    "html",
    "head",
    "meta",
    "title",
    "style",
    "body",
    "h1",
    "h2",
    "h3",
    "p",
    "strong",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "ul",
    "li",
    "code",
    "span",
    "br",
}
_REPORT_ATTRIBUTES = {"lang", "charset", "http-equiv", "content", "name", "class"}


def _tags(document: str) -> list[tuple[str, list[str]]]:
    from html.parser import HTMLParser

    found: list[tuple[str, list[str]]] = []

    class Collector(HTMLParser):
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            found.append((tag, [name for name, _ in attrs]))

    Collector().feed(document)
    return found


HOSTILE = (
    "<script>alert(1)</script><img src=x onerror=alert(2)> [click](javascript:alert(3)) "
    "| a | b | \x1b]0;owned\x07\x1b[2J sk-abcdefghijklmnopqrstuvwx <!-- --> &amp;"
)


def test_hostile_evidence_is_inert_in_html_and_markdown(tmp_path: Path) -> None:
    project = Project(tmp_path, _rows(("hostile", HOSTILE, "safe answer")))
    run_id = project.run()
    report = project.report(run_id)
    [item] = report["evidence"]["items"]
    output = item["execution"]["output_excerpt"]
    assert "sk-abcdefghijklmnopqrstuvwx" not in output and "\x1b" not in output

    html = render(report, "html")
    assert "<script" not in html.lower() and "<img" not in html.lower()
    assert "onerror=alert" not in html.replace("onerror=alert(2)&gt;", "")  # only as text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "javascript:" not in html.split("<body>")[1].replace("(javascript:alert(3))", "")
    assert "sk-abcdefghijklmnopqrstuvwx" not in html and "\x1b" not in html
    assert "Content-Security-Policy" in html and "default-src 'none'" in html
    # Structurally: only the report's own tags and attributes exist in the document.
    tags = _tags(html)
    assert {t for t, _ in tags} <= _REPORT_TAGS, {t for t, _ in tags} - _REPORT_TAGS
    assert {a for _, attrs in tags for a in attrs} <= _REPORT_ATTRIBUTES

    markdown = render(report, "markdown")
    assert "\\<script\\>" in markdown and "\\[click\\]" in markdown
    assert re.search(r"(?<!\\)<", markdown) is None  # every "<" is escaped
    assert "\\&amp;" in markdown  # a literal entity stays literal
    assert "sk-abcdefghijklmnopqrstuvwx" not in markdown
    data_line = next(line for line in markdown.splitlines() if line.startswith("- output:"))
    assert "\\| a \\| b \\|" in data_line  # cannot break out into table cells

    serialized = render(report, "json")
    assert "sk-abcdefghijklmnopqrstuvwx" not in serialized and "\\u001b" not in serialized
    exported = json.loads(serialized)
    [exported_item] = exported["evidence"]["items"]
    assert "[redacted]" in exported_item["execution"]["output_excerpt"]


def test_aggregate_category_keys_are_redacted_in_every_export(tmp_path: Path) -> None:
    project = Project(tmp_path, _rows(("category", "answer", "answer")))
    report = project.report(project.run())
    metric = report["scoring_passes"][0]["metrics"][0]
    summary = metric["summary"]
    selected, completed = summary["selected"], summary["completed"]
    secret = "sk-abcdefghijklmnopqrstuvwx"
    summary["value_summary"] = {"counts": {secret: 3, "[redacted]": 5}}

    serialized = render(report, "json")
    exported = json.loads(serialized)
    exported_summary = exported["scoring_passes"][0]["metrics"][0]["summary"]
    safe_counts = exported_summary["value_summary"]["counts"]
    assert len(safe_counts) == 2
    assert sorted(safe_counts.values()) == [3, 5]
    assert sum(safe_counts.values()) == 8
    assert all(key.startswith("[redacted]") for key in safe_counts)
    assert exported_summary["selected"] == selected
    assert exported_summary["completed"] == completed
    assert secret not in serialized
    assert all(secret not in render(report, fmt) for fmt in ("markdown", "html"))


def test_case_content_can_be_withheld_and_raw_artifacts_are_only_referenced(
    tmp_path: Path,
) -> None:
    # json_schema always records its raw output ({"errors": [...]}) as a restricted artifact
    project = Project(
        tmp_path,
        MIXED,
        metrics=[
            {"metric": "native.exact_match"},
            {"metric": "native.json_schema", "params": {"schema": {"type": "integer"}}},
        ],
    )
    run_id = project.run()
    withheld = project.report(run_id, include_content=False)
    assert withheld["evidence"]["content"] == "withheld"
    for item in withheld["evidence"]["items"]:
        execution = item["execution"]
        assert execution["output_excerpt"] is None and execution["error_excerpt"] is None
        assert execution["retrieved_context_excerpts"] is None
        for result in item["results"]:
            assert result["reason_excerpt"] in (None, "execution_error") or ":" not in str(
                result["reason_excerpt"]
            )
    assert "case content withheld" in render(withheld, "html")
    full = project.report(run_id)
    fail = next(i for i in full["evidence"]["items"] if i["case_id"] == "fail-1")
    assert fail["execution"]["retrieved_context_excerpts"] == ["passage about no"]
    # raw evaluator output is never inlined: only an artifact ID with its digest
    raw = full["evidence"]["raw_artifacts"]
    assert raw, "json_schema failures record raw outputs"
    for ref in raw.values():
        assert set(ref) == {"digest", "mime_type", "size_bytes", "redaction"}
        assert ref["redaction"] == "restricted"
    referenced = {r["raw_artifact"] for i in full["evidence"]["items"] for r in i["results"]} - {
        None
    }
    assert referenced == set(raw)
    storage, artifacts = project.storage()
    try:
        contents = [artifacts.read_bytes(storage.get_artifact(a)).decode("utf-8") for a in raw]
    finally:
        storage.db.close()
    serialized = render(full, "json")
    marker = "Expecting value: line 1 column 1"  # the validator's message, only in raw output
    assert all(marker in c for c in contents)  # the raw content exists in the store...
    assert marker not in serialized  # ...and is not copied into the report


def test_no_content_withholds_case_derived_structured_metric_values() -> None:
    # Evaluator values are not limited to scores: custom evaluators can return explanations
    # or other case-derived text in a structured value.
    result = EvaluationResult(
        result_id="result-1",
        run_id="run-1",
        case_id="case-1",
        metric_id="custom.explainer",
        metric_version="1.0.0",
        status=ExecutionStatus.OK,
        decision=Decision.FAIL,
        value=MetricValue(
            kind="structured",
            value={"explanation": "PRIVATE CASE TEXT: account 12345"},
        ),
    )

    from aibench.services.reports import _evidence

    full = _evidence([], [result], {}, include_content=True)
    withheld = _evidence([], [result], {}, include_content=False)
    assert full[0]["results"][0]["value"] == {"explanation": "PRIVATE CASE TEXT: account 12345"}
    assert withheld[0]["results"][0]["value"] is None


# --------------------------------------------------------------------------- passes, gates


def test_a_rescoring_pass_is_reported_separately_and_is_not_run_spend(tmp_path: Path) -> None:
    project = Project(tmp_path, MIXED)
    run_id = project.run()
    storage, artifacts = project.storage()
    try:
        budget_before = run_budget(storage, artifacts, run_id)
    finally:
        storage.db.close()
    rescore = project.root / "rescore.json"
    rescore.write_text(
        json.dumps(
            {
                "plan_id": "rescore",
                "dataset": "data.jsonl",
                "application": "app.json",
                "metrics": [{"metric": "native.exact_match", "params": {"case_sensitive": False}}],
            }
        ),
        encoding="utf-8",
    )
    calls = project.calls()
    result = CliRunner().invoke(
        app, ["evaluate", run_id, "--plan", str(rescore), "--workspace", str(tmp_path), "--json"]
    )
    assert result.exit_code == 3, result.output
    outcome = json.loads(result.output)
    assert outcome["outcome"]["complete"] is False
    assert outcome["outcome"]["unhealthy_work"] == {"unavailable": 1}
    assert outcome["exit_code"] == 3
    assert project.calls() == calls  # the application was not invoked

    report = project.report(run_id)
    engine, rescored = report["scoring_passes"]
    assert engine["kind"] == "engine" and rescored["kind"] == "rescore"
    assert "without a recorded execution are unavailable" in rescored["basis"]
    [metric] = rescored["metrics"]
    assert metric["profile"]["source"] == "frozen_with_run"
    assert metric["profile"]["params"] == {"case_sensitive": False}
    assert metric["summary"]["selected"] == 4  # every recorded execution, incl. the crash
    # the engine pass is unchanged, and so are the run's own budget counters (06-T2 fix)
    assert engine == project.report(run_id)["scoring_passes"][0]
    storage, artifacts = project.storage()
    try:
        budget_after = run_budget(storage, artifacts, run_id)
    finally:
        storage.db.close()
    assert budget_after["evaluator"] == budget_before["evaluator"]


def test_runs_without_frozen_profiles_derive_them_and_say_so(tmp_path: Path) -> None:
    project = Project(tmp_path, MIXED)
    run_id = project.run()
    storage, artifacts = project.storage()
    try:
        row = storage.conn.execute("SELECT data FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        manifest = json.loads(row[0])
        del manifest["parameters"]["metric_profiles"]  # as recorded before Prompt 11
        storage.conn.execute(
            "UPDATE runs SET data = ? WHERE run_id = ?", (json.dumps(manifest), run_id)
        )
        storage.conn.commit()
        report = build_report(storage, artifacts, run_id)
    finally:
        storage.db.close()
    [metric] = report["scoring_passes"][0]["metrics"]
    assert metric["profile"]["source"] == "derived_from_results"
    assert metric["profile"]["aggregation"] == "rate"
    assert metric["summary"]["decisions"]["pass"] == 2
    assert any("derived from the stored results" in note for note in report["notes"])


def test_release_gates_use_selected_cases_and_drive_exit_code_1(tmp_path: Path) -> None:
    rows = _rows(("a", "yes", "yes"), ("b", "yes", "yes"), ("c", "no", "yes"))
    project = Project(
        tmp_path,
        rows,
        gates=[
            {"gate_id": "strict", "binding": 0, "min_pass_rate": 0.9},
            {"gate_id": "coverage", "binding": 0, "min_completed_coverage": 1.0},
        ],
    )
    cli = CliRunner()
    failed = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(project.plan_path),
            "--trust-local-app",
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    data = json.loads(failed.output)
    assert failed.exit_code == 1, failed.output  # complete, but a gate failed
    assert data["exit_code"] == 1 and data["outcome"]["gates_failed"] == ["strict"]
    strict, coverage = data["gates"]
    assert strict["pass_rate"] == round(2 / 3, 6) and strict["reason"] == "pass rate 2/3 below 0.9"
    assert coverage["status"] == "pass"

    project.write_plan(gates=[{"gate_id": "lenient", "binding": 0, "min_pass_rate": 0.6}])
    passed = cli.invoke(
        app,
        [
            "run",
            "--plan",
            str(project.plan_path),
            "--trust-local-app",
            "--workspace",
            str(tmp_path),
        ],
    )
    assert passed.exit_code == 0, passed.output
    assert "gate lenient: pass" in passed.output


def test_a_lost_observation_can_only_fail_a_gate(tmp_path: Path) -> None:
    # 2 of 3 pass; the third case's app crashes. Against completed cases alone the pass
    # rate would be 2/2; against selected cases it is 2/3, which fails a 0.9 gate.
    rows = _rows(("a", "yes", "yes"), ("b", "yes", "yes"), ("c", "crash", "yes"))
    project = Project(tmp_path, rows, gates=[{"gate_id": "g", "binding": 0, "min_pass_rate": 0.9}])
    report = project.report(project.run())
    [gate] = report["gates"]
    assert gate["status"] == "fail" and gate["passes"] == 2 and gate["selected"] == 3


def test_gates_must_name_an_existing_binding(tmp_path: Path) -> None:
    from pydantic import ValidationError

    from aibench.core.plans import ExecutablePlan

    base = {
        "plan_id": "p",
        "dataset": "d",
        "application": "a",
        "metrics": [{"metric": "native.exact_match"}],
    }
    with pytest.raises(ValidationError, match="refers to metrics"):
        ExecutablePlan.model_validate(
            {**base, "gates": [{"gate_id": "g", "binding": 1, "min_pass_rate": 1}]}
        )
    with pytest.raises(ValidationError, match="needs min_pass_rate"):
        ExecutablePlan.model_validate({**base, "gates": [{"gate_id": "g", "binding": 0}]})
    with pytest.raises(ValidationError, match="duplicate gate_id"):
        ExecutablePlan.model_validate(
            {**base, "gates": [{"gate_id": "g", "binding": 0, "min_pass_rate": 1}] * 2}
        )


def test_report_facts_copy_numbers_without_recomputing(tmp_path: Path) -> None:
    project = Project(tmp_path, MIXED)
    report = project.report(project.run())
    facts = report_facts(report)
    [metric] = facts["metrics"]
    summary = report["scoring_passes"][0]["metrics"][0]["summary"]
    for key in ("selected", "completed", "decisions", "unavailable", "pending"):
        assert metric[key] == summary[key]
    assert facts["non_passing_cases"] == {"total": 2, "first": ["crash-1 r0", "fail-1 r0"]}
    # the run finished normally; its failed application item shows in the outcome instead
    assert facts["provisional"] is False and facts["partial"] is False
    assert facts["outcome"]["complete"] is False
    assert facts["outcome"]["unhealthy_work"] == {"failed": 1}
