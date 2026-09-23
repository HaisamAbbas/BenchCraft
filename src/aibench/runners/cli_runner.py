"""One-shot CLI runner (§7 "CLI protocol", 03-T2).

- argv array only; no shell and no interpolation of case text into arguments. Case data
  reaches the process only as JSON on stdin.
- The child environment is built from an allow-list (`inherit_env`) plus explicitly
  configured `env` / `secret_env`. The harness's own environment — including evaluator
  credentials — is not inherited.
- stdout is capped (exceeding the cap kills the process: `output_limit`); stderr keeps a
  bounded prefix and is drained so the child never blocks on it.
- Timeout, cancellation and normal exit all end with the whole process tree killed.

Trusted-local mode only: a subprocess is not a sandbox (§16).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aibench.core.errors import ConfigError, PolicyError
from aibench.core.models import (
    ApplicationSpec,
    CliTransport,
    EffectState,
    ErrorKind,
    ExecutionStatus,
    ObservationState,
)
from aibench.runners.base import (
    BaseRunner,
    Capture,
    HealthReport,
    InvocationContext,
    InvocationOutcome,
    ResetReport,
    Stopwatch,
    race,
)
from aibench.runners.bindings import (
    AppInputEnvelope,
    BindingError,
    InvalidDocument,
    completeness,
    parse_app_json,
)
from aibench.runners.process_tree import ProcessTree, spawn_kwargs
from aibench.security.secrets import Redactor, resolve_secret

CORRELATION_ENV_VAR = "AIBENCH_CORRELATION_ID"
_READ_CHUNK = 65_536
# After the child exits or is killed, how long to wait for pipes to reach EOF.
_DRAIN_GRACE_SECONDS = 2.0
# How often the supervisor checks whether the direct child has exited; bounds the extra
# wall time a fast application can be charged.
_EXIT_POLL_SECONDS = 0.01


@dataclass
class _Stream:
    """Caller-owned capture buffer, filled incrementally so bytes already read survive
    even if the reader task is cancelled before EOF."""

    limit: int
    buf: bytearray = field(default_factory=bytearray)
    total_bytes: int = 0

    @property
    def data(self) -> bytes:
        return bytes(self.buf)

    @property
    def truncated(self) -> bool:
        return self.total_bytes > self.limit


async def _read_capped(
    stream: asyncio.StreamReader, sink: _Stream, overflow: asyncio.Event | None = None
) -> None:
    """Keep at most `sink.limit` bytes but always read to EOF (discarding the excess): a
    pipe left unread never closes, and asyncio then never releases the subprocess
    transport. `overflow` is set the moment the limit is first exceeded."""
    while True:
        chunk = await stream.read(_READ_CHUNK)
        if not chunk:
            break
        sink.total_bytes += len(chunk)
        room = sink.limit - len(sink.buf)
        if room > 0:
            sink.buf.extend(chunk[:room])
        if sink.truncated and overflow is not None:
            overflow.set()


async def _write_stdin(proc: asyncio.subprocess.Process, data: bytes) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write(data)
        await proc.stdin.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass  # the child exited or closed stdin without reading; its exit status tells why
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _terminate(proc: asyncio.subprocess.Process, tree: ProcessTree) -> None:
    tree.close()
    if proc.returncode is None:
        try:
            proc.kill()  # uncontained tree, or the tree kill has not been reaped yet
        except ProcessLookupError:
            pass


class CliRunner(BaseRunner):
    kind = "cli"

    def __init__(
        self,
        spec: ApplicationSpec,
        *,
        base_dir: Path,
        trusted_local: bool,
        environ: Mapping[str, str] | None = None,
        lifecycle_timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(spec.transport, CliTransport):
            raise ConfigError("CliRunner requires an ApplicationSpec with a cli transport")
        kwargs = {}
        if lifecycle_timeout_seconds is not None:
            kwargs["lifecycle_timeout_seconds"] = lifecycle_timeout_seconds
        super().__init__(spec, **kwargs)
        self.transport: CliTransport = spec.transport
        self.base_dir = base_dir
        self.trusted_local = trusted_local
        self._environ = dict(os.environ if environ is None else environ)
        self._argv: list[str] = []
        self._cwd: Path = base_dir
        self._child_env: dict[str, str] = {}

    # ------------------------------------------------------------------ description

    def _transport_observables(self) -> tuple[str, ...]:
        return ("wall_time", "exit_status", "stderr")

    def _isolation(self) -> str:
        return (
            "process_per_invocation: a fresh process per case; state the application keeps "
            "outside the process (files, databases, services) is not reset"
        )

    def _limitations(self) -> tuple[str, ...]:
        return (
            (
                "trusted-local mode: a subprocess is not a sandbox; hostile code isolation "
                "is unsupported"
            ),
            (
                "retrieval, tool calls, usage and cost are unknown unless the application "
                "reports them in its JSON output and the output binding declares them"
            ),
        )

    # ------------------------------------------------------------------ lifecycle

    async def _prepare(self) -> None:
        if not self.trusted_local:
            raise PolicyError(
                "executing a local application requires explicit trusted-local mode "
                "(a subprocess is not a sandbox)"
            )
        t = self.transport
        cwd = Path(t.cwd) if t.cwd else Path(".")
        self._cwd = (cwd if cwd.is_absolute() else self.base_dir / cwd).resolve()
        if not self._cwd.is_dir():
            raise ConfigError(f"cli cwd does not exist: {self._cwd}")

        env = {k: self._environ[k] for k in t.inherit_env if k in self._environ}
        env.update(t.env)
        secrets: list[tuple[str, str]] = []
        for name, ref in t.secret_env.items():
            value = resolve_secret(ref, self._environ)
            env[name] = value
            secrets.append((ref, value))
        self._child_env = env
        self.redactor = Redactor(secrets)
        self._argv = [self._resolve_executable(t.argv[0]), *t.argv[1:]]

    def _resolve_executable(self, program: str) -> str:
        """Resolve argv[0] once, up front, against the child's PATH (or the cwd for a
        relative path), so a missing program fails in `prepare` with a clear message."""
        candidate = Path(program)
        if candidate.is_absolute() or len(candidate.parts) > 1:
            path = candidate if candidate.is_absolute() else self._cwd / candidate
            if not path.is_file():
                raise ConfigError(f"cli executable not found: {path}")
            return str(path)
        found = shutil.which(program, path=self._child_env.get("PATH"))
        if found is None:
            raise ConfigError(f"cli executable {program!r} not found on PATH")
        return found

    async def _healthcheck(self) -> HealthReport:
        argv = self.transport.healthcheck_argv
        if argv is None:
            return HealthReport("unknown", "no healthcheck_argv configured")
        program = self._resolve_executable(argv[0])
        proc = await asyncio.create_subprocess_exec(
            program,
            *argv[1:],
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self._cwd,
            env=self._child_env,
            **spawn_kwargs(),
        )
        tree = ProcessTree(proc.pid)
        try:
            code = await proc.wait()
        finally:
            _terminate(proc, tree)
        if code == 0:
            return HealthReport("healthy", "healthcheck_argv exited 0")
        return HealthReport("unhealthy", f"healthcheck_argv exited {code}")

    async def _reset(self) -> ResetReport:
        return ResetReport(
            "not_needed", "each invocation runs in a fresh process; no in-process state"
        )

    async def _close(self) -> None:
        return None  # every invocation cleans up its own process tree

    # ------------------------------------------------------------------ invoke

    async def _invoke(
        self, envelope: AppInputEnvelope, ctx: InvocationContext
    ) -> InvocationOutcome:
        clock = Stopwatch()
        t = self.transport

        try:
            payload = self.input_binding.build_payload(envelope)
        except BindingError as exc:
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.NOT_DISPATCHED,
                error_kind=ErrorKind.BINDING,
                error=str(exc),
            )
        stdin_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        # Keep the original bytes for the application, but redact the persisted capture:
        # app-visible input can coincidentally contain a value also resolved as a secret.
        request = Capture("request", self.redactor.data(stdin_bytes), "application/json")

        env = dict(self._child_env)
        env[CORRELATION_ENV_VAR] = ctx.correlation_id
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self._cwd,
                env=env,
                **spawn_kwargs(),
            )
        except OSError as exc:
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.NOT_DISPATCHED,
                error_kind=ErrorKind.SPAWN_FAILED,
                error=f"could not start process: {exc}",
                captures=(request,),
            )

        tree = ProcessTree(proc.pid)
        assert proc.stdout is not None and proc.stderr is not None
        stdout_overflow = asyncio.Event()
        stdout, stderr = _Stream(t.max_stdout_bytes), _Stream(t.max_stderr_bytes)
        stdout_task = asyncio.ensure_future(_read_capped(proc.stdout, stdout, stdout_overflow))
        stderr_task = asyncio.ensure_future(_read_capped(proc.stderr, stderr))
        writer_task = asyncio.ensure_future(_write_stdin(proc, stdin_bytes))

        async def _finished() -> None:
            # Done when the direct child exits, or as soon as stdout overflows (so an
            # unbounded writer is stopped immediately). `proc.wait()` is not used here: it
            # only returns once every pipe closes, so a descendant that inherited stdout
            # would make a finished app look hung. `returncode` is set at child exit.
            overflow_wait = asyncio.ensure_future(stdout_overflow.wait())
            try:
                while proc.returncode is None and not stdout_overflow.is_set():
                    await asyncio.wait({overflow_wait}, timeout=_EXIT_POLL_SECONDS)
            finally:
                overflow_wait.cancel()

        work = asyncio.ensure_future(_finished())
        try:
            reason = await race(work, timeout=t.timeout_seconds, cancel=ctx.cancel)
        except asyncio.CancelledError:
            # Kill, then finish a bounded cleanup before re-raising so no pipe or process
            # transport outlives the cancelled invocation.
            _terminate(proc, tree)
            work.cancel()
            await self._drain(proc, stdout_task, stderr_task, writer_task)
            await self._reap(proc)
            raise
        finally:
            _terminate(proc, tree)  # timeout, cancel, overflow or normal exit: nothing survives

        work.cancel()
        await self._drain(proc, stdout_task, stderr_task, writer_task)
        exit_code = await self._reap(proc)

        stdout_bytes = self.redactor.data(stdout.data, truncated=stdout.truncated)
        stderr_bytes = self.redactor.data(stderr.data, truncated=stderr.truncated)
        captures = (
            request,
            Capture("stdout", stdout_bytes, self._stdout_mime(), stdout.truncated),
            Capture("stderr", stderr_bytes, "text/plain", stderr.truncated),
        )
        base_completeness = {
            "exit_status": completeness(
                ObservationState.OBSERVED if exit_code is not None else ObservationState.UNKNOWN,
                "present" if exit_code is not None else "missing",
                value=exit_code,
            ),
            "stderr": completeness(
                ObservationState.OBSERVED,
                "truncated"
                if stderr.truncated
                else ("empty" if not stderr.total_bytes else "present"),
                total_bytes=stderr.total_bytes,
            ),
        }

        if reason == "cancelled":
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.CANCELLED,
                EffectState.UNKNOWN,
                error_kind=ErrorKind.CANCELLED,
                error="invocation cancelled; process tree killed",
                captures=captures,
                extra=base_completeness,
            )
        if reason == "timeout":
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.UNKNOWN,
                error_kind=ErrorKind.TIMEOUT,
                error=f"timed out after {t.timeout_seconds}s; process tree killed",
                captures=captures,
                extra=base_completeness,
            )
        if stdout.truncated:
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.UNKNOWN,
                error_kind=ErrorKind.OUTPUT_LIMIT,
                error=f"stdout exceeded {t.max_stdout_bytes} bytes; process tree killed",
                captures=captures,
                extra=base_completeness,
                output_detail="truncated",
            )
        if exit_code != 0:
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.COMPLETED,
                error_kind=ErrorKind.NONZERO_EXIT,
                error=f"process exited with code {exit_code}",
                captures=captures,
                extra=base_completeness,
            )
        return self._parse_output(clock, ctx, stdout_bytes, captures, base_completeness)

    def _stdout_mime(self) -> str:
        return "application/json" if self.transport.output_mode == "json" else "text/plain"

    async def _drain(self, proc: asyncio.subprocess.Process, *tasks: asyncio.Future[None]) -> None:
        """Give readers a grace period to reach EOF. A reader still blocked after it (an
        uncontained descendant holds the pipe) is cancelled; its `_Stream` keeps the
        bytes read so far, and our ends of the pipes are closed so neither the pipes nor
        the subprocess transport outlive the invocation."""
        await asyncio.wait(tasks, timeout=_DRAIN_GRACE_SECONDS)
        if all(task.done() for task in tasks):
            return
        for task in tasks:
            task.cancel()
        # asyncio.subprocess.Process exposes no public close(); its transport does.
        transport = getattr(proc, "_transport", None)
        if transport is not None:
            transport.close()

    async def _reap(self, proc: asyncio.subprocess.Process) -> int | None:
        try:
            return await asyncio.wait_for(proc.wait(), timeout=_DRAIN_GRACE_SECONDS)
        except TimeoutError:
            # `wait()` also waits for pipes; the child's own exit status is still known.
            return proc.returncode

    def _parse_output(
        self,
        clock: Stopwatch,
        ctx: InvocationContext,
        stdout: bytes,
        captures: tuple[Capture, ...],
        extra: dict[str, dict[str, Any]],
    ) -> InvocationOutcome:
        text = stdout.decode("utf-8", errors="replace")
        if self.transport.output_mode == "text":
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.OK,
                EffectState.COMPLETED,
                output=text.rstrip("\r\n"),
                output_method="stdout_text",
                captures=captures,
                extra=extra,
            )
        try:
            document = parse_app_json(stdout)
        except InvalidDocument as exc:
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.COMPLETED,
                error_kind=ErrorKind.INVALID_OUTPUT,
                error=f"stdout is not valid JSON: {exc}",
                output_detail="invalid",
                captures=captures,
                extra=extra,
            )
        return self.outcome_from_document(
            clock, ctx, document, source="stdout_json", captures=captures, extra=extra
        )
