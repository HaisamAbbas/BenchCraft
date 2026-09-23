"""HTTP runner integration tests over real loopback sockets (03-T1, 03-T3, 03-T4; gates
03-G1..G4). Servers are the real example apps or small stdlib test servers; the HTTP client
is real httpx — nothing is mocked."""

from __future__ import annotations

import asyncio
import json
import shutil
import ssl
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from aibench.core.errors import PolicyError
from aibench.core.models import EffectState, ErrorKind, ExecutionStatus
from aibench.runners import AppInputEnvelope, HttpRunner, InvocationContext
from tests.runner_support import (
    EXAMPLE_APPS,
    SENTINEL,
    golden_case,
    http_spec,
    load_example,
    run,
    serving,
)

rag_app = load_example("http_rag_app")
effect_app = load_example("effect_counter_app")


class _TestHandler(BaseHTTPRequestHandler):
    """/status/<code>, /redirect/<code>?to=<url>, /big/<bytes>, /badjson, /ok"""

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        sent_headers = headers or {}
        for key, value in sent_headers.items():
            self.send_header(key, value)
        if not any(key.lower() == "content-type" for key in sent_headers):
            self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        url = urlsplit(self.path)
        parts = url.path.strip("/").split("/")
        if parts[0] == "ok":
            self._send(200, b'{"output": "fine"}', {"Set-Cookie": "session=abc"})
        elif parts[0] == "mime-echo":
            token = self.headers.get("Authorization", "")
            self._send(
                200,
                b'{"output": "fine"}',
                {"Content-Type": f"application/json; note={token}"},
            )
        elif parts[0] == "status":
            self._send(int(parts[1]), b'{"error": "boom"}')
        elif parts[0] == "redirect":
            target = parse_qs(url.query)["to"][0]
            self._send(int(parts[1]), b"{}", {"Location": target})
        elif parts[0] == "big":
            self._send(200, b'{"output": "' + b"x" * int(parts[1]) + b'"}')
        elif parts[0] == "deep":
            self._send(200, b"[" * 200_000)
        elif parts[0] == "badjson":
            self._send(200, b"<html>not json</html>")
        else:
            self._send(404, b"{}")


def _test_server() -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", 0), _TestHandler)


def _runner(spec: Any, **kwargs: Any) -> HttpRunner:
    return HttpRunner(spec, base_dir=EXAMPLE_APPS, **kwargs)


async def _invoke(runner: HttpRunner, **ctx: Any) -> Any:
    case = ctx.pop("case", None) or golden_case()
    async with runner:
        return await runner.invoke(
            AppInputEnvelope.from_case(case),
            InvocationContext(run_id="r", case_id=case.case_id, **ctx),
        )


def _exchange(outcome: Any) -> dict[str, Any]:
    return json.loads(next(c.data for c in outcome.captures if c.name == "exchange"))


# --------------------------------------------------------------------------- 03-G1 / 03-G4


def test_rag_fixture_records_answer_and_actually_retrieved_documents() -> None:
    with serving(rag_app.make_server(port=0)) as base:
        spec = http_spec(
            f"{base}/answer",
            transport={"healthcheck_url": f"{base}/health"},
            input_binding={"template": {"top_k": 2}, "fields": {"/question": "/input"}},
            output_binding={
                "output": "/answer",
                "retrieved_context": "/retrieved",
                "retrieved_context_item": "/text",
            },
        )

        async def scenario() -> Any:
            async with _runner(spec) as runner:
                health = await runner.healthcheck()
                reset = await runner.reset()
                outcome = await runner.invoke(
                    AppInputEnvelope.from_case(golden_case()),
                    InvocationContext(run_id="r", case_id="case-1"),
                )
                return health, reset, outcome

        health, reset, outcome = run(scenario())
    assert health.status == "healthy"
    assert reset.status == "unsupported"  # no reset_url: server state is not reset
    assert outcome.status is ExecutionStatus.OK
    assert outcome.output == "Refunds may be requested within 30 days of purchase with a receipt."
    assert outcome.observations.retrieved_context[0] == outcome.output
    assert "Refunds may be requested within 30 days" not in json.dumps(
        golden_case().reference.model_dump(mode="json")  # type: ignore[union-attr]
    )  # retrieval came from the app's corpus, not from reference context
    assert outcome.completeness["retrieved_context"]["state"] == "observed"
    assert outcome.completeness["http_status"]["value"] == 200
    # The fixture exposes no usage or cost, so none is claimed.
    assert outcome.observations.usage is None and outcome.observations.cost is None
    assert outcome.completeness["usage"]["state"] == "unknown"
    assert outcome.timing["wall_ms"] > 0


