"""DeepEval's single-turn metrics as aibench evaluators, one per metric, from one table.

Field translation (checked against DeepEval 4.2.5's `LLMTestCase`); a metric reads only the
fields its spec lists:

    case.input                     -> input (JSON text if not a string)
    execution.output (text)        -> actual_output
    case.reference.answer          -> expected_output
    execution.retrieved_context[]  -> retrieval_context (what the application retrieved)
    case.reference.context[]       -> context (the Golden's reviewed ground truth)
    execution.tool_events[]        -> tools_called (name, arguments, result)
    case.reference.tools.tool_names -> expected_tools (names)

The two contexts are never substituted for each other: retrieval metrics read only what the
application retrieved, and hallucination reads only the reviewed reference context.

Not applicable instead of a score: output empty or not text (`unscorable_output:<kind>`);
a context with no non-blank chunk (`empty:<path>`); no expected tools
(`empty:case.reference.tools`); a tool metric with no tool call it can read, where the
metric scores the calls themselves (`no_tool_calls`). Everything else is DeepEval's own
score: every metric here is normalized so higher is better (DeepEval's success is
`score >= threshold` for all of them, bias and toxicity included). The upstream success
flag is kept as metadata; the harness decides with the binding's rule.

Execution: a new metric (and judge) per case; DeepEval's retries, telemetry, `.env` and
legacy key file are off (set before DeepEval is imported, see `faithfulness`).
"""

from __future__ import annotations

import inspect
import json
import os
import re
import statistics
from dataclasses import dataclass, field
from typing import Any, ClassVar

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricDirection,
    MetricValue,
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

# Test-case field -> (evaluation view path, requirement is non-empty)
FIELDS: dict[str, tuple[str, bool]] = {
    "input": ("case.input", True),
    "actual_output": ("execution.output", False),  # blank or non-text: not applicable
    "expected_output": ("case.reference.answer", True),
    "retrieval_context": ("execution.retrieved_context", False),  # empty: not applicable
    # Required non-empty, so planning sees which datasets have reviewed context at all.
    "context": ("case.reference.context", True),
    "tools_called": ("execution.tool_events", False),  # none called is evidence
    "expected_tools": ("case.reference.tools", True),
    "trace": ("execution.trace", True),  # the imported trace, as DeepEval's trace (below)
}

# Upstream lists worth keeping in the raw artifact, when a metric has them.
_TRACE_ATTRIBUTES = (
    "statements",
    "truths",
    "claims",
    "opinions",
    "extracted_pii",
    "misuses",
    "advices",
    "role_violations",
    "evaluation_steps",
    "verdicts",
)


@dataclass(frozen=True)
class Spec:
    name: str  # evaluator ID suffix: deepeval.<name>
    upstream: str  # DeepEval metric class
    summary: str
    fields: tuple[str, ...]
    concepts: tuple[str, ...]
    judged: bool = True
    params: dict[str, Any] = field(default_factory=dict)  # extra JSON Schema properties
    required: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    needs_tool_calls: bool = False  # not applicable when no tool call can be read


_STRINGS = {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}
_TOOL_NAMES = {"type": "array", "items": {"type": "string", "minLength": 1}}

_TRACE_LIMITS = (
    (
        "Reads the trace imported for the execution (`aibench traces import`); a run "
        "without one is not applicable, and so is a partial trace."
    ),
    "The trace's span inputs and outputs are sent to the judge, each cut to 4000 characters.",
)
_PLAN_LIMITS = (
    *_TRACE_LIMITS,
    "Not applicable when the judge finds no plan in the trace (DeepEval would score it 1).",
)

