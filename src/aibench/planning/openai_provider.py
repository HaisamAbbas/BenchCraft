"""An OpenAI-compatible Chat Completions planner provider (07-T3).

Wire format, checked against OpenAI's published OpenAPI description
(openai/openai-openapi, CreateChatCompletionRequest/Response): request `model`,
`messages`, `tools` ([{type: "function", function: {name, description, parameters}}]),
`tool_choice`, `temperature`, `seed`, `max_tokens`; response
`choices[0].message.{content, tool_calls[{id, type, function: {name, arguments}}]}` where
`arguments` is a JSON string, and `usage.{prompt_tokens, completion_tokens}`. Tool results
go back as `{role: "tool", tool_call_id, content}` after the assistant message that made
the calls. Many servers implement this shape (hosted APIs, vLLM, Ollama); only this subset
is used.

Egress: the base URL must be loopback, or an https origin listed in the policy's
`allowed_planner_origins` (kept separate from application targets), and the API key is a
secret *reference* the policy must allow. Plain http is refused off loopback: the key would
travel in cleartext. What is sent is the planning
briefing (profile, dataset field counts, catalog) — never case contents.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from aibench.core.errors import AibenchError
from aibench.core.models import FrozenModel, SecretRefStr
from aibench.planning.planner import ModelReply, PlannerError, ToolCall
from aibench.security.endpoints import is_loopback, origin_of
from aibench.security.policy import ExecutionPolicy
from aibench.security.secrets import Redactor, resolve_secret

MAX_RESPONSE_BYTES = 2_000_000


class OpenAICompatibleConfig(FrozenModel):
    kind: Literal["openai_compatible"] = "openai_compatible"
    base_url: str  # e.g. "https://api.openai.com/v1"
    model: str = Field(min_length=1)
    api_key: SecretRefStr | None = None  # e.g. "env:OPENAI_API_KEY"
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    max_output_tokens: int = Field(default=4000, ge=1, le=100_000)
    temperature: float = Field(default=0.0, ge=0, le=2)
    seed: int | None = 0


def provider_denials(config: OpenAICompatibleConfig, policy: ExecutionPolicy) -> list[str]:
    denials = []
    try:
        parts = urlsplit(config.base_url)
        hostname = parts.hostname
        if parts.username or parts.password:
            return ["planner base_url must not contain credentials; use api_key"]
        if parts.scheme not in ("http", "https") or not hostname:
            return [f"planner base_url {config.base_url!r} is not an http(s) URL"]
        origin = origin_of(config.base_url)
    except (ValueError, AibenchError) as exc:
        return [f"planner base_url {config.base_url!r} is not a valid URL: {exc}"]
    if not is_loopback(hostname):
        if parts.scheme != "https":
            denials.append(
                f"planner endpoint {origin} uses plain http; the API key and briefing would "
                "travel in cleartext (use https)"
            )
        allowed = set()
        for entry in policy.allowed_planner_origins:
            try:
                allowed.add(origin_of(entry))
            except (ValueError, AibenchError):
                continue
        if origin not in allowed:
            denials.append(
                f"planner endpoint {origin} is not an approved destination (the planning "
                "briefing would leave this machine); add it to allowed_planner_origins"
            )
    if config.api_key is not None and config.api_key not in policy.allowed_secret_refs:
        denials.append(f"secret {config.api_key} is not allowed by the policy")
    return denials


class OpenAICompatibleProvider:
    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        environ: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self.name = f"openai_compatible:{origin_of(config.base_url)}"
        self.model = config.model
        env = dict(os.environ) if environ is None else dict(environ)
        headers = {"Content-Type": "application/json"}
        secrets: list[tuple[str, str]] = []
        if config.api_key is not None:
            key = resolve_secret(config.api_key, env)  # raises ConfigError if unset
            headers["Authorization"] = f"Bearer {key}"
            secrets.append((config.api_key, key))
        self._redactor = Redactor(secrets)
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/") + "/",
            headers=headers,
            timeout=config.timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelReply:
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
        }
        if self.config.seed is not None:
            body["seed"] = self.config.seed
        try:
            with self._client.stream("POST", "chat/completions", json=body) as response:
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise PlannerError(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
                status = response.status_code
        except httpx.HTTPError as exc:
            raise PlannerError(self._redactor.text(f"{type(exc).__name__}: {exc}")) from exc
        text = self._redactor.text(raw.decode("utf-8", errors="replace"))
        if status != 200:
            raise PlannerError(f"HTTP {status}: {text[:300]}")
        return parse_reply(text)


def parse_reply(text: str) -> ModelReply:
    """Parse a chat completion defensively: anything unexpected is a `PlannerError`, which
    the planning loop turns into a template fallback."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlannerError(f"unexpected chat completion response: {text[:300]}") from exc
    choices = data.get("choices") if isinstance(data, dict) else None
    first = choices[0] if isinstance(choices, list) and choices else None
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        raise PlannerError(f"unexpected chat completion response: {text[:300]}")
    calls = []
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise PlannerError("malformed tool_calls: expected a list")
    for raw in raw_calls:
        function = raw.get("function") if isinstance(raw, dict) else None
        if not isinstance(function, dict) or not isinstance(function.get("name"), str):
            raise PlannerError(f"malformed tool call: {raw!r}"[:300])
        arguments = function.get("arguments") or "{}"
        calls.append(
            ToolCall(
                call_id=str(raw.get("id", "")),
                name=function["name"],
                arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
            )
        )
    usage = data.get("usage")
    usage = usage if isinstance(usage, dict) else {}

    def count(name: str) -> int | None:
        value = usage.get(name)
        return (
            value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
        )

    content = message.get("content")
    return ModelReply(
        text=content if isinstance(content, str) else None,
        tool_calls=tuple(calls),
        prompt_tokens=count("prompt_tokens"),
        completion_tokens=count("completion_tokens"),
    )
