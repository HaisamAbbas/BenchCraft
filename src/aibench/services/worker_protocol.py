"""Bounded JSON protocol shared by distributed run coordinators and workers."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hmac
import json
import math
import re
from typing import Any

from aibench.core.models import EffectState, ErrorKind, ExecutionStatus, deep_unfreeze
from aibench.runners.base import Capture, InvocationOutcome
from aibench.runners.bindings import ExtractedObservations

WORKER_PROTOCOL = "aibench-coordinator-worker/1"
MAX_HTTP_HEADER_BYTES = 32 * 1024
MAX_REQUEST_BYTES = 12 * 1024 * 1024
MAX_RESPONSE_BYTES = 12 * 1024 * 1024
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_CAPTURE_COUNT = 32
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


class WorkerProtocolError(ValueError):
    """A request or response did not satisfy the bounded worker protocol."""


class WorkerAuthenticationError(WorkerProtocolError):
    """A request did not provide the coordinator's bearer token."""


def json_bytes(value: Any, *, maximum: int = MAX_REQUEST_BYTES) -> bytes:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise WorkerProtocolError(f"message is not strict JSON: {exc}") from exc
    if len(encoded) > maximum:
        raise WorkerProtocolError(f"message exceeds the {maximum}-byte protocol limit")
    return encoded


async def read_request(
    reader: Any, *, expected_authorization: str | None = None
) -> tuple[str, str, dict[str, str], Any]:
    """Read one small HTTP/1.1 JSON request; chunked bodies and duplicate headers are refused."""
    try:
        request_line = await reader.readline()
        if len(request_line) > 8192 or not request_line.endswith(b"\r\n"):
            raise WorkerProtocolError("invalid HTTP request line")
        parts = request_line[:-2].decode("ascii").split(" ")
        if len(parts) != 3 or parts[2] != "HTTP/1.1":
            raise WorkerProtocolError("only HTTP/1.1 requests are supported")
        method, path, _ = parts
        header_bytes = len(request_line)
        headers: dict[str, str] = {}
        while True:
            line = await reader.readline()
            header_bytes += len(line)
            if header_bytes > MAX_HTTP_HEADER_BYTES:
                raise WorkerProtocolError("HTTP headers exceed the protocol limit")
            if line == b"\r\n":
                break
            if not line.endswith(b"\r\n") or b":" not in line:
                raise WorkerProtocolError("invalid HTTP header")
            name_bytes, value_bytes = line[:-2].split(b":", 1)
            name = name_bytes.decode("ascii").strip().lower()
            if not _HEADER_NAME.fullmatch(name) or name in headers:
                raise WorkerProtocolError("invalid or duplicate HTTP header")
            headers[name] = value_bytes.decode("latin-1").strip()
        if expected_authorization is not None and not hmac.compare_digest(
            headers.get("authorization", ""), expected_authorization
        ):
            raise WorkerAuthenticationError("unauthorized")
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise WorkerProtocolError("Content-Type must be application/json")
        if headers.get("transfer-encoding"):
            raise WorkerProtocolError("transfer-encoded request bodies are not supported")
        raw_length = headers.get("content-length")
        if raw_length is None or not raw_length.isascii() or not raw_length.isdigit():
            raise WorkerProtocolError("a valid Content-Length is required")
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise WorkerProtocolError("request body exceeds the protocol limit")
        body = await reader.readexactly(length)
        if not body:
            document: Any = {}
        else:
            try:
                document = json.loads(body)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise WorkerProtocolError("request body is not valid JSON") from exc
        return method, path, headers, document
    except (UnicodeError, asyncio.IncompleteReadError) as exc:
        raise WorkerProtocolError("incomplete or non-ASCII HTTP request") from exc


