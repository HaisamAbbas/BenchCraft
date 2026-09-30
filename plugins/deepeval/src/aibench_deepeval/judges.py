"""Judge models for the DeepEval metrics, as declared in a binding's `judge` parameter.

- `deepeval_model`: DeepEval's native model support, by name (e.g. an OpenAI model). The
  provider key reaches the worker only through the plugin environment's `secret_env`.
- `openai_compatible`: any Chat Completions endpoint (OpenAI, Z.ai GLM, a local server),
  implemented here, so no custom code is needed. The key is read from the worker environment
  variable the binding names; the harness puts it there from `secret_env`. It counts its own
  calls and tokens; cost is unknown (never zero) because prices are not known here.
- `python_factory`: a trusted function returning a `DeepEvalBaseLLM`, for anything else.

A new judge instance is built per case, so no judge state is shared between cases. DeepEval
is imported only when a judge is built (after the adapter has set DeepEval's environment),
never when manifests are listed.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import time
from functools import cache
from typing import Any
from urllib.parse import urlsplit

import httpx

JUDGE_SCHEMA: dict[str, Any] = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "model"],
            "properties": {
                "kind": {"const": "deepeval_model"},
                "model": {"type": "string", "minLength": 1},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "base_url", "model", "api_key_env"],
            "properties": {
                "kind": {"const": "openai_compatible"},
                "base_url": {"type": "string", "pattern": r"^https?://[^\s@]+$"},
                "model": {"type": "string", "minLength": 1},
                "api_key_env": {"type": "string", "pattern": r"^[A-Z_][A-Z0-9_]*$"},
                "timeout_seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": 600},
                "max_output_tokens": {"type": "integer", "minimum": 16, "maximum": 32768},
                "json_mode": {"type": "boolean"},
                "retry_wait_seconds": {"type": "number", "minimum": 0, "maximum": 60},
                "thinking": {"enum": ["default", "disabled", "enabled"]},
            },
        },
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["kind", "factory"],
            "properties": {
                "kind": {"const": "python_factory"},
                "factory": {"type": "string", "pattern": r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$"},
            },
        },
    ]
}

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")
_ERROR_TEXT_LIMIT = 300

# A rate limit (429), a busy or failing server (5xx) or a dropped connection is usually gone
# in seconds; free endpoints hit these often. Each is retried a few times with a growing
# wait (the server's `Retry-After` when it gives one), inside a total time limit so a case
# still fails cleanly before the harness's own per-case limit. Errors that repeating cannot
# fix (a wrong key, a bad request) are never retried.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_TRANSIENT = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadError,
)
_ATTEMPTS = 5
_DEFAULT_OUTPUT_TOKENS = 8000
_MAX_OUTPUT_TOKENS = 32768
_CONNECT_SECONDS = 15.0
_MAX_WAIT = 30.0
_RETRY_BUDGET_SECONDS = 200.0


class _CutOff(RuntimeError):
    """The reply stopped at the output allowance, before the judge finished its JSON."""


def default_thinking(base_url: str) -> str:
    """Judging is classification against a rubric and DeepEval makes many small calls per
    case; a model that thinks first (GLM on Z.ai) took 76 to 197 s for one call and sometimes
    answered nothing. Z.ai accepts `thinking: disabled` on every model (non-thinking models
    ignore it), so it is off there unless the judge says otherwise; other endpoints are left
    at the provider's default."""
    host = (urlsplit(base_url).hostname or "").lower()
    return "disabled" if host == "z.ai" or host.endswith(".z.ai") else "default"


