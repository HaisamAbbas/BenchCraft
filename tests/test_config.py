"""01-T3: config precedence, safe parsing, path resolution, secret refs, redaction,
and dataset-cannot-override-policy protection."""

from __future__ import annotations

import json

import pytest

from aibench.config.resolve import (
    assert_no_policy_keys_from_dataset,
    load_mapping_file,
    resolve_config,
    resolve_path,
)
from aibench.core.errors import ConfigError, PolicyError


def test_precedence_defaults_lt_config_lt_env_lt_cli(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"dataset_path": "from-config.jsonl"}), encoding="utf-8")

    resolved = resolve_config(
        config_path=config_path,
        cli_overrides={"dataset_path": "from-cli.jsonl"},
        env={"AIBENCH_DATASET_PATH": "from-env.jsonl"},
    )
    assert resolved.config.dataset_path == "from-cli.jsonl"
    assert resolved.sources["dataset_path"] == "cli"


def test_env_overrides_config_file_when_no_cli_value(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"dataset_path": "from-config.jsonl"}), encoding="utf-8")

    resolved = resolve_config(
        config_path=config_path, cli_overrides={}, env={"AIBENCH_DATASET_PATH": "from-env.jsonl"}
    )
    assert resolved.config.dataset_path == "from-env.jsonl"
    assert resolved.sources["dataset_path"] == "env"


def test_unpermitted_env_var_is_ignored(tmp_path) -> None:
    resolved = resolve_config(config_path=None, cli_overrides={}, env={"AIBENCH_RANDOM": "x"})
    assert not hasattr(resolved.config, "random")


def test_missing_config_file_raises_config_error(tmp_path) -> None:
    with pytest.raises(ConfigError):
        resolve_config(config_path=tmp_path / "missing.json", cli_overrides={}, env={})


def test_malformed_yaml_error_does_not_echo_file_contents(tmp_path) -> None:
    pytest.importorskip("yaml")
    config = tmp_path / "config.yaml"
    config.write_text("secret: [sensitive-value", encoding="utf-8")

    with pytest.raises(ConfigError) as error:
        load_mapping_file(config)

    assert "not valid YAML at line 1" in str(error.value)
    assert "sensitive-value" not in str(error.value)


def test_secrets_are_references_not_values(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"secrets": {"judge": "env:JUDGE_API_KEY"}}), encoding="utf-8"
    )
    resolved = resolve_config(config_path=config_path, cli_overrides={}, env={})
    assert resolved.config.secrets["judge"].source == "env"
    assert resolved.config.secrets["judge"].name == "JUDGE_API_KEY"


def test_redacted_config_never_contains_a_literal_secret_value(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps({"secrets": {"judge": "env:JUDGE_API_KEY"}}), encoding="utf-8"
    )
    resolved = resolve_config(config_path=config_path, cli_overrides={}, env={})
    redacted = resolved.config.redacted()
    serialized = json.dumps(redacted)
    assert "JUDGE_API_KEY" not in serialized or "env:JUDGE_API_KEY" in serialized
    assert redacted["secrets"]["judge"] == "env:JUDGE_API_KEY"


def test_dataset_extensions_cannot_define_reserved_policy_keys() -> None:
    with pytest.raises(PolicyError):
        assert_no_policy_keys_from_dataset({"policy": {"allow_network": True}})


def test_ordinary_dataset_extensions_are_allowed() -> None:
    result = assert_no_policy_keys_from_dataset({"custom_field": 1})
    assert result is None


def test_relative_path_resolves_against_root(tmp_path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    resolved = resolve_path("data/set.jsonl", root=root)
    assert resolved == (root / "data" / "set.jsonl").resolve()


def test_content_hash_is_deterministic_for_equivalent_config(tmp_path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"dataset_path": "d.jsonl"}), encoding="utf-8")
    r1 = resolve_config(config_path=config_path, cli_overrides={}, env={})
    r2 = resolve_config(config_path=config_path, cli_overrides={}, env={})
    assert r1.content_hash == r2.content_hash