SPECS: tuple[Spec, ...] = (
    Spec(
        "answer_relevancy",
        "AnswerRelevancyMetric",
        "the share of the answer's statements that address the input",
        ("input", "actual_output"),
        ("relevancy",),
    ),
    Spec(
        "contextual_precision",
        "ContextualPrecisionMetric",
        "whether the retrieved chunks relevant to the reference answer are ranked first",
        ("input", "actual_output", "expected_output", "retrieval_context"),
        ("retrieval_precision",),
    ),
    Spec(
        "contextual_recall",
        "ContextualRecallMetric",
        "the share of the reference answer's statements the retrieved context supports",
        ("input", "actual_output", "expected_output", "retrieval_context"),
        ("retrieval_recall",),
    ),
    Spec(
        "contextual_relevancy",
        "ContextualRelevancyMetric",
        "the share of retrieved statements relevant to the input",
        ("input", "actual_output", "retrieval_context"),
        ("retrieval_relevancy",),
    ),
    Spec(
        "hallucination",
        "HallucinationMetric",
        "the share of the reviewed reference context the answer does not contradict",
        ("input", "actual_output", "context"),
        ("groundedness",),
        limitations=(
            (
                "Reads the Golden's reviewed reference context, not what the application "
                "retrieved; use faithfulness for retrieved passages."
            ),
        ),
    ),
    Spec(
        "bias",
        "BiasMetric",
        "the share of the answer's opinions that are free of bias",
        ("input", "actual_output"),
        ("bias",),
        limitations=("An answer with no opinions scores 1.0 upstream (nothing to be biased).",),
    ),
    Spec(
        "toxicity",
        "ToxicityMetric",
        "the share of the answer's opinions that are not toxic",
        ("input", "actual_output"),
        ("toxicity",),
        limitations=("An answer with no opinions scores 1.0 upstream.",),
    ),
    Spec(
        "pii_leakage",
        "PIILeakageMetric",
        "whether the answer is free of personal data leakage",
        ("input", "actual_output"),
        ("privacy",),
    ),
    Spec(
        "misuse",
        "MisuseMetric",
        "whether the answer avoids misuse outside the application's domain",
        ("input", "actual_output"),
        ("misuse",),
        params={"domain": {"type": "string", "minLength": 1}},
        required=("domain",),
    ),
    Spec(
        "non_advice",
        "NonAdviceMetric",
        "whether the answer avoids giving the listed kinds of advice",
        ("input", "actual_output"),
        ("advice",),
        params={"advice_types": _STRINGS},
        required=("advice_types",),
    ),
    Spec(
        "role_violation",
        "RoleViolationMetric",
        "1 when the answer stays in the declared role, 0 when it breaks it",
        ("input", "actual_output"),
        ("role_adherence",),
        params={"role": {"type": "string", "minLength": 1}},
        required=("role",),
    ),
    Spec(
        "prompt_alignment",
        "PromptAlignmentMetric",
        "the share of the given instructions the answer follows",
        ("input", "actual_output"),
        ("instruction_following",),
        params={"prompt_instructions": _STRINGS},
        required=("prompt_instructions",),
    ),
    Spec(
        "summarization",
        "SummarizationMetric",
        "how well the answer summarizes the input (coverage and alignment)",
        ("input", "actual_output"),
        ("summarization",),
        params={
            "assessment_questions": _STRINGS,
            "n": {"type": "integer", "minimum": 1, "maximum": 50},
            "truths_extraction_limit": {"type": "integer", "minimum": 1},
        },
    ),
    Spec(
        "task_completion",
        "TaskCompletionMetric",
        "how completely the answer accomplishes the task in the input",
        ("input", "actual_output", "tools_called"),
        ("task_completion",),
        params={"task": {"type": "string", "minLength": 1}},
        limitations=(
            (
                "Judged from the input, answer and reported tool calls; aibench records no "
                "agent trace, so DeepEval's trace-based mode is not used."
            ),
        ),
    ),
    Spec(
        "argument_correctness",
        "ArgumentCorrectnessMetric",
        "the share of tool calls whose arguments suit the input",
        ("input", "actual_output", "tools_called"),
        ("tool_arguments",),
        needs_tool_calls=True,
    ),
    Spec(
        "tool_correctness",
        "ToolCorrectnessMetric",
        "how well the called tools match the reference tools, by name",
        ("input", "actual_output", "tools_called", "expected_tools"),
        ("tool_use",),
        params={
            "should_consider_ordering": {"type": "boolean"},
            "should_exact_match": {"type": "boolean"},
        },
        limitations=(
            (
                "DeepEval builds its judge model even when it compares names only, so a judge "
                "must be configured; comparing names makes no judge calls."
            ),
        ),
    ),
    Spec(
        "tool_permission",
        "ToolPermissionMetric",
        "whether every tool call stays within the allowed tools and off the denied ones",
        ("tools_called",),
        ("tool_permissions",),
        judged=False,
        params={"allowed_tools": _TOOL_NAMES, "denied_tools": _TOOL_NAMES},
        required=("allowed_tools|denied_tools",),
    ),
    Spec(
        "step_efficiency",
        "StepEfficiencyMetric",
        "how directly the agent reached its answer, without unneeded steps (from its trace)",
        ("input", "actual_output", "trace"),
        ("agent_efficiency",),
        limitations=_TRACE_LIMITS,
    ),
    Spec(
        "plan_quality",
        "PlanQualityMetric",
        "how sound the plan the agent made for the task was (from its trace)",
        ("input", "actual_output", "trace"),
        ("plan_quality",),
        limitations=_PLAN_LIMITS,
    ),
    Spec(
        "plan_adherence",
        "PlanAdherenceMetric",
        "how closely the agent followed its own plan (from its trace)",
        ("input", "actual_output", "trace"),
        ("plan_adherence",),
        limitations=_PLAN_LIMITS,
    ),
    Spec(
        "agent_loop_detection",
        "AgentLoopDetectionMetric",
        "1 when the trace shows no repeated tool calls, stalled reasoning or call cycles; "
        "lower as the agent loops",
        ("input", "actual_output", "trace"),
        ("agent_loops",),
        judged=False,
        params={
            "check_tool_repetition": {"type": "boolean"},
            "check_reasoning_stagnation": {"type": "boolean"},
            "check_call_graph_cycles": {"type": "boolean"},
            "repetition_threshold": {"type": "integer", "minimum": 2},
            "similarity_threshold": {"type": "number", "minimum": 0, "maximum": 1},
        },
        limitations=_TRACE_LIMITS[:1],
    ),
    Spec(
        "exact_match",
        "ExactMatchMetric",
        "1 when the answer equals the reference answer exactly, else 0",
        ("input", "actual_output", "expected_output"),
        ("correctness",),
        judged=False,
    ),
    Spec(
        "pattern_match",
        "PatternMatchMetric",
        "1 when the whole answer matches the given regular expression, else 0",
        ("input", "actual_output"),
        ("pattern",),
        judged=False,
        params={"pattern": {"type": "string", "minLength": 1}, "ignore_case": {"type": "boolean"}},
        required=("pattern",),
    ),
)


