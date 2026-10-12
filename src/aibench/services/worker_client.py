"""Outbound API client for a distributed BenchCraft application worker."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import ssl
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx

from aibench import __version__
from aibench.core.models import EffectState, ErrorKind, ExecutionStatus
from aibench.runners import BaseRunner, InvocationContext
from aibench.runners.base import InvocationOutcome, Stopwatch
from aibench.runners.bindings import AppInputEnvelope
from aibench.security.endpoints import is_loopback
from aibench.services.worker_protocol import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    WORKER_PROTOCOL,
    WorkerProtocolError,
    json_bytes,
    outcome_to_json,
)

_HEARTBEAT_INTERVAL = 10.0
_HEARTBEAT_LEASE_FAILURES = 3
_RETRY_DELAY = 2.0


class WorkerClientError(RuntimeError):
    """The worker could not join or safely use its coordinator API."""


class DistributedWorker:
    """Poll the coordinator and execute only application invocations, never workspace work."""

    def __init__(
        self,
        runner: BaseRunner,
        *,
        coordinator_url: str,
        token: str,
        application_hash: str,
        capacity: int = 1,
        ca_bundle: str | None = None,
        report_runtime: bool = False,
        on_connected: Callable[[str], None] | None = None,
    ) -> None:
        self.runner = runner
        self.coordinator_url = _coordinator_url(coordinator_url)
        self.token = token
        self.application_hash = application_hash
        self.capacity = capacity
        self.ca_bundle = ca_bundle
        self.report_runtime = report_runtime
        self.on_connected = on_connected
        self.client_id = uuid.uuid4().hex
        self.worker_id: str | None = None
        self._stopping = asyncio.Event()
        if type(capacity) is not int or not 1 <= capacity <= 64:
            raise WorkerClientError("worker capacity must be between 1 and 64")
        if (
            not token.isascii()
            or len(token) < 32
            or len(token) > 4096
            or any(ord(character) < 33 or ord(character) > 126 for character in token)
        ):
            raise WorkerClientError("worker token must be 32 to 4096 visible ASCII characters")

    async def run(self) -> None:
        limits = httpx.Limits(max_connections=self.capacity + 4, max_keepalive_connections=4)
        timeout = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)
        async with httpx.AsyncClient(
            verify=self.ca_bundle or True,
            trust_env=False,
            timeout=timeout,
            limits=limits,
        ) as client:
            await self._register_when_available(client)
            async with asyncio.TaskGroup() as group:
                for _ in range(self.capacity):
                    group.create_task(self._poll(client))

    async def stop(self) -> None:
        self._stopping.set()

    def _registration_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "protocol": WORKER_PROTOCOL,
            "client_id": self.client_id,
            "application_hash": self.application_hash,
            "engine_version": __version__,
            "capacity": self.capacity,
        }
        if self.report_runtime:
            document["runtime"] = {
                "os": platform.system(),
                "architecture": platform.machine(),
                "python": platform.python_version(),
                "logical_cpus": os.cpu_count(),
            }
        return document

    async def _register_when_available(self, client: httpx.AsyncClient) -> None:
        document = self._registration_document()
        while not self._stopping.is_set():
            try:
                reply = await self._request(client, "/v1/register", document, timeout=15.0)
                worker_id = reply.get("worker_id")
                if (
                    reply.get("protocol") != WORKER_PROTOCOL
                    or reply.get("application_hash") != self.application_hash
                    or reply.get("engine_version") != __version__
                    or not isinstance(worker_id, str)
                    or not worker_id
                ):
                    raise WorkerClientError("coordinator returned an invalid registration response")
                self.worker_id = worker_id
                if self.on_connected is not None:
                    self.on_connected(worker_id)
                return
            except _CoordinatorMismatch:
                raise WorkerClientError(
                    "coordinator rejected this worker's application spec or protocol"
                ) from None
            except httpx.HTTPError as exc:
                if _is_tls_verification_error(exc):
                    raise WorkerClientError(
                        "coordinator TLS verification failed; check its certificate or "
                        "provide the correct --ca-bundle"
                    ) from exc
                await asyncio.sleep(_RETRY_DELAY)
            except (WorkerProtocolError, _CoordinatorUnavailable):
                await asyncio.sleep(_RETRY_DELAY)
        raise WorkerClientError("worker stopped before it could register")

    async def _poll(self, client: httpx.AsyncClient) -> None:
        assert self.worker_id is not None
        while not self._stopping.is_set():
            try:
                reply = await self._request(
                    client,
                    "/v1/claim",
                    {"worker_id": self.worker_id},
                    timeout=_CLAIM_TIMEOUT,
                )
            except (httpx.HTTPError, _CoordinatorUnavailable):
                await asyncio.sleep(_RETRY_DELAY)
                await self._register_when_available(client)
                assert self.worker_id is not None
                continue
            except _CoordinatorMismatch as exc:
                if exc.status_code != 401:
                    raise WorkerClientError(
                        f"coordinator rejected worker polling with HTTP {exc.status_code}"
                    ) from exc
                await self._register_when_available(client)
                assert self.worker_id is not None
                continue
            task = reply.get("task")
            if task is None:
                continue
            if not isinstance(task, dict):
                raise WorkerClientError("coordinator returned a malformed task")
            await self._execute_task(client, task)

    async def _execute_task(self, client: httpx.AsyncClient, task: dict[str, Any]) -> None:
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or task.get("protocol") != WORKER_PROTOCOL:
            raise WorkerClientError("coordinator returned an invalid task identity")
        if task.get("application_hash") != self.application_hash:
            raise WorkerClientError("coordinator task does not match this worker's app spec")
        run_id = task.get("run_id")
        context = task.get("context")
        payload = task.get("input")
        if (
            not isinstance(run_id, str)
            or not isinstance(context, dict)
            or not isinstance(payload, dict)
        ):
            raise WorkerClientError("coordinator task has invalid invocation fields")
        try:
            case_id = context["case_id"]
            repetition_id = context["repetition_id"]
            attempt_id = context["attempt_id"]
            correlation_id = context["correlation_id"]
            if (
                not isinstance(case_id, str)
                or type(repetition_id) is not int
                or repetition_id < 0
                or type(attempt_id) is not int
                or attempt_id < 1
                or not isinstance(correlation_id, str)
                or not 1 <= len(correlation_id) <= 128
            ):
                raise ValueError("invalid invocation context")
        except (KeyError, ValueError) as exc:
            raise WorkerClientError(f"coordinator task context is invalid: {exc}") from exc
        envelope = AppInputEnvelope(data=payload)
        invoke_context = InvocationContext(
            run_id=run_id,
            case_id=case_id,
            repetition_id=repetition_id,
            attempt_id=attempt_id,
            correlation_id=correlation_id,
        )
        heartbeat_stop = asyncio.Event()
        heartbeat_lost = asyncio.Event()
        heartbeat = asyncio.create_task(
            self._heartbeat_loop(client, task_id, heartbeat_stop, heartbeat_lost)
        )
        try:
            try:
                outcome = await self.runner.invoke(envelope, invoke_context)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a dispatch may already have happened
                outcome = self.runner.outcome(
                    Stopwatch(),
                    invoke_context,
                    ExecutionStatus.ERROR,
                    EffectState.UNKNOWN,
                    error_kind=ErrorKind.TRANSPORT,
                    error=self.runner.redactor.text(
                        f"worker invocation failed: {type(exc).__name__}: {exc}"
                    )[:4096],
                )
            if heartbeat_lost.is_set():
                return  # the coordinator has already recorded this lease as unknown
            try:
                document = self._completion_document(task_id, invoke_context, outcome)
            except WorkerProtocolError:
                # Keep the worker alive even if its bounded fallback cannot be encoded.
                raise WorkerClientError(
                    "could not encode a bounded worker completion for the coordinator"
                ) from None
            await self._complete_with_retry(client, document)
        finally:
            heartbeat_stop.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)

    def _completion_document(
        self, task_id: str, context: InvocationContext, outcome: InvocationOutcome
    ) -> dict[str, Any]:
        document: dict[str, Any] = {"worker_id": self.worker_id, "task_id": task_id}
        try:
            document["outcome"] = outcome_to_json(outcome)
            json_bytes(document, maximum=MAX_RESPONSE_BYTES)
        except WorkerProtocolError:
            limited = self.runner.outcome(
                Stopwatch(),
                context,
                ExecutionStatus.ERROR,
                outcome.effect_state,
                error_kind=ErrorKind.REQUEST_LIMIT,
                error="worker result exceeded the distributed protocol limits",
            )
            document["outcome"] = outcome_to_json(limited)
            json_bytes(document, maximum=MAX_RESPONSE_BYTES)
        return document

    async def _heartbeat_loop(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        stop: asyncio.Event,
        lost: asyncio.Event,
    ) -> None:
        failures = 0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL)
                return
            except TimeoutError:
                pass
            try:
                await self._request(
                    client,
                    "/v1/heartbeat",
                    {"worker_id": self.worker_id, "task_id": task_id},
                    timeout=10.0,
                )
                failures = 0
            except _CoordinatorMismatch:
                lost.set()
                return
            except (httpx.HTTPError, _CoordinatorUnavailable):
                failures += 1
                if failures >= _HEARTBEAT_LEASE_FAILURES:
                    # Keep the app invocation alive: the coordinator will mark it unknown
                    # after the lease expires. The result will be refused if that happened.
                    lost.set()
                    return

    async def _complete_with_retry(
        self, client: httpx.AsyncClient, document: dict[str, Any]
    ) -> None:
        for attempt in range(5):
            try:
                await self._request(client, "/v1/complete", document, timeout=20.0)
                return
            except _CoordinatorMismatch:
                return  # a timed-out lease is deliberately not reassigned or overwritten
            except (httpx.HTTPError, _CoordinatorUnavailable):
                if attempt == 4:
                    return
                await asyncio.sleep(min(_RETRY_DELAY * (attempt + 1), 10.0))

    async def _request(
        self,
        client: httpx.AsyncClient,
        path: str,
        document: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        body = json_bytes(document, maximum=MAX_REQUEST_BYTES)
        async with client.stream(
            "POST",
            self.coordinator_url + path,
            content=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        ) as response:
            if response.status_code in (400, 401, 403, 404, 409, 413):
                raise _CoordinatorMismatch(response.status_code)
            if response.status_code >= 500:
                raise _CoordinatorUnavailable(response.status_code)
            if response.status_code != 200:
                raise _CoordinatorUnavailable(response.status_code)
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise WorkerProtocolError("coordinator response exceeded the protocol limit")
                chunks.append(chunk)
            try:
                result = json.loads(b"".join(chunks))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise WorkerProtocolError("coordinator response was not valid JSON") from exc
            if not isinstance(result, dict):
                raise WorkerProtocolError("coordinator response must be an object")
            return result


class _CoordinatorUnavailable(Exception):
    pass


class _CoordinatorMismatch(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(status_code)
        self.status_code = status_code


_CLAIM_TIMEOUT = 25.0


def _coordinator_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        _ = parts.port
    except ValueError as exc:
        raise WorkerClientError(f"invalid coordinator URL: {exc}") from exc
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise WorkerClientError(
            "coordinator URL must be an http(s) origin and optional path without credentials, "
            "query, or fragment"
        )
    if parts.scheme == "http" and not is_loopback(parts.hostname):
        raise WorkerClientError("workers require HTTPS for a non-loopback coordinator")
    return value.rstrip("/")


def _is_tls_verification_error(error: BaseException) -> bool:
    pending: list[BaseException] = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, ssl.SSLError):
            return True
        detail = str(current).lower()
        if any(phrase in detail for phrase in ("certificate verify failed", "hostname mismatch")):
            return True
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return False
