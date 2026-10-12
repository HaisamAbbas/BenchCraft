"""Coordinator API runner for remote application workers.

Workers make outbound authenticated requests to this service. They receive only the
app-visible case projection; the coordinator retains the run lease, budgets, retry policy,
artifacts, evaluations, and every workspace write.
"""

from __future__ import annotations

import asyncio
import hashlib
import ssl
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from aibench import __version__
from aibench.core.errors import AibenchError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    ApplicationSpec,
    EffectLevel,
    EffectState,
    ErrorKind,
    ExecutionStatus,
    deep_unfreeze,
)
from aibench.runners.base import (
    BaseRunner,
    HealthReport,
    InvocationContext,
    InvocationOutcome,
    ResetReport,
    Stopwatch,
)
from aibench.runners.bindings import AppInputEnvelope
from aibench.security.endpoints import is_loopback
from aibench.services.worker_protocol import (
    MAX_RESPONSE_BYTES,
    WORKER_PROTOCOL,
    WorkerAuthenticationError,
    WorkerProtocolError,
    json_bytes,
    outcome_from_json,
    read_request,
)

_ACTIVE_WORKER_TTL = 45.0
_TASK_LEASE_SECONDS = 45.0
_CLAIM_WAIT_SECONDS = 20.0
_COMPLETED_MEMORY = 4096
_MAX_WORKERS = 64
_MAX_CONCURRENCY = 64
MAX_ACTIVE_API_CONNECTIONS = 128


def _worker_runtime(value: Any) -> dict[str, str | int | None] | None:
    if not isinstance(value, dict):
        return None

    def text_field(name: str) -> str | None:
        item = value.get(name)
        if not isinstance(item, str):
            return None
        if any(ord(character) < 32 or ord(character) == 127 for character in item):
            return None
        item = item.strip()
        if not item:
            return None
        return item[:64]

    logical_cpus = value.get("logical_cpus")
    if type(logical_cpus) is not int or not 1 <= logical_cpus <= 4096:
        logical_cpus = None
    runtime: dict[str, str | int | None] = {
        "os": text_field("os"),
        "architecture": text_field("architecture"),
        "python": text_field("python"),
        "logical_cpus": logical_cpus,
    }
    return runtime if any(item is not None for item in runtime.values()) else None


@dataclass
class _Worker:
    worker_id: str
    client_id: str
    capacity: int
    last_seen: float
    runtime: dict[str, str | int | None] | None = None


@dataclass
class _Task:
    task_id: str
    payload: dict[str, Any]
    context: InvocationContext
    future: asyncio.Future[InvocationOutcome]
    worker_id: str | None = None
    lease_deadline: float | None = None
    cancelled: bool = False


class DistributedWorkerError(AibenchError):
    """The coordinator API could not safely start or complete remote work."""


