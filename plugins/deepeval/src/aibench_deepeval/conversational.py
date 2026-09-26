"""DeepEval's conversational (multi-turn) metrics as aibench evaluators.

A conversation is an episode: the cases sharing a `group_id`, run in dataset order against a
stateful application. For each turn, the harness hands the evaluator the conversation up to
and including that turn (`episode.turns`), built from the run's recorded executions; the
evaluator turns it into DeepEval 4.2.5's `ConversationalTestCase`:

    each turn's case.input               -> Turn(role="user", content=...)
    each turn's execution.output (text)  -> Turn(role="assistant", content=...)
    each turn's execution.retrieved_context -> that assistant turn's retrieval_context
    each turn's execution.tool_events    -> that assistant turn's tools_called
    this turn's case.reference.answer    -> expected_outcome (turn contextual precision/recall)
    the plan's `chatbot_role`            -> chatbot_role (role adherence)

So every turn is scored on the conversation so far, and the episode's last turn is scored on
the whole conversation. Retrieved passages and tool calls reach the worker only for a metric
that reads them.

Not applicable instead of a score: a case in no episode (`missing:episode.turns`, from the
harness); a conversation with an earlier turn that did not complete
(`episode_incomplete:<case_id>`, from the harness); an assistant turn whose answer is empty
or not text (`unscorable_output:<case_id>`); a retrieval metric where no turn retrieved
anything (`empty:execution.retrieved_context`); tool use where no turn called a tool it
can read (`no_tool_calls`). All scores are DeepEval's own, normalized 0 to 1, higher better.
"""

from __future__ import annotations

import inspect
import os
from dataclasses import dataclass, field
from typing import Any, ClassVar

from aibench.core.models import DecisionRule, EvaluatorManifest, FieldRequirement, MetricDirection
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
from aibench_deepeval.metrics import _TRACE_ATTRIBUTES, _jsonable, _text, out_of_range

EPISODE_TURNS = "episode.turns"
_WINDOW = {"window_size": {"type": "integer", "minimum": 1, "maximum": 50}}
_STRINGS = {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}


@dataclass(frozen=True)
class ConversationSpec:
    name: str  # evaluator ID suffix: deepeval.<name>
    upstream: str  # DeepEval metric class
    summary: str
    concepts: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)  # extra JSON Schema properties
    required: tuple[str, ...] = ()
    retrieval: bool = False  # reads each turn's retrieved passages
    tools: bool = False  # reads each turn's tool calls
    expected_outcome: bool = False  # reads this turn's reference answer
    limitations: tuple[str, ...] = ()


