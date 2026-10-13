"""HTTP request/response runner (§7 "HTTP protocol", 03-T3).

- JSON body built by JSON Pointer input bindings; response read through output bindings.
- Secret headers come from secret references and are redacted in every capture.
- TLS verification is on by default; proxies from the environment are ignored
  (`trust_env=False`) so traffic cannot be silently rerouted.
- Every URL, including each redirect hop, is checked against the endpoint policy.
  Redirects are refused unless enabled, and only 307/308 (method- and body-preserving) are
  ever followed.
- Request and response sizes are capped; the whole exchange has one deadline.
- Dispatch is tracked precisely (httpcore `send_request_headers` trace): a failure before
  that point is `not_dispatched`; after it and before a response it is `unknown` — the
  server may have acted (§7: "A timeout does not prove a server-side operation did not
  occur"). Runners never retry.
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from aibench.core.errors import ConfigError, PolicyError
from aibench.core.models import (
    ApplicationSpec,
    EffectState,
    ErrorKind,
    ExecutionStatus,
    HttpTransport,
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
from aibench.runners.streaming import ResponseStream, ResponseStreamResult
from aibench.security.endpoints import EndpointPolicy, origin_of
from aibench.security.secrets import Redactor, resolve_secret

_METHOD_PRESERVING_REDIRECTS = (307, 308)
_GATEWAY_FAILURES = (502, 503, 504)
_SENSITIVE_RESPONSE_HEADERS = {"set-cookie", "authorization", "proxy-authorization"}
_DISPATCH_EVENTS = ("http11.send_request_headers.started", "http2.send_request_headers.started")


class _ExchangeFailure(Exception):
    def __init__(self, kind: ErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass
class _Exchange:
    """Mutable record of one exchange, readable even if the exchange task is cancelled."""

    url: str
    dispatched: bool = False
    responded: bool = False
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    redirects: list[str] = field(default_factory=list)
    body: bytes = b""
    truncated: bool = False
    total_bytes: int = 0

    def dispatch_state(self) -> EffectState:
        if self.responded and self.status in _GATEWAY_FAILURES:
            # A proxy answered; whether the application behind it acted is unknown.
            return EffectState.UNKNOWN
        if self.responded:
            return EffectState.COMPLETED
        return EffectState.UNKNOWN if self.dispatched else EffectState.NOT_DISPATCHED


def _retry_after_seconds(headers: dict[str, str]) -> float | None:
    """Retry-After in its delta-seconds form (RFC 9110 §10.2.3); the HTTP-date form is
    ignored rather than trusting a remote clock."""
    value = headers.get("retry-after", "").strip()
    return float(value) if value.isdigit() else None


def _check_header(name: str, value: str) -> None:
    if any(c in name + value for c in "\r\n\0"):
        raise ConfigError(f"header {name!r} contains control characters")


def _build_client(**kwargs: Any) -> httpx.AsyncClient:
    """The client, plus the modules httpcore imports on its first request (the async
    backend and HTTP/1.1 parser), loaded here in the worker thread rather than on the loop
    during a run's first call."""
    import anyio._backends._asyncio
    import anyio._core._sockets
    import anyio.streams.tls  # noqa: F401
    import h11  # noqa: F401
    import httpcore._backends.anyio
    import httpcore._backends.auto  # noqa: F401

    return httpx.AsyncClient(**kwargs)


