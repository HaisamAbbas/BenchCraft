"""09-T1: default entry guidance and the non-TTY JSON chat surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
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
    assert first_payload["_cli"]["exit_code"] == first.exit_code
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
    monkeypatch.setattr(chat_cli, "open_provider", lambda *_, **__: (provider, []))
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


def test_headless_slash_commands_receive_provider_and_plugin_judge(
    tmp_path: Path, monkeypatch
) -> None:
    from aibench.services import plugins
    from tests.test_openai_provider import chat_server, completion, tool_call

    project = tmp_path / "project"
    application, dataset = _project(project)
    source_quote = "Refunds may be requested within 30 days of purchase."
    (project / "rules.md").write_text(source_quote, encoding="utf-8")
    captured: dict[str, object] = {}

    class InstallPreview:
        def summary(self) -> dict[str, str]:
            return {"name": "deepeval"}

    def preview_install(*args, **kwargs):
        captured.update(kwargs)
        return InstallPreview()

    monkeypatch.setattr(plugins, "plan_install", preview_install)
    cases = [
        {
            "input": "When can I request a refund?",
            "expected_answer": source_quote,
            "source_id": "source_1",
            "source_quote": source_quote,
        }
    ]
    with chat_server(
        [(200, completion([tool_call("write_candidates", {"cases": cases})]))]
    ) as server:
        host, port = server.server_address[:2]
        base_url = f"http://{host}:{port}/v1"
        provider_config = project / "provider.json"
        provider_config.write_text(
            json.dumps({"base_url": base_url, "model": "case-writer"}),
            encoding="utf-8",
        )

        generated = runner.invoke(
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
                str(provider_config),
                "--send",
                "/cases generate rules.md --max 1",
                "--json",
            ],
        )
        assert generated.exit_code == 0, generated.stdout
        generated_payload = json.loads(generated.stdout)
        assert generated_payload["kind"] == "cases"
        assert generated_payload["data"]["generated"] is True
        assert len(generated_payload["data"]["rows"]) == 1
        assert len(server.requests) == 1
        request = server.requests[0]
        assert request["path"] == "/v1/chat/completions"
        assert request["body"]["model"] == "case-writer"
        assert request["body"]["tools"][0]["function"]["name"] == "write_candidates"

        plugin_preview = runner.invoke(
            app,
            [
                "chat",
                "--project",
                str(project),
                "--resume",
                generated_payload["session_id"],
                "--provider-config",
                str(provider_config),
                "--send",
                "/plugins install deepeval",
                "--json",
            ],
        )
        assert plugin_preview.exit_code == 0, plugin_preview.stdout
        assert json.loads(plugin_preview.stdout)["kind"] == "plugin_preview"

    assert captured["judge"] == {
        "kind": "openai_compatible",
        "base_url": base_url,
        "model": "case-writer",
        "api_key_env": "AIBENCH_JUDGE_KEY",
    }
    assert captured["secret_env"] == {}


@pytest.mark.parametrize("command", ['/cases generate "unfinished', '/compare "unfinished'])
def test_headless_malformed_slash_command_emits_json_error(tmp_path: Path, command: str) -> None:
    project = tmp_path / "project"
    application, dataset = _project(project)
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
            command,
            "--json",
        ],
    )
    assert result.exit_code == 2, result.output
    payload = json.loads(result.stdout)
    assert payload["status"] == "error"
    assert payload["exit_code"] == 2
    assert payload["_cli"]["exit_code"] == result.exit_code
    assert "invalid command quoting" in payload["message"]
    assert "Traceback" not in result.output


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


def _send(project: Path, *options: str) -> dict:
    application, dataset = project / "app.json", project / "data.jsonl"
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
            *options,
            "--send",
            "/sessions",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def test_continue_opens_the_latest_session_with_work_in_it(tmp_path: Path) -> None:
    """A user coming back had to look up a session ID: `--resume` alone failed with "Option
    '--resume' requires an argument". `--continue` (or `--resume latest`) opens the most
    recently used session that has an objective or a run; a newer empty one (opened and
    left) is passed over. `--resume` keeps its value required, so the next option is never
    read as an ID."""
    project = tmp_path / "project"
    _project(project)
    older = _send(project, "--new", "--objective", "catch wrong answers")["session_id"]
    latest = _send(project, "--new", "--objective", "check fine amounts")["session_id"]
    empty = _send(project, "--new")["session_id"]
    assert len({older, latest, empty}) == 3

    assert _send(project, "--continue")["session_id"] == latest
    assert _send(project, "-c")["session_id"] == latest
    assert _send(project, "--resume", "latest")["session_id"] == latest
    assert _send(project, "--resume", older)["session_id"] == older  # an ID still works


def test_continue_without_a_session_says_how_to_start_one(tmp_path: Path) -> None:
    project = tmp_path / "project"
    application, dataset = _project(project)
    base = ["chat", "--project", str(project), "--app", str(application)]
    base += ["--dataset", str(dataset), "--send", "/sessions", "--json"]
    result = runner.invoke(app, [*base, "--continue"])
    assert result.exit_code != 0
    assert "no session to resume" in result.output
    both = runner.invoke(app, [*base, "--continue", "--new"])
    assert both.exit_code != 0 and "one of --resume, --continue and --new" in both.output


@pytest.mark.parametrize(
    ("arguments", "resume", "new", "latest"),
    [
        ([], None, False, False),
        (["--continue"], None, False, True),
        (["-c"], None, False, True),
        (["--resume", "ses-0123"], "ses-0123", False, False),
        (["--new"], None, True, False),
    ],
)
def test_bare_benchcraft_takes_continue_resume_and_new(
    monkeypatch: pytest.MonkeyPatch,
    arguments: list[str],
    resume: str | None,
    new: bool,
    latest: bool,
) -> None:
    """`benchcraft --continue` opens the conversation like `benchcraft chat --continue`."""
    import aibench.cli.chat as chat_cli

    seen: dict[str, object] = {}
    monkeypatch.setattr(chat_cli, "interactive_terminal", lambda: True)
    monkeypatch.setattr(chat_cli, "chat", lambda **kwargs: seen.update(kwargs))
    result = runner.invoke(app, arguments)
    assert result.exit_code == 0, result.output
    assert (seen["resume"], seen["new"], seen["continue_latest"]) == (resume, new, latest)


def test_project_settings_use_resolved_project_root_and_keep_absolute_overrides(
    tmp_path: Path,
) -> None:
    from aibench.cli.chat import project_settings

    parent = tmp_path / "monorepo"
    nested = parent / "subproject"
    nested.mkdir(parents=True)
    config = {
        "project_root": "subproject",
        "application_target": "app.json",
        "dataset_path": "data.jsonl",
        "policy_path": "policy.json",
        "plan_path": "plan.json",
    }
    (parent / "aibench.json").write_text(json.dumps(config), encoding="utf-8")
    for name in ("app.json", "data.jsonl", "policy.json", "plan.json"):
        (nested / name).write_text("{}\n", encoding="utf-8")

    settings = project_settings(parent, None, None, None)
    assert settings["application"] == (nested / "app.json").resolve()
    assert settings["dataset"] == (nested / "data.jsonl").resolve()
    assert settings["policy"] == (nested / "policy.json").resolve()
    assert settings["plan"] == (nested / "plan.json").resolve()

    override_app = (tmp_path / "override-app.json").resolve()
    override_dataset = (tmp_path / "override-data.jsonl").resolve()
    override_policy = (tmp_path / "override-policy.json").resolve()
    overridden = project_settings(parent, override_app, override_dataset, override_policy)
    assert overridden["application"] == override_app
    assert overridden["dataset"] == override_dataset
    assert overridden["policy"] == override_policy
