"""OpenTelemetry trace import (§18 step 5, 16-T2).

Reads OTLP/JSON exports (one document, or JSON Lines of documents as file exporters write
them) and normalizes each trace into harness observations:

- **correlation.** A trace is attached to the execution whose correlation ID it carries: a
  span attribute `aibench.correlation_id`, or the `x-request-id` request header the HTTP
  runner sends (`http.request.header.x-request-id`). Unmatched traces are kept but
  attached to nothing;
- **completeness.** A trace is partial when a span's parent is missing, when it has no root
  span, when any span is marked not sampled (W3C trace flags), when the exporter dropped
  attributes, events or links, when two different spans share a span ID, or when parent
  links form a cycle. A partial trace stays partial: its counts are lower bounds. An exact
  duplicate of a span (an exporter retry) is ignored and counted;
- **usage.** Token usage from `gen_ai.usage.*` attributes (and the older
  `prompt_tokens`/`completion_tokens` and `llm.token_count.*` names) is counted only on
  the lowest spans that carry it. A span whose descendants also report usage is treated as
  an aggregate and excluded, so parent and child totals are never added together;
- **tools, models, errors.** Tool spans (`gen_ai.tool.name`, or
  `gen_ai.operation.name = execute_tool`) with their status, requested/response model
  names, and spans with an error status.

The raw export is stored unchanged as an artifact. Normalized fields follow the attribute
mapping `NORMALIZATION`; attributes it does not know are kept only in the raw artifact.
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

NORMALIZATION = "otel-gen-ai/1"
_CORRELATION_KEYS = (
    "aibench.correlation_id",
    "http.request.header.x-request-id",
    "http.request.header.x_request_id",
)
_INPUT_KEYS = ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.token_count.prompt")
_OUTPUT_KEYS = (
    "gen_ai.usage.output_tokens",
    "gen_ai.usage.completion_tokens",
    "llm.token_count.completion",
)
_TRACE_FLAG_SAMPLED = 0x01
_STATUS_ERROR = 2


class TraceFormatError(ValueError):
    """The file is not an OTLP/JSON trace export."""


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    attributes: dict[str, Any]
    start_ns: int | None
    end_ns: int | None
    sampled: bool | None  # None when the export carries no trace flags
    dropped: int
    error: bool


@dataclass
class Trace:
    trace_id: str
    spans: list[Span] = field(default_factory=list)
    duplicates_ignored: int = 0  # exact copies of a span already present
    conflicting: int = 0  # different spans claiming an ID already present
    _by_id: dict[str, Span] = field(default_factory=dict, repr=False, compare=False)

    def add(self, span: Span) -> None:
        """Add a span, once: an exact copy is ignored; a different span with the same ID
        is not added and makes the trace partial."""
        if not self._by_id and self.spans:
            self._by_id = {s.span_id: s for s in self.spans}
        existing = self._by_id.get(span.span_id)
        if existing is None:
            self.spans.append(span)
            self._by_id[span.span_id] = span
        elif existing == span:
            self.duplicates_ignored += 1
        else:
            self.conflicting += 1

    def in_cycles(self) -> set[str]:
        """Span IDs whose parent links loop back (including a span that is its own
        parent): such spans have no root, so their usage can't be placed."""
        parent = {s.span_id: s.parent_span_id for s in self.spans}
        looped: set[str] = set()
        for start in parent:
            seen: list[str] = []
            node: str | None = start
            while node is not None and node in parent and node not in seen:
                seen.append(node)
                node = parent[node]
            if node is not None and node in seen:
                looped.update(seen[seen.index(node) :])
        return looped

    def completeness(self) -> list[str]:
        ids = {s.span_id for s in self.spans}
        reasons = []
        missing = sorted(
            {
                s.parent_span_id
                for s in self.spans
                if s.parent_span_id and s.parent_span_id not in ids
            }
        )
        if missing:
            reasons.append(f"missing_parent:{len(missing)}")
        if not any(s.parent_span_id is None for s in self.spans):
            reasons.append("no_root_span")
        if any(s.sampled is False for s in self.spans):
            reasons.append("not_sampled")
        dropped = sum(s.dropped for s in self.spans)
        if dropped:
            reasons.append(f"dropped:{dropped}")
        if self.conflicting:
            reasons.append(f"conflicting_span_id:{self.conflicting}")
        looped = self.in_cycles()
        if looped:
            reasons.append(f"parent_cycle:{len(looped)}")
        return reasons

    def correlation_id(self) -> str | None:
        for span in self.spans:
            for key in _CORRELATION_KEYS:
                value = span.attributes.get(key)
                if isinstance(value, list) and value:
                    value = value[0]
                if isinstance(value, str) and value:
                    return value
        return None


