"""Bounded DeepEval MCP metrics over recorded tool calls and declared MCP inventory.

BenchCraft stores application tool events, not live MCP sessions. The adapter marks only
successfully recorded calls as MCP calls and supplies the binding's frozen server/tool
inventory to DeepEval. It never connects to an MCP server. The current execution schema does
not distinguish MCP resource reads or prompt retrievals from other tool events, so those
interactions are not inferred.
"""

from __future__ import annotations

import inspect
import json
import math
import os
from dataclasses import dataclass
from typing import Any, ClassVar

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.agent import parse_tool_events
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench_deepeval._version import __version__
from aibench_deepeval.faithfulness import (
    DEEPEVAL_ENVIRONMENT,
    PINNED_DEEPEVAL,
    _require_pinned_deepeval,
)
from aibench_deepeval.judges import JUDGE_SCHEMA, build_judge, report_judge_usage
from aibench_deepeval.metrics import _text, out_of_range

MAX_MCP_SERVERS = 16
MAX_PRIMITIVES_PER_KIND = 64
MAX_MCP_INVENTORY_BYTES = 128 * 1024
MAX_MCP_CALLS_PER_TURN = 32
MAX_MCP_CALLS_PER_CASE = 64
MAX_MCP_CALLS_PER_EPISODE = 256
MAX_MCP_EVENTS_PER_TURN = 256
MAX_MCP_EVENTS_PER_CASE = 512
MAX_MCP_EVENTS_PER_EPISODE = 1_024
MAX_MCP_CALL_EVIDENCE_BYTES = 64 * 1024
MAX_MCP_EPISODE_EVIDENCE_BYTES = 256 * 1024
MAX_MCP_TURNS = 50
MAX_TEXT_CHARS = 16_000
MAX_EPISODE_TEXT_BYTES = 1024 * 1024
_MCP_EVENT_TYPES = frozenset({"mcp", "mcp_tool", "mcp_tool_call", "mcptool", "mcptoolcall"})
_MAX_JSON_DEPTH = 8
_MAX_JSON_ITEMS = 128
_MAX_JSON_STRING_CHARS = 4_000


def _object_schema(properties: dict[str, Any], required: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": properties,
    }


_PRIMITIVE_SCHEMAS = {
    "available_tools": {
        "type": "array",
        "maxItems": MAX_PRIMITIVES_PER_KIND,
        "items": _object_schema(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": {"type": "string", "maxLength": 2_000},
                # MCP tool argument schemas are JSON values and are bounded again before use.
                "inputSchema": {"type": "object", "maxProperties": _MAX_JSON_ITEMS},
            },
            ("name",),
        ),
    },
    "available_resources": {
        "type": "array",
        "maxItems": MAX_PRIMITIVES_PER_KIND,
        "items": _object_schema(
            {
                "uri": {"type": "string", "minLength": 1, "maxLength": 2_000},
                "name": {"type": "string", "maxLength": 128},
                "description": {"type": "string", "maxLength": 2_000},
                "mimeType": {"type": "string", "maxLength": 128},
            },
            ("uri",),
        ),
    },
    "available_prompts": {
        "type": "array",
        "maxItems": MAX_PRIMITIVES_PER_KIND,
        "items": _object_schema(
            {
                "name": {"type": "string", "minLength": 1, "maxLength": 128},
                "description": {"type": "string", "maxLength": 2_000},
                "arguments": {"type": "array", "maxItems": MAX_PRIMITIVES_PER_KIND},
            },
            ("name",),
        ),
    },
}
MCP_SERVER_SCHEMA = _object_schema(
    {
        "server_name": {"type": "string", "minLength": 1, "maxLength": 128},
        "transport": {"enum": ["stdio", "sse", "streamable-http"]},
        **_PRIMITIVE_SCHEMAS,
    },
    ("server_name",),
)
MCP_SERVERS_SCHEMA = {
    "type": "array",
    "minItems": 1,
    "maxItems": MAX_MCP_SERVERS,
    "items": MCP_SERVER_SCHEMA,
}


@dataclass(frozen=True)
class MCPMetricSpec:
    name: str
    upstream: str
    summary: str
    concepts: tuple[str, ...]
    episode: bool = False
    max_parallel_judge_calls: int = 1
    max_judge_calls: int = 2
    limitations: tuple[str, ...] = ()


