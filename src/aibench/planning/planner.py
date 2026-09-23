"""Bounded model-backed planning (§8, 07-T3).

The loop: give the model the evidence summaries, let it call narrow read-only tools, and
accept a draft only through `write_plan_draft`, which the harness validates outside the
model. Invalid drafts get the findings back for a bounded number of repairs; running out of
model calls, tool calls, repairs or tokens — or any provider failure — falls back to the
deterministic template, with the reason recorded (§8: "fall back to a deterministic
template with unresolved gaps"). If the plan already needs a permission the policy does not
grant (application, data roots, plugin environments), the model is not contacted at all:
the briefing would describe data the user has not authorized for this use.

Tools (§8 names; all read-only functions over in-memory state): `read_profile`,
`summarize_dataset`, `list_evaluators`, `describe_evaluator`, `validate_plan`,
`estimate_cost`, `write_plan_draft`. There is no terminal, file, network or process tool;
an unknown tool name is refused and counted (07-G4).

What the model sees: the declared-config profile, dataset field *counts* and the evaluator
catalog. Never case inputs, reference answers or other label values.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import AibenchError
from aibench.inspection.dataset_summary import DatasetSummary
from aibench.inspection.profile import ApplicationProfile
from aibench.planning.catalog import CONCEPTS, ENGINE_RECORDED, MetricOption
from aibench.planning.drafts import (
    DraftContext,
    DraftProposal,
    DraftValidation,
    PlannerProvenance,
    build_plan,
    validate_draft,
)
from aibench.planning.template import template_proposal
from aibench.runners import load_application
from aibench.security.policy import application_denials, plan_denials


class PlannerError(AibenchError):
    """A provider failed: transport, protocol or response format."""


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str  # JSON text, as providers return it


@dataclass(frozen=True)
class ModelReply:
    text: str | None
    tool_calls: tuple[ToolCall, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


class PlannerProvider(Protocol):
    """A chat model that can call tools. Messages and tool specs use the widely supported
    chat-completions shapes; a provider adapts them to its API."""

    name: str
    model: str

    def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply: ...


@dataclass(frozen=True)
class PlannerLimits:
    max_model_calls: int = 6
    max_tool_calls: int = 12
    max_repairs: int = 2
    max_total_tokens: int | None = 60_000


@dataclass
class PlanningInputs:
    objectives: list[str]
    profile: ApplicationProfile
    dataset: DatasetSummary
    catalog: list[MetricOption]
    context: DraftContext


@dataclass
class PlanningOutcome:
    proposal: DraftProposal
    validation: DraftValidation
    provenance: PlannerProvenance


SYSTEM_PROMPT = """You plan evaluations for an AI application benchmark harness.
Rules:
- Choose metrics ONLY from the evaluator catalog, by their exact "metric" id. Prefer
  eligible options; an ineligible option cannot run and must become a gap instead.
- Every objective needs at least one metric, or an explicit gap explaining why it cannot be
  measured with the available evidence. latency and reliability are recorded for every
  execution by the engine and need no metric.
- Copy every user objective verbatim into "objectives" with source "user"; never drop,
  merge or reword them. Assign each the concepts its wording asks about.
