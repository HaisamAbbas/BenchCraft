"""OpenAI-compatible endpoint runner (§7, 15-T1).

The application is a chat-completions endpoint: each case becomes one
`POST {base_url}/chat/completions` request, through the HTTP protocol (endpoint policy,
bounded bodies, secret headers, redaction, dispatch tracking). What is observable is what
the endpoint returns: the message content, the usage it reports and any tool calls it
asks for. Tool calls here are requests by the model, not executed effects. Hidden
application internals and cost are never observed. This transport is unrelated to any
OpenAI evaluator plugin.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aibench.core.errors import ConfigError
from aibench.core.models import (
    ApplicationSpec,
    HttpSecretHeader,
    HttpTransport,
    OpenAICompatibleTransport,
    deep_unfreeze,
)
from aibench.runners.bindings import AppInputEnvelope, BindingError, InputBinding
from aibench.runners.http_runner import HttpRunner


def _http_transport(t: OpenAICompatibleTransport) -> HttpTransport:
    secret_headers = (
        {"Authorization": HttpSecretHeader(ref=t.api_key, prefix="Bearer ")} if t.api_key else {}
    )
    return HttpTransport(
        url=t.base_url.rstrip("/") + "/chat/completions",
        headers=t.headers,
        secret_headers=secret_headers,
        verify_tls=t.verify_tls,
        allow_plaintext_http=t.allow_plaintext_http,
        timeout_seconds=t.timeout_seconds,
        connect_timeout_seconds=t.connect_timeout_seconds,
        max_request_bytes=t.max_request_bytes,
        max_response_bytes=t.max_response_bytes,
    )


class ChatPayload:
    """Builds a chat-completions request from the app-visible envelope. A string input is
    the user message. An object input with `messages` supplies the conversation. An
    `input_binding` in the config, when given, selects the user message instead."""

    def __init__(self, t: OpenAICompatibleTransport, binding: InputBinding | None) -> None:
        self.transport = t
        self.binding = binding

    def build_payload(self, envelope: AppInputEnvelope) -> dict[str, Any]:
        t = self.transport
        value = (
            self.binding.build_payload(envelope)
            if self.binding is not None
            else envelope.data.get("input")
        )
        messages: list[Any] = []
        if t.system_prompt:
            messages.append({"role": "system", "content": t.system_prompt})
        if isinstance(value, dict) and isinstance(value.get("messages"), list):
            messages.extend(value["messages"])
        elif isinstance(value, str):
            messages.append({"role": "user", "content": value})
        else:
            raise BindingError(
                "an OpenAI-compatible application needs a string input or an object with "
                "a 'messages' list (or an input_binding selecting one)"
            )
        payload: dict[str, Any] = {**deep_unfreeze(t.parameters), "model": t.model}
        payload["messages"] = messages
        if t.tools:
            payload["tools"] = [deep_unfreeze(tool) for tool in t.tools]
        return payload


class OpenAICompatibleRunner(HttpRunner):
    kind = "openai_compatible"

    def __init__(
        self,
        spec: ApplicationSpec,
        *,
        base_dir: Path,
        environ: Mapping[str, str] | None = None,
        lifecycle_timeout_seconds: float | None = None,
    ) -> None:
        if not isinstance(spec.transport, OpenAICompatibleTransport):
            raise ConfigError(
                "OpenAICompatibleRunner requires an ApplicationSpec with an "
                "openai_compatible transport"
            )
        self.openai: OpenAICompatibleTransport = spec.transport
        super().__init__(
            spec,
            base_dir=base_dir,
            environ=environ,
            lifecycle_timeout_seconds=lifecycle_timeout_seconds,
            transport=_http_transport(spec.transport),
        )
        declared = deep_unfreeze(spec.input_binding) or {}
        self.input_binding = ChatPayload(  # type: ignore[assignment]
            spec.transport, InputBinding.from_spec(declared) if declared else None
        )

    def _isolation(self) -> str:
        return (
            "stateless_request: each case is one chat-completions request; any state the "
            "endpoint keeps is not visible or reset"
        )

    def _limitations(self) -> tuple[str, ...]:
        return (
            (
                "observes only the endpoint's response: message content, reported usage and "
                "requested tool calls; hidden application internals are unknown"
            ),
            "tool calls are requests by the model, not executed effects",
            "cost is unknown: endpoints report tokens, not prices",
            (
                "responses from a sampled model vary between runs unless the endpoint is "
                "deterministic"
            ),
        )