def test_effectful_app_completed_request_and_reset() -> None:
    server = effect_app.make_server(port=0)
    with serving(server) as base:
        spec = http_spec(
            f"{base}/book",
            effects="reversible",
            transport={"reset_url": f"{base}/reset"},
            input_binding={"fields": {"/destination": "/input"}},
        )

        async def scenario() -> Any:
            async with _runner(spec) as runner:
                outcome = await runner.invoke(
                    AppInputEnvelope.from_case(golden_case(text="Dubai")),
                    InvocationContext(run_id="r", case_id="case-1"),
                )
                count_before_reset = server.count
                return outcome, count_before_reset, await runner.reset()

        outcome, count_before_reset, reset = run(scenario())
    assert outcome.output == "Booked trip to Dubai."
    assert outcome.effect_state is EffectState.COMPLETED
    assert count_before_reset == 1
    assert reset.status == "reset" and server.count == 0


@pytest.mark.parametrize(
    ("path", "kind"),
    [("status/500", ErrorKind.HTTP_STATUS), ("badjson", ErrorKind.INVALID_OUTPUT)],
)
def test_http_failures_are_recorded_with_the_response(path: str, kind: ErrorKind) -> None:
    with serving(_test_server()) as base:
        outcome = run(_invoke(_runner(http_spec(f"{base}/{path}", effects="reversible"))))
    assert outcome.status is ExecutionStatus.ERROR
    assert outcome.error_kind is kind
    assert outcome.effect_state is EffectState.COMPLETED  # the server answered
    body = next(c for c in outcome.captures if c.name == "response_body")
    assert body.data


def test_connection_refused_is_not_dispatched() -> None:
    server = _test_server()
    port = server.server_address[1]
    server.server_close()  # nothing listens on this port now
    outcome = run(
        _invoke(_runner(http_spec(f"http://127.0.0.1:{port}/ok", effects="irreversible")))
    )
    assert outcome.error_kind is ErrorKind.TRANSPORT
    assert outcome.effect_state is EffectState.NOT_DISPATCHED  # provably safe to retry
    assert _exchange(outcome)["dispatched"] is False


# --------------------------------------------------------------------------- 03-G2


def test_sentinel_never_reaches_the_server_and_secrets_are_redacted_in_captures() -> None:
    server = effect_app.make_server(port=0)
    environ = {"APP_TOKEN": "tok-abcdef123"}
    with serving(server) as base:
        spec = http_spec(
            f"{base}/book",
            transport={
                "headers": {"X-Client": "aibench-test"},
                "secret_headers": {"Authorization": {"ref": "env:APP_TOKEN", "prefix": "Bearer "}},
            },
        )
        outcome = run(_invoke(_runner(spec, environ=environ), correlation_id="corr-42"))
    assert outcome.status is ExecutionStatus.OK
    [received] = server.received
    assert SENTINEL not in json.dumps(received["headers"]) + received["body"].decode()
    assert json.loads(received["body"])["fixtures"] == {"visible": {"note": "shown to the app"}}
    assert received["headers"]["Authorization"] == "Bearer tok-abcdef123"
    assert received["headers"]["X-Request-ID"] == "corr-42"
    for capture in outcome.captures:
        assert SENTINEL.encode() not in capture.data, capture.name
        assert b"tok-abcdef123" not in capture.data, capture.name
    headers = _exchange(outcome)["request"]["headers"]
    assert headers["Authorization"] == "Bearer <redacted:env:APP_TOKEN>"


def test_sensitive_response_headers_are_not_persisted() -> None:
    with serving(_test_server()) as base:
        outcome = run(_invoke(_runner(http_spec(f"{base}/ok"))))
    assert _exchange(outcome)["response"]["headers"]["set-cookie"] == "<redacted>"


