"""JSON Pointer input/output bindings (§7: "Use JSON Pointer-style mappings instead of
arbitrary template execution").

Input bindings can only read from the app-visible envelope (`AppInputEnvelope`), which is
built from `BenchmarkCase.application_input_projection()` and therefore never contains
`reference` data or fixtures not marked `app_visible` — the leakage boundary is structural,
not a filter applied afterwards.

Output bindings read declared fields from the application's JSON response. An undeclared
capability is `unknown`; a declared one that is absent is `missing`. Neither is ever
fabricated (§7: "Tool calls, token usage, retrieval results, and cost remain unknown unless
exposed").
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import ConfigError
from aibench.core.models import ObservationState, deep_unfreeze

MISSING: Any = object()  # sentinel: pointer path does not exist


class BindingError(Exception):
    """A binding could not be applied to a concrete document."""


class InvalidDocument(ValueError):
    """Application output that is not safe, strict JSON."""


MAX_DOCUMENT_DEPTH = 64


def _reject_constant(name: str) -> Any:
    raise InvalidDocument(f"non-standard JSON constant {name} is not allowed")


def parse_app_json(data: bytes | str) -> Any:
    """Parse untrusted application output. Every way it can be unusable — invalid UTF-8,
    invalid JSON, NaN/Infinity, oversized integers, lone surrogates (valid JSON escapes
    that cannot be stored), nesting deeper than `MAX_DOCUMENT_DEPTH` — raises
    `InvalidDocument`, so it is recorded as the application's failure and can never crash
    the invocation or the commit of its result."""
    try:
        text = data.decode("utf-8") if isinstance(data, bytes) else data
        document = json.loads(text, parse_constant=_reject_constant)
        # `json.loads` accepts exponent overflow such as `1e999` as `float('inf')`,
        # even though explicit NaN/Infinity tokens are rejected above. Re-serializing
        # with `allow_nan=False` rejects those non-finite values as well as lone
        # surrogates, before they reach a result or artifact serializer.
        json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except InvalidDocument:
        raise
    except (ValueError, RecursionError) as exc:  # JSONDecodeError/UnicodeError are ValueErrors
        raise InvalidDocument(f"{type(exc).__name__}: {exc}"[:500]) from exc
    stack = [(document, 1)]
    while stack:
        value, depth = stack.pop()
        if depth > MAX_DOCUMENT_DEPTH:
            raise InvalidDocument(f"JSON nesting exceeds {MAX_DOCUMENT_DEPTH} levels")
        if isinstance(value, dict):
            stack.extend((v, depth + 1) for v in value.values())
        elif isinstance(value, list):
            stack.extend((v, depth + 1) for v in value)
    return document


# --------------------------------------------------------------------------- JSON Pointer


def _parse_pointer(pointer: str) -> list[str]:
    if pointer == "":
        return []
    if not pointer.startswith("/"):
        raise ConfigError(f"JSON pointer must be empty or start with '/': {pointer!r}")
    return [token.replace("~1", "/").replace("~0", "~") for token in pointer[1:].split("/")]


def resolve_pointer(document: Any, pointer: str) -> Any:
    """RFC 6901 lookup. Returns `MISSING` when the path does not exist."""
    current = document
    for token in _parse_pointer(pointer):
        if isinstance(current, Mapping):
            if token not in current:
                return MISSING
            current = current[token]
        elif isinstance(current, (list, tuple)):
            if not token.isdigit() or int(token) >= len(current):
                return MISSING
            current = current[int(token)]
        else:
            return MISSING
    return current


def set_pointer(document: dict[str, Any], pointer: str, value: Any) -> dict[str, Any]:
    """Set `value` at `pointer` inside object-only paths, creating intermediate objects."""
    tokens = _parse_pointer(pointer)
    if not tokens:
        raise BindingError("cannot bind to the document root; use a field pointer like /q")
    current: Any = document
    for token in tokens[:-1]:
        nxt = current.setdefault(token, {})
        if not isinstance(nxt, dict):
            raise BindingError(f"pointer {pointer!r} crosses a non-object value at {token!r}")
        current = nxt
    current[tokens[-1]] = value
    return document


# --------------------------------------------------------------------------- input


@dataclass(frozen=True)
class AppInputEnvelope:
    """The only data an application runner ever receives about a case."""

    data: Mapping[str, Any]

    @classmethod
    def from_case(cls, case: Any) -> AppInputEnvelope:
        return cls(data=case.application_input_projection())


class InputBinding(BaseModel):
    """`fields` maps a destination pointer in the request payload to a source pointer in the
    app-visible envelope (`/case_id`, `/input`, `/fixtures/<name>`). With no fields, the whole
    envelope is sent unchanged."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    template: dict[str, Any] = Field(default_factory=dict)
    fields: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_spec(cls, raw: Any) -> InputBinding:
        try:
            binding = cls.model_validate(deep_unfreeze(raw) or {})
        except PydanticValidationError as exc:
            raise ConfigError(f"invalid input_binding: {exc}") from exc
        if binding.template and not binding.fields:
            raise ConfigError("input_binding.template requires at least one entry in fields")
        for dest, src in binding.fields.items():
            _parse_pointer(dest)
            _parse_pointer(src)
        return binding

    def build_payload(self, envelope: AppInputEnvelope) -> Any:
        source = deep_unfreeze(envelope.data)
        if not self.fields:
            return source
        payload = copy.deepcopy(self.template)
        for dest, src in self.fields.items():
            value = resolve_pointer(source, src)
            if value is MISSING:
                raise BindingError(f"input binding source {src!r} not present in case input")
            set_pointer(payload, dest, copy.deepcopy(value))
        return payload


