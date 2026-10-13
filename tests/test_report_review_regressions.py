"""Regressions for the Prompt 11 independent review (ADR 0010, "Changes after independent
review"). Each test reproduces a confirmed finding and pins the fix."""

from __future__ import annotations

import io
import json
from pathlib import Path

from rich.console import Console
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.conversation.agent import TurnOutcome, _status_line, check_claims
from aibench.core.models import Decision, EvaluationResult, ExecutionStatus
from aibench.reporting.aggregation import reason_code, summarize
from aibench.reporting.render import render
from aibench.services.reports import report_facts
from aibench.tui import render as tui_render
from tests.test_reports import MIXED, Project, _rows

cli = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_APPS = REPO_ROOT / "examples" / "apps"
SUPPORT = str(REPO_ROOT / "examples" / "datasets" / "support.valid.jsonl")


# --------------------------------------------------------------------------- major


def test_a_rescore_keeps_work_that_never_ran_in_its_denominator(tmp_path: Path) -> None:
    # Review 1: a budget stops the run after 2 of 4 executions; rescoring the 2 recorded
    # outputs must not read as "2/2 = 100%".
    project = Project(
        tmp_path,
        _rows(("a", "yes", "yes"), ("b", "yes", "yes"), ("c", "yes", "yes"), ("d", "yes", "yes")),
        budgets={"max_application_calls": 2},
    )
    run_id = project.run()
    rescore = tmp_path / "rescore.json"
    rescore.write_text(
        json.dumps(
            {
                "plan_id": "r",
                "dataset": "data.jsonl",
                "application": "app.json",
                "metrics": [{"metric": "native.exact_match", "params": {"case_sensitive": False}}],
            }
        ),
        encoding="utf-8",
    )
    result = cli.invoke(
        app, ["evaluate", run_id, "--plan", str(rescore), "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 3, result.output  # incomplete rescoring is an unsuccessful run
    engine, rescored = project.report(run_id)["scoring_passes"]
    [metric] = rescored["metrics"]
    s = metric["summary"]
    assert s["selected"] == 4 and s["completed"] == 2 and s["unavailable"] == 2
    assert s["reasons"]["not_executed"] == 2 and s["completed_coverage"] == 0.5
    assert "without a recorded execution are unavailable" in rescored["basis"]
    assert engine["metrics"][0]["summary"]["selected"] == 4


def _result(reason: str, status: ExecutionStatus = ExecutionStatus.ERROR) -> EvaluationResult:
    return EvaluationResult(
        result_id="x",
        run_id="r",
        case_id="c1",
        metric_id="custom.m",
        metric_version="1.0.0",
        status=status,
        decision=Decision.NOT_EVALUATED,
        reason=reason,
    )


def test_free_text_reasons_never_become_codes() -> None:
    # Review 2: a reason without a colon was shown whole as its "code".
    assert reason_code("the app answered <output> which is private") is None
    assert reason_code("timeout:evaluation exceeded 60s") == "timeout"
    assert reason_code("missing:execution.output") == "missing"
    from aibench.evaluators.native import ExactMatch

    summary = summarize(
        [_result("the app answered secret-output which is private")],
        manifest=ExactMatch.manifest,
        binding_hash="sha256:" + "0" * 64,
    )
    assert summary.reasons == {"unclassified": 1}


def test_doctor_never_prints_credentials_from_a_provider_config(tmp_path: Path) -> None:
    # Review 3: userinfo in base_url and a literal key echoed by the validation error.
    with_userinfo = tmp_path / "p1.json"
    with_userinfo.write_text(
        json.dumps(
            {
                "base_url": "https://bob:hunter2PlainPassword@api.example.com/v1?key=q1w2e3",
                "model": "m",
            }
        ),
        encoding="utf-8",
    )
    literal_key = tmp_path / "p2.json"
    literal_key.write_text(
        json.dumps(
            {
                "base_url": "https://api.example.com/v1",
                "model": "m",
                "api_key": "MyLiteralToken12345xyz",
            }
        ),
        encoding="utf-8",
    )
    for config in (with_userinfo, literal_key):
        for extra in ([], ["--json"]):
            result = cli.invoke(
                app,
                ["doctor", "--project", str(tmp_path), "--provider-config", str(config), *extra],
            )
            for secret in ("hunter2PlainPassword", "bob:", "q1w2e3", "MyLiteralToken12345xyz"):
                assert secret not in result.output, (config.name, result.output)
    shown = cli.invoke(
        app, ["doctor", "--project", str(tmp_path), "--provider-config", str(with_userinfo)]
    )
    assert "https://api.example.com/v1" in shown.output


FACTS = json.dumps(
    {
        "metrics": [
            {
                "metric": "native.exact_match@1.0.0",
                "selected": 10,
                "completed": 9,
                "decisions": {"pass": 8, "fail": 1, "indeterminate": 0, "not_evaluated": 1},
                "unavailable": 1,
            }
        ],
        "latency_ms": {"p50_ms": 1152.7, "successful_requests": 9},
    }
)


def test_the_claim_check_catches_invented_numbers_the_review_found() -> None:
    # Review 4: units escaped the check; any ratio of two counts "verified" a percentage;
    # the user's own question counted as a source.
    sources = [("user message", "Did 97% pass?"), ("get_report", FACTS)]
    claims, unverified = check_claims(
        "Yes, 97% passed. p50 latency was 999ms, it took 42s and improved 7x; "
        "50% were retrieval errors, 11% timed out and 100% completed.",
        sources,
    )
    assert unverified == ["97%", "999ms", "42s", "7x", "50%", "11%", "100%"]
    assert claims == []
    good, bad = check_claims(
        "8 of 10 passed (80%), 90% completed, p50 was 1152.7ms, 10% unavailable.", sources
    )
    assert bad == []
    assert {c["number"] for c in good} == {"8", "10", "80%", "90%", "1152.7ms", "10%"}


def test_chat_run_starts_a_clear_request_without_second_confirmation(
    tmp_path: Path,
) -> None:
    # `/run` is an explicit bounded request and starts the validated draft directly.
    project = tmp_path / "q"
    assert cli.invoke(app, ["init", str(project)]).exit_code == 0
    first = cli.invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--new",
            "--objective",
            "answers are correct",
            "--send",
            "/run",
            "--json",
        ],
    )
    data = json.loads(first.stdout.strip().splitlines()[-1])
    assert data["kind"] == "action" and data["ok"] is True
    assert data["data"]["state"] == "done"
    assert data["runs"]
    assert data["runs"][0]["run_id"] == data["data"]["run_id"]
    assert first.exit_code != 4  # the request was authorized and execution started