def test_response_content_type_redacts_an_echoed_secret() -> None:
    token = "tok-content-type"
    with serving(_test_server()) as base:
        spec = http_spec(
            f"{base}/mime-echo",
            transport={"secret_headers": {"Authorization": {"ref": "env:APP_TOKEN"}}},
        )
        outcome = run(_invoke(_runner(spec, environ={"APP_TOKEN": token})))
    body = next(c for c in outcome.captures if c.name == "response_body")
    assert token not in body.mime_type
    assert "<redacted:env:APP_TOKEN>" in body.mime_type


# --------------------------------------------------------------------------- 03-G3


def test_timeout_after_dispatch_is_an_unknown_effect_and_is_not_retried() -> None:
    server = effect_app.make_server(port=0, respond_delay=1.0)
    with serving(server) as base:
        spec = http_spec(f"{base}/book", effects="irreversible", transport={"timeout_seconds": 0.3})
        outcome = run(_invoke(_runner(spec)))
        time.sleep(1.2)  # let the server finish handling the request we abandoned
    assert outcome.status is ExecutionStatus.ERROR
    assert outcome.error_kind is ErrorKind.TIMEOUT
    assert outcome.effect_state is EffectState.UNKNOWN
    assert _exchange(outcome)["dispatched"] is True
    # The effect happened although the client gave up, and exactly one request was sent.
    assert server.count == 1
    assert len(server.received) == 1


def test_cooperative_cancel_mid_request_is_recorded_as_unknown_effect() -> None:
    server = effect_app.make_server(port=0, respond_delay=1.0)
    with serving(server) as base:
        spec = http_spec(f"{base}/book", effects="irreversible")

        async def scenario() -> Any:
            cancel = asyncio.Event()
            async with _runner(spec) as runner:
                task = asyncio.ensure_future(
                    runner.invoke(
                        AppInputEnvelope.from_case(golden_case()),
                        InvocationContext(run_id="r", case_id="case-1", cancel=cancel),
                    )
                )
                while not server.received:
                    await asyncio.sleep(0.02)
                cancel.set()
                return await task

        outcome = run(scenario())
        time.sleep(1.2)
    assert outcome.status is ExecutionStatus.CANCELLED
    assert outcome.effect_state is EffectState.UNKNOWN
    assert server.count == 1


def test_effect_free_app_reports_none_declared_even_on_timeout() -> None:
    with serving(effect_app.make_server(port=0, respond_delay=1.0)) as base:
        spec = http_spec(f"{base}/book", transport={"timeout_seconds": 0.3})
        outcome = run(_invoke(_runner(spec)))
        time.sleep(1.1)
    assert outcome.error_kind is ErrorKind.TIMEOUT
    assert outcome.effect_state is EffectState.NONE_DECLARED


# --------------------------------------------------------------------------- 03-T3 limits / policy


def test_redirects_are_refused_by_default() -> None:
    with serving(_test_server()) as base:
        url = f"{base}/redirect/307?to={base}/ok"
        outcome = run(_invoke(_runner(http_spec(url))))
    assert outcome.error_kind is ErrorKind.REDIRECT_REJECTED
    assert "follow_redirects is false" in outcome.error


def test_enabled_redirects_follow_only_policy_approved_307_308() -> None:
    with serving(_test_server()) as base, serving(_test_server()) as other:
        follow = {"follow_redirects": True}
        same_origin = run(
            _invoke(_runner(http_spec(f"{base}/redirect/308?to=/ok", transport=follow)))
        )
        escaping = run(
            _invoke(_runner(http_spec(f"{base}/redirect/307?to={other}/ok", transport=follow)))
        )
        method_changing = run(
            _invoke(_runner(http_spec(f"{base}/redirect/302?to=/ok", transport=follow)))
        )
        looping = run(
            _invoke(
                _runner(
                    http_spec(
                        f"{base}/redirect/307?to=/redirect/307%3Fto%3D/ok",
                        transport={"follow_redirects": True, "max_redirects": 1},
                    )
                )
            )
        )
    assert same_origin.status is ExecutionStatus.OK and same_origin.output == "fine"
    assert _exchange(same_origin)["redirects"] == [f"{base}/ok"]
    assert escaping.error_kind is ErrorKind.REDIRECT_REJECTED
    assert "outside the allowed endpoints" in escaping.error
    assert method_changing.error_kind is ErrorKind.REDIRECT_REJECTED
    assert looping.error_kind is ErrorKind.REDIRECT_REJECTED
    assert "more than 1 redirects" in looping.error