MCP_METRIC_SPECS = (
    MCPMetricSpec(
        "mcp_use",
        "MCPUseMetric",
        "whether MCP tools were selected and called with arguments that match the declared inventory",
        ("tool_use",),
        limitations=(
            "Reads successfully recorded execution.tool_events only; model tool-call requests without a recorded successful execution are not counted.",
            "The current execution record cannot distinguish MCP resource reads or prompt retrievals from ordinary tool calls, so only MCP tool calls are scored.",
            "Generic tool events have no provenance; set tool_events_are_mcp_only only when this application's entire recorded tool-event stream contains MCP calls.",
        ),
    ),
    MCPMetricSpec(
        "mcp_task_completion",
        "MCPTaskCompletionMetric",
        "whether recorded MCP tool use and the assistant response complete each user task",
        ("task_completion",),
        episode=True,
        max_parallel_judge_calls=MAX_MCP_TURNS,
        max_judge_calls=MAX_MCP_TURNS + 1,
        limitations=(
            "Reads successfully recorded execution.tool_events only; model tool-call requests without a recorded successful execution are not counted.",
            "The current execution record cannot distinguish MCP resource reads or prompt retrievals from ordinary tool calls, so only MCP tool calls are scored.",
            "Generic tool events have no provenance; set tool_events_are_mcp_only only when this application's entire recorded tool-event stream contains MCP calls.",
        ),
    ),
    MCPMetricSpec(
        "multi_turn_mcp_use",
        "MultiTurnMCPUseMetric",
        "whether MCP tools and arguments were used appropriately across a conversation",
        ("tool_use",),
        episode=True,
        max_parallel_judge_calls=MAX_MCP_TURNS,
        max_judge_calls=2 * MAX_MCP_TURNS + 1,
        limitations=(
            "Reads successfully recorded execution.tool_events only; model tool-call requests without a recorded successful execution are not counted.",
            "The current execution record cannot distinguish MCP resource reads or prompt retrievals from ordinary tool calls, so only MCP tool calls are scored.",
            "Generic tool events have no provenance; set tool_events_are_mcp_only only when this application's entire recorded tool-event stream contains MCP calls.",
        ),
    ),
)


def _manifest(spec: MCPMetricSpec) -> EvaluatorManifest:
    requirements = (
        (
            FieldRequirement(path="case.group_id"),
            FieldRequirement(path="episode.turns"),
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="execution.tool_events", non_empty=False),
        )
        if spec.episode
        else (
            FieldRequirement(path="case.input"),
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="execution.tool_events", non_empty=False),
        )
    )
    return EvaluatorManifest(
        evaluator_id=f"deepeval.{spec.name}",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="deepeval",
        package_version=PINNED_DEEPEVAL,
        description=f"DeepEval {PINNED_DEEPEVAL} {spec.upstream}: {spec.summary}.",
        limitations=(
            "Judge-dependent: scores from different judge models are not directly comparable.",
            "The declared MCP server inventory, recorded prompts, tool arguments, and tool results are sent to the configured judge endpoint; a local python_factory judge makes no provider request.",
            "The inventory is limited to 16 servers, 64 primitives of each kind per server, and 128 KiB total.",
            "Recorded events are limited to 256 per turn, 512 per case, and 1,024 per episode; successful calls are limited to 32 per turn, 64 per case, and 256 per episode.",
            "Each tool call is limited to 16 KiB; call evidence is limited to 64 KiB per case and 256 KiB per episode.",
            "Input and output text is limited to 16,000 characters per field and 1 MiB total episode evidence.",
            "The adapter uses only the supplied frozen inventory; it never connects to or discovers a live MCP server.",
            "Generic successful events are counted only when tool_events_are_mcp_only is true; otherwise the event type must be exactly one of mcp, mcp_tool, mcp_tool_call, mcptool, or mcptoolcall.",
            *spec.limitations,
        ),
        concepts=spec.concepts,
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=requirements,
        default_rule=DecisionRule(comparator=">=", threshold=0.5),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["judge", "mcp_servers"],
            "properties": {
                "judge": JUDGE_SCHEMA,
                "mcp_servers": MCP_SERVERS_SCHEMA,
                "tool_events_are_mcp_only": {"type": "boolean"},
            },
        },
        uses_models=True,
        credentials=("the judge provider's key, passed explicitly to the plugin environment",),
        network_destinations=(
            "the configured judge model's API (none for a local python_factory judge)",
        ),
        # The OpenAI-compatible judge makes at most four retries per request. MCPUse
        # sends two sequential requests; the conversational metrics can fan out per task.
        internal_retries=4 * spec.max_judge_calls,
        internal_concurrency=spec.max_parallel_judge_calls,
        requires_worker=True,
    )