class HttpRunner(BaseRunner):
    kind = "http"

    def __init__(
        self,
        spec: ApplicationSpec,
        *,
        base_dir: Path,
        environ: Mapping[str, str] | None = None,
        lifecycle_timeout_seconds: float | None = None,
        transport: HttpTransport | None = None,
    ) -> None:
        """`transport` lets a derived runner (an OpenAI-compatible endpoint) describe its
        requests as the HTTP protocol."""
        chosen = transport if transport is not None else spec.transport
        if not isinstance(chosen, HttpTransport):
            raise ConfigError("HttpRunner requires an ApplicationSpec with an http transport")
        kwargs = {}
        if lifecycle_timeout_seconds is not None:
            kwargs["lifecycle_timeout_seconds"] = lifecycle_timeout_seconds
        super().__init__(spec, **kwargs)
        self.transport: HttpTransport = chosen
        self.base_dir = base_dir
        self._environ = dict(os.environ if environ is None else environ)
        self._policy: EndpointPolicy | None = None
        self._headers: dict[str, str] = {}
        self._redacted_headers: dict[str, str] = {}
        self._client: httpx.AsyncClient | None = None

    # ------------------------------------------------------------------ description

    def _transport_observables(self) -> tuple[str, ...]:
        return ("wall_time", "http_status")

    def _isolation(self) -> str:
        if self.transport.reset_url:
            return (
                f"resettable via reset_url (declared reset_policy={self.spec.reset_policy.value})"
            )
        return (
            "unknown: server-side state is shared across invocations unless the application "
            "isolates it; no reset_url configured"
        )

    def _limitations(self) -> tuple[str, ...]:
        notes = [
            (
                "only what the response exposes is observable; traces, retrieval, tools, "
                "usage and cost are unknown unless declared in the output binding"
            ),
            "a timed-out or cancelled request may still complete server-side",
        ]
        if not self.transport.verify_tls:
            notes.append("TLS certificate verification is disabled for this application")
        return tuple(notes)

    # ------------------------------------------------------------------ lifecycle

    async def _prepare(self) -> None:
        t = self.transport
        policy = EndpointPolicy(
            t.allowed_endpoints or (origin_of(t.url),),
            allow_plaintext_http=t.allow_plaintext_http,
        )
        for url in (t.url, t.healthcheck_url, t.reset_url):
            if url is not None:
                policy.check(url)
        self._policy = policy

        headers = dict(t.headers)
        redacted = dict(t.headers)
        secrets: list[tuple[str, str]] = []
        for name, header in t.secret_headers.items():
            value = resolve_secret(header.ref, self._environ)
            headers[name] = header.prefix + value
            redacted[name] = f"{header.prefix}<redacted:{header.ref}>"
            secrets.append((header.ref, value))
        for name, value in headers.items():
            _check_header(name, value)
        self._headers = headers
        self._redacted_headers = redacted
        self.redactor = Redactor(secrets)

        # Building the client loads TLS context and CA certificates (over a second on a
        # loaded Windows machine): done in a worker thread so the event loop, and a chat
        # sharing it, never freezes while a run starts (16-T4).
        self._client = await asyncio.to_thread(
            _build_client,
            verify=self._ssl_verify(),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx.Timeout(t.timeout_seconds, connect=t.connect_timeout_seconds),
        )

    def _ssl_verify(self) -> ssl.SSLContext | bool:
        t = self.transport
        if not t.verify_tls:
            return False
        if t.ca_bundle is None:
            return True
        path = Path(t.ca_bundle)
        path = path if path.is_absolute() else self.base_dir / path
        if not path.is_file():
            raise ConfigError(f"ca_bundle not found: {path}")
        return ssl.create_default_context(cafile=str(path))

    async def _healthcheck(self) -> HealthReport:
        url = self.transport.healthcheck_url
        if url is None:
            return HealthReport("unknown", "no healthcheck_url configured")
        try:
            response = await self._require_client().get(url, headers=self._headers)
        except httpx.HTTPError as exc:
            return HealthReport("unhealthy", f"healthcheck request failed: {type(exc).__name__}")
        if response.is_success:
            return HealthReport("healthy", f"healthcheck_url returned {response.status_code}")
        return HealthReport("unhealthy", f"healthcheck_url returned {response.status_code}")

    @property
    def resettable(self) -> bool:
        return self.transport.reset_url is not None

    async def _reset(self, seed: Any) -> ResetReport:
        url = self.transport.reset_url
        if url is None:
            return ResetReport("unsupported", "no reset_url configured; server state is not reset")
        body = {} if seed is None else seed
        try:
            response = await self._require_client().post(url, headers=self._headers, json=body)
        except httpx.HTTPError as exc:
            return ResetReport("failed", f"reset request failed: {type(exc).__name__}")
        if response.is_success:
            return ResetReport("reset", f"reset_url returned {response.status_code}")
        return ResetReport("failed", f"reset_url returned {response.status_code}")

    async def _close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _require_client(self) -> httpx.AsyncClient:
        assert self._client is not None, "prepare() creates the client"
        return self._client

    # ------------------------------------------------------------------ invoke

    async def _invoke(
        self, envelope: AppInputEnvelope, ctx: InvocationContext
    ) -> InvocationOutcome:
        return await self._invoke_http(envelope, ctx)

    async def _invoke_http(
        self,
        envelope: AppInputEnvelope,
        ctx: InvocationContext,
        *,
        response_stream: ResponseStream | None = None,
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
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        exchange = _Exchange(url=t.url)
        if len(body) > t.max_request_bytes:
            return self.outcome(
                clock,
                ctx,
                ExecutionStatus.ERROR,
                EffectState.NOT_DISPATCHED,
                error_kind=ErrorKind.REQUEST_LIMIT,
                error=f"request body of {len(body)} bytes exceeds {t.max_request_bytes}",
                captures=(self._exchange_capture(exchange, payload, ctx),),
            )

        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "Accept": response_stream.accept if response_stream is not None else "application/json",
            t.correlation_header: ctx.correlation_id,
        }
        work = asyncio.ensure_future(
            self._exchange(
                exchange,
                body,
                headers,
                on_response_chunk=response_stream.feed if response_stream is not None else None,
            )
        )
        failure: _ExchangeFailure | None = None
        try:
            reason = await race(work, timeout=t.timeout_seconds, cancel=ctx.cancel)
        except asyncio.CancelledError:
            work.cancel()
            raise
        if reason == "done":
            error = work.exception()
            if error is not None and not isinstance(error, _ExchangeFailure):
                raise error  # a bug, not an application failure: never disguise it
            failure = error
        else:
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)

        captures = (
            self._exchange_capture(exchange, payload, ctx),
            Capture(
                "response_body",
                self.redactor.data(exchange.body, truncated=exchange.truncated),
                self.redactor.text(
                    exchange.response_headers.get("content-type", "application/octet-stream")
                ),
                exchange.truncated,
            ),
        )
        extra = {
            "http_status": completeness(
                ObservationState.OBSERVED
                if exchange.status is not None
                else ObservationState.UNKNOWN,
                "present" if exchange.status is not None else "missing",
                value=exchange.status,
                retry_after_seconds=_retry_after_seconds(exchange.response_headers),
            )
        }
        dispatch = exchange.dispatch_state()
        stream_result: ResponseStreamResult | None = None

        def attach_stream(outcome: InvocationOutcome) -> InvocationOutcome:
            nonlocal stream_result
            if response_stream is None:
                return outcome
            if stream_result is None:
                stream_result = response_stream.finish()
            integrity = stream_result.integrity
            return replace(
                outcome,
                timing={**outcome.timing, "streaming": stream_result.metrics},
                completeness={
                    **outcome.completeness,
                    "stream_integrity": completeness(
                        ObservationState.OBSERVED,
                        "complete" if integrity["complete"] else "incomplete",
                        value=integrity,
                    ),
                },
            )

        def fail(
            status: ExecutionStatus, kind: ErrorKind, message: str, **kw: Any
        ) -> InvocationOutcome:
            return attach_stream(
                self.outcome(
                    clock,
                    ctx,
                    status,
                    dispatch,
                    error_kind=kind,
                    error=message,
                    captures=captures,
                    extra=extra,
                    **kw,
                )
            )

        if reason == "cancelled":
            return fail(ExecutionStatus.CANCELLED, ErrorKind.CANCELLED, "invocation cancelled")
        if reason == "timeout":
            return fail(
                ExecutionStatus.ERROR, ErrorKind.TIMEOUT, f"timed out after {t.timeout_seconds}s"
            )
        if failure is not None:
            return fail(ExecutionStatus.ERROR, failure.kind, str(failure))
        if exchange.truncated:
            return fail(
                ExecutionStatus.ERROR,
                ErrorKind.OUTPUT_LIMIT,
                f"response exceeded {t.max_response_bytes} bytes",
                output_detail="truncated",
            )
        assert exchange.status is not None
        if not 200 <= exchange.status < 300:
            return fail(ExecutionStatus.ERROR, ErrorKind.HTTP_STATUS, f"HTTP {exchange.status}")
        if response_stream is not None:
            stream_result = response_stream.finish()
            if stream_result.error is not None or stream_result.document is None:
                return fail(
                    ExecutionStatus.ERROR,
                    ErrorKind.INVALID_OUTPUT,
                    stream_result.error or "stream did not produce a response document",
                    output_detail="invalid",
                )
            raw_document = json.dumps(stream_result.document, ensure_ascii=False).encode("utf-8")
            try:
                document = parse_app_json(self.redactor.data(raw_document))
            except InvalidDocument as exc:
                return fail(
                    ExecutionStatus.ERROR,
                    ErrorKind.INVALID_OUTPUT,
                    f"stream response is not valid JSON: {exc}",
                    output_detail="invalid",
                )
        else:
            try:
                document = parse_app_json(self.redactor.data(exchange.body))
            except InvalidDocument as exc:
                return fail(
                    ExecutionStatus.ERROR,
                    ErrorKind.INVALID_OUTPUT,
                    f"response is not valid JSON: {exc}",
                    output_detail="invalid",
                )
        return attach_stream(
            self.outcome_from_document(
                clock, ctx, document, source="response_json", captures=captures, extra=extra
            )
        )

    async def _exchange(
        self,
        exchange: _Exchange,
        body: bytes,
        headers: dict[str, str],
        *,
        on_response_chunk: Callable[[bytes], None] | None = None,
    ) -> None:
        """Send the request, following only policy-approved 307/308 redirects. Raises
        `_ExchangeFailure`; records progress on `exchange` so a cancelled or timed-out call
        still reports how far it got."""
        t = self.transport
        client = self._require_client()
        assert self._policy is not None

        async def trace(event: str, info: Any) -> None:
            if event in _DISPATCH_EVENTS:
                exchange.dispatched = True

        url = t.url
        origin = origin_of(url)
        while True:
            try:
                self._policy.check(url)
            except PolicyError as exc:
                raise _ExchangeFailure(ErrorKind.POLICY_DENIED, str(exc)) from exc
            exchange.url = url
            hop_headers = headers
            if origin_of(url) != origin:
                # Credentials are scoped to the configured origin, even across an allowed
                # redirect (matching httpx's own cross-origin Authorization stripping).
                hop_headers = {k: v for k, v in headers.items() if k not in t.secret_headers}
            try:
                async with client.stream(
                    t.method, url, content=body, headers=hop_headers, extensions={"trace": trace}
                ) as response:
                    exchange.responded = True
                    exchange.status = response.status_code
                    exchange.response_headers = {
                        k.lower(): ("<redacted>" if k.lower() in _SENSITIVE_RESPONSE_HEADERS else v)
                        for k, v in response.headers.items()
                    }
                    if response.is_redirect:
                        url = self._next_hop(response, exchange)
                        continue
                    await self._read_body(
                        response, exchange, on_response_chunk=on_response_chunk
                    )
                    return
            except httpx.TimeoutException as exc:
                kind = ErrorKind.TRANSPORT if not exchange.dispatched else ErrorKind.TIMEOUT
                raise _ExchangeFailure(kind, f"{type(exc).__name__} contacting {url}") from exc
            except httpx.HTTPError as exc:
                raise _ExchangeFailure(
                    ErrorKind.TRANSPORT, f"{type(exc).__name__} contacting {url}"
                ) from exc

    def _next_hop(self, response: httpx.Response, exchange: _Exchange) -> str:
        t = self.transport
        location = response.headers.get("location", "")
        if not t.follow_redirects:
            raise _ExchangeFailure(
                ErrorKind.REDIRECT_REJECTED,
                f"HTTP {response.status_code} redirect to {location!r} not followed "
                "(follow_redirects is false)",
            )
        if response.status_code not in _METHOD_PRESERVING_REDIRECTS:
            raise _ExchangeFailure(
                ErrorKind.REDIRECT_REJECTED,
                f"HTTP {response.status_code} redirect would change the request method; "
                "only 307/308 are followed",
            )
        if len(exchange.redirects) >= t.max_redirects:
            raise _ExchangeFailure(
                ErrorKind.REDIRECT_REJECTED, f"more than {t.max_redirects} redirects"
            )
        target = str(response.url.join(location))
        assert self._policy is not None
        try:
            self._policy.check(target)
        except PolicyError as exc:
            raise _ExchangeFailure(
                ErrorKind.REDIRECT_REJECTED, f"redirect target refused by policy: {exc}"
            ) from exc
        exchange.redirects.append(target)
        # A new hop is a new request; the previous response is not the final one.
        exchange.responded = False
        exchange.status = None
        return target

    async def _read_body(
        self,
        response: httpx.Response,
        exchange: _Exchange,
        *,
        on_response_chunk: Callable[[bytes], None] | None = None,
    ) -> None:
        limit = self.transport.max_response_bytes
        buf = bytearray()
        async for chunk in response.aiter_bytes():  # decoded: the cap bounds decompression
            exchange.total_bytes += len(chunk)
            accepted = chunk[: max(0, limit - len(buf))]
            if accepted:
                buf.extend(accepted)
                if on_response_chunk is not None:
                    on_response_chunk(accepted)
            if exchange.total_bytes > limit:
                exchange.truncated = True
                break
        exchange.body = bytes(buf)

    def _exchange_capture(
        self, exchange: _Exchange, payload: Any, ctx: InvocationContext
    ) -> Capture:
        record = {
            "correlation_id": ctx.correlation_id,
            "request": {
                "method": self.transport.method,
                "url": self.transport.url,
                "headers": self._redacted_headers,
                "body": payload,
            },
            "final_url": exchange.url,
            "redirects": exchange.redirects,
            "dispatched": exchange.dispatched,
            "response": {
                "status": exchange.status,
                "headers": exchange.response_headers,
                "body_bytes": exchange.total_bytes,
                "truncated": exchange.truncated,
            },
        }
        data = json.dumps(record, ensure_ascii=False, indent=2).encode("utf-8")
        return Capture("exchange", self.redactor.data(data), "application/json")