# --------------------------------------------------------------------------- minor


def _smoke_and_score_twice(workspace: Path) -> str:
    smoke = cli.invoke(
        app,
        [
            "app",
            "smoke",
            str(EXAMPLE_APPS / "cli_chatbot.app.json"),
            "--dataset",
            SUPPORT,
            "--workspace",
            str(workspace),
            "--trust-local-app",
            "--json",
        ],
    )
    assert smoke.exit_code == 0, smoke.output
    run_id = json.loads(smoke.output)["run_id"]
    metrics = workspace / "m.json"
    metrics.write_text(
        json.dumps({"metrics": [{"metric": "native.exact_match"}]}), encoding="utf-8"
    )
    for _ in range(2):
        scored = cli.invoke(
            app, ["score", run_id, "--metrics", str(metrics), "--workspace", str(workspace)]
        )
        assert scored.exit_code == 0, scored.output
    return run_id


def test_runs_without_an_engine_pass_report_one_named_pass_and_record_profiles(
    tmp_path: Path,
) -> None:
    # Review 6, 7, 14: evidence merged every pass, the cost row was the first pass, the
    # "derived profile" note was wrong for a fresh `aibench score`, and "Plan None".
    run_id = _smoke_and_score_twice(tmp_path)
    result = cli.invoke(
        app, ["report", run_id, "--workspace", str(tmp_path), "--format", "json", "--out", "-"]
    )
    report = json.loads(result.output)
    first, second = report["scoring_passes"]
    assert report["evidence"]["scoring_id"] == second["scoring_id"]  # the latest pass
    assert report["cost"]["evaluator_scoring_id"] == second["scoring_id"]
    items = report["evidence"]["items"]
    assert len({i["case_id"] for i in items}) == len(items)  # each failure once
    assert all(
        m["profile"]["source"] == "frozen_with_run" for p in (first, second) for m in p["metrics"]
    )
    assert not any("derived" in note for note in report["notes"])
    markdown = render(report, "markdown")
    assert "not a plan run" in markdown and "None (" not in markdown
    assert f"Evaluator (scoring pass {second['scoring_id']})" in markdown