def test_response_size_limit_truncates_and_fails() -> None:
    with serving(_test_server()) as base:
        spec = http_spec(f"{base}/big/50000", transport={"max_response_bytes": 1000})
        outcome = run(_invoke(_runner(spec)))
    assert outcome.error_kind is ErrorKind.OUTPUT_LIMIT
    body = next(c for c in outcome.captures if c.name == "response_body")
    assert body.truncated and len(body.data) == 1000


def test_request_size_limit_is_enforced_before_sending() -> None:
    server = effect_app.make_server(port=0)
    with serving(server) as base:
        spec = http_spec(
            f"{base}/book", effects="irreversible", transport={"max_request_bytes": 10}
        )
        outcome = run(_invoke(_runner(spec)))
    assert outcome.error_kind is ErrorKind.REQUEST_LIMIT
    assert outcome.effect_state is EffectState.NOT_DISPATCHED
    assert server.received == []


def test_prepare_refuses_urls_outside_policy() -> None:
    cases = [
        http_spec("http://127.0.0.1:1/x", transport={"allowed_endpoints": ["http://127.0.0.1:2/"]}),
        http_spec("http://example.com/x"),  # plaintext to a non-loopback host
        http_spec("http://127.0.0.1:1/x", transport={"healthcheck_url": "http://127.0.0.1:9/h"}),
    ]
    for spec in cases:
        with pytest.raises(PolicyError):
            run(_runner(spec).prepare())


# --------------------------------------------------------------------------- TLS


def _self_signed_server(tmp_path: Path) -> tuple[ThreadingHTTPServer, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not available to generate a test certificate")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=127.0.0.1",
            "-addext",
            "subjectAltName=IP:127.0.0.1",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        capture_output=True,
    )
    server = _test_server()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    return server, cert


def test_tls_verification_is_on_by_default_and_a_ca_bundle_can_be_trusted(tmp_path: Path) -> None:
    server, cert = _self_signed_server(tmp_path)
    with serving(server) as base:
        url = base.replace("http://", "https://") + "/ok"
        untrusted = run(_invoke(_runner(http_spec(url, effects="irreversible"))))
        trusted = run(_invoke(_runner(http_spec(url, transport={"ca_bundle": str(cert)}))))
        unverified_runner = _runner(http_spec(url, transport={"verify_tls": False}))
        unverified = run(_invoke(unverified_runner))
    assert untrusted.error_kind is ErrorKind.TRANSPORT
    assert untrusted.effect_state is EffectState.NOT_DISPATCHED  # handshake failed first
    assert trusted.status is ExecutionStatus.OK and trusted.output == "fine"
    assert unverified.status is ExecutionStatus.OK
    assert any(
        "verification is disabled" in note for note in unverified_runner.describe().limitations
    )


# --------------------------------------------------------------------------- review regressions


def test_gateway_failure_on_an_effectful_app_is_an_unknown_effect() -> None:
    with serving(_test_server()) as base:
        outcome = run(_invoke(_runner(http_spec(f"{base}/status/503", effects="irreversible"))))
    assert outcome.error_kind is ErrorKind.HTTP_STATUS
    assert outcome.effect_state is EffectState.UNKNOWN  # a proxy answered, not the app


def test_deeply_nested_response_is_an_invalid_output_not_a_crash() -> None:
    with serving(_test_server()) as base:
        outcome = run(_invoke(_runner(http_spec(f"{base}/deep"))))
    assert outcome.status is ExecutionStatus.ERROR
    assert outcome.error_kind is ErrorKind.INVALID_OUTPUT


def test_secret_headers_are_not_forwarded_across_origins_on_redirect() -> None:
    target = effect_app.make_server(port=0)
    with serving(_test_server()) as base, serving(target) as other:
        spec = http_spec(
            f"{base}/redirect/307?to={other}/book",
            transport={
                "follow_redirects": True,
                "allowed_endpoints": [f"{base}/", f"{other}/"],
                "secret_headers": {"Authorization": {"ref": "env:APP_TOKEN"}},
            },
        )
        outcome = run(_invoke(_runner(spec, environ={"APP_TOKEN": "tok-crossorigin"})))
    assert outcome.status is ExecutionStatus.OK, outcome.error
    [received] = target.received
    assert "Authorization" not in received["headers"]