CONVERSATION_SPECS: tuple[ConversationSpec, ...] = (
    ConversationSpec(
        "conversation_completeness",
        "ConversationCompletenessMetric",
        "the share of the user's intentions the conversation satisfied",
        ("conversation_completeness",),
        params=_WINDOW,
    ),
    ConversationSpec(
        "knowledge_retention",
        "KnowledgeRetentionMetric",
        "whether the assistant keeps what the user told it earlier in the conversation",
        ("knowledge_retention",),
    ),
    ConversationSpec(
        "role_adherence",
        "RoleAdherenceMetric",
        "the share of assistant turns that stay in the declared role",
        ("role_adherence",),
        params={"chatbot_role": {"type": "string", "minLength": 1}},
        required=("chatbot_role",),
    ),
    ConversationSpec(
        "goal_accuracy",
        "GoalAccuracyMetric",
        "whether the assistant achieved the user's goal in the conversation",
        ("goal_accuracy",),
    ),
    ConversationSpec(
        "topic_adherence",
        "TopicAdherenceMetric",
        "whether the assistant answers within the relevant topics and declines the rest",
        ("topic_adherence",),
        params={"relevant_topics": _STRINGS},
        required=("relevant_topics",),
    ),
    ConversationSpec(
        "tool_use",
        "ToolUseMetric",
        "whether the assistant chose and called the right available tools across the turns",
        ("tool_use",),
        params={"available_tools": _STRINGS},
        required=("available_tools",),
        tools=True,
    ),
    ConversationSpec(
        "turn_relevancy",
        "TurnRelevancyMetric",
        "the share of assistant turns relevant to the conversation so far",
        ("turn_relevancy",),
        params=_WINDOW,
    ),
    ConversationSpec(
        "turn_faithfulness",
        "TurnFaithfulnessMetric",
        "how well each assistant turn is supported by the passages retrieved for it",
        ("turn_groundedness",),
        params={
            **_WINDOW,
            "truths_extraction_limit": {"type": "integer", "minimum": 1},
            "penalize_ambiguous_claims": {"type": "boolean"},
        },
        retrieval=True,
    ),
    ConversationSpec(
        "turn_contextual_precision",
        "TurnContextualPrecisionMetric",
        "whether each turn's relevant passages are ranked first, against the expected outcome",
        ("turn_retrieval_precision",),
        params=_WINDOW,
        retrieval=True,
        expected_outcome=True,
    ),
    ConversationSpec(
        "turn_contextual_recall",
        "TurnContextualRecallMetric",
        "the share of the expected outcome each turn's retrieved passages support",
        ("turn_retrieval_recall",),
        params=_WINDOW,
        retrieval=True,
        expected_outcome=True,
    ),
    ConversationSpec(
        "turn_contextual_relevancy",
        "TurnContextualRelevancyMetric",
        "the share of each turn's retrieved passages relevant to the conversation",
        ("turn_retrieval_relevancy",),
        params=_WINDOW,
        retrieval=True,
    ),
)

_EXPECTED_OUTCOME_LIMIT = (
    (
        "The expected outcome is this turn's reviewed reference answer; a turn without one "
        "is not applicable."
    ),
)


def _requirements(spec: ConversationSpec) -> tuple[FieldRequirement, ...]:
    requires = [
        FieldRequirement(path="case.group_id"),
        FieldRequirement(path=EPISODE_TURNS),
        FieldRequirement(path="execution.output", non_empty=False),
    ]
    if spec.retrieval:
        requires.append(FieldRequirement(path="execution.retrieved_context", non_empty=False))
    if spec.tools:
        requires.append(FieldRequirement(path="execution.tool_events", non_empty=False))
    if spec.expected_outcome:
        requires.append(FieldRequirement(path="case.reference.answer"))
    return tuple(requires)


def _manifest(spec: ConversationSpec) -> EvaluatorManifest:
    required = ["judge", *spec.required]
    return EvaluatorManifest(
        evaluator_id=f"deepeval.{spec.name}",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="deepeval",
        package_version=PINNED_DEEPEVAL,
        description=(
            f"DeepEval {PINNED_DEEPEVAL} {spec.upstream}: {spec.summary}, scored on the "
            "conversation up to each turn."
        ),
        limitations=(
            "Judge-dependent: scores from different judge models are not comparable.",
            (
                "Each turn is scored on the conversation so far; an episode's last turn "
                "carries the whole conversation's score."
            ),
            *(_EXPECTED_OUTCOME_LIMIT if spec.expected_outcome else ()),
            *spec.limitations,
        ),
        concepts=spec.concepts,
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=_requirements(spec),
        default_rule=DecisionRule(comparator=">=", threshold=0.5),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": {"judge": JUDGE_SCHEMA, **spec.params},
        },
        uses_models=True,
        credentials=("the judge provider's key, passed explicitly to the plugin environment",),
        network_destinations=(
            "the configured judge model's API (none for a local python_factory judge)",
        ),
        internal_retries=0,
        internal_concurrency=2,
        requires_worker=True,
    )