def _parameters_schema(spec: Spec) -> dict[str, Any]:
    properties: dict[str, Any] = dict(spec.params)
    required = [name for name in spec.required if "|" not in name]
    alternatives = [name.split("|") for name in spec.required if "|" in name]
    if spec.judged:
        properties["judge"] = JUDGE_SCHEMA
        required.insert(0, "judge")
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }
    if alternatives:
        schema["anyOf"] = [{"required": [name]} for name in alternatives[0]]
    return schema


def _manifest(spec: Spec) -> EvaluatorManifest:
    judged = spec.judged
    return EvaluatorManifest(
        evaluator_id=f"deepeval.{spec.name}",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="deepeval",
        package_version=PINNED_DEEPEVAL,
        description=f"DeepEval {PINNED_DEEPEVAL} {spec.upstream}: {spec.summary}.",
        limitations=(
            *(
                ("Judge-dependent: scores from different judge models are not comparable.",)
                if judged
                else ()
            ),
            *spec.limitations,
        ),
        concepts=spec.concepts,
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=tuple(
            FieldRequirement(path=FIELDS[name][0], non_empty=FIELDS[name][1])
            for name in spec.fields
        ),
        default_rule=DecisionRule(comparator=">=", threshold=0.5),
        parameters_schema=_parameters_schema(spec),
        uses_models=judged,
        credentials=(
            ("the judge provider's key, passed explicitly to the plugin environment",)
            if judged
            else ()
        ),
        network_destinations=(
            ("the configured judge model's API (none for a local python_factory judge)",)
            if judged
            else ()
        ),
        internal_retries=0,
        internal_concurrency=2 if judged else 1,
        requires_worker=True,
    )


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def out_of_range(score: Any) -> EvaluationOutcome | None:
    """An error for a score outside 0..1: every metric here is normalized, so anything
    else is a judge answer DeepEval did not bound, never a score to record."""
    if isinstance(score, (int, float)) and 0.0 <= float(score) <= 1.0:
        return None
    return EvaluationOutcome.error(f"judge_out_of_range: DeepEval returned {score!r}, not 0..1")


