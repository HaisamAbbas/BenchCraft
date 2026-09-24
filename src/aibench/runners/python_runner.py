"""Python callable runner (§7 "Python callable", 15-T1).

The callable runs in a fresh process of the application's own interpreter through
`python_shim.py`, so it inherits the CLI protocol's guarantees: JSON in and out, bounded
output, a timeout that kills the whole process tree, an allow-listed environment, and no
harness state in the application's process. Executing local code needs trusted-local mode:
a process is not a sandbox (§16).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aibench.core.errors import ConfigError
from aibench.core.models import ApplicationSpec, CliTransport, PythonTransport
from aibench.runners.base import ResetReport
from aibench.runners.cli_runner import CliRunner

SHIM = Path(__file__).with_name("python_shim.py")


def _cli_transport(t: PythonTransport, base_dir: Path) -> CliTransport:
    args = [str(SHIM)]
    for extra in t.paths:
        path = Path(extra)
        args += ["--path", str(path if path.is_absolute() else (base_dir / path).resolve())]
    return CliTransport(
        argv=(t.python, *args, _absolute_target(t.callable, base_dir)),
        cwd=t.cwd,
        output_mode="json",
        timeout_seconds=t.timeout_seconds,
        max_stdout_bytes=t.max_stdout_bytes,
        max_stderr_bytes=t.max_stderr_bytes,
        env=t.env,
        secret_env=t.secret_env,
        inherit_env=t.inherit_env,
    )


def _absolute_target(target: str, base_dir: Path) -> str:
    """A file target is resolved against the config file's directory, like `cwd`."""
    location, _, name = target.rpartition(":")
    if not location.endswith(".py"):
        return target
    path = Path(location)
    return f"{path if path.is_absolute() else (base_dir / path).resolve()}:{name}"


class PythonRunner(CliRunner):
    kind = "python"

    def __init__(
        self,
        spec: ApplicationSpec,
        *,
        base_dir: Path,
        trusted_local: bool,
        environ: Mapping[str, str] | None = None,
        lifecycle_timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(spec.transport, PythonTransport):
            raise ConfigError("PythonRunner requires an ApplicationSpec with a python transport")
        self.python_transport: PythonTransport = spec.transport
        super().__init__(
            spec,
            base_dir=base_dir,
            trusted_local=trusted_local,
            environ=environ,
            lifecycle_timeout_seconds=lifecycle_timeout_seconds,
            transport=_cli_transport(spec.transport, base_dir),
        )

    def _isolation(self) -> str:
        return (
            "process_per_invocation: the callable runs in a fresh interpreter per case; state "
            "it keeps outside the process is reset only by reset_callable"
        )

    def _limitations(self) -> tuple[str, ...]:
        return (
            (
                "trusted-local mode: the callable runs with your permissions; a process is not "
                "a sandbox"
            ),
            (
                "only what the callable returns is observable; retrieval, tools, usage, cost and "
                "world state are unknown unless it returns them and the output binding declares "
                "them"
            ),
        )

    @property
    def resettable(self) -> bool:
        return self.python_transport.reset_callable is not None

    async def _reset(self, seed: Any) -> ResetReport:
        target = self.python_transport.reset_callable
        if target is None:
            return ResetReport(
                "not_needed", "each invocation runs in a fresh process; no reset_callable"
            )
        head = self._argv[:-1]  # interpreter, shim and --path options
        return await self._run_reset(
            [head[0], head[1], "--reset", *head[2:], _absolute_target(target, self.base_dir)],
            seed,
        )