- Never invent parameters, JSON schemas, thresholds, endpoints or evidence. Use only the
  parameters and rules in "user_supplied"; otherwise leave params empty and rule null (the
  evaluator's documented default applies), or ask a question.
- When you select an evaluator for which the user supplied parameters or a rule, copy those
  settings exactly; do not omit or change them.
- You cannot choose which cases run; the user decides that.
- Reference context in the dataset is judge-only; it is NOT observed retrieval.
- Finish by calling write_plan_draft exactly once with the complete draft. If it returns
  findings, fix them and call write_plan_draft again.
Known concepts: {concepts}."""


def _tool(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


_NO_ARGS: dict[str, Any] = {"type": "object", "properties": {}, "additionalProperties": False}


def tool_specs() -> list[dict[str, Any]]:
    draft_schema = DraftProposal.model_json_schema()
    return [
        _tool("read_profile", "The application's evidence profile.", _NO_ARGS),
        _tool("summarize_dataset", "Dataset field coverage counts (no values).", _NO_ARGS),
        _tool("list_evaluators", "Installed evaluators with eligibility.", _NO_ARGS),
        _tool(
            "describe_evaluator",
            "Full details of one catalog entry.",
            {
                "type": "object",
                "properties": {"metric": {"type": "string"}},
                "required": ["metric"],
                "additionalProperties": False,
            },
        ),
        _tool("validate_plan", "Validate a draft without submitting it.", draft_schema),
        _tool("estimate_cost", "Call counts and cost estimate for a draft.", draft_schema),
        _tool("write_plan_draft", "Submit the final draft for validation.", draft_schema),
    ]


TOOL_NAMES = frozenset(spec["function"]["name"] for spec in tool_specs())


class _Session:
    def __init__(
        self,
        inputs: PlanningInputs,
        provider: PlannerProvider,
        limits: PlannerLimits,
    ) -> None:
        self.inputs = inputs
        self.provider = provider
        self.limits = limits
        self.model_calls = 0
        self.tool_calls = 0
        self.repairs = 0
        self.rejected: list[str] = []
        self.prompt_tokens: int | None = None
        self.completion_tokens: int | None = None
        self.calls_without_usage = 0
        self.accepted: tuple[DraftProposal, DraftValidation] | None = None
        self.handlers: dict[str, Callable[[dict[str, Any]], str]] = {
            "read_profile": lambda _: self.inputs.profile.model_dump_json(),
            "summarize_dataset": lambda _: self.inputs.dataset.model_dump_json(),
            "list_evaluators": lambda _: json.dumps([o.as_dict() for o in self.inputs.catalog]),
            "describe_evaluator": self._describe,
            "validate_plan": lambda args: self._validate(args, submit=False),
            "estimate_cost": self._estimate,
            "write_plan_draft": lambda args: self._validate(args, submit=True),
        }
        assert set(self.handlers) == TOOL_NAMES

    # ------------------------------------------------------------------ tools

    def _describe(self, args: dict[str, Any]) -> str:
        wanted = str(args.get("metric", ""))
        for option in self.inputs.catalog:
            if wanted in (option.metric, option.evaluator_id):
                return json.dumps(option.as_dict())
        return json.dumps({"error": f"no installed evaluator {wanted!r}"})

    def _proposal(self, args: dict[str, Any]) -> DraftProposal | str:
        try:
            return DraftProposal.model_validate(args)
        except PydanticValidationError as exc:
            problems = [
                f"{'.'.join(str(p) for p in e['loc']) or 'draft'}: {e['msg']}" for e in exc.errors()
            ]
            return json.dumps({"accepted": False, "schema_errors": problems[:20]})
        except RecursionError:
            return json.dumps(
                {"accepted": False, "schema_errors": ["draft is nested too deeply to validate"]}
            )

    def _validate(self, args: dict[str, Any], *, submit: bool) -> str:
        proposal = self._proposal(args)
        if isinstance(proposal, str):
            if submit:
                self.repairs += 1
            return proposal
        validation = validate_draft(proposal, self.inputs.context)
        blocking = validation.blocking_messages()
        if submit and not blocking:
            self.accepted = (proposal, validation)
            return json.dumps({"accepted": True})
        if submit:
            self.repairs += 1
        return json.dumps(
            {
                "accepted": False if submit else None,
                "blocking_findings": blocking,
                "warnings": [f.message for f in validation.findings if not f.blocking],
            }
        )

    def _estimate(self, args: dict[str, Any]) -> str:
        proposal = self._proposal(args)
        if isinstance(proposal, str):
            return proposal
        validation = validate_draft(proposal, self.inputs.context)
        estimate = validation.estimate
        return estimate.model_dump_json() if estimate else json.dumps({"error": "no estimate"})

    # ------------------------------------------------------------------ loop

    def _account(self, reply: ModelReply) -> None:
        if reply.prompt_tokens is None and reply.completion_tokens is None:
            self.calls_without_usage += 1
        if reply.prompt_tokens is not None:
            self.prompt_tokens = (self.prompt_tokens or 0) + reply.prompt_tokens
        if reply.completion_tokens is not None:
            self.completion_tokens = (self.completion_tokens or 0) + reply.completion_tokens

    def _over_tokens(self) -> bool:
        cap = self.limits.max_total_tokens
        total = (self.prompt_tokens or 0) + (self.completion_tokens or 0)
        return cap is not None and total > cap

    def _opening(self) -> list[dict[str, Any]]:
        inputs = self.inputs
        ctx = inputs.context
        briefing = {
            "objectives": inputs.objectives or ["(none stated: ask the user)"],
            "user_supplied": {
                "params": ctx.user_params,
                "rules": {k: v.model_dump(mode="json") for k, v in ctx.user_rules.items()},
            },
            "application_profile": json.loads(inputs.profile.model_dump_json()),
            "dataset_summary": json.loads(inputs.dataset.model_dump_json()),
            "evaluator_catalog": [o.as_dict() for o in inputs.catalog],
            "engine_recorded_concepts": sorted(ENGINE_RECORDED),
        }
        return [
            {"role": "system", "content": SYSTEM_PROMPT.format(concepts=", ".join(CONCEPTS))},
            {"role": "user", "content": json.dumps(briefing)},
        ]

    def run(self) -> str | None:
        """Returns None when a draft was accepted, else the reason planning stopped."""
        messages = self._opening()
        tools = tool_specs()
        while True:
            if self.model_calls >= self.limits.max_model_calls:
                return f"model call limit reached ({self.limits.max_model_calls})"
            self.model_calls += 1
            try:
                reply = self.provider.complete(messages, tools)
            except PlannerError as exc:
                return f"provider failed: {exc}"
            except Exception as exc:  # noqa: BLE001 - any provider bug falls back, never crashes
                return f"provider failed: {type(exc).__name__}: {exc}"[:500]
            self._account(reply)
            if self._over_tokens():
                return f"planner token limit reached ({self.limits.max_total_tokens})"
            assistant: dict[str, Any] = {"role": "assistant", "content": reply.text}
            if reply.tool_calls:
                assistant["tool_calls"] = [
                    {
                        "id": c.call_id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": c.arguments},
                    }
                    for c in reply.tool_calls
                ]
            messages.append(assistant)
            if not reply.tool_calls:
                self.repairs += 1
                if self.repairs > self.limits.max_repairs:
                    return f"repair limit reached ({self.limits.max_repairs})"
                messages.append(
                    {"role": "user", "content": "Submit the draft by calling write_plan_draft."}
                )
                continue
            for call in reply.tool_calls:
                self.tool_calls += 1
                if self.tool_calls > self.limits.max_tool_calls:
                    return f"tool call limit reached ({self.limits.max_tool_calls})"
                messages.append(
                    {"role": "tool", "tool_call_id": call.call_id, "content": self._dispatch(call)}
                )
                if self.accepted is not None:
                    return None
                if self.repairs > self.limits.max_repairs:
                    return f"repair limit reached ({self.limits.max_repairs})"

    def _dispatch(self, call: ToolCall) -> str:
        handler = self.handlers.get(call.name)
        if handler is None:
            self.rejected.append(call.name)
            return json.dumps(
                {"error": f"unknown tool {call.name!r}; available: {sorted(TOOL_NAMES)}"}
            )
        try:
            args = json.loads(call.arguments or "{}")
        except json.JSONDecodeError as exc:
            return json.dumps({"error": f"arguments are not valid JSON: {exc.msg}"})
        except (RecursionError, ValueError):
            return json.dumps({"error": "arguments could not be parsed safely"})
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must be a JSON object"})
        return handler(args)


def briefing_denials(inputs: PlanningInputs) -> list[str]:
    """Permissions the plan needs before any model may see its briefing."""
    ctx = inputs.context
    effective = ctx.policy.with_trusted_local(ctx.trusted_local)
    probe = build_plan(DraftProposal(), ctx)
    denials = plan_denials(effective, probe, ctx.out_dir)
    try:
        denials += application_denials(effective, load_application(ctx.application).spec)
    except AibenchError as exc:
        denials.append(str(exc))
    return denials


def plan_with_template(inputs: PlanningInputs) -> PlanningOutcome:
    ctx = inputs.context
    proposal = template_proposal(
        inputs.objectives, inputs.catalog, params=ctx.user_params, rules=ctx.user_rules
    )
    validation = validate_draft(proposal, inputs.context)
    return PlanningOutcome(proposal, validation, PlannerProvenance(kind="template"))


def plan_with_model(
    inputs: PlanningInputs, provider: PlannerProvider, limits: PlannerLimits | None = None
) -> PlanningOutcome:
    denials = briefing_denials(inputs)
    if denials:
        fallback = plan_with_template(inputs)
        reason = "model not contacted: the plan needs permissions the policy does not grant"
        provenance = PlannerProvenance(
            kind="model", provider=provider.name, model=provider.model, fallback_reason=reason
        )
        return PlanningOutcome(fallback.proposal, fallback.validation, provenance)
    session = _Session(inputs, provider, limits or PlannerLimits())
    stop = session.run()
    provenance = PlannerProvenance(
        kind="model",
        provider=provider.name,
        model=provider.model,
        model_calls=session.model_calls,
        tool_calls=session.tool_calls,
        rejected_tool_calls=tuple(session.rejected),
        repairs=session.repairs,
        prompt_tokens=session.prompt_tokens,
        completion_tokens=session.completion_tokens,
        calls_without_usage=session.calls_without_usage,
        fallback_reason=stop,
    )
    if session.accepted is not None:
        proposal, validation = session.accepted
        return PlanningOutcome(proposal, validation, provenance)
    fallback = plan_with_template(inputs)  # provenance keeps the model's attempt and reason
    return PlanningOutcome(fallback.proposal, fallback.validation, provenance)


__all__ = [
    "TOOL_NAMES",
    "ModelReply",
    "PlannerError",
    "PlannerLimits",
    "PlannerProvider",
    "PlanningInputs",
    "PlanningOutcome",
    "ToolCall",
    "plan_with_model",
    "plan_with_template",
    "tool_specs",
]
