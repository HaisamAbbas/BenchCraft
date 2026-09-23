"""09-T1: default entry guidance and the non-TTY JSON chat surface."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from aibench.cli.main import app
from tests.planning_support import write_app, write_dataset

runner = CliRunner()


def _project(root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    app = write_app(root)
    dataset = write_dataset(root, [{"case_id": "one", "input": "hello"}])
    return app, dataset


def test_bare_non_tty_invocation_prints_command_guidance() -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == 0
    assert "No interactive terminal" in result.stdout
    assert "aibench chat --send TEXT --json" in result.stdout


def test_chat_without_tty_or_send_fails_with_actionable_guidance() -> None:
    result = runner.invoke(app, ["chat", "--project", "."])
    assert result.exit_code == 2
    assert "needs an interactive terminal" in result.output
    assert "--send TEXT" in result.output and "--json" in result.output


def test_non_tty_chat_json_is_machine_readable_and_can_resume(tmp_path: Path) -> None:
    project = tmp_path / "project"
    application, dataset = _project(project)
    first = runner.invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--app",
            str(application),
            "--dataset",
            str(dataset),
            "--new",
            "--send",
            "/help",
            "--json",
        ],
    )
    assert first.exit_code == 0, first.stdout
    first_payload = json.loads(first.stdout)
    session_id = first_payload["session_id"]
    assert first_payload["command"] == "/help"
    assert "/status" in first_payload["data"]["commands"]

    resumed = runner.invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--resume",
            session_id,
            "--send",
            "/sessions",
            "--json",
        ],
    )
    assert resumed.exit_code == 0, resumed.stdout
    resumed_payload = json.loads(resumed.stdout)
    assert resumed_payload["session_id"] == session_id
    assert resumed_payload["kind"] == "sessions"


def test_chat_closes_provider_when_command_finishes(tmp_path: Path, monkeypatch) -> None:
    import aibench.cli.chat as chat_cli

    project = tmp_path / "project"
    application, dataset = _project(project)

    class Provider:
        name = "test-provider"
        model = "test-model"

        def __init__(self) -> None:
            self.closed = False

        def complete(self, messages, tools):
            raise AssertionError("slash command must not call the provider")

        def close(self) -> None:
            self.closed = True

    provider = Provider()
    monkeypatch.setattr(chat_cli, "open_provider", lambda *_: (provider, []))
    result = runner.invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--app",
            str(application),
            "--dataset",
            str(dataset),
            "--new",
            "--provider-config",
            str(project / "provider.json"),
            "--send",
            "/help",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert provider.closed