class ConversationalMetric(Evaluator):
    """One DeepEval conversational metric; subclasses differ in `spec` and `manifest`."""

    spec: ClassVar[ConversationSpec]

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        os.environ.update(DEEPEVAL_ENVIRONMENT)
        _require_pinned_deepeval()
        import deepeval.metrics  # noqa: F401 - import once up front so failures surface here

        build_judge(self.params["judge"])  # validate the judge before any case runs

    def _reads(self) -> tuple[bool, bool, bool]:
        """Whether this metric reads retrieved passages, tool calls, the expected outcome."""
        return self.spec.retrieval, self.spec.tools, self.spec.expected_outcome

    def _test_case(self, view: EvaluationView) -> tuple[Any | None, str | None]:
        """DeepEval's ConversationalTestCase, or the reason the turn is not applicable."""
        from deepeval.test_case import ConversationalTestCase, ToolCall, Turn

        retrieval, tools, expected = self._reads()
        turns: list[Any] = []
        retrieved_any = called_any = False
        for turn in view.get(EPISODE_TURNS):
            output = turn["output"]
            if not isinstance(output, str) or not output.strip():
                return None, f"unscorable_output:{turn['case_id']}"
            extra: dict[str, Any] = {}
            if retrieval:
                chunks = [c for c in turn.get("retrieved_context") or [] if c.strip()]
                retrieved_any = retrieved_any or bool(chunks)
                extra["retrieval_context"] = chunks
            if tools:
                calls = [a for a in parse_tool_events(turn.get("tool_events") or []) if a.name]
                called_any = called_any or bool(calls)
                extra["tools_called"] = [
                    ToolCall(
                        name=call.name,
                        input_parameters=call.arguments
                        if isinstance(call.arguments, dict)
                        else None,
                        output=call.result,
                    )
                    for call in calls
                ]
            turns.append(Turn(role="user", content=_text(turn["input"])))
            turns.append(Turn(role="assistant", content=output, **extra))
        if retrieval and not retrieved_any:
            return None, "empty:execution.retrieved_context"
        if tools and not called_any:
            return None, "no_tool_calls"
        case: dict[str, Any] = {"turns": turns}
        if expected:
            answer = view.get("case.reference.answer")
            if not isinstance(answer, str) or not answer.strip():
                return None, "missing:case.reference.answer"
            case["expected_outcome"] = answer
        if self.params.get("chatbot_role"):
            case["chatbot_role"] = self.params["chatbot_role"]
        return ConversationalTestCase(**case), None

    def _new_metric(self, judge: Any) -> Any:
        import deepeval.metrics
        from deepeval.test_case import ToolCall

        upstream = getattr(deepeval.metrics, self.spec.upstream)
        arguments: dict[str, Any] = {
            key: self.params[key]
            for key in self.spec.params
            if key in self.params and key != "chatbot_role"  # a test case field, not an init one
        }
        if "available_tools" in arguments:
            arguments["available_tools"] = [ToolCall(name=n) for n in arguments["available_tools"]]
        arguments.update(
            model=judge,
            threshold=0.5,  # upstream flag only; the harness decides with its own rule
            include_reason=True,
            async_mode=True,
            strict_mode=False,
            verbose_mode=False,
        )
        accepted = inspect.signature(upstream.__init__).parameters
        return upstream(**{k: v for k, v in arguments.items() if k in accepted})

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        test_case, not_applicable = self._test_case(view)
        if test_case is None:
            return EvaluationOutcome.not_applicable(str(not_applicable))
        judge = build_judge(self.params["judge"])
        metric = self._new_metric(judge)
        try:
            score = await metric.a_measure(test_case, _show_indicator=False)
        finally:
            report_judge_usage(ctx, metric, judge)
        bad = out_of_range(score)
        if bad is not None:
            return bad
        raw: dict[str, Any] = {
            "deepeval_version": PINNED_DEEPEVAL,
            "metric": self.spec.upstream,
            "judge": getattr(metric, "evaluation_model", None),
            "turns": len(test_case.turns) // 2,
            "score": metric.score,
            "reason": getattr(metric, "reason", None),
            "upstream_success": metric.success,
            "upstream_threshold": metric.threshold,
            "evaluation_cost": getattr(metric, "evaluation_cost", None),
        }
        for attribute in (*_TRACE_ATTRIBUTES, "user_intentions", "knowledges"):
            value = getattr(metric, attribute, None)
            if isinstance(value, (list, tuple)):
                raw[attribute] = _jsonable(value)
        return EvaluationOutcome.ok(
            "scalar",
            float(score),
            evidence=tuple(r.path for r in self.manifest.requires if r.path != "case.group_id"),
            raw=raw,
        )