_SPAN_TYPES = {"agent": "agent", "llm": "llm", "tool": "tool", "retriever": "retriever"}


def deepeval_trace(
    tree: dict[str, Any], task: str | None = None, answer: str | None = None
) -> dict[str, Any]:
    """aibench's span tree (`observations.otel.span_tree`) as the nested trace dict DeepEval's
    agent metrics read: each span's `name`, `type`, `input`, `output`, `model`, `error` and
    `children`. Several root spans hang under one `base` span, as DeepEval expects one root.

    DeepEval reads the agent's task from the root's input. OpenTelemetry instrumentation
    often leaves message content out (GenAI content capture is off by default), so a root
    without input or output takes the case's input as `task` and the recorded answer."""

    def span(node: dict[str, Any]) -> dict[str, Any]:
        converted: dict[str, Any] = {
            "name": node["name"],
            "type": _SPAN_TYPES.get(node["kind"], "base"),
        }
        for key in ("input", "output", "model"):
            if key in node:
                converted[key] = node[key]
        if node.get("error"):
            converted["error"] = "the span ended with an error status"
        converted["children"] = [span(child) for child in node.get("children", [])]
        return converted

    roots = [span(root) for root in tree["spans"]]
    root = roots[0] if len(roots) == 1 else {"name": "trace", "type": "base", "children": roots}
    if task and not root.get("input"):
        root["input"] = task
    if answer and not root.get("output"):
        root["output"] = answer
    return root


