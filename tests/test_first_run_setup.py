"""First-run setup: the assistant's model is chosen once per user and remembered; the file
never holds the key. The chat uses that model in any project without its own policy; a
project policy still decides."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aibench import userconfig
from aibench.cli import chat as chat_cli
from aibench.planning.openai_provider import OpenAICompatibleConfig


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    monkeypatch.setenv("BENCHCRAFT_HOME", str(home))
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    return home


def _scripted(answers: list[str]):
    queue = list(answers)
    return lambda _prompt: queue.pop(0)


def test_setup_saves_the_model_and_the_key_reference_never_the_key(tmp_path: Path) -> None:
    saved: dict[str, str] = {}
    said: list[str] = []
    config = userconfig.run_setup(
        said.append,
        ask=_scripted(["1"]),  # Z.ai: its model is known, so only the choice is asked
        secret=_scripted(["zai-secret-key"]),
        persist=lambda name, value: saved.setdefault(name, value) is not None,
    )
    assert config is not None
    assert (config.base_url, config.model, config.api_key) == (
        "https://api.z.ai/api/paas/v4",
        "glm-4.7-flash",
        "env:ZAI_API_KEY",
    )
    assert saved == {"ZAI_API_KEY": "zai-secret-key"}
    stored = userconfig.config_file().read_text(encoding="utf-8")
    assert "zai-secret-key" not in stored
    assert json.loads(stored)["provider"]["model"] == "glm-4.7-flash"
    assert userconfig.decided() and userconfig.saved_provider() == config


def test_skipping_is_remembered_and_asks_nothing_about_keys() -> None:
    config = userconfig.run_setup(
        lambda _line: None, ask=_scripted(["s"]), secret=_scripted([]), persist=lambda *_: False
    )
    assert config is None and userconfig.decided() and userconfig.saved_provider() is None


def test_explicit_setup_replaces_a_selected_provider_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = OpenAICompatibleConfig(
        base_url="https://profile.test/v1", model="profile-model", api_key="env:PROFILE_KEY"
    )
    userconfig.save_provider_profile("selected", profile)
    userconfig.select_provider_profile("selected")
    monkeypatch.setenv("MY_SETUP_KEY", "present")

    setup = userconfig.run_setup(
        lambda _line: None,
        ask=_scripted(["4", "https://setup.test/v1", "setup-model", "MY_SETUP_KEY"]),
        secret=_scripted([]),
        persist=lambda *_: False,
    )

    assert setup is not None and setup.model == "setup-model"
    assert userconfig.active_provider_profile() is None
    assert userconfig.saved_provider() == setup


def test_a_custom_endpoint_and_a_key_already_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_KEY", "present")
    config = userconfig.run_setup(
        lambda _line: None,
        ask=_scripted(["4", "https://llm.internal/v1", "house-model", "MY_KEY"]),
        secret=_scripted([]),  # not asked: the variable is already set
        persist=lambda *_: False,
    )
    assert config == OpenAICompatibleConfig(
        base_url="https://llm.internal/v1", model="house-model", api_key="env:MY_KEY"
    )


def test_a_corrupt_settings_file_counts_as_not_set_up(_home: Path) -> None:
    _home.mkdir()
    userconfig.config_file().write_text("{broken", encoding="utf-8")
    assert not userconfig.decided() and userconfig.saved_provider() is None


def test_the_chosen_model_opens_where_no_project_policy_exists_but_a_policy_decides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    config = OpenAICompatibleConfig(
        base_url="https://api.z.ai/api/paas/v4", model="glm-4.6", api_key="env:ZAI_API_KEY"
    )
    provider, denials = chat_cli.open_provider(config, None, user_approved=True)
    assert denials == [] and provider is not None
    provider.close()

    # Not chosen in setup (e.g. an unreviewed config): the default policy refuses it.
    provider, denials = chat_cli.open_provider(config, None)
    assert provider is None and any("not an approved destination" in d for d in denials)

    # A project policy governs even the user's own choice.
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"data_roots": ["."]}), encoding="utf-8")
    provider, denials = chat_cli.open_provider(config, policy, user_approved=True)
    assert provider is None and denials


def test_the_chat_uses_the_saved_model_unless_one_is_given(tmp_path: Path) -> None:
    config = OpenAICompatibleConfig(base_url="https://x.test/v1", model="m", api_key="env:K")
    userconfig.save_provider(config)
    assert chat_cli._assistant_model(None, interactive=False) == config
    given = tmp_path / "provider.json"
    assert chat_cli._assistant_model(given, interactive=True) == given


def test_connect_allows_the_setup_model_so_the_chat_can_use_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Install, setup, `connect http`, chat: the policy connect writes allows the model the
    user chose in setup (and nothing else), so the assistant is not disabled."""
    from typer.testing import CliRunner

    from aibench.cli.main import app

    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    config = OpenAICompatibleConfig(
        base_url="https://api.z.ai/api/paas/v4", model="glm-4.6", api_key="env:ZAI_API_KEY"
    )
    userconfig.save_provider(config)
    project = tmp_path / "project"
    project.mkdir()
    (project / "cases.jsonl").write_text(
        json.dumps({"case_id": "c1", "input": "What is the refund policy?"}) + "\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        ["connect", "http", "--project", str(project), "--url", "http://127.0.0.1:8765/answer",
         "--dataset", str(project / "cases.jsonl"), "--app-id", "rag", "--effects", "none"],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["assistant_model_allowed"] == (
        "glm-4.6 at https://api.z.ai/api/paas/v4"
    )
    policy = project / "policy.json"
    written = json.loads(policy.read_text(encoding="utf-8"))
    assert written["allowed_planner_origins"] == ["https://api.z.ai/api/paas/v4"]
    assert "env:ZAI_API_KEY" in written["allowed_secret_refs"]
    provider, denials = chat_cli.open_provider(config, policy, user_approved=True)
    assert denials == [] and provider is not None
    provider.close()