def _value(raw: Any) -> Any:
    """An OTLP AnyValue as a plain value."""
    if not isinstance(raw, dict):
        return raw
    for key in ("stringValue", "boolValue", "doubleValue"):
        if key in raw:
            return raw[key]
    if "intValue" in raw:
        try:
            return int(raw["intValue"])
        except (TypeError, ValueError):
            return raw["intValue"]
    if "arrayValue" in raw:
        array = raw["arrayValue"]
        if not isinstance(array, dict):
            raise TraceFormatError("arrayValue must be an object")
        return [_value(v) for v in _array(array.get("values"), "arrayValue.values")]
    if "kvlistValue" in raw:
        kvlist = raw["kvlistValue"]
        if not isinstance(kvlist, dict):
            raise TraceFormatError("kvlistValue must be an object")
        values = _array(kvlist.get("values"), "kvlistValue.values")
        if any(not isinstance(item, dict) for item in values):
            raise TraceFormatError("kvlistValue.values entries must be objects")
        if any(not isinstance(item.get("key"), str) for item in values):
            raise TraceFormatError("kvlistValue.values entries need a string key")
        return {kv.get("key"): _value(kv.get("value")) for kv in values}
    return None


def _array(raw: Any, field: str, *, allow_none: bool = True) -> list[Any]:
    if raw is None:
        if allow_none:
            return []
        raise TraceFormatError(f"{field} must be a list")
    if not isinstance(raw, list):
        raise TraceFormatError(f"{field} must be a list")
    return raw