def outcome_to_json(outcome: InvocationOutcome) -> dict[str, Any]:
    if len(outcome.captures) > MAX_CAPTURE_COUNT:
        raise WorkerProtocolError("worker outcome has too many captures")
    captures = []
    for capture in outcome.captures:
        if len(capture.data) > MAX_CAPTURE_BYTES:
            raise WorkerProtocolError("worker capture exceeds the per-capture limit")
        captures.append(
            {
                "name": capture.name,
                "data": base64.b64encode(capture.data).decode("ascii"),
                "mime_type": capture.mime_type,
                "truncated": capture.truncated,
            }
        )
    observations = outcome.observations
    return {
        "status": outcome.status.value,
        "effect_state": outcome.effect_state.value,
        "correlation_id": outcome.correlation_id,
        "timing": deep_unfreeze(outcome.timing),
        "completeness": deep_unfreeze(outcome.completeness),
        "output": deep_unfreeze(outcome.output),
        "observations": {
            "retrieved_context": observations.retrieved_context,
            "tool_events": deep_unfreeze(observations.tool_events),
            "usage": deep_unfreeze(observations.usage),
            "cost": observations.cost,
            "world_state": deep_unfreeze(observations.world_state),
            "completeness": deep_unfreeze(observations.completeness),
        },
        "error_kind": outcome.error_kind.value if outcome.error_kind else None,
        "error": outcome.error,
        "captures": captures,
    }


def outcome_from_json(value: Any, *, correlation_id: str) -> InvocationOutcome:
    if not isinstance(value, dict):
        raise WorkerProtocolError("worker outcome must be an object")
    try:
        if value.get("correlation_id") != correlation_id:
            raise WorkerProtocolError("worker returned a different correlation id")
        observations_raw = value["observations"]
        if not isinstance(observations_raw, dict):
            raise WorkerProtocolError("worker observations must be an object")
        captures_raw = value["captures"]
        if not isinstance(captures_raw, list) or len(captures_raw) > MAX_CAPTURE_COUNT:
            raise WorkerProtocolError("worker captures must be a bounded array")
        captures: list[Capture] = []
        for raw in captures_raw:
            if not isinstance(raw, dict):
                raise WorkerProtocolError("worker capture must be an object")
            encoded = raw["data"]
            if not isinstance(encoded, str) or len(encoded) > (MAX_CAPTURE_BYTES * 4 // 3 + 8):
                raise WorkerProtocolError("worker capture encoding exceeds the size limit")
            data = base64.b64decode(encoded, validate=True)
            if len(data) > MAX_CAPTURE_BYTES:
                raise WorkerProtocolError("worker capture exceeds the per-capture limit")
            captures.append(
                Capture(
                    name=_bounded_text(raw["name"], "capture name", 120),
                    data=data,
                    mime_type=_bounded_text(raw["mime_type"], "capture MIME type", 200),
                    truncated=_strict_bool(raw["truncated"], "capture truncated"),
                )
            )
        timing = value["timing"]
        completeness = value["completeness"]
        observed_completeness = observations_raw.get("completeness", {})
        if not isinstance(timing, dict) or not isinstance(completeness, dict):
            raise WorkerProtocolError("worker timing and completeness must be objects")
        if not isinstance(observed_completeness, dict):
            raise WorkerProtocolError("worker observation completeness must be an object")
        observations = ExtractedObservations(
            retrieved_context=_optional_text_tuple(observations_raw.get("retrieved_context")),
            tool_events=_json_tuple(observations_raw.get("tool_events", []), "tool events"),
            usage=observations_raw.get("usage"),
            cost=_optional_finite_float(observations_raw.get("cost"), "observation cost"),
            world_state=observations_raw.get("world_state"),
            completeness=observed_completeness,
        )
        error_kind = value.get("error_kind")
        return InvocationOutcome(
            status=ExecutionStatus(value["status"]),
            effect_state=EffectState(value["effect_state"]),
            correlation_id=correlation_id,
            timing=timing,
            completeness=completeness,
            output=value.get("output"),
            observations=observations,
            error_kind=ErrorKind(error_kind) if error_kind is not None else None,
            error=_optional_bounded_text(value.get("error"), "worker error", 4096),
            captures=tuple(captures),
        )
    except WorkerProtocolError:
        raise
    except (KeyError, TypeError, ValueError, binascii.Error) as exc:
        raise WorkerProtocolError(f"invalid worker outcome: {exc}") from exc


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise WorkerProtocolError(f"{label} is invalid")
    return value


def _optional_bounded_text(value: Any, label: str, maximum: int) -> str | None:
    return None if value is None else _bounded_text(value, label, maximum)


def _strict_bool(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise WorkerProtocolError(f"{label} must be a boolean")
    return value


def _optional_finite_float(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WorkerProtocolError(f"{label} must be a finite number or null")
    return float(value)


def _json_tuple(value: Any, label: str) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise WorkerProtocolError(f"{label} must be an array")
    return tuple(value)


def _optional_text_tuple(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise WorkerProtocolError("retrieved context must be an array of strings or null")
    return tuple(value)
