"""Config precedence, safe parsing, path resolution, and policy-key protection (01-T3)."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from aibench.config.model import RESERVED_POLICY_KEYS, AibenchConfig, SecretRef
from aibench.core.errors import ConfigError, PolicyError
from aibench.core.hashes import content_hash
from aibench.security.secrets import SUPPORTED_SOURCES

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
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path} is not valid UTF-8 text") from exc
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise ConfigError(
                f"{path} is YAML but PyYAML is not installed; use JSON or install PyYAML"
            ) from exc
        try:
            loaded = yaml.safe_load(text) if text.strip() else {}
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = (
                f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""
            )
            raise ConfigError(f"{path} is not valid YAML{location}") from exc
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

    secrets_raw = merged.pop("secrets", {})
    if not isinstance(secrets_raw, dict):
        raise ConfigError("invalid configuration: secrets must be an object of references")
    secrets: dict[str, SecretRef] = {}
    for name, value in secrets_raw.items():
        try:
            if isinstance(value, str):
                secrets[name] = SecretRef.parse(value)
            elif isinstance(value, dict):
                secrets[name] = SecretRef.model_validate(value)
            else:
                raise TypeError
        except (TypeError, ValueError, PydanticValidationError) as exc:
            raise ConfigError(
                f"invalid configuration: secrets.{name} must be a secret reference"
            ) from exc
        if secrets[name].source not in SUPPORTED_SOURCES:
            raise ConfigError(
                f"invalid configuration: secrets.{name} uses an unsupported secret source "
                f"(supported: {', '.join(SUPPORTED_SOURCES)})"
            )
    merged["secrets"] = secrets

    extensions = merged.get("extensions", {})
    if not isinstance(extensions, Mapping):
        raise ConfigError("invalid configuration: extensions must be an object")
    assert_no_policy_keys_from_dataset(extensions)

    try:
        config = AibenchConfig(**merged)
    except PydanticValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in error['loc'])}: "
            f"{'must be a valid secret reference' if 'secret_env' in error['loc'] else error['msg']}"
            for error in exc.errors(include_url=False, include_context=False, include_input=False)
        ]
        raise ConfigError("invalid configuration: " + "; ".join(problems)) from exc

    for environment in config.plugin_environments:
        for variable, reference in environment.secret_env.items():
            source = reference.partition(":")[0]
            if source not in SUPPORTED_SOURCES:
                raise ConfigError(
                    f"invalid configuration: plugin secret {environment.name}.{variable} "
                    f"uses an unsupported secret source (supported: {', '.join(SUPPORTED_SOURCES)})"
                )

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