def _evaluator_class(spec: ConversationSpec) -> type[ConversationalMetric]:
    name = "".join(part.title() for part in spec.name.split("_"))
    return type(name, (ConversationalMetric,), {"spec": spec, "manifest": _manifest(spec)})


CONVERSATIONAL_METRICS: tuple[type[ConversationalMetric], ...] = tuple(
    _evaluator_class(spec) for spec in CONVERSATION_SPECS
)


# --------------------------------------------------------------------------- G-Eval

# Conversation fields G-Eval may be asked to read beyond each turn's role and content, with
# the recorded field each one needs.
_GEVAL_EXTRA = {
    "retrieval_context": {"path": "execution.retrieved_context", "non_empty": False},
    "tools_called": {"path": "execution.tool_events", "non_empty": False},
    "expected_outcome": {"path": "case.reference.answer", "non_empty": True},
    "chatbot_role": None,  # from the plan's `chatbot_role`
}

CONVERSATIONAL_GEVAL_SPEC = ConversationSpec(
    "conversational_g_eval",
    "ConversationalGEval",
    "a judge scores the conversation against criteria you write",
    ("custom_criteria",),
)


class ConversationalGEval(ConversationalMetric):
    """Conversational G-Eval: the plan states the criteria (or steps) and which parts of the
    conversation the judge sees (`evaluation_params`, default role and content). The criteria
    are part of the metric's identity."""

    spec = CONVERSATIONAL_GEVAL_SPEC
    manifest = EvaluatorManifest(
        **{
            **_manifest(CONVERSATIONAL_GEVAL_SPEC).model_dump(),
            "description": (
                f"DeepEval {PINNED_DEEPEVAL} ConversationalGEval: a judge scores the "
                "conversation up to each turn against criteria the plan states (0 to 1)."
            ),
            "limitations": (
                "Judge-dependent: scores from different judge models are not comparable.",
                (
                    "Criteria wording changes the metric: runs with different criteria are not "
                    "comparable."
                ),
                (
                    "Each turn is scored on the conversation so far; an episode's last turn "
                    "carries the whole conversation's score."
                ),
            ),
            "parameter_requirements": {
                "evaluation_params": {k: v for k, v in _GEVAL_EXTRA.items() if v is not None}
            },
            "parameters_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["judge", "name"],
                "anyOf": [{"required": ["criteria"]}, {"required": ["evaluation_steps"]}],
                "properties": {
                    "judge": JUDGE_SCHEMA,
                    "name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "criteria": {"type": "string", "minLength": 1},
                    "evaluation_steps": _STRINGS,
                    "evaluation_params": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"enum": ["role", "content", *_GEVAL_EXTRA]},
                    },
                    "chatbot_role": {"type": "string", "minLength": 1},
                },
            },
        }
    )

    def _chosen(self) -> list[str]:
        return list(self.params.get("evaluation_params") or ["role", "content"])

    def _reads(self) -> tuple[bool, bool, bool]:
        chosen = self._chosen()
        return (
            "retrieval_context" in chosen,
            "tools_called" in chosen,
            "expected_outcome" in chosen,
        )

    def _new_metric(self, judge: Any) -> Any:
        from deepeval.metrics import ConversationalGEval as UpstreamGEval
        from deepeval.test_case import MultiTurnParams

        return UpstreamGEval(
            name=self.params["name"],
            evaluation_params=[MultiTurnParams(name) for name in self._chosen()],
            criteria=self.params.get("criteria"),
            evaluation_steps=self.params.get("evaluation_steps"),
            model=judge,
            threshold=0.5,
            async_mode=True,
            strict_mode=False,
            verbose_mode=False,
        )
