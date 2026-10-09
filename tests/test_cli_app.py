"""`aibench app describe` / `aibench app smoke` end to end through the Typer app (03-T4)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from tests.runner_support import EXAMPLE_APPS, MISBEHAVING, REPO_ROOT

cli = CliRunner()
CHATBOT_APP = str(EXAMPLE_APPS / "cli_chatbot.app.json")
CHAT_DATA = str(REPO_ROOT / "examples" / "datasets" / "chatbot.valid.jsonl")


def test_describe_reports_the_observability_gap() -> None:
    result = cli.invoke(app, ["app", "describe", str(EXAMPLE_APPS / "http_rag.app.json"), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["observable"]["retrieved_context"] == "declared"
    assert data["observable"]["usage"] == "unknown"
    assert data["observable"]["http_status"] == "observed"


def test_smoke_refuses_local_execution_without_explicit_trust(tmp_path: Path) -> None:
    result = cli.invoke(
        app, ["app", "smoke", CHATBOT_APP, "--dataset", CHAT_DATA, "--workspace", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "trusted-local" in result.output


def test_smoke_runs_records_and_is_visible_to_runs_commands(tmp_path: Path) -> None:
    result = cli.invoke(
        app,
        [
            "app",
            "smoke",
            CHATBOT_APP,
            "--dataset",
            CHAT_DATA,
            "--workspace",
            str(tmp_path),
            "--trust-local-app",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["mode"] == "developer_smoke" and data["status"] == "completed"
    assert [r["status"] for r in data["results"]] == ["ok", "ok"]
    shown = cli.invoke(
        app, ["runs", "show", data["run_id"], "--workspace", str(tmp_path), "--json"]
    )
    assert shown.exit_code == 0 and json.loads(shown.output)["status"] == "completed"


def test_smoke_freezes_changed_application_revisions_for_scoring(tmp_path: Path) -> None:
    script = tmp_path / "revisioned_app.py"
    script.write_text(
        "import json, sys\n"
        "json.load(sys.stdin)\n"
        "print(json.dumps({'output': 'answer', 'tool_events': []}))\n",
        encoding="utf-8",
    )
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(
        json.dumps(
            {
                "case_id": "case-1",
                "input": "question",
                "reference": {
                    "answer": "answer",
                    "tools": {"tool_names": [], "match_mode": "exact"},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    config = tmp_path / "revisioned.app.json"

    def write_config(revision: str, *, exposes_tool_events: bool) -> None:
        output_binding = {"output": "/output"}
        if exposes_tool_events:
            output_binding["tool_events"] = "/tool_events"
        config.write_text(
            json.dumps(
                {
                    "application_id": "revisioned",
                    "revision": revision,
                    "runner": "cli",
                    "target": str(script),
                    "output_binding": output_binding,
                    "transport": {
                        "kind": "cli",
                        "argv": [sys.executable, str(script)],
                        "timeout_seconds": 10,
                    },
                }
            ),
            encoding="utf-8",
        )

    def smoke() -> str:
        result = cli.invoke(
            app,
            [
                "app",
                "smoke",
                str(config),
                "--dataset",
                str(dataset),
                "--workspace",
                str(tmp_path),
                "--trust-local-app",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        return json.loads(result.output)["run_id"]

    write_config("revision-1", exposes_tool_events=False)
    first_run = smoke()
    write_config("revision-2", exposes_tool_events=True)
    second_run = smoke()

    from aibench.core.models import deep_unfreeze
    from aibench.storage.artifacts import ArtifactStore
    from aibench.storage.db import Database, Workspace
    from aibench.storage.repositories import Storage

    workspace = Workspace.at(tmp_path)
    storage = Storage(Database.open_workspace(workspace))
    artifacts = ArtifactStore(workspace.artifacts_dir, create=False)
    run_records = {}
    try:
        catalog_spec = storage.get_application("revisioned")
        assert catalog_spec is not None and catalog_spec.revision == "revision-1"
        for run_id, expected_revision in (
            (first_run, "revision-1"),
            (second_run, "revision-2"),
        ):
            record = storage.get_run(run_id)
            assert record is not None
            run_records[run_id] = record
            artifact_id = (deep_unfreeze(record.manifest.parameters) or {}).get(
                "application_artifact_id"
            )
            assert isinstance(artifact_id, str)
            artifact_ref = storage.get_artifact(artifact_id)
            assert artifact_ref is not None
            frozen = json.loads(artifacts.read_bytes(artifact_ref))
            assert frozen["revision"] == expected_revision
        assert (
            run_records[first_run].manifest.application_hash
            != run_records[second_run].manifest.application_hash
        )
    finally:
        storage.db.close()

    for run_id, expected_revision in (
        (first_run, "revision-1"),
        (second_run, "revision-2"),
    ):
        report = cli.invoke(
            app,
            [
                "report",
                run_id,
                "--format",
                "json",
                "--out",
                "-",
                "--workspace",
                str(tmp_path),
            ],
        )
        assert report.exit_code == 0, report.output
        assert json.loads(report.output)["run"]["application_revision"] == expected_revision

    metrics = tmp_path / "tool-calls.metrics.json"
    metrics.write_text(
        json.dumps({"metrics": [{"metric": "native.tool_calls"}]}), encoding="utf-8"
    )
    current_revision_score = cli.invoke(
        app,
        ["score", second_run, "--metrics", str(metrics), "--workspace", str(tmp_path), "--json"],
    )
    assert current_revision_score.exit_code == 0, current_revision_score.output

    old_revision_score = cli.invoke(
        app,
        ["score", first_run, "--metrics", str(metrics), "--workspace", str(tmp_path), "--json"],
    )
    assert old_revision_score.exit_code == 2
    assert "output_binding.tool_events is not declared" in old_revision_score.output

    metrics.write_text(
        json.dumps({"metrics": [{"metric": "native.exact_match"}]}), encoding="utf-8"
    )
    first_revision_score = cli.invoke(
        app,
        [
            "score",
            first_run,
            "--metrics",
            str(metrics),
            "--workspace",
            str(tmp_path),
            "--json",
        ],
    )
    assert first_revision_score.exit_code == 0, first_revision_score.output


def test_smoke_exits_nonzero_when_an_invocation_fails(tmp_path: Path) -> None:
    config = tmp_path / "failing.app.json"
    config.write_text(
        json.dumps(
            {
                "application_id": "failing",
                "runner": "cli",
                "target": "misbehaving",
                "transport": {
                    "kind": "cli",
                    "argv": [sys.executable, str(MISBEHAVING), "exit", "5"],
                },
            }
        ),
        encoding="utf-8",
    )
    result = cli.invoke(
        app,
        [
            "app",
            "smoke",
            str(config),
            "--dataset",
            CHAT_DATA,
            "--case",
            "chat-002",
            "--workspace",
            str(tmp_path),
            "--trust-local-app",
        ],
    )
    assert result.exit_code == 1
    assert "nonzero_exit" in result.output
    assert "0 ok, 1 failed" in result.output


def test_untrusted_output_cannot_inject_terminal_control_or_markup(tmp_path: Path) -> None:
    script = tmp_path / "hostile.py"
    script.write_text(
        "import json\nprint(json.dumps({'output': '\\x1b[31m[bold red]FAKE PASS[/bold red]\\x07'}))\n",
        encoding="utf-8",
    )
    config = tmp_path / "hostile.app.json"
    config.write_text(
        json.dumps(
            {
                "application_id": "hostile",
                "runner": "cli",
                "target": "hostile",
                "transport": {"kind": "cli", "argv": [sys.executable, str(script)]},
            }
        ),
        encoding="utf-8",
    )
    result = cli.invoke(
        app,
        [
            "app",
            "smoke",
            str(config),
            "--dataset",
            CHAT_DATA,
            "--limit",
            "1",
            "--workspace",
            str(tmp_path),
            "--trust-local-app",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "\x1b" not in result.output and "\x07" not in result.output
    unwrapped = " ".join(result.output.split())  # Rich wraps long lines at console width
    assert "[bold red]FAKE PASS[/bold red]" in unwrapped  # shown literally, not styled


def test_smoke_reports_unknown_case_ids(tmp_path: Path) -> None:
    result = cli.invoke(
        app,
        [
            "app",
            "smoke",
            CHATBOT_APP,
            "--dataset",
            CHAT_DATA,
            "--case",
            "nope",
            "--workspace",
            str(tmp_path),
            "--trust-local-app",
        ],
    )
    assert result.exit_code == 2
    assert "nope" in result.output