def test_connect_maps_retrieved_documents_for_rag_metrics(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from aibench.cli.main import app

    project = tmp_path / "project"
    project.mkdir()
    (project / "cases.jsonl").write_text(
        json.dumps({"case_id": "c1", "input": "Refunds?"}) + "\n", encoding="utf-8"
    )
    base = ["connect", "http", "--project", str(project), "--url", "http://127.0.0.1:8765/answer",
            "--dataset", str(project / "cases.jsonl"), "--app-id", "rag", "--effects", "none"]  # fmt: skip
    refused = CliRunner().invoke(app, [*base, "--context-text-path", "/text"])
    assert refused.exit_code != 0 and "needs --context-path" in refused.output
    result = CliRunner().invoke(
        app, [*base, "--context-path", "/retrieved", "--context-text-path", "/text"]
    )
    assert result.exit_code == 0, result.output
    spec = json.loads((project / "application.http.json").read_text(encoding="utf-8"))
    assert spec["output_binding"] == {
        "output": "/answer",
        "retrieved_context": "/retrieved",
        "retrieved_context_item": "/text",
    }


def test_piped_answers_starting_with_a_byte_order_mark_are_understood() -> None:
    """PowerShell prefixes piped input with a byte-order mark."""
    config = userconfig.run_setup(
        lambda _line: None,
        ask=_scripted(["\ufeffs"]),
        secret=_scripted([]),
        persist=lambda *_: False,
    )
    assert config is None and userconfig.decided()


def test_the_chat_in_a_folder_without_a_project_explains_and_creates_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Typed in the home or a system folder: guidance to connect an app, no .aibench/."""
    from typer.testing import CliRunner

    from aibench.cli.main import app

    folder = tmp_path / "not-a-project"
    folder.mkdir()
    result = CliRunner().invoke(app, ["chat", "--project", str(folder), "--send", "/help"])
    assert result.exit_code != 0
    assert "no application is connected" in result.output
    assert "benchcraft connect http" in result.output
    assert not (folder / ".aibench").exists()


def test_a_folder_benchcraft_cannot_write_to_gets_a_message_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from aibench.cli.main import app
    from aibench.storage.db import Database

    def denied(_workspace: object) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(Database, "open_workspace", classmethod(lambda cls, w: denied(w)))
    folder = tmp_path / "locked"
    folder.mkdir()
    app_config = folder / "app.json"
    app_config.write_text("{}", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["chat", "--project", str(folder), "--app", str(app_config), "--send", "/help"]
    )
    assert result.exit_code != 0
    assert "cannot create its folder" in result.output and "Access is denied" in result.output
    assert "Traceback" not in result.output


def test_stored_keys_are_adopted_by_a_terminal_opened_before_they_were_saved() -> None:
    """A key saved as a user variable is invisible to a terminal that was already open, which
    showed up as "secret env:OPENROUTER_API_KEY is not set" right after the key was stored.
    BenchCraft adopts stored API keys and tokens the terminal does not have; it never
    overrides one the terminal has, and ignores every other stored variable."""
    from aibench import userconfig

    stored = {
        "OPENROUTER_API_KEY": "sk-stored",
        "ZAI_API_KEY": "zai-stored",
        "GITHUB_TOKEN": "gh-stored",
        "PATH": "C:/stored/path",  # not a key: never adopted
        "EDITOR": "vim",
    }
    environ = {"ZAI_API_KEY": "zai-from-this-terminal"}
    adopted = userconfig.adopt_user_environment(lambda: stored, environ)
    assert sorted(adopted) == ["GITHUB_TOKEN", "OPENROUTER_API_KEY"]
    assert environ == {
        "ZAI_API_KEY": "zai-from-this-terminal",  # the terminal's own value wins
        "OPENROUTER_API_KEY": "sk-stored",
        "GITHUB_TOKEN": "gh-stored",
    }

    off = {userconfig.NO_USER_ENV: "1"}
    assert userconfig.adopt_user_environment(lambda: stored, off) == []
    assert off == {userconfig.NO_USER_ENV: "1"}

    blank = {"OPENROUTER_API_KEY": ""}  # an empty variable counts as not set
    assert sorted(userconfig.adopt_user_environment(lambda: stored, blank)) == [
        "GITHUB_TOKEN",
        "OPENROUTER_API_KEY",
        "ZAI_API_KEY",
    ]
    assert blank["OPENROUTER_API_KEY"] == "sk-stored"


def test_reading_the_stored_user_environment_is_safe_everywhere() -> None:
    """On this platform it returns a mapping of strings (empty off Windows); it never raises."""
    from aibench import userconfig

    values = userconfig.read_user_environment()
    assert isinstance(values, dict)
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in values.items())