def no_plan(metric: Any) -> bool:
    """DeepEval's plan metrics score 1 when the trace holds no plan at all; that is no
    evidence of a good plan, so the harness reports it as not applicable. The upstream
    signal is only the reason text (pinned version), checked in both its wordings."""
    return "no plans to evaluate" in str(getattr(metric, "reason", "") or "").lower()


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class DeepEvalMetric(Evaluator):
    """One DeepEval single-turn metric; subclasses differ only in `spec` and `manifest`."""

    spec: ClassVar[Spec]

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        os.environ.update(DEEPEVAL_ENVIRONMENT)
        _require_pinned_deepeval()
        import deepeval.metrics  # noqa: F401 - import once up front so failures surface here

        if self.spec.judged:
            build_judge(self.params["judge"])  # validate the judge before any case runs

    def _fields(self) -> tuple[str, ...]:
        return self.spec.fields

    def _test_case(self, view: EvaluationView) -> tuple[dict[str, Any] | None, str | None]:
        """The LLMTestCase arguments, or the reason the case is not applicable."""
        from deepeval.test_case import ToolCall

        values: dict[str, Any] = {}
        fields = self._fields()
        # The harness checks the manifest's static requirements before evaluating; G-Eval's
        # depend on its parameters, so every field read here is checked again.
        for name in fields:
            if view.state(FIELDS[name][0]) == "missing":
                return None, f"missing:{FIELDS[name][0]}"
        # DeepEval requires input and actual_output on every test case; they are only
        # *judged* when a metric reads them.
        values["input"] = _text(view.get("case.input"))
        values["actual_output"] = ""
        if "actual_output" in fields:
            output = view.get("execution.output")
            if not isinstance(output, str):
                return None, f"unscorable_output:{type(output).__name__}"
            if not output.strip():
                return None, "unscorable_output:blank"
            values["actual_output"] = output
        if "expected_output" in fields:
            values["expected_output"] = view.get("case.reference.answer")
        for name in ("retrieval_context", "context"):
            if name in fields:
                path = FIELDS[name][0]
                chunks = [c for c in view.get(path) if isinstance(c, str) and c.strip()]
                if not chunks:
                    return None, f"empty:{path}"
                values[name] = chunks
        if "tools_called" in fields:
            attempts = [a for a in parse_tool_events(view.get("execution.tool_events")) if a.name]
            if self.spec.needs_tool_calls and not attempts:
                return None, "no_tool_calls"
            values["tools_called"] = [
                ToolCall(
                    name=attempt.name,
                    input_parameters=attempt.arguments
                    if isinstance(attempt.arguments, dict)
                    else None,
                    output=attempt.result,
                )
                for attempt in attempts
            ]
        if "trace" in fields and not view.get("execution.trace").get("spans"):
            return None, "empty:execution.trace"
        if "expected_tools" in fields:
            names = (view.get("case.reference.tools") or {}).get("tool_names") or []
            if not names:
                return None, "empty:case.reference.tools"
            values["expected_tools"] = [ToolCall(name=name) for name in names]
        return values, None

    def _metric_arguments(self, judge: Any) -> dict[str, Any]:
        arguments = {key: self.params[key] for key in self.spec.params if key in self.params}
        arguments.update(
            threshold=0.5,  # upstream flag only; the harness decides with its own rule
            include_reason=True,
            async_mode=True,
            strict_mode=False,
            verbose_mode=False,
        )
        if judge is not None:
            arguments["model"] = judge
        return arguments

    def _new_metric(self, judge: Any) -> Any:
        import deepeval.metrics

        upstream = getattr(deepeval.metrics, self.spec.upstream)
        accepted = inspect.signature(upstream.__init__).parameters
        arguments = {k: v for k, v in self._metric_arguments(judge).items() if k in accepted}
        return upstream(**arguments)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        from deepeval.test_case import LLMTestCase

        values, not_applicable = self._test_case(view)
        if values is None:
            return EvaluationOutcome.not_applicable(str(not_applicable))
        judge = build_judge(self.params["judge"]) if self.spec.judged else None
        metric = self._new_metric(judge)
        test_case = LLMTestCase(**values)
        if "trace" in self._fields():
            test_case._trace_dict = deepeval_trace(
                view.get("execution.trace"), values["input"], values["actual_output"]
            )
        try:
            score = await metric.a_measure(test_case, _show_indicator=False)
        finally:
            if judge is not None:
                report_judge_usage(ctx, metric, judge)
        bad = out_of_range(score)
        if bad is not None:
            return bad
        if self.spec.name in ("plan_quality", "plan_adherence") and no_plan(metric):
            return EvaluationOutcome.not_applicable("no_plan_in_trace")
        raw: dict[str, Any] = {
            "deepeval_version": PINNED_DEEPEVAL,
            "metric": self.spec.upstream,
            "judge": getattr(metric, "evaluation_model", None),
            "score": metric.score,
            "reason": getattr(metric, "reason", None),
            "upstream_success": metric.success,
            "upstream_threshold": metric.threshold,
            "evaluation_cost": getattr(metric, "evaluation_cost", None),
        }
        for attribute in _TRACE_ATTRIBUTES:
            value = getattr(metric, attribute, None)
            if isinstance(value, (list, tuple)):
                raw[attribute] = _jsonable(value)
        return EvaluationOutcome.ok(
            "scalar",
            float(score),
            evidence=tuple(FIELDS[name][0] for name in self._fields()),
            raw=raw,
        )


