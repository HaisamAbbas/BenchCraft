"""Local integration trials of the two pilot recipes (13-T3).

Each test follows its recipe in `docs/pilot/` command by command, through the real CLI,
against a local stand-in for the pilot team's application: the example HTTP RAG service
and the example command-line assistant. They prove the recipes work as written on this
machine. They are not real-team trials, which stay pending until observed."""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app

REPO = Path(__file__).resolve().parents[1]
PILOT = REPO / "examples" / "pilot"
cli = CliRunner()


def _ok(args: list[str], code: int = 0) -> str:
    result = cli.invoke(app, args)
    assert result.exit_code == code, f"{args}: {result.output}"
    return result.stdout


def _attempts(workspace: Path) -> int:
    with sqlite3.connect(workspace / ".aibench" / "aibench.db") as conn:
        return int(conn.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0])


def test_recipe_a_benchmarks_a_running_http_rag_service(tmp_path: Path) -> None:
    from tests.runner_support import load_example

    server = load_example("http_rag_app").make_server(port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        project = tmp_path / "rag-bench"
        shutil.copytree(PILOT / "http-rag", project)
        # Recipe step 1: point app.json at the service (here: the port the stand-in got).
        config = json.loads((project / "app.json").read_text(encoding="utf-8"))
        base = f"http://127.0.0.1:{server.server_port}"
        config["target"] = config["transport"]["url"] = f"{base}/answer"
        config["transport"]["healthcheck_url"] = f"{base}/health"
        (project / "app.json").write_text(json.dumps(config), encoding="utf-8")
        ws = ["--workspace", str(project)]

        _ok(["dataset", "validate", str(project / "dataset.jsonl")])
        described = _ok(["app", "describe", str(project / "app.json")])
        assert "retrieved_context: declared" in described
        smoke = _ok(
            [
                "app",
                "smoke",
                str(project / "app.json"),
                "--dataset",
                str(project / "dataset.jsonl"),
                "--limit",
                "2",
                *ws,
            ]
        )
        assert "healthcheck: healthy" in smoke and "2 ok, 0 failed" in smoke

        # The plan's release gate needs 90%; the service misses one question: exit 1.
        run = json.loads(
            _ok(
                [
                    "run",
                    "--plan",
                    str(project / "plan.json"),
                    "--policy",
                    str(project / "policy.json"),
                    "--json",
                    *ws,
                ],
                code=1,
            )
        )
        [gate] = run["gates"]
        assert (gate["status"], gate["passes"], gate["selected"]) == ("fail", 5, 6)

        report = _ok(["report", run["run_id"], "--format", "markdown", "--out", "-", *ws])
        late = report[report.index("### late-return") :]
        assert "retrieved (1 of 1): We ship to over 40 countries" in late  # the evidence
        assert (project / ".aibench").is_dir()
    finally:
        server.shutdown()
        server.server_close()


def test_recipe_b_scores_a_cli_assistant_with_the_teams_own_oracle(tmp_path: Path) -> None:
    recipe = PILOT / "cli-assistant"
    ws = ["--workspace", str(tmp_path)]
    smoke = _ok(
        [
            "app",
            "smoke",
            str(recipe / "app.json"),
            "--dataset",
            str(recipe / "dataset.jsonl"),
            "--limit",
            "1",
            "--trust-local-app",
            *ws,
        ]
    )
    assert "1 ok, 0 failed" in smoke

    run = json.loads(
        _ok(
            [
                "run",
                "--plan",
                str(recipe / "plan.json"),
                "--policy",
                str(recipe / "policy.json"),
                "--json",
                *ws,
            ]
        )
    )
    run_id = run["run_id"]
    assert run["counts"]["execution"] == {"succeeded": 6}

    # The team's domain oracle, applied to the stored answers: no application call.
    before = _attempts(tmp_path)
    scored = json.loads(
        _ok(
            [
                "score",
                run_id,
                "--metrics",
                str(recipe / "oracle.metrics.json"),
                "--custom-evaluator",
                str(REPO / "examples" / "evaluators" / "refund_window.py"),
                "--trust-local-code",
                "--json",
                *ws,
            ]
        )
    )
    [summary] = scored["summaries"]
    assert summary["value_summary"]["counts"] == {"correct": 3, "no_window_stated": 1}
    assert (summary["completed"], summary["not_applicable"]) == (4, 2)
    assert _attempts(tmp_path) == before == 7  # 6 run + 1 smoke; scoring added none

    report = _ok(["report", run_id, "--format", "markdown", "--out", "-", *ws])
    assert "## Metrics: rescore scoring pass" in report
    assert "no\\_window\\_stated: 1" in report
