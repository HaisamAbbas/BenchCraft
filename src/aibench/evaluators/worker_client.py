"""Run a third-party evaluator in its own environment (§9, §10; 05-T3).

`WorkerEvaluator` is an `Evaluator` whose methods are carried out by a worker process
(`aibench.registry.eval_worker`) started with the plugin environment's Python. The
framework (e.g. DeepEval) is only ever imported in that process.

- One request at a time per worker; the scorer creates one worker per binding, so no two
  concurrent tasks can share a worker's mutable evaluator state.
- Cancellation is enforceable: when the harness's per-case timeout (or any cancellation)
  interrupts an evaluation, the worker's whole process tree is killed and the next case
  starts a fresh worker. A hung judge call cannot keep running behind the harness's back.
- The worker gets a minimal environment: an allow-list, a private working directory and
  HOME (so no `.env` or key files from the user's project or home are picked up), and only
  the secrets explicitly configured for this plugin environment. Error text coming back is
  redacted of those secrets.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from aibench.core.models import SCHEMA_VERSION, EvaluatorManifest, ExecutionStatus, MetricValue
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.runners.process_tree import ProcessTree, spawn_kwargs
from aibench.security.secrets import Redactor, resolve_secret

WORKER_ENV_ALLOWLIST = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
)
MAX_PROTOCOL_LINE_BYTES = 16 * 1024 * 1024
_STDERR_TAIL_BYTES = 8_192
_CLOSE_GRACE_SECONDS = 5.0
MAX_ERROR_CHARS = 2_000


class WorkerError(Exception):
    """The worker process failed, exited, or broke the protocol."""


@dataclass(frozen=True)
class WorkerSpec:
    """How to start workers for one plugin environment."""

    python: Path
    target: str  # entry point "module:attribute" inside the plugin environment
    extra_paths: tuple[Path, ...] = ()
    secret_env: Mapping[str, str] = field(default_factory=dict)  # name -> secret ref
    startup_timeout_seconds: float = 120.0


def make_worker_factory(manifest: EvaluatorManifest, spec: WorkerSpec) -> type[Evaluator]:
    """An `Evaluator` class bound to one worker-executed manifest."""
    return type(
        f"Worker_{manifest.evaluator_id.replace('.', '_')}",
        (WorkerEvaluator,),
        {"manifest": manifest, "spec": spec},
    )


class WorkerEvaluator(Evaluator):
    spec: ClassVar[WorkerSpec]

    def __init__(self) -> None:
        super().__init__()
        self._proc: asyncio.subprocess.Process | None = None
        self._tree: ProcessTree | None = None
        self._workdir: Path | None = None
        self._stderr_tail = bytearray()
        self._stderr_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._redactor = Redactor()
        self._reaping: set[asyncio.Task[None]] = set()
        self.restarts = 0

    # ------------------------------------------------------------------ lifecycle

    async def prepare(self, params: Mapping[str, Any]) -> None:
        self.params = dict(params)
        await self._start()

    async def ensure_ready(self) -> None:
        """Rebuild the worker if the previous one was killed (e.g. after a timeout). The
        scorer calls this outside the case's time budget."""
        async with self._start_lock:
            if self._proc is None:
                self.restarts += 1
                await self._start()  # a fresh process: no state carried over

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        await self.ensure_ready()  # no-op when the scorer already did it
        request = {
            "op": "evaluate",
            "case": view.case.model_dump(mode="json"),
            "execution": view.execution.model_dump(mode="json"),
        }
        try:
            response = await self._call(request)
        except WorkerError as exc:
            return EvaluationOutcome.error(self._redactor.text(f"worker_failed:{exc}"))
        if not response.get("ok"):
            return EvaluationOutcome.error(
                self._clean(f"worker_error:{response.get('error', 'unknown')}")
            )
        usages = response.get("usage", [])
        if not isinstance(usages, list):
            return EvaluationOutcome.error("conformance:invalid usage from worker: not a list")
        problems = [problem for usage in usages for problem in self._usage_problems(usage)]
        if problems:
            return EvaluationOutcome.error(
                self._clean(f"conformance:invalid usage from worker: {problems[0]}")
            )
        for usage in usages:
            provider = usage.get("provider")
            tokens = usage.get("tokens") or {}
            ctx.report_usage(
                provider=self._redactor.text(provider) if isinstance(provider, str) else provider,
                calls=usage.get("calls"),
                tokens={self._redactor.text(name): count for name, count in tokens.items()} or None,
                cost=usage.get("cost"),
            )
        return self._outcome(response.get("outcome") or {})

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                await asyncio.wait_for(self._call({"op": "close"}), _CLOSE_GRACE_SECONDS)
            except (WorkerError, TimeoutError):
                pass
        self._kill()
        if self._reaping:
            await asyncio.gather(*self._reaping, return_exceptions=True)
        if self._workdir is not None:
            shutil.rmtree(self._workdir, ignore_errors=True)
            self._workdir = None

    # ------------------------------------------------------------------ process management

    def _environment(self) -> dict[str, str]:
        env = {k: os.environ[k] for k in WORKER_ENV_ALLOWLIST if k in os.environ}
        assert self._workdir is not None
        env.update(
            HOME=str(self._workdir),
            USERPROFILE=str(self._workdir),
            PYTHONNOUSERSITE="1",
            PYTHONDONTWRITEBYTECODE="1",
            PYTHONIOENCODING="utf-8",
        )
        if self.spec.extra_paths:
            env["PYTHONPATH"] = os.pathsep.join(str(p) for p in self.spec.extra_paths)
        secrets = []
        for name, ref in self.spec.secret_env.items():
            value = resolve_secret(ref, os.environ)
            env[name] = value
            secrets.append((ref, value))
        self._redactor = Redactor(secrets)
        return env

    async def _start(self) -> None:
        self._workdir = self._workdir or Path(tempfile.mkdtemp(prefix="aibench-worker-"))
        self._stderr_tail = bytearray()  # never report a previous worker's stderr
        manifest = self.manifest
        self._proc = await asyncio.create_subprocess_exec(
            str(self.spec.python),
            "-m",
            "aibench.registry.eval_worker",
            self.spec.target,
            manifest.evaluator_id,
            manifest.version,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._workdir,
            env=self._environment(),
            limit=MAX_PROTOCOL_LINE_BYTES,
            **spawn_kwargs(),
        )
        self._tree = ProcessTree(self._proc.pid)
        self._stderr_task = asyncio.ensure_future(self._drain_stderr(self._proc))
        try:
            response = await asyncio.wait_for(
                self._call({"op": "prepare", "params": self.params}),
                self.spec.startup_timeout_seconds,
            )
        except (WorkerError, TimeoutError) as exc:
            self._kill()
            raise WorkerError(self._redactor.text(f"worker did not start: {exc}")) from exc
        if not response.get("ok"):
            self._kill()
            raise WorkerError(self._clean(f"worker prepare failed: {response.get('error')}"))
        version_problem = self._version_problem(response)
        if version_problem:
            self._kill()
            raise WorkerError(version_problem)

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        while chunk := await proc.stderr.read(65_536):
            self._stderr_tail.extend(chunk)
            del self._stderr_tail[:-_STDERR_TAIL_BYTES]

    def _kill(self) -> None:
        if self._tree is not None:
            self._tree.close()
            self._tree = None
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.kill()
            except ProcessLookupError:
                pass
        # Reap the child and let its stderr reader reach EOF (the tree kill closes the
        # pipe) so no subprocess transport outlives the worker.
        if self._proc is not None:
            proc, stderr_task = self._proc, self._stderr_task
            self._proc, self._stderr_task = None, None
            task = asyncio.ensure_future(_reap(proc, stderr_task))
            self._reaping.add(task)
            task.add_done_callback(self._reaping.discard)

    async def _call(self, request: dict[str, Any]) -> dict[str, Any]:
        """Send one request and read one response. Any interruption — including the
        harness's timeout cancelling this coroutine — kills the worker."""
        async with self._lock:
            proc = self._proc
            if proc is None or proc.stdin is None or proc.stdout is None:
                raise WorkerError("worker is not running")
            try:
                proc.stdin.write(json.dumps(request).encode("utf-8") + b"\n")
                await proc.stdin.drain()
                line = await proc.stdout.readline()
            except asyncio.CancelledError:
                self._kill()
                raise
            except (OSError, ValueError, asyncio.LimitOverrunError) as exc:
                self._kill()
                raise WorkerError(f"{type(exc).__name__}: {exc}") from exc
            if not line:
                tail = self._stderr_tail.decode("utf-8", "replace").strip().splitlines()
                self._kill()
                raise WorkerError(f"worker exited: {tail[-1] if tail else 'no output'}")
            try:
                response: dict[str, Any] = json.loads(line)
            except ValueError as exc:
                self._kill()
                raise WorkerError(f"invalid protocol line: {exc}") from exc
            return response

    def _outcome(self, raw: Mapping[str, Any]) -> EvaluationOutcome:
        value = raw.get("value")
        reason = raw.get("reason")
        return EvaluationOutcome(
            status=ExecutionStatus(raw.get("status", "error")),
            value=None if value is None else MetricValue.model_validate(value),
            reason=self._clean(reason) if isinstance(reason, str) else reason,
            evidence=tuple(self._redactor.text(str(e)) for e in raw.get("evidence") or ()),
            raw=self._redact_json(raw.get("raw")),
        )

    def _clean(self, text: str) -> str:
        """Redact configured secrets, then truncate, so a cut can never leave a partial
        secret behind."""
        return self._redactor.text(text)[:MAX_ERROR_CHARS]

    @staticmethod
    def _version_problem(response: Mapping[str, Any]) -> str | None:
        theirs = response.get("schema_version")
        if theirs != SCHEMA_VERSION:
            return (
                f"worker uses core schema {theirs!r} but this harness uses {SCHEMA_VERSION!r}; "
                "reinstall aibench into the plugin environment"
            )
        return None

    @staticmethod
    def _usage_problems(usage: Any) -> list[str]:
        if not isinstance(usage, Mapping):
            return ["usage entry must be an object"]
        problems = []
        calls, cost = usage.get("calls"), usage.get("cost")
        tokens = usage.get("tokens") or {}
        if calls is not None and (
            isinstance(calls, bool) or not isinstance(calls, int) or calls < 0
        ):
            problems.append(f"calls must be a non-negative integer or null, got {calls!r}")
        if cost is not None and (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            problems.append(f"cost must be a non-negative number or null, got {cost!r}")
        if not isinstance(tokens, Mapping) or not all(
            isinstance(k, str)
            and isinstance(v, int)
            and not isinstance(v, bool)
            and v >= 0
            for k, v in tokens.items()
        ):
            problems.append(f"tokens must map names to non-negative integers, got {tokens!r}")
        provider = usage.get("provider")
        if provider is not None and not isinstance(provider, str):
            problems.append(f"provider must be a string or null, got {provider!r}")
        return problems

    def _redact_json(self, value: Any) -> Any:
        if value is None:
            return None
        return json.loads(self._redactor.text(json.dumps(value, ensure_ascii=False)))


async def _reap(proc: asyncio.subprocess.Process, stderr_task: asyncio.Task[None] | None) -> None:
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except (TimeoutError, ProcessLookupError):
        pass
    if stderr_task is not None:
        try:
            await asyncio.wait_for(stderr_task, 5)
        except (TimeoutError, asyncio.CancelledError):
            stderr_task.cancel()