def _evaluator_class(spec: Spec) -> type[DeepEvalMetric]:
    name = "".join(part.title() for part in spec.name.split("_"))
    return type(name, (DeepEvalMetric,), {"spec": spec, "manifest": _manifest(spec)})


METRICS: tuple[type[DeepEvalMetric], ...] = tuple(_evaluator_class(spec) for spec in SPECS)


# --------------------------------------------------------------------------- G-Eval

_GEVAL_FIELDS = ("input", "actual_output", "expected_output", "retrieval_context", "context")

GEVAL_SPEC = Spec(
    "g_eval",
    "GEval",
    "a judge scores the answer against criteria you write",
    ("input", "actual_output"),
    ("custom_criteria",),
)


# One G-Eval score from a small judge is not reliable: the same answer, criteria and judge
# scored 0.2 in a run and 1.0 when scored again. Each case is scored `repeats` times and the
# median decides; scores further apart than UNSTABLE_SPREAD are flagged, not trusted.
DEFAULT_REPEATS = 3

# Criteria that speak of the expected answer cannot be judged without it. DeepEval only
# shows the judge the fields named in `evaluation_params`, so criteria that say "the same
# facts as the expected answer" with the default fields (question and answer) made the
# judge reply that the expected answer was missing, and score every case 0.
_MENTIONS_EXPECTED = re.compile(
    r"\b(?:expected|reference|gold|ground[ -]truth)\s+(?:answer|output|response)\b",
    re.IGNORECASE,
)
UNSTABLE_SPREAD = 0.3


