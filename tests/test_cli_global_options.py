"""Root-level scripting/config options propagate through nested CLI commands."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

from aibench.cli.main import app

cli = CliRunner()


def _world_app(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "application_id": "global-options",
                "runner": "http",
                "target": "http://127.0.0.1:8765/chat",
                "transport": {"kind": "http", "url": "http://127.0.0.1:8765/chat"},
                "test_worlds": {"demo": {"seed": {"state": "clean"}}},
            }
        ),
        encoding="utf-8",
    )
    return path


def test_root_json_and_policy_propagate_and_local_policy_wins(tmp_path: Path) -> None:
    application = _world_app(tmp_path / "app.json")
    allowed = tmp_path / "allowed.json"
    allowed.write_text(json.dumps({"allowed_test_worlds": ["global-options:demo"]}), encoding="utf-8")
    denied = tmp_path / "denied.json"
    denied.write_text(json.dumps({"allowed_test_worlds": []}), encoding="utf-8")

    inherited = cli.invoke(
        app,
        ["--json", "--policy", str(allowed), "app", "describe", str(application)],
    )
    assert inherited.exit_code == 0, inherited.output
    assert json.loads(inherited.output)["test_worlds"][0]["approved"] is True

    overridden = cli.invoke(
        app,
        [
            "--json",
            "--policy",
            str(allowed),
            "app",
            "describe",
            str(application),
            "--policy",
            str(denied),
        ],
    )
    assert overridden.exit_code == 0, overridden.output
    assert json.loads(overridden.output)["test_worlds"][0]["approved"] is False


def test_root_config_overrides_discovery_and_root_policy_overrides_config(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    discovered = project / "aibench.json"
    discovered.write_text(json.dumps({"policy_path": "discovered-policy.json"}), encoding="utf-8")
    selected = project / "selected.json"
    selected.write_text(json.dumps({"policy_path": "configured-policy.json"}), encoding="utf-8")
    for name in ("configured-policy.json", "discovered-policy.json", "cli-policy.json"):
        (project / name).write_text("{}", encoding="utf-8")

    result = cli.invoke(
        app,
        [
            "--config",
            str(selected),
            "--policy",
            str(project / "cli-policy.json"),
            "--json",
            "doctor",
            "--project",
            str(project),
        ],
    )
    assert result.exit_code == 0, result.output
    checks = {check["name"]: check["detail"] for check in json.loads(result.output)["checks"]}
    assert checks["config"] == str(selected)
    assert checks["policy"] == str(project / "cli-policy.json")


def test_non_interactive_setup_fails_without_prompting(monkeypatch) -> None:
    from aibench import userconfig

    def unexpected_setup(*_args, **_kwargs):
        raise AssertionError("interactive setup must not start")

    monkeypatch.setattr(userconfig, "run_setup", unexpected_setup)
    result = cli.invoke(app, ["--non-interactive", "setup"])
    assert result.exit_code == 2
    assert "setup is interactive" in result.output


def test_non_interactive_mode_prevents_chat_even_when_a_terminal_exists(monkeypatch) -> None:
    from aibench.cli import chat as chat_cli

    monkeypatch.setattr(chat_cli, "interactive_terminal", lambda: True)
    result = cli.invoke(app, ["--non-interactive", "chat"])
    assert result.exit_code == 2
    assert "chat needs an interactive terminal" in result.output


def test_root_non_interactive_selects_benchmark_headless_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aibench.cli import benchmark as benchmark_cli

    monkeypatch.setattr(benchmark_cli, "interactive_terminal", lambda: True)

    def unexpected_chat(**_kwargs: object) -> None:
        pytest.fail("root --non-interactive must keep benchmark out of the conversation")

    monkeypatch.setattr(benchmark_cli, "chat", unexpected_chat)
    application = _world_app(tmp_path / "app.json")
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps({"case_id": "one", "input": "hello"}) + "\n", encoding="utf-8")
    plan = tmp_path / "plan.json"

    result = cli.invoke(
        app,
        [
            "--non-interactive",
            "--json",
            "benchmark",
            str(application),
            "--dataset",
            str(dataset),
            "--out",
            str(plan),
        ],
    )
    assert result.exit_code in {2, 3, 4}, result.output
    assert json.loads(result.output)["status"] in {"blocked", "authorization_required"}
    assert plan.is_file()


def test_non_interactive_connect_fails_missing_values_without_prompting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aibench.cli import connect as connect_cli

    class TerminalInput:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(connect_cli, "sys", SimpleNamespace(stdin=TerminalInput()))
    monkeypatch.setattr(typer, "prompt", lambda *_args, **_kwargs: pytest.fail("prompt invoked"))
    result = cli.invoke(
        app,
        ["--non-interactive", "connect", "http", "--project", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "required in non-interactive mode" in result.output


def test_non_interactive_plugin_install_requires_explicit_yes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aibench.cli import project as project_cli
    from aibench.services import plugins as plugin_service

    class InstallPlan:
        @staticmethod
        def summary() -> dict[str, object]:
            return {}

    monkeypatch.setattr(plugin_service, "plan_install", lambda *_args, **_kwargs: InstallPlan())
    monkeypatch.setattr(project_cli, "install_preview", lambda _summary: [])
    monkeypatch.setattr(
        plugin_service,
        "install",
        lambda *_args, **_kwargs: pytest.fail("install must require explicit --yes"),
    )
    result = cli.invoke(
        app,
        ["--non-interactive", "plugins", "install", "deepeval", "--project", str(tmp_path)],
    )
    assert result.exit_code == 2
    assert "pass --yes in non-interactive mode" in result.output


def test_root_help_lists_global_scripting_and_selection_options() -> None:
    result = cli.invoke(app, ["--help"])
    assert result.exit_code == 0
    for option in ("--config", "--json", "--non-interactive", "--policy"):
        assert option in result.output