class CoordinatorRunner(BaseRunner):
    """A `BaseRunner` backed by workers that pull invocation tasks over an API."""

    kind = "distributed"

    def __init__(
        self,
        spec: ApplicationSpec,
        *,
        run_id: str,
        options: dict[str, Any],
        token: str,
        cancel: asyncio.Event | None = None,
        stop_startup: Callable[[], bool] | None = None,
        on_ready: Callable[[dict[str, Any]], None] | None = None,
        event_sink: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        super().__init__(
            spec,
            lifecycle_timeout_seconds=float(options["startup_timeout_seconds"]) + 20.0,
        )
        self.run_id = run_id
        self.options = dict(options)
        self.token = token
        self.cancel = cancel
        self.stop_startup = stop_startup
        if (
            not token.isascii()
            or len(token) < 32
            or len(token) > 4096
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
        ):
            raise DistributedWorkerError(
                "worker API token must be 32 to 4096 visible ASCII characters"
            )
        self.on_ready = on_ready
        self.event_sink = event_sink
        self.application_hash = content_hash(spec.model_dump(mode="json"))
        self._server: asyncio.AbstractServer | None = None
        self._workers: dict[str, _Worker] = {}
        self._clients: dict[str, _Worker] = {}
        self._available_workers = asyncio.Event()
        self._tasks: dict[str, _Task] = {}
        self._leases: dict[str, str] = {}
        self._completed: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self._queue: deque[str] = deque()
        self._queue_changed = asyncio.Event()
        self._connections: set[asyncio.Task[None]] = set()
        self._connection_count = 0
        self._port: int | None = None
        self._advertised_url: str | None = None

    def _transport_observables(self) -> tuple[str, ...]:
        return ("wall_time",)

    def _isolation(self) -> str:
        return "remote application process; each worker owns its own configured environment"

    def _limitations(self) -> tuple[str, ...]:
        return (
            "workers must run the same frozen application specification",
            "reset-dependent application state is not supported by coordinator API v1",
            "a lost worker after leasing an invocation has an unknown dispatch outcome",
        )

    async def _prepare(self) -> None:
        host = self.options["listen_host"]
        port = int(self.options["listen_port"])
        certificate = self.options.get("tls_cert")
        private_key = self.options.get("tls_key")
        if bool(certificate) != bool(private_key):
            raise DistributedWorkerError("both coordinator TLS certificate and key are required")
        if not is_loopback(host) and not (certificate and private_key):
            raise DistributedWorkerError(
                "a non-loopback coordinator listener requires TLS; set both --worker-tls-cert "
                "and --worker-tls-key"
            )
        ssl_context = None
        if certificate and private_key:
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            try:
                ssl_context.load_cert_chain(certificate, private_key)
            except (OSError, ssl.SSLError) as exc:
                raise DistributedWorkerError(
                    f"could not load coordinator TLS identity: {exc}"
                ) from exc
        advertised = self.options.get("advertise_url")
        if advertised:
            try:
                parts = urlsplit(str(advertised))
                _ = parts.port
            except ValueError as exc:
                raise DistributedWorkerError(f"invalid coordinator advertise URL: {exc}") from exc
            if (
                parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
                or parts.path not in ("", "/")
            ):
                raise DistributedWorkerError(
                    "coordinator advertise URL must be an http(s) origin without credentials, "
                    "path, query, or fragment"
                )
            if parts.scheme == "https" and ssl_context is None:
                raise DistributedWorkerError("an HTTPS advertise URL requires coordinator TLS")
            if ssl_context is not None and parts.scheme != "https":
                raise DistributedWorkerError("a TLS coordinator requires an HTTPS advertise URL")
            if parts.scheme == "http" and not is_loopback(parts.hostname):
                raise DistributedWorkerError(
                    "worker API bearer tokens require HTTPS on non-loopback coordinator URLs"
                )

        try:
            self._server = await asyncio.start_server(
                self._accept,
                host,
                port,
                ssl=ssl_context,
                limit=64 * 1024,
                start_serving=True,
            )
        except OSError as exc:
            raise DistributedWorkerError(f"could not start coordinator API: {exc}") from exc
        sockets = self._server.sockets or ()
        if not sockets:
            raise DistributedWorkerError("coordinator API did not bind a listening socket")
        self._port = int(sockets[0].getsockname()[1])
        if advertised:
            self._advertised_url = advertised.rstrip("/")
        else:
            scheme = "https" if ssl_context is not None else "http"
            display_host = "127.0.0.1" if is_loopback(host) else host
            if ":" in display_host and not display_host.startswith("["):
                display_host = f"[{display_host}]"
            self._advertised_url = f"{scheme}://{display_host}:{self._port}"
        ready = {
            "url": self._advertised_url,
            "listen_host": host,
            "listen_port": self._port,
            "expected_workers": int(self.options["workers"]),
            "application_hash": self.application_hash,
            "protocol": WORKER_PROTOCOL,
            "token_env": self.options.get("token_env"),
            "trust_local_app": bool(self.options.get("trust_local_app")),
        }
        if self.event_sink is not None:
            self.event_sink("distributed_coordinator_ready", ready)
        if self.on_ready is not None:
            self.on_ready(ready)

        wanted = int(self.options["workers"])
        deadline = time.monotonic() + float(self.options["startup_timeout_seconds"])
        cancel_waiter = asyncio.create_task(self.cancel.wait()) if self.cancel is not None else None
        try:
            while True:
                self._prune_workers()
                if len(self._workers) >= wanted:
                    break
                if self.stop_startup is not None and self.stop_startup():
                    break
                if cancel_waiter is not None and cancel_waiter.done():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DistributedWorkerError(
                        f"timed out waiting for {wanted} worker(s); connected "
                        f"{len(self._workers)} worker(s) with capacity "
                        f"{sum(w.capacity for w in self._workers.values())}"
                    )
                self._available_workers.clear()
                availability = asyncio.create_task(self._available_workers.wait())
                waiters = {availability}
                if cancel_waiter is not None:
                    waiters.add(cancel_waiter)
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=min(remaining, 1.0),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if availability not in done:
                    availability.cancel()
                    await asyncio.gather(availability, return_exceptions=True)
        finally:
            if cancel_waiter is not None:
                cancel_waiter.cancel()
                await asyncio.gather(cancel_waiter, return_exceptions=True)

    async def _healthcheck(self) -> HealthReport:
        self._prune_workers()
        capacity = sum(worker.capacity for worker in self._workers.values())
        wanted = int(self.options["workers"])
        if len(self._workers) >= wanted:
            return HealthReport(
                "healthy", f"{len(self._workers)} connected worker(s), capacity {capacity}"
            )
        return HealthReport(
            "unhealthy", f"{len(self._workers)} workers are connected; {wanted} required"
        )

    async def _invoke(
        self, envelope: AppInputEnvelope, ctx: InvocationContext
    ) -> InvocationOutcome:
        payload = {
            "protocol": WORKER_PROTOCOL,
            "application_hash": self.application_hash,
            "run_id": ctx.run_id,
            "context": {
                "case_id": ctx.case_id,
                "repetition_id": ctx.repetition_id,
                "attempt_id": ctx.attempt_id,
                "correlation_id": ctx.correlation_id,
            },
            "input": deep_unfreeze(envelope.data),
        }
        # Serialize before allocating a task so malformed or oversized input is known not to
        # have reached a worker and can be recorded as a local request-limit failure.
        try:
            json_bytes(payload)
        except WorkerProtocolError as exc:
            return self.outcome(
                Stopwatch(),
                ctx,
                ExecutionStatus.ERROR,
                EffectState.NOT_DISPATCHED,
                error_kind=ErrorKind.REQUEST_LIMIT,
                error=str(exc),
            )
        loop = asyncio.get_running_loop()
        task_id = uuid.uuid4().hex
        future: asyncio.Future[InvocationOutcome] = loop.create_future()
        task = _Task(task_id, payload, ctx, future)
        self._tasks[task_id] = task
        self._queue.append(task_id)
        self._queue_changed.set()
        no_worker_since: float | None = None
        try:
            while True:
                if future.done():
                    return future.result()
                if ctx.cancel is not None and ctx.cancel.is_set():
                    task.cancelled = True
                    self._tasks.pop(task_id, None)
                    self._leases.pop(task_id, None)
                    return self.outcome(
                        Stopwatch(),
                        ctx,
                        ExecutionStatus.CANCELLED,
                        EffectState.UNKNOWN if task.worker_id else EffectState.NOT_DISPATCHED,
                        error_kind=ErrorKind.CANCELLED,
                        error="coordinator cancelled the worker invocation",
                    )
                self._prune_workers()
                now = time.monotonic()
                if self._workers:
                    no_worker_since = None
                elif no_worker_since is None:
                    no_worker_since = now
                elif now - no_worker_since >= float(self.options["startup_timeout_seconds"]):
                    task.cancelled = True
                    self._tasks.pop(task_id, None)
                    self._leases.pop(task_id, None)
                    return self.outcome(
                        Stopwatch(),
                        ctx,
                        ExecutionStatus.ERROR,
                        EffectState.NOT_DISPATCHED,
                        error_kind=ErrorKind.TRANSPORT,
                        error="no worker remained connected before the reconnect timeout expired",
                    )
                await self._expire_tasks()
                try:
                    await asyncio.wait_for(asyncio.shield(future), timeout=1.0)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            task.cancelled = True
            self._tasks.pop(task_id, None)
            self._leases.pop(task_id, None)
            raise

    async def _reset(self, seed: Any) -> ResetReport:
        del seed
        return ResetReport(
            "unsupported", "distributed coordinator v1 does not support reset-dependent runs"
        )

    async def _close(self) -> None:
        server = self._server
        if server is not None:
            server.close()
        connections = tuple(self._connections)
        for connection in connections:
            connection.cancel()
        if connections:
            await asyncio.gather(*connections, return_exceptions=True)
        if server is not None:
            await server.wait_closed()
            self._server = None
        for task in list(self._tasks.values()):
            if not task.future.done():
                task.future.set_result(
                    self.outcome(
                        Stopwatch(),
                        task.context,
                        ExecutionStatus.ERROR,
                        EffectState.UNKNOWN,
                        error_kind=ErrorKind.TRANSPORT,
                        error="coordinator stopped before a worker returned the invocation",
                    )
                )
        self._tasks.clear()
        self._leases.clear()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._connection_count >= MAX_ACTIVE_API_CONNECTIONS:
            writer.close()
            return
        self._connection_count += 1
        task = asyncio.create_task(self._handle(reader, writer))
        self._connections.add(task)

        def finished(done: asyncio.Task[None]) -> None:
            self._connections.discard(done)
            self._connection_count -= 1

        task.add_done_callback(finished)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        status = 400
        response: dict[str, Any] = {"error": "invalid request"}
        try:
            try:
                method, path, _, document = await asyncio.wait_for(
                    read_request(reader, expected_authorization=f"Bearer {self.token}"),
                    timeout=15.0,
                )
                if method != "POST":
                    status, response = 405, {"error": "method not allowed"}
                else:
                    status, response = await self._route(path, document)
            except WorkerAuthenticationError:
                status, response = 401, {"error": "unauthorized"}
            except (WorkerProtocolError, TimeoutError, ValueError) as exc:
                status, response = 400, {"error": str(exc)[:500]}
            except Exception:  # noqa: BLE001 - never disclose coordinator internals to a worker
                # Internal failures are deliberately not reflected to a remote worker.
                status, response = 500, {"error": "coordinator internal error"}
            try:
                body = json_bytes(response, maximum=MAX_RESPONSE_BYTES)
            except WorkerProtocolError:
                status = 500
                body = b'{"error":"coordinator response exceeded protocol limits"}'
            try:
                writer.write(
                    f"HTTP/1.1 {status} {_reason(status)}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n"
                    "Connection: close\r\n\r\n".encode("ascii")
                    + body
                )
                await writer.drain()
            except (ConnectionError, OSError):
                pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _route(self, path: str, document: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(document, dict):
            return 400, {"error": "request body must be an object"}
        if path == "/v1/register":
            return self._register(document)
        if path == "/v1/claim":
            return await self._claim(document)
        if path == "/v1/heartbeat":
            return self._heartbeat(document)
        if path == "/v1/complete":
            return self._complete(document)
        return 404, {"error": "unknown coordinator API route"}

    def _register(self, document: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if document.get("protocol") != WORKER_PROTOCOL:
            return 400, {"error": "unsupported worker protocol"}
        if document.get("application_hash") != self.application_hash:
            return 409, {"error": "worker application specification does not match this run"}
        if document.get("engine_version") != __version__:
            return 409, {"error": "worker BenchCraft version does not match the coordinator"}
        client_id = document.get("client_id")
        capacity = document.get("capacity")
        runtime = _worker_runtime(document.get("runtime"))
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 80:
            return 400, {"error": "client_id must be a non-empty string of at most 80 characters"}
        if type(capacity) is not int or not 1 <= capacity <= _MAX_CONCURRENCY:
            return 400, {"error": f"worker capacity must be between 1 and {_MAX_CONCURRENCY}"}
        self._prune_workers()
        worker = self._clients.get(client_id)
        if worker is None:
            if len(self._workers) >= _MAX_WORKERS:
                return 429, {"error": f"coordinator supports at most {_MAX_WORKERS} workers"}
            worker = _Worker(
                uuid.uuid4().hex, client_id, capacity, time.monotonic(), runtime=runtime
            )
            self._workers[worker.worker_id] = worker
            self._clients[client_id] = worker
        else:
            worker.capacity = capacity
            worker.last_seen = time.monotonic()
            worker.runtime = runtime
        self._available_workers.set()
        if self.event_sink is not None:
            event_payload: dict[str, Any] = {
                "worker_id": worker.worker_id,
                "capacity": worker.capacity,
            }
            if worker.runtime is not None:
                event_payload["runtime"] = worker.runtime
            self.event_sink(
                "distributed_worker_connected",
                event_payload,
            )
        return 200, {
            "protocol": WORKER_PROTOCOL,
            "worker_id": worker.worker_id,
            "application_hash": self.application_hash,
            "engine_version": __version__,
            "capacity": worker.capacity,
        }

    async def _claim(self, document: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        worker = self._worker(document)
        if worker is None:
            return 401, {"error": "worker is not registered"}
        worker.last_seen = time.monotonic()
        await self._expire_tasks()
        while self._queue:
            task_id = self._queue.popleft()
            task = self._tasks.get(task_id)
            if task is None or task.cancelled:
                continue
            task.worker_id = worker.worker_id
            task.lease_deadline = time.monotonic() + _TASK_LEASE_SECONDS
            self._leases[task_id] = worker.worker_id
            return 200, {"task": {"task_id": task_id, **task.payload}}
        self._queue_changed.clear()
        try:
            await asyncio.wait_for(self._queue_changed.wait(), timeout=_CLAIM_WAIT_SECONDS)
        except TimeoutError:
            pass
        await self._expire_tasks()
        while self._queue:
            task_id = self._queue.popleft()
            task = self._tasks.get(task_id)
            if task is None or task.cancelled:
                continue
            task.worker_id = worker.worker_id
            task.lease_deadline = time.monotonic() + _TASK_LEASE_SECONDS
            self._leases[task_id] = worker.worker_id
            return 200, {"task": {"task_id": task_id, **task.payload}}
        return 200, {"task": None}

    def _heartbeat(self, document: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        worker = self._worker(document)
        if worker is None:
            return 401, {"error": "worker is not registered"}
        worker.last_seen = time.monotonic()
        task_id = document.get("task_id")
        if not isinstance(task_id, str):
            return 400, {"error": "task_id is required"}
        task = self._tasks.get(task_id)
        if task is None or self._leases.get(task_id) != worker.worker_id:
            return 409, {"error": "task lease is no longer active"}
        now = time.monotonic()
        if task.lease_deadline is None or task.lease_deadline <= now:
            self._expire_task(task_id, task)
            return 409, {"error": "task lease is no longer active"}
        task.lease_deadline = now + _TASK_LEASE_SECONDS
        return 200, {"lease": "active"}

    def _complete(self, document: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        worker = self._worker(document)
        if worker is None:
            return 401, {"error": "worker is not registered"}
        worker.last_seen = time.monotonic()
        task_id = document.get("task_id")
        raw_outcome = document.get("outcome")
        if not isinstance(task_id, str) or not isinstance(raw_outcome, dict):
            return 400, {"error": "task_id and outcome are required"}
        outcome_hash = hashlib.sha256(
            json_bytes(raw_outcome, maximum=MAX_RESPONSE_BYTES)
        ).hexdigest()
        previous = self._completed.get(task_id)
        if previous is not None:
            if previous == (worker.worker_id, outcome_hash):
                self._completed.move_to_end(task_id)
                return 200, {"accepted": True, "duplicate": True}
            return 409, {"error": "a different result was already accepted for this task"}
        task = self._tasks.get(task_id)
        if task is None:
            return 409, {"error": "task is no longer active"}
        if self._leases.get(task_id) != worker.worker_id:
            return 409, {"error": "task is leased to a different worker"}
        now = time.monotonic()
        if task.lease_deadline is None or task.lease_deadline <= now:
            self._expire_task(task_id, task)
            return 409, {"error": "task lease is no longer active"}
        try:
            outcome = outcome_from_json(raw_outcome, correlation_id=task.context.correlation_id)
        except WorkerProtocolError as exc:
            return 400, {"error": str(exc)[:500]}
        if self.spec.effects is EffectLevel.NONE:
            outcome = replace(outcome, effect_state=EffectState.NONE_DECLARED)
        elif outcome.effect_state is EffectState.NONE_DECLARED:
            outcome = replace(outcome, effect_state=EffectState.UNKNOWN)
        if not task.future.done():
            task.future.set_result(outcome)
        self._tasks.pop(task_id, None)
        self._leases.pop(task_id, None)
        self._completed[task_id] = (worker.worker_id, outcome_hash)
        while len(self._completed) > _COMPLETED_MEMORY:
            self._completed.popitem(last=False)
        return 200, {"accepted": True, "duplicate": False}

    def _worker(self, document: dict[str, Any]) -> _Worker | None:
        worker_id = document.get("worker_id")
        if not isinstance(worker_id, str):
            return None
        self._prune_workers()
        return self._workers.get(worker_id)

    def _prune_workers(self) -> None:
        cutoff = time.monotonic() - _ACTIVE_WORKER_TTL
        for worker_id, worker in list(self._workers.items()):
            if worker.last_seen < cutoff:
                self._workers.pop(worker_id, None)
                if self._clients.get(worker.client_id) is worker:
                    self._clients.pop(worker.client_id, None)

    async def _expire_tasks(self) -> None:
        now = time.monotonic()
        for task_id, task in list(self._tasks.items()):
            if task.lease_deadline is None or task.lease_deadline > now:
                continue
            self._expire_task(task_id, task)

    def _expire_task(self, task_id: str, task: _Task) -> None:
        if not task.future.done():
            task.future.set_result(
                self.outcome(
                    Stopwatch(),
                    task.context,
                    ExecutionStatus.ERROR,
                    EffectState.UNKNOWN,
                    error_kind=ErrorKind.TIMEOUT,
                    error="worker lease expired after invocation dispatch; outcome is unknown",
                )
            )
        self._tasks.pop(task_id, None)
        self._leases.pop(task_id, None)


def _reason(status: int) -> str:
    return {
        200: "OK",
        400: "Bad Request",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        409: "Conflict",
        429: "Too Many Requests",
        500: "Internal Server Error",
    }.get(status, "Error")