def _clean_json(value: Any, *, depth: int = 0) -> Any:
    """Accept only bounded JSON-shaped evidence before it reaches DeepEval's prompts."""
    if depth > _MAX_JSON_DEPTH:
        raise ValueError("MCP evidence exceeds the JSON nesting limit")
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("MCP evidence contains a non-finite number")
        return value
    if isinstance(value, str):
        if len(value) > _MAX_JSON_STRING_CHARS:
            raise ValueError("MCP evidence contains an oversized string")
        return value
    if isinstance(value, list):
        if len(value) > _MAX_JSON_ITEMS:
            raise ValueError("MCP evidence contains too many array items")
        return [_clean_json(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        if len(value) > _MAX_JSON_ITEMS or not all(isinstance(key, str) for key in value):
            raise ValueError("MCP evidence contains an invalid object")
        if any(len(key) > _MAX_JSON_STRING_CHARS for key in value):
            raise ValueError("MCP evidence contains an oversized object key")
        return {key: _clean_json(item, depth=depth + 1) for key, item in value.items()}
    raise ValueError("MCP evidence must contain only JSON values")


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _normalize_inventory(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MCP_SERVERS:
        raise ValueError(f"mcp_servers must contain 1 to {MAX_MCP_SERVERS} server objects")
    normalized: list[dict[str, Any]] = []
    names: set[str] = set()
    for server in raw:
        if not isinstance(server, dict):
            raise TypeError("each mcp_servers item must be an object")
        allowed_server_keys = {
            "server_name",
            "transport",
            "available_tools",
            "available_resources",
            "available_prompts",
        }
        if set(server) - allowed_server_keys:
            raise ValueError("mcp_servers contains an unsupported server field")
        name = server.get("server_name")
        if not isinstance(name, str) or not name.strip() or len(name) > 128:
            raise ValueError(
                "each MCP server needs a non-empty server_name of at most 128 characters"
            )
        if name in names:
            raise ValueError("mcp_servers server_name values must be unique")
        names.add(name)
        transport = server.get("transport")
        if transport is not None and transport not in ("stdio", "sse", "streamable-http"):
            raise ValueError("MCP server transport must be stdio, sse, or streamable-http")
        clean: dict[str, Any] = {"server_name": name}
        if transport is not None:
            clean["transport"] = transport
        for kind, required_fields in (
            ("available_tools", {"name"}),
            ("available_resources", {"uri"}),
            ("available_prompts", {"name"}),
        ):
            items = server.get(kind, [])
            if not isinstance(items, list) or len(items) > MAX_PRIMITIVES_PER_KIND:
                raise ValueError(
                    f"{kind} must be an array of at most {MAX_PRIMITIVES_PER_KIND} objects"
                )
            safe_items: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict) or not required_fields <= item.keys():
                    raise ValueError(
                        f"each {kind} entry must be an object with {sorted(required_fields)}"
                    )
                allowed_fields = {
                    "available_tools": {"name", "description", "inputSchema"},
                    "available_resources": {"uri", "name", "description", "mimeType"},
                    "available_prompts": {"name", "description", "arguments"},
                }[kind]
                if set(item) - allowed_fields:
                    raise ValueError(f"{kind} contains an unsupported field")
                safe = _clean_json(item)
                for required_field in required_fields:
                    if (
                        not isinstance(safe[required_field], str)
                        or not safe[required_field].strip()
                    ):
                        raise ValueError(f"each {kind} entry needs a non-empty {required_field}")
                safe_items.append(safe)
            if safe_items:
                clean[kind] = safe_items
        normalized.append(clean)
    if _json_size(normalized) > MAX_MCP_INVENTORY_BYTES:
        raise ValueError("mcp_servers inventory exceeds the 128 KiB evidence limit")
    return normalized


def _bounded_text(value: Any) -> str:
    text = _text(value)
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("MCP episode text exceeds the per-turn limit")
    return text


def _successful_calls(
    events: Any,
    *,
    call_limit: int,
    event_limit: int,
    tool_events_are_mcp_only: bool,
) -> list[dict[str, Any]]:
    if not isinstance(events, list):
        raise TypeError("recorded MCP tool events must be an array")
    raw_events = events
    if len(raw_events) > event_limit:
        raise ValueError("MCP evidence contains too many recorded tool events")
    successful = [
        attempt
        for attempt in parse_tool_events(raw_events)
        if attempt.status == "ok"
        and attempt.name
        and attempt.index < len(raw_events)
        and _is_mcp_event(raw_events[attempt.index], tool_events_are_mcp_only)
    ]
    if len(successful) > call_limit:
        raise ValueError("MCP evidence contains too many successful tool calls")
    calls: list[dict[str, Any]] = []
    for attempt in successful:
        if len(attempt.name or "") > 128:
            raise ValueError("MCP evidence contains an oversized tool name")
        args = _clean_json(attempt.arguments) if attempt.arguments is not None else {}
        if not isinstance(args, dict):
            raise TypeError("MCP tool arguments must be a JSON object")
        result = _clean_json(attempt.result) if attempt.result is not None else None
        call = {"name": attempt.name, "arguments": args, "result": result}
        if _json_size(call) > 16 * 1024:
            raise ValueError("one MCP tool call exceeds the 16 KiB evidence limit")
        calls.append(call)
    return calls


def _is_mcp_event(event: Any, tool_events_are_mcp_only: bool) -> bool:
    if not isinstance(event, dict):
        return False
    type_key = next((key for key in ("type", "kind", "call_type") if key in event), None)
    if type_key is None:
        return tool_events_are_mcp_only
    event_type = event[type_key]
    if not isinstance(event_type, str):
        return False
    normalized = event_type.strip().lower().replace("-", "_")
    return normalized in _MCP_EVENT_TYPES


def _tool_calls(calls: list[dict[str, Any]]) -> list[Any]:
    from deepeval.test_case import ToolCall, ToolCallType

    return [
        ToolCall(
            name=call["name"],
            type=ToolCallType.MCP,
            input_parameters=call["arguments"],
            output=call["result"],
        )
        for call in calls
    ]


class MCPMetric(Evaluator):
    """One DeepEval MCP metric; the manifest and scope come from its spec."""

    spec: ClassVar[MCPMetricSpec]

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        os.environ.update(DEEPEVAL_ENVIRONMENT)
        _require_pinned_deepeval()
        import deepeval.metrics  # noqa: F401 - fail early if the pinned package is incomplete
        from deepeval.test_case import MCPServer

        self.inventory = _normalize_inventory(self.params["mcp_servers"])
        self.tool_events_are_mcp_only = self.params.get("tool_events_are_mcp_only", False)
        if not isinstance(self.tool_events_are_mcp_only, bool):
            raise TypeError("tool_events_are_mcp_only must be true or false")
        self.mcp_servers = [
            MCPServer(
                server_name=server["server_name"],
                transport=server.get("transport"),
                available_tools=server.get("available_tools"),
                available_resources=server.get("available_resources"),
                available_prompts=server.get("available_prompts"),
            )
            for server in self.inventory
        ]
        build_judge(self.params["judge"])

    def _new_metric(self, judge: Any) -> Any:
        import deepeval.metrics

        upstream = getattr(deepeval.metrics, self.spec.upstream)
        arguments = {
            "model": judge,
            "threshold": 0.5,  # upstream flag only; the harness applies the binding's rule
            "include_reason": True,
            "async_mode": True,
            "strict_mode": False,
            "verbose_mode": False,
        }
        accepted = inspect.signature(upstream.__init__).parameters
        return upstream(**{key: value for key, value in arguments.items() if key in accepted})

    async def _measure(
        self, test_case: Any, ctx: EvaluatorContext, *, call_count: int, turn_count: int
    ) -> EvaluationOutcome:
        judge = build_judge(self.params["judge"])
        metric = self._new_metric(judge)
        try:
            score = await metric.a_measure(test_case, _show_indicator=False)
        except BaseException:
            settle_pending = getattr(judge, "settle_pending_calls", None)
            try:
                if callable(settle_pending):
                    await settle_pending()
            finally:
                report_judge_usage(ctx, metric, judge)
            raise
        report_judge_usage(ctx, metric, judge)
        bad = out_of_range(score)
        if bad is not None:
            return bad
        return EvaluationOutcome.ok(
            "scalar",
            float(score),
            evidence=tuple(requirement.path for requirement in self.manifest.requires),
            raw={
                "deepeval_version": PINNED_DEEPEVAL,
                "metric": self.spec.upstream,
                "judge": getattr(metric, "evaluation_model", None),
                "turns": turn_count,
                "successful_mcp_tool_calls": call_count,
                "mcp_servers": len(self.inventory),
                "score": metric.score,
                "reason": getattr(metric, "reason", None),
                "upstream_success": metric.success,
                "upstream_threshold": metric.threshold,
                "evaluation_cost": getattr(metric, "evaluation_cost", None),
            },
        )


class MCPUse(MCPMetric):
    spec = MCP_METRIC_SPECS[0]
    manifest = _manifest(spec)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        from deepeval.test_case import LLMTestCase

        output = view.get("execution.output")
        if not isinstance(output, str) or not output.strip():
            return EvaluationOutcome.not_applicable("unscorable_output")
        try:
            case_input = _bounded_text(view.get("case.input"))
            actual_output = _bounded_text(output)
            calls = _successful_calls(
                view.get("execution.tool_events"),
                call_limit=MAX_MCP_CALLS_PER_CASE,
                event_limit=MAX_MCP_EVENTS_PER_CASE,
                tool_events_are_mcp_only=self.tool_events_are_mcp_only,
            )
        except (ValueError, TypeError):
            return EvaluationOutcome.not_applicable("oversized_or_invalid:mcp_evidence")
        if not calls:
            return EvaluationOutcome.not_applicable("no_successful_mcp_tool_calls")
        if _json_size(calls) > MAX_MCP_CALL_EVIDENCE_BYTES:
            return EvaluationOutcome.not_applicable("oversized:mcp_evidence")
        test_case = LLMTestCase(
            input=case_input,
            actual_output=actual_output,
            tools_called=_tool_calls(calls),
            mcp_servers=self.mcp_servers,
        )
        return await self._measure(test_case, ctx, call_count=len(calls), turn_count=1)


class ConversationalMCPMetric(MCPMetric):
    """DeepEval MCP metrics reconstruct one MCP interaction turn per stored case."""

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        from deepeval.test_case import ConversationalTestCase, Turn

        episode = view.get("episode.turns")
        if not isinstance(episode, (list, tuple)) or not episode:
            return EvaluationOutcome.not_applicable("missing:episode.turns")
        if len(episode) > MAX_MCP_TURNS:
            return EvaluationOutcome.not_applicable("oversized:episode.turns")
        turns: list[Any] = []
        total_text = 0
        total_evidence = 0
        total_calls = 0
        total_events = 0
        try:
            for turn in episode:
                user_text = _bounded_text(turn.get("input"))
                output = turn.get("output")
                if not isinstance(output, str) or not output.strip():
                    return EvaluationOutcome.not_applicable(
                        f"unscorable_output:{turn.get('case_id', 'unknown')}"
                    )
                output_text = _bounded_text(output)
                total_text += len(user_text.encode("utf-8")) + len(output_text.encode("utf-8"))
                tool_events = turn.get("tool_events")
                if tool_events is None:
                    return EvaluationOutcome.not_applicable(
                        f"missing:execution.tool_events:{turn.get('case_id', 'unknown')}"
                    )
                if isinstance(tool_events, list):
                    total_events += len(tool_events)
                if total_events > MAX_MCP_EVENTS_PER_EPISODE:
                    raise ValueError("too many recorded tool events in episode")
                calls = _successful_calls(
                    tool_events,
                    call_limit=MAX_MCP_CALLS_PER_TURN,
                    event_limit=MAX_MCP_EVENTS_PER_TURN,
                    tool_events_are_mcp_only=self.tool_events_are_mcp_only,
                )
                total_calls += len(calls)
                if total_calls > MAX_MCP_CALLS_PER_EPISODE:
                    raise ValueError("too many MCP tool calls in episode")
                total_evidence += _json_size(calls)
                if total_evidence > MAX_MCP_EPISODE_EVIDENCE_BYTES:
                    raise ValueError("episode MCP call evidence exceeds its byte limit")
                turns.append(Turn(role="user", content=user_text))
                if calls:
                    turns.append(
                        Turn(
                            role="assistant",
                            content="Recorded MCP tool invocations.",
                            tools_called=_tool_calls(calls),
                        )
                    )
                turns.append(Turn(role="assistant", content=output_text))
            if total_text + total_evidence + _json_size(self.inventory) > MAX_EPISODE_TEXT_BYTES:
                raise ValueError("total MCP judge evidence exceeds the 1 MiB limit")
        except (ValueError, TypeError, AttributeError):
            return EvaluationOutcome.not_applicable("oversized_or_invalid:mcp_evidence")
        if not total_calls:
            return EvaluationOutcome.not_applicable("no_successful_mcp_tool_calls")
        test_case = ConversationalTestCase(turns=turns, mcp_servers=self.mcp_servers)
        return await self._measure(test_case, ctx, call_count=total_calls, turn_count=len(episode))


class MCPTaskCompletion(ConversationalMCPMetric):
    spec = MCP_METRIC_SPECS[1]
    manifest = _manifest(spec)


class MultiTurnMCPUse(ConversationalMCPMetric):
    spec = MCP_METRIC_SPECS[2]
    manifest = _manifest(spec)


MCP_EVALUATORS = (MCPUse, MCPTaskCompletion, MultiTurnMCPUse)