def test_terminal_report_never_shows_missing_accounting_as_zero(tmp_path: Path) -> None:
    # Review 8: "at least USD 0 (unknown)", "USD 0 (complete)" with no calls, "p50 None ms".
    project = Project(tmp_path, _rows(("a", "crash", "yes"), ("b", "crash", "yes")))
    facts = report_facts(project.report(project.run()))
    buffer = io.StringIO()
    tui_render.report(Console(file=buffer, force_terminal=False, width=200), facts)
    text = buffer.getvalue()
    assert "application cost: unknown (no call reported its cost)" in text
    assert "evaluator cost: no calls" in text
    assert "not measured (no successful request)" in text
    assert "None" not in text and "USD 0" not in text
    assert "cancelled 0" in text


def test_run_dir_stores_the_run_in_that_project(tmp_path: Path, monkeypatch) -> None:
    # Review 9: `aibench run DIR` created the workspace in the current directory.
    project = tmp_path / "proj"
    assert cli.invoke(app, ["init", str(project)]).exit_code == 0
    monkeypatch.chdir(tmp_path)
    result = cli.invoke(app, ["run", str(project), "--json"])
    assert result.exit_code == 3, result.output
    assert (project / ".aibench").is_dir() and not (tmp_path / ".aibench").exists()


def test_init_on_a_file_is_invalid_input_not_a_crash(tmp_path: Path) -> None:
    # Review 10: a traceback and exit 1 (which means "gate failed").
    target = tmp_path / "afile.txt"
    target.write_text("x", encoding="utf-8")
    result = cli.invoke(app, ["init", str(target)])
    assert result.exit_code == 2 and "not a directory" in result.output
    assert target.read_text(encoding="utf-8") == "x"


