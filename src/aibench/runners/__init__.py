"""Application runners (§7): CLI, HTTP, Python callable, container and OpenAI-compatible
transports behind one lifecycle contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from aibench.config.resolve import load_mapping_file
from aibench.core.errors import ConfigError
from aibench.core.models import (
    ApplicationSpec,
    CliTransport,
    HttpTransport,
    PythonTransport,
    RunnerKind,
)
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
from aibench.runners.container_runner import ContainerRunner
from aibench.runners.http_runner import HttpRunner
from aibench.runners.openai_runner import OpenAICompatibleRunner
from aibench.runners.python_runner import PythonRunner

__all__ = [
    "AppInputEnvelope",
    "BaseRunner",
    "CliRunner",
    "ContainerRunner",
    "HealthReport",
    "HttpRunner",
    "InvocationContext",
    "InvocationOutcome",
    "LoadedApplication",
    "OpenAICompatibleRunner",
    "PythonRunner",
    "ResetReport",
    "RunnerDescription",
    "RunnerLifecycleError",
    "create_runner",
    "load_application",
    "reset_hook",
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


def reset_hook(spec: ApplicationSpec) -> str | None:
    """The configured hook that restores state an application keeps between invocations,
    or None. A fresh process or container per invocation is not a reset hook: it does not
    reset state kept outside the process."""
    t = spec.transport
    if isinstance(t, HttpTransport) and t.reset_url:
        return "reset_url"
    if isinstance(t, CliTransport) and t.reset_argv:
        return "reset_argv"
    if isinstance(t, PythonTransport) and t.reset_callable:
        return "reset_callable"
    return None


def create_runner(
    app: LoadedApplication,
    *,
    trusted_local: bool = False,
    environ: Mapping[str, str] | None = None,
) -> BaseRunner:
    """`trusted_local` must be explicitly True to execute local code: a CLI application or a
    Python callable (§16). A container is a configured sandbox that the policy approves by
    image instead."""
    kind = app.spec.runner
    if kind is RunnerKind.CLI:
        return CliRunner(
            app.spec, base_dir=app.base_dir, trusted_local=trusted_local, environ=environ
        )
    if kind is RunnerKind.PYTHON:
        return PythonRunner(
            app.spec, base_dir=app.base_dir, trusted_local=trusted_local, environ=environ
        )
    if kind is RunnerKind.CONTAINER:
        return ContainerRunner(app.spec, base_dir=app.base_dir, environ=environ)
    if kind is RunnerKind.OPENAI_COMPATIBLE:
        return OpenAICompatibleRunner(app.spec, base_dir=app.base_dir, environ=environ)
    return HttpRunner(app.spec, base_dir=app.base_dir, environ=environ)
