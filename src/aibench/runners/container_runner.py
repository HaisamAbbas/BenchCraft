"""Container runner (§7 "Container", §16, 15-T1).

One container per invocation, started by the container engine's CLI with JSON on stdin and
stdout, so the CLI protocol's bounds apply. The command line is built only from the frozen
`ContainerTransport`:

- the image is pinned by digest and must already be present (nothing is pulled);
- a non-root user, a read-only root filesystem, `--cap-drop ALL`, `no-new-privileges`;
- read-only bind mounts only; writable space is tmpfs;
- memory (without extra swap), CPU and process-count limits;
- `--network none` unless the transport asks for `bridge` and the policy allows it;
- secrets reach the container as `-e NAME` from the engine client's environment, never
  as values on the command line.

Killing the engine client does not stop a container, so every invocation names its
container and removes it afterwards (`rm -f`), whatever the outcome. This is stronger
environment control, not a hostile multi-tenant sandbox (§16).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aibench.core.errors import ConfigError
from aibench.core.models import (
    ApplicationSpec,
    CliTransport,
    ContainerTransport,
    _engine_socket_path,
)
from aibench.runners.base import HealthReport, InvocationContext, InvocationOutcome, ResetReport
from aibench.runners.bindings import AppInputEnvelope
from aibench.runners.cli_runner import CORRELATION_ENV_VAR, CliRunner
from aibench.runners.process_tree import spawn_kwargs

_ENGINE_TIMEOUT_SECONDS = 30.0


def _engine_env(environ: Mapping[str, str]) -> tuple[str, ...]:
    """What the engine client itself needs from the harness environment to find the
    engine (never passed into the container)."""
    return tuple(
        name
        for name in (
            "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "HOME",
            "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "DOCKER_HOST",
            "DOCKER_CONFIG", "DOCKER_CONTEXT", "XDG_RUNTIME_DIR",
        )
        if name in environ
    )  # fmt: skip


def container_argv(t: ContainerTransport, base_dir: Path, name: str) -> list[str]:
    """The `run` command line for one invocation (without the engine executable)."""
    argv = [
        "run", "--pull=never", "--rm", "-i", "--name", name,
        "--network", t.network,
        "--read-only",
        "--user", t.user,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--memory", f"{t.memory_mb}m", "--memory-swap", f"{t.memory_mb}m",
        "--cpus", f"{t.cpus:g}",
        "--pids-limit", str(t.pids_limit),
        "--label", "aibench.managed=1",
    ]  # fmt: skip
    for target in t.tmpfs:
        argv += ["--tmpfs", f"{target}:rw,noexec,nosuid,size={t.tmpfs_size_mb}m"]
    for mount in t.mounts:
        source = Path(mount.source)
        source = source if source.is_absolute() else (base_dir / source).resolve()
        # `--mount` is a CSV value: quote the source field so a ',' in a host path is data.
        field = f"source={source}"
        if any(c in field for c in ',"'):
            field = '"' + field.replace('"', '""') + '"'
        argv += ["--mount", f"type=bind,{field},target={mount.target},readonly"]
    if t.workdir:
        argv += ["--workdir", t.workdir]
    for env_name in (*t.env, *t.secret_env, CORRELATION_ENV_VAR):
        argv += ["-e", env_name]  # the value comes from the client's environment
    return [*argv, t.image, *t.argv]


def _as_cli_transport(t: ContainerTransport, environ: Mapping[str, str]) -> CliTransport:
    return CliTransport(
        argv=(t.engine, "run"),  # placeholder; the real argv is built per invocation
        output_mode=t.output_mode,
        timeout_seconds=t.timeout_seconds,
        max_stdout_bytes=t.max_stdout_bytes,
        max_stderr_bytes=t.max_stderr_bytes,
        env=t.env,
        secret_env=t.secret_env,
        inherit_env=_engine_env(environ),
    )


class ContainerRunner(CliRunner):
    kind = "container"

    def __init__(
        self,
        spec: ApplicationSpec,
        *,
        base_dir: Path,
        environ: Mapping[str, str] | None = None,
        lifecycle_timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(spec.transport, ContainerTransport):
            raise ConfigError(
                "ContainerRunner requires an ApplicationSpec with a container transport"
            )
        environ = dict(os.environ if environ is None else environ)
        self.container: ContainerTransport = spec.transport
        # A configured sandbox, not trusted-local execution (§16); the policy approves the
        # image instead.
        super().__init__(
            spec,
            base_dir=base_dir,
            trusted_local=True,
            environ=environ,
            lifecycle_timeout_seconds=lifecycle_timeout_seconds,
            transport=_as_cli_transport(spec.transport, environ),
        )
        self._engine = ""

    def _transport_observables(self) -> tuple[str, ...]:
        return ("wall_time", "exit_status", "stderr")

    def _isolation(self) -> str:
        t = self.container
        return (
            f"container_per_invocation: a fresh container of {t.image} per case (user "
            f"{t.user}, read-only root, network {t.network}, {t.memory_mb} MB, {t.cpus:g} "
            f"CPU, {t.pids_limit} processes); nothing persists between cases"
        )

    def _limitations(self) -> tuple[str, ...]:
        return (
            (
                "a container is stronger environment control, not a hostile multi-tenant "
                "sandbox; it shares the host kernel"
            ),
            "per_episode state is not supported: each invocation gets a new container",
            (
                "only what the application prints is observable; retrieval, tools, usage, cost "
                "and world state are unknown unless it reports them and the output binding "
                "declares them"
            ),
        )

    async def _prepare(self) -> None:
        await super()._prepare()
        engine = shutil.which(self.container.engine, path=self._child_env.get("PATH"))
        if engine is None:
            raise ConfigError(f"container engine {self.container.engine!r} not found on PATH")
        self._engine = engine
        code, out = await self._engine_command("version", "--format", "{{.Server.Version}}")
        if code != 0:
            raise ConfigError(
                f"container engine {self.container.engine!r} is not running: {out[-300:]}"
            )
        code, _ = await self._engine_command("image", "inspect", self.container.image)
        if code != 0:
            raise ConfigError(
                f"image {self.container.image} is not present locally; nothing is pulled "
                f"automatically. Pull it first: {self.container.engine} pull "
                f"{self.container.image}"
            )
        for mount in self.container.mounts:
            source = Path(mount.source)
            source = (source if source.is_absolute() else self.base_dir / source).resolve()
            if not source.exists():
                raise ConfigError(f"container mount source does not exist: {source}")
            # Checked again on the resolved path: a symlink or relative path can lead to
            # the engine socket even when the configured string does not name it.
            if _engine_socket_path(source.as_posix()) or (
                source.is_dir() and any(_engine_socket_path(p.as_posix()) for p in source.iterdir())
            ):
                raise ConfigError(f"mount {source} exposes a container engine socket; refused")

    async def _healthcheck(self) -> HealthReport:
        code, _ = await self._engine_command("image", "inspect", self.container.image)
        if code == 0:
            return HealthReport("healthy", f"engine running; image {self.container.image} present")
        return HealthReport("unhealthy", f"image {self.container.image} not present")

    @property
    def resettable(self) -> bool:
        return False

    async def _reset(self, seed: Any) -> ResetReport:
        return ResetReport("not_needed", "each invocation runs in a new container")

    def _argv_for(self, ctx: InvocationContext) -> list[str]:
        return [self._engine, *container_argv(self.container, self.base_dir, _name(ctx))]

    async def _invoke(
        self, envelope: AppInputEnvelope, ctx: InvocationContext
    ) -> InvocationOutcome:
        try:
            return await super()._invoke(envelope, ctx)
        finally:
            # A timeout or cancellation kills the client, not the container.
            await asyncio.shield(self._engine_command("rm", "-f", _name(ctx)))

    async def _engine_command(self, *args: str) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            self._engine,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._child_env,
            **spawn_kwargs(),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_ENGINE_TIMEOUT_SECONDS)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return -1, f"{self.container.engine} {args[0]} timed out"
        code = proc.returncode if proc.returncode is not None else -1
        return code, out.decode("utf-8", "replace").strip()


def _name(ctx: InvocationContext) -> str:
    return "aibench-" + re.sub(r"[^a-zA-Z0-9_.-]", "", ctx.correlation_id)[:40]


def describe_command(t: ContainerTransport, base_dir: Path) -> str:
    """The command line a user can inspect (`app describe`)."""
    return json.dumps([t.engine, *container_argv(t, base_dir, "aibench-<invocation>")])
