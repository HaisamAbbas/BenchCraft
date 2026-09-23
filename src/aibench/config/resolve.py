"""Config precedence, safe parsing, path resolution, and policy-key protection (01-T3)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aibench.config.model import RESERVED_POLICY_KEYS, AibenchConfig, SecretRef
from aibench.core.errors import ConfigError, PolicyError
from aibench.core.hashes import content_hash

# Only these environment variables are ever consulted. This is an explicit allowlist, not
# a general os.environ merge, so undocumented variables cannot silently alter policy.
PERMITTED_ENV_OVERRIDES = {
    "AIBENCH_DATASET_PATH": "dataset_path",
    "AIBENCH_APPLICATION_TARGET": "application_target",
    "AIBENCH_POLICY_PATH": "policy_path",
}

_DEFAULTS: dict[str, Any] = {"project_root": "."}


@dataclass
class ResolvedConfig:
    config: AibenchConfig
    root: Path
    content_hash: str
    sources: dict[str, str]  # field -> "default" | "config_file" | "env" | "cli"


def load_mapping_file(path: Path) -> dict[str, Any]:
    """Parse config bytes without executing code. JSON is always supported; YAML is
    supported only via safe_load if PyYAML is installed, never via arbitrary eval/exec."""
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigError(
                f"{path} is YAML but PyYAML is not installed; use JSON or install PyYAML"
            ) from exc
        loaded = yaml.safe_load(text) or {}
    else:
        try:
            loaded = json.loads(text) if text.strip() else {}
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a JSON/YAML object at the top level")
    return loaded


def assert_no_policy_keys_from_dataset(extensions: Mapping[str, Any]) -> None:
    """Dataset `extensions` content must never be able to redefine policy-relevant
    configuration. Called wherever dataset-derived data is merged into anything
    config-shaped."""
    collision = RESERVED_POLICY_KEYS & set(extensions.keys())
    if collision:
        raise PolicyError(
            "dataset extensions cannot define reserved policy keys: " + ", ".join(sorted(collision))
        )


def resolve_config(
    *,
    config_path: Path | None,
    cli_overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ResolvedConfig:
    """Resolve configuration with explicit precedence:
    defaults < config file < permitted environment overrides < CLI flags.
    """
    env = env or {}
    cli_overrides = cli_overrides or {}

    merged: dict[str, Any] = dict(_DEFAULTS)
    sources: dict[str, str] = {k: "default" for k in merged}

    root = Path.cwd()
    if config_path is not None:
        if not config_path.exists():
            raise ConfigError(f"config file not found: {config_path}")
        root = config_path.resolve().parent
        file_values = load_mapping_file(config_path)
        for key, value in file_values.items():
            merged[key] = value
            sources[key] = "config_file"

    for env_key, field_name in PERMITTED_ENV_OVERRIDES.items():
        if env_key in env:
            merged[field_name] = env[env_key]
            sources[field_name] = "env"

    for key, value in cli_overrides.items():
        if value is None:
            continue
        merged[key] = value
        sources[key] = "cli"

    secrets_raw = merged.pop("secrets", {}) or {}
    secrets = {k: SecretRef.parse(v) if isinstance(v, str) else SecretRef(**v) for k, v in secrets_raw.items()}
    merged["secrets"] = secrets

    extensions = merged.get("extensions", {}) or {}
    assert_no_policy_keys_from_dataset(extensions)

    try:
        config = AibenchConfig(**merged)
    except Exception as exc:  # pydantic ValidationError -> ConfigError
        raise ConfigError(f"invalid configuration: {exc}") from exc

    resolved_root = (root / config.project_root).resolve()

    if config.dataset_path is not None:
        dataset_path = Path(config.dataset_path)
        if not dataset_path.is_absolute():
            resolved_dataset = (resolved_root / dataset_path).resolve()
            _assert_within(resolved_dataset, resolved_root)

    digest = content_hash(config.redacted())
    return ResolvedConfig(config=config, root=resolved_root, content_hash=digest, sources=sources)


def _assert_within(path: Path, root: Path) -> None:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PolicyError(f"path {path} escapes the project root {root}") from exc


def resolve_path(relative_or_absolute: str, *, root: Path) -> Path:
    """Resolve a dataset/config-referenced path relative to the project root. An absolute
    path alone is not treated as portable identity (§6); it is still checked for
    containment where policy requires it."""
    candidate = Path(relative_or_absolute)
    if candidate.is_absolute():
        return candidate.resolve()
    return (root / candidate).resolve()