def test_run_output_sanitizes_gate_ids(tmp_path: Path) -> None:
    # Review 11: a gate_id with terminal escapes reached the terminal raw.
    project = Project(
        tmp_path,
        MIXED,
        gates=[{"gate_id": "g\x1b]0;pwned\x07\x1b[31mRED", "binding": 0, "min_pass_rate": 0.1}],
    )
    result = cli.invoke(
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
    assert "\x1b" not in result.output and "gate g" in result.output


def test_status_line_does_not_call_a_finished_run_a_snapshot() -> None:
    # Review 12.
    outcome = TurnOutcome(turn_id="t", replies_to="u")
    outcome.results = [
        {
            "tool": "get_report",
            "run_id": "r",
            "status": "cancelled",
            "provisional": False,
            "partial": True,
        }
    ]
    line = _status_line(outcome, revision=1)
    assert "results are partial: run r ended cancelled" in line and "snapshot" not in line


# --------------------------------------------------------------------------- nits, suspected


def test_benchmark_default_output_does_not_replace_the_project_plan(
    tmp_path: Path, monkeypatch
) -> None:
    # Review 17.
    project = tmp_path / "q"
    assert cli.invoke(app, ["init", str(project)]).exit_code == 0
    before = (project / "plan.json").read_text(encoding="utf-8")
    monkeypatch.chdir(project)  # the default --out is relative to the working directory
    cli.invoke(
        app,
        [
            "benchmark",
            str(project / "support.app.json"),
            "--dataset",
            str(project / "dataset.jsonl"),
            "--objective",
            "answers are correct",
            "--policy",
            str(project / "policy.json"),
            "--non-interactive",
            "--workspace",
            str(project),
        ],
        catch_exceptions=False,
    )
    assert (project / "plan.json").read_text(encoding="utf-8") == before
    assert (project / "benchmark.plan.json").is_file()


def test_a_pass_rate_gate_needs_a_decision_rule(tmp_path: Path) -> None:
    # Suspected 1: without a rule every decision is indeterminate; the gate could only fail.
    from aibench.core.models import EvaluatorManifest, MetricDirection
    from aibench.engine.compile import analyze_plan, load_plan
    from aibench.evaluators.protocol import EvaluationOutcome, Evaluator
    from aibench.registry import EvaluatorRegistry
    from aibench.security.policy import ExecutionPolicy

    class Score(Evaluator):  # a scalar score with no default pass/fail rule
        manifest = EvaluatorManifest(
            evaluator_id="test.score",
            version="1.0.0",
            plugin_id="test",
            plugin_version="0",
            description="a score",
            value_kind="scalar",
            direction=MetricDirection.HIGHER,
            aggregation="mean",
        )

        async def evaluate(self, view, ctx):  # type: ignore[no-untyped-def]
            return EvaluationOutcome.ok("scalar", 0.5)

    project = Project(tmp_path, [{"case_id": "a", "input": "x", "expected_output": "x"}])
    registry = EvaluatorRegistry.with_native()
    registry.register(Score)
    policy = ExecutionPolicy(allowed_evaluators=("*",))

    def findings(**binding: object) -> list[str]:
        plan = json.loads(project.plan_path.read_text(encoding="utf-8"))
        plan["metrics"] = [{"metric": "test.score", **binding}]
        plan["gates"] = [{"gate_id": "g", "binding": 0, "min_pass_rate": 0.5}]
        project.plan_path.write_text(json.dumps(plan), encoding="utf-8")
        analysis = analyze_plan(
            load_plan(project.plan_path),
            tmp_path,
            policy=policy,
            trusted_local=True,
            registry=registry,
        )
        return [f.message for f in analysis.blocking()]

    assert any("has no pass/fail rule" in m for m in findings())
    assert not any(
        "pass/fail rule" in m for m in findings(rule={"comparator": ">=", "threshold": 0.5})
    )


def test_export_needs_an_export_verb_and_a_report_object() -> None:
    from aibench.conversation.agent import _EXPORT_OBJECTS, _EXPORT_VERBS

    for quote in ("write it down", "save my settings", "json is fine"):
        assert not (_EXPORT_VERBS.search(quote) and _EXPORT_OBJECTS.search(quote)), quote
    assert _EXPORT_VERBS.search("export the report") and _EXPORT_OBJECTS.search("export the report")


def test_error_results_are_not_claim_sources(tmp_path: Path) -> None:
    # Suspected 3: the text of a tool error ("no installed evaluator '404'") verified "404".
    import asyncio

    from aibench.conversation.agent import ConversationAgent
    from tests.session_support import ScriptedProvider, SessionHarness, call, say

    ctl = SessionHarness(tmp_path).open_session({"a": "hi"}, objectives=("catch wrong answers",))
    provider = ScriptedProvider(
        [call("describe_evaluator", metric="404"), say("There were 404 failures.")]
    )
    try:
        outcome = asyncio.run(ConversationAgent(ctl, provider).handle_message("describe it"))
    finally:
        ctl.storage.db.close()
    assert outcome.unverified_numbers == ["404"]
