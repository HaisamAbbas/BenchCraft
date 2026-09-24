"""`deepeval.faithfulness`: DeepEval's FaithfulnessMetric over recorded executions (§10).

Field translation (checked against DeepEval 4.2.5's `LLMTestCase`):

    case.input                        -> LLMTestCase.input (JSON text if not a string)
    execution.output (text)           -> LLMTestCase.actual_output
    execution.retrieved_context[]     -> LLMTestCase.retrieval_context

`case.reference.context` is never used: a Golden's reference context is not what the
application retrieved, and substituting it would invent a score.

Policies:
- retrieval not observed      -> not_applicable, reason `missing:execution.retrieved_context`
                                 (the harness applies this before the metric runs)
- retrieval observed but empty -> not_applicable, reason `empty:execution.retrieved_context`
                                 (`empty_context_policy: not_applicable`, the only policy
                                 implemented; another needs an explicit rubric decision)
- output empty or not text    -> not_applicable, reason `unscorable_output:<kind>`
- judge extracts no claims    -> not_applicable, reason `no_claims` (upstream would score a
                                 vacuous 1.0: nothing was claimed, so nothing was grounded)
Answer quality is for correctness metrics to judge; the coverage denominator shows the loss.
Blank retrieved chunks are dropped; if nothing remains the context counts as empty.

Execution: a new FaithfulnessMetric (and a new judge instance) per case, so no mutable
metric state is ever shared between cases or tasks. DeepEval's own retries are disabled
(the harness owns retry policy, §15), its telemetry, .env loading and legacy key file are
turned off, and nothing is published anywhere. The upstream `success` flag is kept as
metadata only; the harness applies its own frozen rule. Unknown judge cost stays unknown.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
from typing import Any

from aibench_deepeval._version import __version__

from aibench.core.models import DecisionRule, EvaluatorManifest, FieldRequirement, MetricDirection
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)

PINNED_DEEPEVAL = "4.2.5"

# Set before DeepEval is imported. The worker environment is already an allow-list, so these
# are the only DeepEval settings present.
DEEPEVAL_ENVIRONMENT = {
    "DEEPEVAL_TELEMETRY_OPT_OUT": "1",  # no PostHog telemetry
    "DEEPEVAL_DISABLE_DOTENV": "1",  # never read .env files from the working directory
    "DEEPEVAL_DISABLE_LEGACY_KEYFILE": "1",  # never read ~/.deepeval keys (e.g. Confident AI)
    "DEEPEVAL_RETRY_MAX_ATTEMPTS": "1",  # no nested retries under the harness
}

_EVIDENCE = ("case.input", "execution.output", "execution.retrieved_context")


def _require_pinned_deepeval() -> None:
    try:
        installed = importlib.metadata.version("deepeval")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("deepeval is not installed in this plugin environment") from exc
    if installed != PINNED_DEEPEVAL:
        raise RuntimeError(
            f"deepeval {installed} is installed but this adapter is pinned to "
            f"{PINNED_DEEPEVAL}; its field mapping has not been verified for other versions"
        )


def build_test_case(view: EvaluationView) -> Any:
    """The real DeepEval `LLMTestCase` for one recorded execution (blank chunks dropped)."""
    from deepeval.test_case import LLMTestCase

    case_input = view.get("case.input")
    return LLMTestCase(
        input=case_input
        if isinstance(case_input, str)
        else json.dumps(case_input, ensure_ascii=False),
        actual_output=view.get("execution.output"),
        retrieval_context=[c for c in view.get("execution.retrieved_context") if c.strip()],
    )


class Faithfulness(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="deepeval.faithfulness",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        description=(
            f"DeepEval {PINNED_DEEPEVAL} FaithfulnessMetric: the share of the answer's claims "
            "supported by the context the application actually retrieved."
        ),
        limitations=(
            "Judge-dependent: scores from different judge models are not comparable.",
            "Needs observed retrieval; never uses the Golden's reference context.",
            "Not numerically equivalent to Ragas faithfulness.",
            "An answer with no extractable claims is not applicable (upstream would give 1.0).",
        ),
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=(
            FieldRequirement(path="case.input"),
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="execution.retrieved_context"),  # empty -> not_applicable
        ),
        default_rule=DecisionRule(comparator=">=", threshold=0.5),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["judge"],
            "properties": {
                "judge": {
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
                            "required": ["kind", "factory"],
                            "properties": {
                                "kind": {"const": "python_factory"},
                                "factory": {
                                    "type": "string",
                                    "pattern": r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$",
                                },
                            },
                        },
                    ]
                },
                "truths_extraction_limit": {"type": "integer", "minimum": 1},
                "penalize_ambiguous_claims": {"type": "boolean"},
                "empty_context_policy": {"enum": ["not_applicable"]},
            },
        },
        uses_models=True,
        credentials=("the judge provider's key, passed explicitly to the plugin environment",),
        network_destinations=(
            "the configured judge model's API (none for a local python_factory judge)",
        ),
        internal_retries=0,
        internal_concurrency=2,  # async_mode extracts truths and claims concurrently
        requires_worker=True,
    )

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        os.environ.update(DEEPEVAL_ENVIRONMENT)
        _require_pinned_deepeval()
        import deepeval.metrics  # noqa: F401 - import once up front so failures surface here

        self._new_judge()  # validate the judge configuration before any case runs

    def _new_judge(self) -> Any:
        judge = self.params["judge"]
        if judge["kind"] == "deepeval_model":
            return judge["model"]  # resolved by DeepEval's native model support
        from deepeval.models import DeepEvalBaseLLM

        module_name, _, attribute = judge["factory"].partition(":")
        instance = getattr(importlib.import_module(module_name), attribute)()
        if not isinstance(instance, DeepEvalBaseLLM):
            raise TypeError(f"{judge['factory']} did not return a DeepEvalBaseLLM")
        return instance

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        from deepeval.metrics import FaithfulnessMetric

        output = view.get("execution.output")
        if not isinstance(output, str):
            return EvaluationOutcome.not_applicable(f"unscorable_output:{type(output).__name__}")
        if not output.strip():
            return EvaluationOutcome.not_applicable("unscorable_output:blank")
        if not [chunk for chunk in view.get("execution.retrieved_context") if chunk.strip()]:
            return EvaluationOutcome.not_applicable("empty:execution.retrieved_context")

        metric = FaithfulnessMetric(
            threshold=0.5,  # upstream flag only; the harness decides with its own rule
            model=self._new_judge(),
            include_reason=True,
            async_mode=True,
            strict_mode=False,
            verbose_mode=False,
            truths_extraction_limit=self.params.get("truths_extraction_limit"),
            penalize_ambiguous_claims=self.params.get("penalize_ambiguous_claims", False),
        )
        score = await metric.a_measure(build_test_case(view), _show_indicator=False)

        cost = metric.evaluation_cost  # None for custom judges: unknown, not zero
        tokens = {
            k: v
            for k, v in (("input", metric.input_tokens), ("output", metric.output_tokens))
            if isinstance(v, int)
        }
        if cost is not None or tokens:
            # Native judges: report what DeepEval measured; the call count is not exposed.
            ctx.report_usage(
                provider=metric.evaluation_model,
                calls=None,
                tokens=tokens,
                cost=None if cost is None else float(cost),
            )
        if not metric.claims:
            return EvaluationOutcome.not_applicable("no_claims")
        return EvaluationOutcome.ok(
            "scalar",
            float(score),
            evidence=_EVIDENCE,
            raw={
                "deepeval_version": PINNED_DEEPEVAL,
                "judge": metric.evaluation_model,
                "score": metric.score,
                "reason": metric.reason,
                "upstream_success": metric.success,
                "upstream_threshold": metric.threshold,
                "truths": list(metric.truths),
                "claims": list(metric.claims),
                "verdicts": [v.model_dump(mode="json") for v in metric.verdicts],
                "evaluation_cost": cost,
            },
        )