class _OpenAICompatible:
    """A Chat Completions judge that answers DeepEval's prompts with JSON. Mixed into
    DeepEval's `DeepEvalBaseLLM` by `openai_compatible_judge`."""

    counts_own_usage = True
    name: Any

    def __init__(self, config: dict[str, Any]) -> None:
        key = os.environ.get(config["api_key_env"])
        if not key:
            raise RuntimeError(
                f"judge key {config['api_key_env']} is not set in the plugin environment "
                "(pass it with the plugin environment's secret_env)"
            )
        self._url = config["base_url"].rstrip("/") + "/chat/completions"
        self._key = key
        self._timeout = float(config.get("timeout_seconds", 120))
        # A reasoning model spends part of this on thinking before it writes the JSON.
        self._max_tokens = int(config.get("max_output_tokens", _DEFAULT_OUTPUT_TOKENS))
        self._json_mode = bool(config.get("json_mode", True))
        self._thinking = str(config.get("thinking") or default_thinking(config["base_url"]))
        self._retry_wait = float(config.get("retry_wait_seconds", 2))
        self._retry_until = time.monotonic() + _RETRY_BUDGET_SECONDS
        self.retries = 0
        self.calls = 0
        self.tokens: dict[str, int] = {}
        super().__init__(model=config["model"])  # type: ignore[call-arg]

    def load_model(self, *args: Any, **kwargs: Any) -> Any:
        return self

    def get_model_name(self, *args: Any, **kwargs: Any) -> str:
        return str(self.name)

    def generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        with httpx.Client(timeout=self._timeouts()) as client:
            for attempt in range(_ATTEMPTS):
                try:
                    response = client.post(
                        self._url, headers=self._headers(), json=self._body(prompt)
                    )
                except _TRANSIENT:
                    wait = self._wait(attempt, None)
                    if wait is None:
                        raise
                    time.sleep(wait)
                    continue
                wait = self._wait(attempt, response)
                if wait is None:
                    try:
                        return self._parse(response, schema)
                    except _CutOff:
                        if attempt == _ATTEMPTS - 1 or not self._grow():
                            raise
                        continue
                time.sleep(wait)
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(timeout=self._timeouts()) as client:
            for attempt in range(_ATTEMPTS):
                try:
                    response = await client.post(
                        self._url, headers=self._headers(), json=self._body(prompt)
                    )
                except _TRANSIENT:
                    wait = self._wait(attempt, None)
                    if wait is None:
                        raise
                    await asyncio.sleep(wait)
                    continue
                wait = self._wait(attempt, response)
                if wait is None:
                    try:
                        return self._parse(response, schema)
                    except _CutOff:
                        if attempt == _ATTEMPTS - 1 or not self._grow():
                            raise
                        continue
                await asyncio.sleep(wait)
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _wait(self, attempt: int, response: httpx.Response | None) -> float | None:
        """Seconds to wait before trying again, or None to stop: the reply is final (a
        success or an error retrying cannot fix), or attempts or time are used up. A
        transient failure has no reply (`response` None)."""
        if response is not None and response.status_code not in _RETRY_STATUSES:
            return None
        last = attempt == _ATTEMPTS - 1
        if last:
            return None
        wait = min(_MAX_WAIT, self._retry_wait * 2**attempt)
        header = response.headers.get("retry-after") if response is not None else None
        if header is not None:
            try:
                wait = min(_MAX_WAIT, max(0.0, float(header)))
            except ValueError:
                pass  # an HTTP date: keep the computed wait
        if time.monotonic() + wait > self._retry_until:
            return None
        self.retries += 1
        return wait

    def _timeouts(self) -> httpx.Timeout:
        """A reply may take `timeout_seconds`, but a connection that does not open within a
        few seconds is abandoned and retried: a stalled handshake held one case for 260 s."""
        return httpx.Timeout(self._timeout, connect=min(_CONNECT_SECONDS, self._timeout))

    def _grow(self) -> bool:
        """Double the output allowance for the next try; False once it is at its ceiling."""
        grown = min(self._max_tokens * 2, _MAX_OUTPUT_TOKENS)
        if grown == self._max_tokens:
            return False
        self._max_tokens = grown
        self.retries += 1
        return True

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"}

    def _body(self, prompt: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [
                {"role": "system", "content": "Reply with one JSON object and nothing else."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": self._max_tokens,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        if self._thinking != "default":
            body["thinking"] = {"type": self._thinking}
        return body

    def _parse(self, response: httpx.Response, schema: Any) -> Any:
        self.calls += 1
        if response.status_code >= 400:
            detail = response.text[:_ERROR_TEXT_LIMIT].replace(self._key, "[redacted]")
            raise RuntimeError(f"judge HTTP {response.status_code}: {detail}")
        payload = response.json()
        usage = payload.get("usage") or {}
        for ours, theirs in (("input", "prompt_tokens"), ("output", "completion_tokens")):
            if isinstance(usage.get(theirs), int):
                self.tokens[ours] = self.tokens.get(ours, 0) + usage[theirs]
        choice = payload["choices"][0]
        if choice.get("finish_reason") == "length":
            raise _CutOff(
                f"judge output was cut off at {self._max_tokens} tokens (a reasoning model "
                "spends them thinking before it answers): raise the judge's max_output_tokens "
                'or set its "thinking" to "disabled"'
            )
        content = choice["message"].get("content") or ""
        text = _FENCE.sub("", content.strip())
        if schema is None:
            return text
        return schema.model_validate(json.loads(text))


@cache
def _judge_class() -> type:
    from deepeval.models import DeepEvalBaseLLM

    return type("OpenAICompatibleJudge", (_OpenAICompatible, DeepEvalBaseLLM), {})


def openai_compatible_judge(config: dict[str, Any]) -> Any:
    return _judge_class()(config)


def build_judge(config: dict[str, Any]) -> Any:
    """What DeepEval's `model=` takes for this judge configuration: a model name for native
    support, or a fresh `DeepEvalBaseLLM` instance."""
    kind = config["kind"]
    if kind == "deepeval_model":
        return config["model"]
    if kind == "openai_compatible":
        return openai_compatible_judge(config)
    from deepeval.models import DeepEvalBaseLLM

    module_name, _, attribute = config["factory"].partition(":")
    instance = getattr(importlib.import_module(module_name), attribute)()
    if not isinstance(instance, DeepEvalBaseLLM):
        raise TypeError(f"{config['factory']} did not return a DeepEvalBaseLLM")
    return instance


def report_judge_usage(ctx: Any, metric: Any, judge: Any) -> None:
    """Report what is known about a case's judge calls. A judge that counts its own calls
    (`openai_compatible`) reports them; DeepEval's native models report tokens and cost; a
    custom judge that reports nothing leaves cost unknown."""
    if getattr(judge, "counts_own_usage", False):
        ctx.report_usage(
            provider=judge.get_model_name(), calls=judge.calls, tokens=judge.tokens, cost=None
        )
        return
    cost = getattr(metric, "evaluation_cost", None)
    tokens = {
        k: v
        for k, v in (
            ("input", getattr(metric, "input_tokens", None)),
            ("output", getattr(metric, "output_tokens", None)),
        )
        if isinstance(v, int)
    }
    if cost is not None or tokens:
        ctx.report_usage(
            provider=getattr(metric, "evaluation_model", None),
            calls=None,
            tokens=tokens,
            cost=None if cost is None else float(cost),
        )
