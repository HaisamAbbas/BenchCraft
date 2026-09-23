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
