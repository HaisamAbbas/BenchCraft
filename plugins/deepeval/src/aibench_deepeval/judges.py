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
from pydantic import ValidationError

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
    TimeoutError,  # a call that ran past its total deadline (see `_post`)
    httpx.DecodingError,  # a compressed reply cut or garbled in transit: the next try is fine
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


class _BadJSON(RuntimeError):
    """The reply is not JSON even after the usual repair; asking again often gives JSON."""


class _ReasoningRequired(_BadJSON):
    """The model refuses to run with thinking switched off (GLM 5.3 Flash on OpenRouter: "Reasoning
    is mandatory"). Asked again with the field left out, like a reply that cannot be read."""


_TRAILING_COMMA = re.compile(r",(\s*[}\]])")


def _undouble_braces(text: str) -> str:
    """Collapse `{{ ... }}` to `{ ... }` outside strings. DeepEval's prompts show their
    example JSON with doubled braces (they are escaped for formatting) and some models copy
    that literally. Only a doubled opener is collapsed, and only its own doubled closer is
    dropped, so ordinary nested objects (`}}` closing two) are left alone."""
    out: list[str] = []
    doubled: list[bool] = []  # one entry per open object: was it opened with `{{`
    in_string = escaped = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_string:
            out.append(c)
            if escaped:
                escaped = False
            elif c == "\\":
                escaped = True
            elif c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif c == "{":
            twice = text[i + 1 : i + 2] == "{"
            doubled.append(twice)
            out.append(c)
            i += 1 if twice else 0
        elif c == "}":
            twice = doubled.pop() if doubled else False
            out.append(c)
            i += 1 if twice and text[i + 1 : i + 2] == "}" else 0
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _load_json(text: str) -> Any:
    """`json.loads`, with the repairs for the near-misses models make: a comma before a
    closing bracket, and doubled braces copied from DeepEval's prompt. Tried only when the
    text is not valid as it is."""
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        for repaired in (
            _TRAILING_COMMA.sub(r"\1", text),
            _undouble_braces(text),
            _TRAILING_COMMA.sub(r"\1", _undouble_braces(text)),
        ):
            try:
                return json.loads(repaired)
            except json.JSONDecodeError:
                continue
        raise _BadJSON(f"judge reply is not valid JSON ({exc.msg}, char {exc.pos})") from exc


class _CutOff(RuntimeError):
    """The reply stopped at the output allowance, before the judge finished its JSON."""


def default_thinking(base_url: str) -> str:
    """Judging is classification against a rubric and DeepEval makes many small calls per
    case; a model that thinks first (GLM on Z.ai) took 76 to 197 s for one call and sometimes
    answered nothing. Z.ai accepts `thinking: disabled` on every model (non-thinking models
    ignore it), so it is off there unless the judge says otherwise. OpenRouter takes it as
    `reasoning: {"enabled": false}`: DeepSeek V4 Flash answered the same JSON in 2 s instead of
    8 s and at a sixth of the cost, and a faithfulness case that reasoned for over 300 s no
    longer does. Other endpoints are left at the provider's default."""
    host = (urlsplit(base_url).hostname or "").lower()
    stops_thinking = ("z.ai", "openrouter.ai")
    return "disabled" if host.endswith(stops_thinking) else "default"


def _thinking_field(base_url: str, thinking: str) -> dict[str, Any]:
    """The request field that turns a model's thinking on or off, in the endpoint's own form."""
    host = (urlsplit(base_url).hostname or "").lower()
    if host == "openrouter.ai" or host.endswith(".openrouter.ai"):
        return {"reasoning": {"enabled": thinking == "enabled"}}
    return {"thinking": {"type": thinking}}


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
        self._base_url = config["base_url"]
        self._thinking = str(config.get("thinking") or default_thinking(config["base_url"]))
        self._retry_wait = float(config.get("retry_wait_seconds", 2))
        # Room for a call that stalls to its deadline twice and then succeeds: a stalled upstream
        # is usually fine on the next try (2 of 3 stalled on one prompt, the third took 7 s).
        budget = max(_RETRY_BUDGET_SECONDS, 3 * self._timeout)
        self._retry_until = time.monotonic() + budget
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
                    response = self._post(client, prompt)
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
                    except (_CutOff, _BadJSON) as exc:
                        if attempt == _ATTEMPTS - 1 or (
                            isinstance(exc, _CutOff) and not self._grow()
                        ):
                            raise
                        self.retries += isinstance(exc, _BadJSON)
                        continue
                time.sleep(wait)
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def a_generate(self, prompt: str, schema: Any = None, **kwargs: Any) -> Any:
        async with httpx.AsyncClient(timeout=self._timeouts()) as client:
            for attempt in range(_ATTEMPTS):
                try:
                    response = await self._apost(client, prompt)
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
                    except (_CutOff, _BadJSON) as exc:
                        if attempt == _ATTEMPTS - 1 or (
                            isinstance(exc, _CutOff) and not self._grow()
                        ):
                            raise
                        self.retries += isinstance(exc, _BadJSON)
                        continue
                await asyncio.sleep(wait)
        raise AssertionError("unreachable: the last attempt returns or raises")

    async def _apost(self, client: httpx.AsyncClient, prompt: str) -> httpx.Response:
        try:
            return await asyncio.wait_for(
                client.post(self._url, headers=self._headers(), json=self._body(prompt)),
                self._timeout,
            )
        except TimeoutError as exc:  # asyncio's carries no text: say what ran out
            raise TimeoutError(f"no complete reply within {self._timeout:g} s") from exc

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

    def _post(self, client: httpx.Client, prompt: str) -> httpx.Response:
        """One request that cannot outlast `timeout_seconds` in total. The HTTP client's own
        timeout is per read, and OpenRouter keeps a waiting request alive with small
        "processing" bytes, so a stalled upstream never tripped it: a judge call hung for over
        ten minutes against a 180 s limit. The body is read in pieces against a deadline."""
        deadline = time.monotonic() + self._timeout
        with client.stream(
            "POST", self._url, headers=self._headers(), json=self._body(prompt)
        ) as streamed:
            chunks = []
            for chunk in streamed.iter_bytes():
                if time.monotonic() > deadline:
                    raise httpx.ReadTimeout(
                        f"no complete reply within {self._timeout:g} s", request=streamed.request
                    )
                chunks.append(chunk)
            if time.monotonic() > deadline:
                raise httpx.ReadTimeout(
                    f"no complete reply within {self._timeout:g} s", request=streamed.request
                )
            return httpx.Response(
                streamed.status_code,
                headers=streamed.headers,
                content=b"".join(chunks),
                request=streamed.request,
            )

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
            body.update(_thinking_field(self._base_url, self._thinking))
        return body

    def _parse(self, response: httpx.Response, schema: Any) -> Any:
        self.calls += 1
        if response.status_code >= 400:
            detail = response.text[:_ERROR_TEXT_LIMIT].replace(self._key, "[redacted]")
            if (
                response.status_code == 400
                and self._thinking == "disabled"
                and "reasoning is mandatory" in detail.lower()
            ):
                self._thinking = "default"  # this model always thinks: stop asking it not to
                raise _ReasoningRequired(f"judge HTTP 400: {detail}")
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
        try:
            return schema.model_validate(_load_json(text))
        except ValidationError as exc:
            # Valid JSON of the wrong shape (a model answered with a document instead of the
            # verdicts DeepEval asked for): asked again, like a reply that is not JSON.
            raise _BadJSON(
                f"judge reply does not match the expected shape: {exc.error_count()} error(s)"
            ) from exc


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
