"""Recorded-output replay of allowlisted openai/evals evals (§11A, 17-T1).

Each case is one upstream sample: `{"input": case.input, "ideal": case.reference.answer}`.
The upstream eval runs unchanged and asks its completion function for an answer; the
replay completion function answers only when the eval's request is exactly the input
the application was given, and only once. A request that differs (a few-shot expansion,
a rewritten prompt) or a second request (a follow-up) fails clearly: one recorded answer
cannot honestly stand in for a different question.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, ClassVar

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench_openai_evals_oss import upstream
from aibench_openai_evals_oss._version import __version__

PLUGIN_ID = "aibench-openai-evals-oss"
_EVIDENCE = ("case.input", "execution.output", "case.reference.answer")

_FEW_SHOT = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["sample"],
        "properties": {"sample": {"type": "array"}},
    },
}
_PARAMS: dict[str, dict[str, Any]] = {
    "match": {
        "num_few_shot": {"type": "integer", "minimum": 0},
        "few_shot": _FEW_SHOT,
    },
    "includes": {"ignore_case": {"type": "boolean"}},
    "fuzzy_match": {},
    "json_match": {},
}
_DESCRIPTIONS = {
    "match": "the output starts with a reference answer",
    "includes": "the output contains a reference answer",
    "fuzzy_match": "the output and a reference answer contain each other after normalization",
    "json_match": "the output parses as JSON equal to a reference answer",
}


def replay_answer(recorded_input: Any, output: str) -> Any:
    """The replay completion function's answer rule (exact request, once)."""
    recorded = upstream.canonical(recorded_input)

    def answer(prompt: Any, index: int) -> str:
        if index > 0:
            raise upstream.RequestRefused(
                "unsupported_follow_up",
                f"the eval made request {index + 1} for one sample; replay has one recorded "
                "answer per case and cannot answer follow-up requests",
            )
        if upstream.canonical(prompt) != recorded:
            raise upstream.RequestRefused(
                "unsupported_dynamic_request",
                "the eval requested a prompt that differs from the input the application "
                "was given (for example a few-shot expansion); replay needs an exact match",
            )
        return output

    return answer


class _Replay(Evaluator):
    eval_type: ClassVar[str]

    async def prepare(self, params: Mapping[str, Any]) -> None:
        self.params = dict(params)
        upstream.require_pinned_evals()
        await asyncio.to_thread(upstream._eval_class, self.eval_type)  # warm the import

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        output = view.get("execution.output")
        ideal = view.get("case.reference.answer")
        if not isinstance(output, str):
            return EvaluationOutcome.error("unsupported_output: the recorded output is not text")
        if not (isinstance(ideal, str) or (isinstance(ideal, list) and ideal)):
            return EvaluationOutcome.not_applicable("unsupported_reference: not text or a list")
        sample = {"input": view.get("case.input"), "ideal": ideal}
        run = await asyncio.to_thread(
            upstream.run_sample,
            self.eval_type,
            self.params,
            sample,
            replay_answer(sample["input"], output),
        )
        raw = {
            "eval_type": self.eval_type,
            "upstream": f"evals=={upstream.PINNED_EVALS}",
            "mode": "recorded_replay",
            "events": run.events,
            "requests": len(run.requests),
        }
        if run.error is not None:
            return EvaluationOutcome.error(run.error, raw=raw)
        if run.correct is None:
            return EvaluationOutcome.error("upstream_events: expected one match event", raw=raw)
        return EvaluationOutcome.ok("boolean", run.correct, evidence=_EVIDENCE, raw=raw)


def _replay_class(eval_type: str) -> type[_Replay]:
    upstream_class = upstream.ALLOWLIST[eval_type][0]
    manifest = EvaluatorManifest(
        evaluator_id=f"openai_evals_oss.{eval_type}",
        version="1.0.0",
        plugin_id=PLUGIN_ID,
        plugin_version=__version__,
        package_name="evals",
        package_version=upstream.PINNED_EVALS,
        description=(
            f"openai/evals {upstream.PINNED_EVALS} `{upstream_class}` on the recorded output: "
            f"{_DESCRIPTIONS[eval_type]}."
        ),
        limitations=(
            (
                "Recorded replay: the eval's request must equal the application input "
                "exactly, and only one request per sample is answered; other requests fail "
                "as unsupported_dynamic_request or unsupported_follow_up."
            ),
            (
                "Only the allowlisted basic evals (match, includes, fuzzy_match, json_match) "
                "are supported; model-graded, solver and multi-turn evals are refused."
            ),
            "Scores are the upstream eval's own match verdict, not a harness reimplementation.",
            (
                "The request is compared with the case input; an application whose "
                "input_binding sends only part of the input is not seen by the evaluator, so "
                "replay such runs only when the whole input reached the application."
            ),
        ),
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(
            FieldRequirement(path="case.input"),
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="case.reference.answer"),
        ),
        default_rule=DecisionRule(comparator="is_true"),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": _PARAMS[eval_type],
        },
        consumes="recorded_outputs",
        uses_models=False,
        credentials=(),
        network_destinations=(),
        internal_retries=0,
        internal_concurrency=1,
        requires_worker=True,
    )
    return type(
        f"Replay{eval_type.title().replace('_', '')}",
        (_Replay,),
        {"eval_type": eval_type, "manifest": manifest},
    )


REPLAY_EVALUATORS = tuple(_replay_class(t) for t in upstream.ALLOWLIST)
