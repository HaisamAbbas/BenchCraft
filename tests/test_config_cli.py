"""Project config inspection/editing and named assistant-provider profile workflows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from aibench import userconfig
from aibench.cli.chat import _assistant_model
from aibench.cli.main import app
from aibench.cli.plan import _provider_source
from aibench.planning.openai_provider import OpenAICompatibleConfig

runner = CliRunner()


@pytest.fixture(autouse=True)
def _user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("BENCHCRAFT_HOME", str(tmp_path / "user"))


def test_config_show_reports_effective_sources_and_redacts_secret_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = project / "aibench.json"
    config.write_text(
        json.dumps(
            {
                "dataset_path": "from-config.jsonl",
                "secrets": {"judge": "env:JUDGE_KEY"},
                "extensions": {
                    "api_key": "literal-profile-credential",
                    "api_token": "custom:inline-extension-secret",
                    "ordinary": "ok",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AIBENCH_DATASET_PATH", "from-env.jsonl")
    result = runner.invoke(app, ["config", "show", "--project", str(project), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["effective"]["dataset_path"] == "from-env.jsonl"
    assert data["sources"]["dataset_path"] == "env"
    assert data["sources"]["secrets"] == "config_file"
    assert data["effective"]["secrets"]["judge"] == "env:JUDGE_KEY"
    assert data["effective"]["extensions"]["api_key"] == "[redacted]"
    assert data["effective"]["extensions"]["api_token"] == "[redacted]"
    assert "literal-profile-credential" not in result.stdout
    assert "inline-extension-secret" not in result.stdout
    assert "JUDGE_KEY" in result.stdout

    policy = tmp_path / "policy.json"
    policy.write_text("{}", encoding="utf-8")
    cli_override = runner.invoke(
        app,
        ["--policy", str(policy), "config", "show", "--project", str(project), "--json"],
    )
    assert cli_override.exit_code == 0, cli_override.output
    effective = json.loads(cli_override.stdout)
    assert effective["effective"]["policy_path"] == str(policy.resolve())
    assert effective["sources"]["policy_path"] == "cli"


def test_config_validate_and_set_are_schema_checked_and_atomic(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    validated = runner.invoke(app, ["config", "validate", "--project", str(project), "--json"])
    assert validated.exit_code == 0, validated.output
    assert json.loads(validated.stdout)["valid"] is True

    changed = runner.invoke(
        app,
        ["config", "set", "dataset_path", "cases.jsonl", "--project", str(project), "--json"],
    )
    assert changed.exit_code == 0, changed.output
    config_path = project / "aibench.json"
    before = config_path.read_text(encoding="utf-8")
    assert json.loads(before)["dataset_path"] == "cases.jsonl"

    invalid = runner.invoke(
        app,
        ["config", "set", "unknown_field", "secret-value", "--project", str(project), "--json"],
    )
    assert invalid.exit_code == 2
    assert "unknown config field" in invalid.stdout
    assert "secret-value" not in invalid.stdout
    assert config_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize(
    "contents",
    [
        '{"secrets": []}',
        '{"secrets": null}',
        '{"secrets": {"judge": "custom:inline-secret-value"}}',
        '{"secrets": {"judge": "env:"}}',
        '{"secrets": {"judge": {"source": "env", "name": ""}}}',
        '{"extensions": []}',
        '{"extensions": null}',
    ],
)
def test_config_validate_rejects_falsey_non_object_fields(contents: str, tmp_path: Path) -> None:
    config = tmp_path / "aibench.json"
    config.write_text(contents, encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", str(config), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "error"
    assert "inline-secret-value" not in result.stdout


def test_config_validate_rejects_yaml_top_level_sequence(tmp_path: Path) -> None:
    pytest.importorskip("yaml")
    config = tmp_path / "aibench.yaml"
    config.write_text("[]\n", encoding="utf-8")
    result = runner.invoke(app, ["config", "validate", str(config), "--json"])
    assert result.exit_code == 2
    assert json.loads(result.stdout)["status"] == "error"


@pytest.mark.parametrize("reference", ["env:", "env: ", "custom:plugin-secret-value"])
def test_config_validate_rejects_invalid_plugin_secret_references_without_echoing_them(
    reference: str, tmp_path: Path
) -> None:
    config = tmp_path / "aibench.json"
    config.write_text(
        json.dumps(
            {
                "plugin_environments": [
                    {"name": "judge", "python": "python", "secret_env": {"JUDGE": reference}}
                ]
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(app, ["config", "validate", str(config), "--json"])
    assert result.exit_code == 2
    assert "plugin-secret-value" not in result.stdout


def test_config_validate_formats_filesystem_errors_as_cli_errors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["config", "validate", str(tmp_path), "--json"])
    assert result.exit_code == 2
    data = json.loads(result.stdout)
    assert data["status"] == "error"
    assert data["message"] == "could not read the selected config file"


def test_config_cli_hash_does_not_fingerprint_redacted_extension_values(tmp_path: Path) -> None:
    hashes = []
    for value in ("a", "b"):
        project = tmp_path / value
        project.mkdir()
        (project / "aibench.json").write_text(
            json.dumps({"extensions": {"api_key": value}}), encoding="utf-8"
        )
        shown = runner.invoke(app, ["config", "show", "--project", str(project), "--json"])
        assert shown.exit_code == 0, shown.output
        hashes.append(json.loads(shown.stdout)["content_hash"])
    assert hashes[0] == hashes[1]

    set_hashes = []
    for value in ("a", "b"):
        project = tmp_path / f"set-{value}"
        project.mkdir()
        result = runner.invoke(
            app,
            [
                "config",
                "set",
                "extensions.api_key",
                json.dumps(value),
                "--project",
                str(project),
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        set_hashes.append(json.loads(result.stdout)["content_hash"])
    assert set_hashes[0] == set_hashes[1]


def test_config_set_only_accepts_secret_references_and_never_echoes_values(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    literal = "credential-that-must-not-appear"
    result = runner.invoke(
        app,
        ["config", "set", "secrets.judge", literal, "--project", str(project), "--json"],
    )
    assert result.exit_code == 2
    assert literal not in result.stdout
    assert not (project / "aibench.json").exists()

    reference = runner.invoke(
        app,
        ["config", "set", "secrets.judge", "env:JUDGE_KEY", "--project", str(project), "--json"],
    )
    assert reference.exit_code == 0, reference.output
    assert json.loads(reference.stdout)["stored_value"]["judge"] == "env:JUDGE_KEY"


def test_config_set_rejects_extra_fields_on_secret_references_without_leaking_them(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    literal = "literal-secret-in-extra-field"
    result = runner.invoke(
        app,
        [
            "config",
            "set",
            "secrets.TOKEN",
            json.dumps({"source": "env", "name": "TOKEN", "value": literal}),
            "--project",
            str(project),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert literal not in result.stdout
    assert not (project / "aibench.json").exists()


def test_named_provider_profiles_are_validated_redacted_and_selectable(tmp_path: Path) -> None:
    provider_file = tmp_path / "provider.json"
    provider_file.write_text(
        json.dumps(
            {
                "base_url": "https://api.example.test/v1",
                "model": "model-a",
                "api_key": "env:PROFILE_KEY",
            }
        ),
        encoding="utf-8",
    )
    added = runner.invoke(
        app,
        [
            "config",
            "profiles",
            "add",
            "fast",
            "--provider-config",
            str(provider_file),
            "--json",
        ],
    )
    assert added.exit_code == 0, added.output
    assert json.loads(added.stdout)["active"] is True

    listed = runner.invoke(app, ["config", "profiles", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert json.loads(listed.stdout)["profiles"][0]["name"] == "fast"

    shown = runner.invoke(app, ["config", "profiles", "show", "fast", "--json"])
    assert shown.exit_code == 0, shown.output
    assert json.loads(shown.stdout)["provider"]["api_key"] == "env:PROFILE_KEY"
    assert "PROFILE_KEY" in shown.stdout

    config = OpenAICompatibleConfig(
        base_url="https://api.example.test/v1", model="model-a", api_key="env:PROFILE_KEY"
    )
    assert userconfig.saved_provider() == config
    assert _assistant_model(None, interactive=False, provider_profile="fast") == config
    assert _provider_source(None, "fast", json_output=True) == config
    stored = userconfig.config_file().read_text(encoding="utf-8")
    assert "PROFILE_KEY" in stored
    assert "actual-secret" not in stored

    second = config.model_copy(update={"model": "model-b"})
    userconfig.save_provider_profile("careful", second)
    selected = runner.invoke(app, ["config", "profiles", "use", "careful", "--json"])
    assert selected.exit_code == 0, selected.output
    assert userconfig.saved_provider() == second
    assert _assistant_model(None, interactive=False) == second
    effective = runner.invoke(
        app, ["config", "show", "--project", str(tmp_path), "--json"]
    )
    assert effective.exit_code == 0, effective.output
    assistant = json.loads(effective.stdout)["assistant_provider"]
    assert assistant["source"] == "profile:careful"
    assert assistant["model"] == "model-b"

    removed = runner.invoke(app, ["config", "profiles", "remove", "fast", "--json"])
    assert removed.exit_code == 0, removed.output
    assert userconfig.provider_profile_names() == ["careful"]
    assert userconfig.active_provider_profile() == "careful"
    removed = runner.invoke(app, ["config", "profiles", "remove", "careful", "--json"])
    assert removed.exit_code == 0, removed.output
    assert userconfig.provider_profile_names() == []


def test_profile_human_output_sanitizes_untrusted_model_and_endpoint_text(
    tmp_path: Path,
) -> None:
    provider_file = tmp_path / "provider.json"
    provider_file.write_text(
        json.dumps(
            {
                "base_url": "https://api.example.test/v1/\x1b]0;endpoint-title\x07",
                "model": "model-\x1b]0;model-title\x07",
            }
        ),
        encoding="utf-8",
    )
    added = runner.invoke(
        app, ["config", "profiles", "add", "safe", "--provider-config", str(provider_file)]
    )
    assert added.exit_code == 0, added.output
    assert "\x1b" not in added.stdout
    assert "endpoint-title" not in added.stdout
    assert "model-title" not in added.stdout

    shown = runner.invoke(app, ["config", "profiles", "show", "safe"])
    assert shown.exit_code == 0, shown.output
    assert "\x1b" not in shown.stdout
    assert "endpoint-title" not in shown.stdout
    assert "model-title" not in shown.stdout

    listed = runner.invoke(app, ["config", "profiles", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    assert "endpoint-title" not in listed.stdout
    assert "model-title" not in listed.stdout


def test_config_json_paths_are_sanitized_for_validate_and_profile_mutations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    secret_path = tmp_path / "api_key=filesystem-secret"
    secret_path.mkdir()
    monkeypatch.setenv("BENCHCRAFT_HOME", str(secret_path))
    project = secret_path / "project"
    project.mkdir()
    (project / "aibench.json").write_text("{}\n", encoding="utf-8")
    validated = runner.invoke(
        app, ["config", "validate", str(project / "aibench.json"), "--json"]
    )
    assert validated.exit_code == 0, validated.output
    assert "filesystem-secret" not in validated.stdout

    provider = OpenAICompatibleConfig(base_url="https://api.example.test/v1", model="model")
    userconfig.save_provider_profile("safe", provider)
    used = runner.invoke(app, ["config", "profiles", "use", "safe", "--json"])
    assert used.exit_code == 0, used.output
    assert "filesystem-secret" not in used.stdout
    removed = runner.invoke(app, ["config", "profiles", "remove", "safe", "--json"])
    assert removed.exit_code == 0, removed.output
    assert "filesystem-secret" not in removed.stdout


def test_provider_profile_import_refuses_embedded_credentials_without_echoing_them(
    tmp_path: Path,
) -> None:
    provider_file = tmp_path / "provider.json"
    provider_file.write_text(
        json.dumps(
            {
                "base_url": "https://user:secret-value@api.example.test/v1?token=secret-value",
                "model": "model-a",
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "config",
            "profiles",
            "add",
            "unsafe",
            "--provider-config",
            str(provider_file),
            "--json",
        ],
    )
    assert result.exit_code == 2
    assert "without credentials" in result.stdout
    assert "secret-value" not in result.stdout
    assert userconfig.provider_profile_names() == []


@pytest.mark.parametrize(
    "provider",
    [
        {"base_url": "https://api.example.test/v1?token=query-secret", "model": "model-a"},
        {"base_url": "https://api.example.test/v1#fragment-secret", "model": "model-a"},
        {"base_url": "https://api.example.test:99999/v1", "model": "model-a"},
        {
            "base_url": "https://api.example.test/v1",
            "model": "model-a",
            "api_key": "literal-api-key-secret",
        },
        {
            "base_url": "https://api.example.test/v1",
            "model": "model-a",
            "api_key": "custom:literal-reference-secret",
        },
    ],
)
def test_provider_profile_import_rejects_inline_credentials_without_echoing_them(
    provider: dict[str, str], tmp_path: Path
) -> None:
    provider_file = tmp_path / "provider.json"
    provider_file.write_text(json.dumps(provider), encoding="utf-8")
    result = runner.invoke(
        app,
        ["config", "profiles", "add", "unsafe", "--provider-config", str(provider_file)],
    )
    assert result.exit_code == 2
    assert "query-secret" not in result.stdout
    assert "fragment-secret" not in result.stdout
    assert "99999" not in result.stdout
    assert "literal-api-key-secret" not in result.stdout
    assert "literal-reference-secret" not in result.stdout
    assert userconfig.provider_profile_names() == []


def test_profile_use_and_remove_format_user_settings_write_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(_name: str) -> Path:
        raise OSError("private path detail")

    monkeypatch.setattr(userconfig, "select_provider_profile", fail)
    used = runner.invoke(app, ["config", "profiles", "use", "private", "--json"])
    assert used.exit_code == 2
    assert json.loads(used.stdout)["message"] == "could not update user provider settings"
    assert "private path detail" not in used.stdout

    monkeypatch.setattr(userconfig, "remove_provider_profile", fail)
    removed = runner.invoke(app, ["config", "profiles", "remove", "private", "--json"])
    assert removed.exit_code == 2
    assert json.loads(removed.stdout)["message"] == "could not update user provider settings"
    assert "private path detail" not in removed.stdout
