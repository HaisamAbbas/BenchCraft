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


def test_chat_send_json_sanitizes_command_data(tmp_path: Path, monkeypatch) -> None:
    from aibench.tui.commands import CommandResult, Commands

    project = tmp_path / "project"
    application, dataset = _project(project)

    async def hostile_command(self, text):
        return CommandResult(
            text,
            "report",
            {
                "credential": "sk-abcdefghijklmnopqrstuvwx",
                "message": "before\x1b[2Jafter",
            },
        )

    monkeypatch.setattr(Commands, "run", hostile_command)
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
            "--send",
            "/report",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["data"]["credential"] == "[redacted]"
    assert payload["data"]["message"] == "beforeafter"


def test_new_chat_reuses_the_only_compatible_policy_approved_dataset(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    application = write_app(project)
    (project / "datasets").mkdir()
    dataset = write_dataset(
        project,
        [{"case_id": "one", "input": "private input", "reference": {"answer": "private answer"}}],
        name="datasets/support.jsonl",
    )
    policy = project / "policy.json"
    policy.write_text(json.dumps({"inspection_roots": ["."]}), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--app",
            str(application),
            "--policy",
            str(policy),
            "--new",
            "--send",
            "/help",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["command"] == "/help"
    assert any("only compatible dataset" in item for item in payload["dataset_selection"])
    assert str(dataset.resolve()) not in result.stdout
    assert "private input" not in result.stdout and "private answer" not in result.stdout


def test_noninteractive_new_chat_asks_for_material_dataset_ambiguity(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    application = write_app(project)
    (project / "datasets").mkdir()
    write_dataset(project, [{"case_id": "one", "input": "q1"}], name="datasets/first.jsonl")
    write_dataset(project, [{"case_id": "two", "input": "q2"}], name="datasets/second.jsonl")
    policy = project / "policy.json"
    policy.write_text(json.dumps({"inspection_roots": ["."]}), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "chat",
            "--project",
            str(project),
            "--app",
            str(application),
            "--policy",
            str(policy),
            "--new",
            "--send",
            "/help",
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "Which dataset should be used?" in result.output
    assert "datasets/first.jsonl" in result.output
    assert "datasets/second.jsonl" in result.output
    assert "--dataset PATH" in result.output
