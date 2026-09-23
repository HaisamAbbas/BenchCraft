"""Application runners (§7): CLI and HTTP transports behind one lifecycle contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from aibench.config.resolve import load_mapping_file
from aibench.core.errors import ConfigError
from aibench.core.models import ApplicationSpec, RunnerKind
from aibench.runners.base import (
    BaseRunner,
    HealthReport,
    InvocationContext,
    InvocationOutcome,
    ResetReport,
    RunnerDescription,
    RunnerLifecycleError,
)
from aibench.runners.bindings import AppInputEnvelope
from aibench.runners.cli_runner import CliRunner
from aibench.runners.http_runner import HttpRunner

__all__ = [
    "AppInputEnvelope",
    "BaseRunner",
    "CliRunner",
    "HealthReport",
    "HttpRunner",
    "InvocationContext",
    "InvocationOutcome",
    "LoadedApplication",
    "ResetReport",
    "RunnerDescription",
    "RunnerLifecycleError",
    "create_runner",
    "load_application",
]


@dataclass(frozen=True)
class LoadedApplication:
    spec: ApplicationSpec
    base_dir: Path  # relative cwd/ca_bundle paths resolve here; not part of the identity


def load_application(path: Path) -> LoadedApplication:
    """Load an application config file (JSON, or YAML when PyYAML is installed)."""
    if not path.is_file():
        raise ConfigError(f"application config not found: {path}")
    raw = load_mapping_file(path)
    try:
        spec = ApplicationSpec.model_validate(raw)
    except PydanticValidationError as exc:
        raise ConfigError(f"invalid application config {path}: {exc}") from exc
    if spec.transport is None:
        raise ConfigError(f"application config {path} has no transport section")
    return LoadedApplication(spec=spec, base_dir=path.resolve().parent)


def create_runner(
    app: LoadedApplication,
    *,
    trusted_local: bool = False,
    environ: Mapping[str, str] | None = None,
) -> BaseRunner:
    """`trusted_local` must be explicitly True to execute a local CLI application (§16)."""
    if app.spec.runner is RunnerKind.CLI:
        return CliRunner(
            app.spec, base_dir=app.base_dir, trusted_local=trusted_local, environ=environ
        )
    return HttpRunner(app.spec, base_dir=app.base_dir, environ=environ)