# --------------------------------------------------------------------------- output

OPTIONAL_CAPABILITIES = ("retrieved_context", "tool_events", "usage", "cost", "world_state")


class OutputBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    output: str = "/output"
    retrieved_context: str | None = None
    retrieved_context_item: str | None = None  # pointer inside each item, e.g. "/text"
    tool_events: str | None = None
    usage: str | None = None
    cost: str | None = None
    world_state: str | None = None  # the test world's state after the invocation

    @classmethod
    def from_spec(cls, raw: Any) -> OutputBinding:
        try:
            binding = cls.model_validate(deep_unfreeze(raw) or {})
        except PydanticValidationError as exc:
            raise ConfigError(f"invalid output_binding: {exc}") from exc
        for name in ("output", *OPTIONAL_CAPABILITIES, "retrieved_context_item"):
            pointer = getattr(binding, name)
            if pointer is not None:
                _parse_pointer(pointer)
        return binding


def completeness(
    state: ObservationState, detail: str, *, method: str | None = None, **extra: Any
) -> dict[str, Any]:
    entry: dict[str, Any] = {"state": state.value, "detail": detail}
    if method is not None:
        entry["method"] = method
    entry.update(extra)
    return entry


def unknown_everywhere(detail: str) -> dict[str, dict[str, Any]]:
    """Completeness when no response document could be read at all."""
    return {name: completeness(ObservationState.UNKNOWN, detail) for name in OPTIONAL_CAPABILITIES}


@dataclass
class ExtractedObservations:
    retrieved_context: tuple[str, ...] | None = None
    tool_events: tuple[Any, ...] = ()
    usage: Any = None
    cost: float | None = None
    world_state: Any = None
    completeness: dict[str, dict[str, Any]] = field(default_factory=dict)


def extract_optional(document: Any, binding: OutputBinding) -> ExtractedObservations:
    """Read the optional capabilities the binding declares. Never raises: a malformed
    observation is recorded as `unknown`/`invalid` without discarding the output itself."""
    result = ExtractedObservations()
    for name in OPTIONAL_CAPABILITIES:
        pointer = getattr(binding, name)
        if pointer is None:
            result.completeness[name] = completeness(ObservationState.UNKNOWN, "not_bound")
            continue
        method = f"app_response:{pointer}"
        raw = resolve_pointer(document, pointer)
        if raw is MISSING or raw is None:
            result.completeness[name] = completeness(
                ObservationState.UNKNOWN, "missing", method=method
            )
            continue
        try:
            value, empty = _coerce(name, raw, binding)
        except BindingError as exc:
            result.completeness[name] = completeness(
                ObservationState.UNKNOWN, "invalid", method=method, reason=str(exc)
            )
            continue
        setattr(result, name, value)
        result.completeness[name] = completeness(
            ObservationState.OBSERVED,
            "empty" if empty else "present",
            method=method,
            limitations="self-reported by the application",
        )
    return result


def _coerce(name: str, raw: Any, binding: OutputBinding) -> tuple[Any, bool]:
    if name == "retrieved_context":
        if not isinstance(raw, list):
            raise BindingError("retrieved_context must be a list")
        texts: list[str] = []
        for item in raw:
            if binding.retrieved_context_item is not None:
                item = resolve_pointer(item, binding.retrieved_context_item)
            # An application may group passages per source (LightRAG: one reference per file,
            # each with a list of chunk texts): a list of strings is flattened, in order.
            parts = item if isinstance(item, list) else [item]
            if not all(isinstance(part, str) for part in parts):
                raise BindingError(
                    "retrieved_context items must be strings, or lists of strings (set "
                    "retrieved_context_item to select the text field from each document)"
                )
            texts.extend(parts)
        return tuple(texts), not texts
    if name == "tool_events":
        if not isinstance(raw, list):
            raise BindingError("tool_events must be a list")
        return tuple(raw), not raw
    if name == "world_state":
        if not isinstance(raw, (dict, list)):
            raise BindingError("world_state must be an object or a list")
        return raw, not raw
    if name == "cost":
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0:
            raise BindingError("cost must be a non-negative number")
        return float(raw), False
    if not isinstance(raw, dict):
        raise BindingError("usage must be an object")
    return raw, not raw