def _object(raw: Any, field: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TraceFormatError(f"{field} entries must be objects")
    return raw


def _attributes(items: Any, field: str = "attributes") -> dict[str, Any]:
    result: dict[str, Any] = {}
    for item in _array(items, field):
        kv = _object(item, f"{field}[]")
        if "key" in kv:
            result[str(kv["key"])] = _value(kv.get("value"))
    return result


def _int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def parse_otlp(data: bytes) -> list[Trace]:
    """Every trace in an OTLP/JSON export, spans grouped by trace ID."""
    try:
        text = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError as exc:
        raise TraceFormatError("trace export is not valid UTF-8") from exc
    documents: list[Any] = []
    try:
        documents = [json.loads(text)]
    except json.JSONDecodeError:
        try:
            documents = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise TraceFormatError(f"not OTLP/JSON: {exc}") from exc
    traces: dict[str, Trace] = {}
    found_any = False
    for document in documents:
        if not isinstance(document, dict) or "resourceSpans" not in document:
            raise TraceFormatError("expected OTLP/JSON with a 'resourceSpans' list")
        found_any = True
        for resource_index, raw_resource_spans in enumerate(
            _array(document.get("resourceSpans"), "resourceSpans", allow_none=False)
        ):
            resource_spans = _object(raw_resource_spans, f"resourceSpans[{resource_index}]")
            raw_resource = resource_spans.get("resource") or {}
            resource_obj = _object(raw_resource, f"resourceSpans[{resource_index}].resource")
            resource = _attributes(
                resource_obj.get("attributes"),
                f"resourceSpans[{resource_index}].resource.attributes",
            )
            for scope_index, raw_scope_spans in enumerate(
                _array(
                    resource_spans.get("scopeSpans"),
                    f"resourceSpans[{resource_index}].scopeSpans",
                )
            ):
                scope_spans = _object(
                    raw_scope_spans,
                    f"resourceSpans[{resource_index}].scopeSpans[{scope_index}]",
                )
                for span_index, raw_span in enumerate(
                    _array(
                        scope_spans.get("spans"),
                        f"resourceSpans[{resource_index}].scopeSpans[{scope_index}].spans",
                    )
                ):
                    span_obj = _object(
                        raw_span,
                        f"resourceSpans[{resource_index}].scopeSpans[{scope_index}].spans[{span_index}]",
                    )
                    span = _span(span_obj, resource)
                    traces.setdefault(span.trace_id, Trace(span.trace_id)).add(span)
    if not found_any:
        raise TraceFormatError("expected OTLP/JSON with a 'resourceSpans' list")
    return list(traces.values())


def _span(raw: dict[str, Any], resource: dict[str, Any]) -> Span:
    if not raw.get("traceId") or not raw.get("spanId"):
        raise TraceFormatError("a span needs traceId and spanId")
    flags = _int(raw.get("flags"))
    status = raw.get("status") or {}
    if not isinstance(status, dict):
        raise TraceFormatError("span status must be an object")
    code = status.get("code")
    return Span(
        trace_id=str(raw["traceId"]),
        span_id=str(raw["spanId"]),
        parent_span_id=str(raw["parentSpanId"]) if raw.get("parentSpanId") else None,
        name=str(raw.get("name", "")),
        attributes={**resource, **_attributes(raw.get("attributes"), "span attributes")},
        start_ns=_int(raw.get("startTimeUnixNano")),
        end_ns=_int(raw.get("endTimeUnixNano")),
        sampled=None if flags is None else bool(flags & _TRACE_FLAG_SAMPLED),
        dropped=sum(
            _int(raw.get(k)) or 0
            for k in ("droppedAttributesCount", "droppedEventsCount", "droppedLinksCount")
        ),
        error=code == _STATUS_ERROR or code == "STATUS_CODE_ERROR",
    )


def _usage_of(span: Span) -> dict[str, int] | None:
    found = {}
    for label, keys in (("input_tokens", _INPUT_KEYS), ("output_tokens", _OUTPUT_KEYS)):
        value = next((span.attributes[k] for k in keys if k in span.attributes), None)
        if isinstance(value, int | float) and not isinstance(value, bool):
            found[label] = int(value)
    return found or None


def normalize(trace: Trace) -> dict[str, Any]:
    """The harness observation for one trace (see the module docstring)."""
    looped = trace.in_cycles()
    children: dict[str, list[Span]] = defaultdict(list)
    for span in trace.spans:
        if span.parent_span_id and span.span_id not in looped:
            children[span.parent_span_id].append(span)

    def has_usage_below(span: Span) -> bool:
        seen = {span.span_id}
        stack = list(children.get(span.span_id, []))
        while stack:
            child = stack.pop()
            if child.span_id in seen:
                continue
            seen.add(child.span_id)
            if _usage_of(child) is not None:
                return True
            stack.extend(children.get(child.span_id, []))
        return False

    totals = {"input_tokens": 0, "output_tokens": 0}
    counted, aggregates, unplaced = [], [], []
    for span in trace.spans:
        usage = _usage_of(span)
        if usage is None:
            continue
        if span.span_id in looped:
            unplaced.append(span.span_id)  # may repeat usage counted elsewhere: left out
            continue
        if has_usage_below(span):
            aggregates.append(span.span_id)  # its descendants report the same tokens
            continue
        counted.append(span.span_id)
        for key, value in usage.items():
            totals[key] += value
    tools = [
        {
            "name": s.attributes.get("gen_ai.tool.name") or s.name,
            "status": "error" if s.error else "ok",
            "span_id": s.span_id,
        }
        for s in trace.spans
        if "gen_ai.tool.name" in s.attributes
        or s.attributes.get("gen_ai.operation.name") == "execute_tool"
    ]
    models = sorted(
        {
            str(v)
            for s in trace.spans
            for k in ("gen_ai.request.model", "gen_ai.response.model")
            if (v := s.attributes.get(k))
        }
    )
    reasons = trace.completeness()
    return {
        "normalization": NORMALIZATION,
        "correlation_id": trace.correlation_id(),
        "span_count": len(trace.spans),
        "duplicate_spans_ignored": trace.duplicates_ignored,
        "partial_reasons": reasons,
        "usage": (
            {
                **totals,
                "total_tokens": totals["input_tokens"] + totals["output_tokens"],
                "counted_spans": counted,
                "aggregate_spans_excluded": aggregates,
                "unplaced_spans_excluded": unplaced,
                "rule": "lowest spans reporting usage; spans whose descendants report usage "
                "are aggregates and excluded",
                "bound": "lower_bound" if reasons else "complete",
            }
            if counted
            else None
        ),
        "tools": tools,
        "models": models,
        "error_spans": [s.span_id for s in trace.spans if s.error],
    }


# --------------------------------------------------------------------------- span trees

TREE_FORMAT = "aibench-span-tree/1"
TEXT_LIMIT = 4_000  # characters kept of one span's input or output; the rest is cut and marked

_OPENINFERENCE_KINDS = {
    "LLM": "llm",
    "TOOL": "tool",
    "AGENT": "agent",
    "RETRIEVER": "retriever",
}
_OPERATION_KINDS = {
    "chat": "llm",
    "text_completion": "llm",
    "generate_content": "llm",
    "execute_tool": "tool",
    "invoke_agent": "agent",
    "create_agent": "agent",
    "retrieval": "retriever",
}
# (input, output) attributes, first found wins: gen-ai semantic conventions (current and
# older), tool calls, then OpenInference.
_IO_KEYS = (
    ("gen_ai.input.messages", "gen_ai.output.messages"),
    ("gen_ai.prompt", "gen_ai.completion"),
    ("gen_ai.tool.call.arguments", "gen_ai.tool.call.result"),
    ("input.value", "output.value"),
)


def _kind(span: Span) -> str:
    attributes = span.attributes
    marked = _OPENINFERENCE_KINDS.get(str(attributes.get("openinference.span.kind", "")).upper())
    if marked:
        return marked
    operation = _OPERATION_KINDS.get(str(attributes.get("gen_ai.operation.name", "")))
    if operation:
        return operation
    if "gen_ai.tool.name" in attributes:
        return "tool"
    if "gen_ai.agent.name" in attributes:
        return "agent"
    if "gen_ai.request.model" in attributes or "gen_ai.response.model" in attributes:
        return "llm"
    return "other"


def _text(value: Any, limit: int) -> Any:
    """A span's input or output, decoded when it is JSON text, with long text cut to
    `limit` characters and marked, so one span cannot fill a judge's context."""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            decoded = value
        if not isinstance(decoded, str):
            value = decoded
    if not isinstance(value, str):
        encoded = json.dumps(value, ensure_ascii=False, default=str)
        if len(encoded) <= limit:
            return value
        value = encoded
    if len(value) <= limit:
        return value
    return value[:limit] + f"... [cut: {len(value) - limit} more characters]"


def span_tree(trace: Trace, *, text_limit: int = TEXT_LIMIT) -> list[dict[str, Any]]:
    """The trace as a tree of spans, roots first, children in start order: each span's
    `name`, `kind` (agent, llm, tool, retriever or other), `input`, `output`, `model` and
    `error`, from the attributes it carries (`_IO_KEYS`, `_kind`). Nothing is invented: a
    field the span does not carry is left out. Spans in a parent cycle are left out."""
    looped = trace.in_cycles()
    order = sorted(
        (s for s in trace.spans if s.span_id not in looped),
        key=lambda s: (s.start_ns is None, s.start_ns or 0, s.span_id),
    )
    ids = {s.span_id for s in order}
    children: dict[str | None, list[Span]] = defaultdict(list)
    for span in order:
        parent = span.parent_span_id if span.parent_span_id in ids else None
        children[parent].append(span)

    def node(span: Span) -> dict[str, Any]:
        attributes = span.attributes
        kind = _kind(span)
        name = (
            attributes.get("gen_ai.tool.name")
            if kind == "tool"
            else attributes.get("gen_ai.agent.name")
            if kind == "agent"
            else None
        )
        entry: dict[str, Any] = {"name": str(name or span.name), "kind": kind}
        for input_key, output_key in _IO_KEYS:
            if input_key in attributes or output_key in attributes:
                if input_key in attributes:
                    entry["input"] = _text(attributes[input_key], text_limit)
                if output_key in attributes:
                    entry["output"] = _text(attributes[output_key], text_limit)
                break
        model = attributes.get("gen_ai.response.model") or attributes.get("gen_ai.request.model")
        if model:
            entry["model"] = str(model)
        if span.error:
            entry["error"] = True
        entry["children"] = [node(child) for child in children.get(span.span_id, [])]
        return entry

    return [node(root) for root in children.get(None, [])]