class GEval(DeepEvalMetric):
    """G-Eval: the plan supplies the criteria (or explicit steps), which fields the judge
    sees, and optionally a rubric. The criteria are part of the metric's identity. Fields
    beyond input and output are required only when named (`parameter_requirements`)."""

    spec = GEVAL_SPEC
    manifest = EvaluatorManifest(
        **{
            **_manifest(GEVAL_SPEC).model_dump(),
            "version": "1.1.0",  # 1.0.0 scored once; the median of repeats is a new meaning
            "description": (
                f"DeepEval {PINNED_DEEPEVAL} GEval: a judge scores the answer against "
                "criteria the plan states (0 to 1)."
            ),
            "limitations": (
                "Judge-dependent: scores from different judge models are not comparable.",
                (
                    "Criteria wording changes the metric: runs with different criteria are not "
                    "comparable."
                ),
            ),
            # Criteria that speak of the expected answer need it sent to the judge (see
            # `_MENTIONS_EXPECTED`); the harness reads this to send the field to the worker.
            "parameter_patterns": {
                name: {
                    "pattern": _MENTIONS_EXPECTED.pattern,
                    "requires": {"path": FIELDS["expected_output"][0], "non_empty": True},
                    "unless_set": "evaluation_params",
                }
                for name in ("criteria", "evaluation_steps")
            },
            "parameter_requirements": {
                "evaluation_params": {
                    name: {"path": FIELDS[name][0], "non_empty": FIELDS[name][1]}
                    for name in _GEVAL_FIELDS
                    if name not in ("input", "actual_output")
                }
            },
            "parameters_schema": {
                "type": "object",
                "additionalProperties": False,
                "required": ["judge"],
                "anyOf": [{"required": ["criteria"]}, {"required": ["evaluation_steps"]}],
                "properties": {
                    "judge": JUDGE_SCHEMA,
                    "name": {"type": "string", "minLength": 1, "maxLength": 80},
                    "repeats": {"type": "integer", "minimum": 1, "maximum": 9},
                    "criteria": {"type": "string", "minLength": 1},
                    "evaluation_steps": _STRINGS,
                    "evaluation_params": {
                        "type": "array",
                        "minItems": 1,
                        "uniqueItems": True,
                        "items": {"enum": list(_GEVAL_FIELDS)},
                    },
                    "rubric": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["score_range", "expected_outcome"],
                            "properties": {
                                "score_range": {
                                    "type": "array",
                                    "minItems": 2,
                                    "maxItems": 2,
                                    "items": {"type": "integer", "minimum": 0, "maximum": 10},
                                },
                                "expected_outcome": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                },
            },
        }
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        repeats = int(self.params.get("repeats", DEFAULT_REPEATS))
        if repeats == 1:
            return await super().evaluate(view, ctx)
        from deepeval.test_case import LLMTestCase

        values, not_applicable = self._test_case(view)
        if values is None:
            return EvaluationOutcome.not_applicable(str(not_applicable))
        judge = build_judge(self.params["judge"])
        scores: list[float] = []
        reasons: list[str | None] = []
        metric: Any = None
        try:
            for _ in range(repeats):  # one after another: a small judge's rate limit is shared
                metric = self._new_metric(judge)
                score = await metric.a_measure(LLMTestCase(**values), _show_indicator=False)
                bad = out_of_range(score)
                if bad is not None:
                    return bad
                scores.append(float(score))
                reasons.append(getattr(metric, "reason", None))
        finally:
            if metric is not None:
                report_judge_usage(ctx, metric, judge)
        median = statistics.median(scores)
        spread = max(scores) - min(scores)
        unstable = spread > UNSTABLE_SPREAD + 1e-9
        listed = ", ".join(f"{score:.2f}" for score in scores)
        nearest = min(range(repeats), key=lambda i: abs(scores[i] - median))
        raw: dict[str, Any] = {
            "deepeval_version": PINNED_DEEPEVAL,
            "metric": self.spec.upstream,
            "judge": getattr(metric, "evaluation_model", None),
            "scores": scores,
            "median": median,
            "spread": spread,
            "unstable": unstable,
            "reasons": reasons,
        }
        return EvaluationOutcome(
            ExecutionStatus.OK,
            MetricValue(kind="scalar", value=median),
            # "unstable:" is a stable code the report counts; the rest is for the reader.
            (
                f"unstable: the judge's {repeats} scores disagree ({listed}); median {median:.2f}"
                if unstable
                else f"median of {repeats} judge scores ({listed})"
            )
            + (f". Judge: {reasons[nearest]}" if reasons[nearest] else ""),
            tuple(FIELDS[name][0] for name in self._fields()),
            raw,
        )

    def _fields(self) -> tuple[str, ...]:
        chosen = self.params.get("evaluation_params")
        if not chosen:
            chosen = ["input", "actual_output"]
            written = " ".join(
                [self.params.get("criteria") or "", *(self.params.get("evaluation_steps") or [])]
            )
            if _MENTIONS_EXPECTED.search(written):
                chosen.append("expected_output")
        return tuple(name for name in _GEVAL_FIELDS if name in chosen)

    def _new_metric(self, judge: Any) -> Any:
        from deepeval.metrics import GEval as UpstreamGEval
        from deepeval.metrics.g_eval import Rubric
        from deepeval.test_case import SingleTurnParams

        rubric = [
            Rubric(
                score_range=tuple(item["score_range"]), expected_outcome=item["expected_outcome"]
            )
            for item in self.params.get("rubric") or []
        ]
        return UpstreamGEval(
            # `name` is a label only; the criteria are what the judge reads.
            name=self.params.get("name") or "custom criteria",
            evaluation_params=[SingleTurnParams(name) for name in self._fields()],
            criteria=self.params.get("criteria"),
            evaluation_steps=self.params.get("evaluation_steps"),
            rubric=rubric or None,
            model=judge,
            threshold=0.5,
            async_mode=True,
            strict_mode=False,
            verbose_mode=False,
        )
