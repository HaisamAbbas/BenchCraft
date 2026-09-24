"""``ragas.faithfulness``: the pinned Ragas text faithfulness metric.

The adapter deliberately keeps the Ragas API translation in this worker-only
package.  Ragas 0.4.3 is pinned because the collection metric and its structured
response models are versioned API surface, not interchangeable implementation
details.

The mapping is deliberately narrow:

* ``case.input`` becomes ``user_input`` (non-text Golden input is encoded as
  JSON text);
* the recorded text output becomes ``response``; and
* observed ``execution.retrieved_context`` text chunks become
  ``retrieved_contexts``.

``case.reference.context`` is never read.  It is judge-only Golden data and is
not evidence of what the application retrieved.  Blank context chunks are
removed; an empty observed context is not scored.

Ragas' modern ``Faithfulness`` returns ``float('nan')`` when its statement
generator produces no statements.  That is an unavailable measurement, not a
quality score: this adapter maps every non-finite value to ``not_applicable``
and never converts it to 0 or 1.  The upstream result's score, reason, traces,
and Ragas version are retained in the raw artifact using a JSON-safe
representation of non-finite values.

A fresh judge and a fresh metric are constructed for every case.  The adapter
never retries a judge call and never runs a second application execution.  The
harness owns the decision rule; Ragas' score is only the canonical value
produced by the metric.

Security note: Ragas 0.4.3 is affected by GHSA-95ww-475f-pr4f /
CVE-2026-6587 in multi-modal URL/file processing.  This adapter exposes only the
text metric, validates text context before calling it, and never selects or
calls the multi-modal metric.  The pinned ``ragas.metrics.collections`` package
may load module definitions as an import side effect, but no URL/file processing
entry point is reachable through this adapter.  The package is still installed
as an executable third-party dependency in an isolated worker; this is a
compensating exposure control, not a claim that the dependency is
vulnerability-free.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import json
import math
import os
from collections.abc import Mapping
from numbers import Real
from typing import Any

from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    ExecutionStatus,
    FieldRequirement,
    MetricDirection,
)
from aibench.evaluators.protocol import (
    MISSING,
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)

# This assignment is intentionally at module import time, before any lazy Ragas
# import below.  The worker has a minimal environment; force the safe value even
# if a caller supplied a false value in its parent environment.
os.environ["RAGAS_DO_NOT_TRACK"] = "true"

from aibench_ragas._version import __version__

PINNED_RAGAS = "0.4.3"
_EVIDENCE = ("case.input", "execution.output", "execution.retrieved_context")


class _Unscorable(ValueError):
    """Internal validation error carrying a stable canonical reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _require_pinned_ragas() -> None:
    """Refuse an environment whose Ragas API has not been verified."""

    try:
        installed = importlib.metadata.version("ragas")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("ragas is not installed in this plugin environment") from exc
    if installed != PINNED_RAGAS:
        raise RuntimeError(
            f"ragas {installed} is installed but this adapter is pinned to "
            f"{PINNED_RAGAS}; its field mapping has not been verified for other versions"
        )


def _case_input_text(value: Any) -> str:
    """Map a Golden input to the text-only Ragas ``user_input`` argument."""

    if value is MISSING:
        raise _Unscorable("missing:case.input")
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise _Unscorable(f"unscorable_input:{type(value).__name__}") from exc


def _text_context(value: Any) -> list[str]:
    """Validate observed context and drop only blank chunks."""

    if value is MISSING:
        raise _Unscorable("missing:execution.retrieved_context")
    if not isinstance(value, (list, tuple)):
        raise _Unscorable(f"unscorable_context:{type(value).__name__}")
    if any(not isinstance(chunk, str) for chunk in value):
        # Do not stringify arbitrary application data.  The metric is explicitly
        # text-only and a non-text chunk is not silently invented evidence.
        kinds = sorted({type(chunk).__name__ for chunk in value if not isinstance(chunk, str)})
        raise _Unscorable(f"unscorable_context:{','.join(kinds)}")
    return [chunk for chunk in value if chunk.strip()]


def build_inputs(view: EvaluationView) -> dict[str, Any]:
    """Return the exact three arguments passed to Ragas ``Faithfulness.ascore``.

    This helper is intentionally small and has no side effects.  It is useful
    for contract tests and makes it obvious that reference context is not part
    of the mapping.  Evaluation performs the additional empty/non-text policy
    checks before calling the metric.
    """

    user_input = _case_input_text(view.get("case.input"))
    response = view.get("execution.output")
    contexts = _text_context(view.get("execution.retrieved_context"))
    return {
        "user_input": user_input,
        "response": response,
        "retrieved_contexts": contexts,
    }


# A descriptive alias for callers that prefer the metric-oriented name.  Keep
# both names small and side-effect free rather than exposing Ragas objects.
build_metric_inputs = build_inputs


def _finite_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise TypeError(f"Ragas faithfulness returned a non-numeric value: {value!r}")
    score = float(value)
    return score if math.isfinite(score) else None


def _safe_json(value: Any) -> Any:
    """Make upstream Ragas metadata JSON-safe without changing finite values."""

    if value is MISSING:
        return None
    if isinstance(value, Real) and not isinstance(value, bool):
        numeric = float(value)
        if not math.isfinite(numeric):
            # JSON has no NaN/Infinity literal.  Keep the exact spelling as a
            # string while the adapter's canonical value remains not_applicable.
            return repr(numeric)
        return numeric
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    if isinstance(value, Mapping):
        return {str(key): _safe_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_json(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_safe_json(item) for item in sorted(value, key=repr)]
    # Pydantic models are used by Ragas traces/structured output.  Avoid a
    # hard dependency on a particular Pydantic release here.
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _safe_json(model_dump(mode="json"))
        except TypeError:
            try:
                return _safe_json(model_dump())
            except Exception:  # noqa: BLE001 - third-party metadata is best effort
                return repr(value)
        except Exception:  # noqa: BLE001 - third-party metadata is best effort
            return repr(value)
    return repr(value)


def _result_value(result: Any) -> Any:
    return getattr(result, "value", None)


def _raw_result(result: Any, judge_config: Mapping[str, Any]) -> dict[str, Any]:
    """Capture upstream score/reason/traces and the pinned implementation."""

    value = _result_value(result)
    finite = _finite_score(value)
    safe_value = _safe_json(value)
    value_repr = (
        repr(float(value))
        if isinstance(value, Real) and not isinstance(value, bool)
        else repr(value)
    )
    safe_reason = _safe_json(getattr(result, "reason", None))
    safe_traces = _safe_json(getattr(result, "traces", None))
    judge = dict(judge_config)
    kind = judge.get("kind")
    if kind == "python_factory":
        judge_label = f"python_factory:{judge.get('factory')}"
    else:
        judge_label = f"{kind}:{judge.get('provider', 'openai')}:{judge.get('model')}"
    return {
        "ragas_version": PINNED_RAGAS,
        "score": finite,
        "value_repr": value_repr,
        "upstream_value": safe_value,
        "reason": safe_reason,
        "traces": safe_traces,
        "judge": judge_label,
        "judge_config": _safe_json(judge),
        "upstream": {
            "value": safe_value,
            "reason": safe_reason,
            "traces": safe_traces,
        },
    }


def _factory_callable(spec: str) -> Any:
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("python_factory must look like 'module:function'")
    module = importlib.import_module(module_name)
    factory = getattr(module, attribute)
    if not callable(factory):
        raise TypeError(f"{spec} is not callable")
    return factory


def _load_ragas_contract() -> tuple[type[Any], type[Any]]:
    """Import and sanity-check the exact pinned Ragas API off the event loop."""

    # Keep this literal import in the worker.  Ragas 0.4.3's collection package
    # eagerly imports its metric catalogue and is comparatively expensive on
    # cold Windows workers; prepare() starts this check in a helper thread.
    from ragas.llms.base import InstructorBaseRagasLLM
    from ragas.metrics.collections import Faithfulness

    if not hasattr(Faithfulness, "ascore"):
        raise RuntimeError("pinned Ragas Faithfulness has no ascore API")
    if not hasattr(InstructorBaseRagasLLM, "agenerate"):
        raise RuntimeError("pinned Ragas InstructorBaseRagasLLM has no agenerate API")
    return Faithfulness, InstructorBaseRagasLLM


class Faithfulness(Evaluator):
    """Ragas 0.4.3 text Faithfulness over recorded executions."""

    manifest = EvaluatorManifest(
        evaluator_id="ragas.faithfulness",
        version="1.0.0",
        plugin_id="aibench-ragas",
        plugin_version=__version__,
        package_name="ragas",
        package_version=PINNED_RAGAS,
        description=(
            f"Ragas {PINNED_RAGAS} text Faithfulness: the share of extracted answer "
            "statements supported by the context the application actually retrieved."
        ),
        limitations=(
            (
                "Text-only: case input is encoded as text, output and retrieved context must be text; "
                "image, audio, URL and file inputs are unsupported."
            ),
            (
                "Not numerically equivalent to DeepEval faithfulness; each framework's score, judge, "
                "raw result and decision semantics are preserved."
            ),
            "Requires observed retrieved context; the Golden's reference context is never used.",
            "A non-finite/no-statements Ragas result is not_applicable, never 0 or 1.",
            "Judge-dependent: scores from different judge models are not comparable.",
        ),
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=(
            FieldRequirement(path="case.input"),
            # An empty answer is still an observed application output; the
            # evaluator records the deliberate not_applicable policy.
            FieldRequirement(path="execution.output", non_empty=False),
            # Missing/empty retrieval is rejected by the harness before the
            # worker is called; blank-only chunks are handled below.
            FieldRequirement(path="execution.retrieved_context"),
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
                            "required": ["kind", "factory"],
                            "properties": {
                                "kind": {"const": "python_factory"},
                                "factory": {
                                    "type": "string",
                                    "pattern": r"^[A-Za-z_][\w.]*:[A-Za-z_]\w*$",
                                },
                            },
                        },
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["kind", "model"],
                            "properties": {
                                "kind": {"const": "llm_factory"},
                                "model": {"type": "string", "minLength": 1},
                                "provider": {"const": "openai"},
                            },
                        },
                    ]
                }
            },
        },
        consumes="recorded_outputs",
        uses_models=True,
        credentials=(
            (
                "judge credentials are read only from the isolated worker environment "
                "(the built-in OpenAI path uses OPENAI_API_KEY)"
            ),
        ),
        network_destinations=(
            "the configured OpenAI-compatible judge API for llm_factory; none for a local python_factory judge",
        ),
        internal_retries=0,
        internal_concurrency=1,
        requires_worker=True,
    )

    async def _preload_ragas_contract(self) -> None:
        await asyncio.to_thread(_load_ragas_contract)

    async def _preload_ragas(self) -> None:
        await self._preload_ragas_contract()
        judge = self.params.get("judge")
        if isinstance(judge, Mapping) and judge.get("kind") == "python_factory":
            # Validate the factory and its return type off the event loop too.
            # The instance is discarded; evaluate constructs another per case.
            await asyncio.to_thread(self._new_judge)

    async def _ensure_ragas(self) -> None:
        task = getattr(self, "_ragas_preload", None)
        if task is None:
            await asyncio.to_thread(_load_ragas_contract)
            return
        await task

    async def prepare(self, params: Any) -> None:
        # Keep this assignment before the first Ragas import, including the
        # version/manifest import performed by the worker.
        os.environ["RAGAS_DO_NOT_TRACK"] = "true"
        self.params = dict(params)
        _require_pinned_ragas()

        judge = self.params.get("judge")
        if not isinstance(judge, Mapping):
            raise TypeError("judge configuration is required")
        if judge.get("kind") == "python_factory":
            # Complete the cold Ragas import in the bounded prepare phase. The
            # first case must not pay the catalogue-import cost under its own
            # per-case timeout.
            self._ragas_preload = asyncio.create_task(self._preload_ragas())
        elif judge.get("kind") == "llm_factory":
            if judge.get("provider", "openai") != "openai":
                raise ValueError("the built-in llm_factory path currently supports provider=openai")
            # Do not construct a provider client here: a missing credential is
            # an evaluation-time error, and constructing a client in prepare
            # would make an unscored binding require paid-provider setup. The
            # Ragas import is still completed during the bounded prepare phase.
            self._ragas_preload = asyncio.create_task(self._preload_ragas_contract())
        else:
            raise ValueError(f"unsupported judge kind: {judge.get('kind')!r}")
        await self._ragas_preload

    def _new_judge(self) -> Any:
        """Construct and validate one fresh Ragas Instructor judge."""

        # The environment assignment is repeated for direct/in-process contract
        # use as well as the normal worker lifecycle.
        os.environ["RAGAS_DO_NOT_TRACK"] = "true"
        judge = self.params.get("judge")
        if not isinstance(judge, Mapping):
            raise TypeError("judge configuration is required")
        kind = judge.get("kind")
        if kind == "python_factory":
            instance = _factory_callable(str(judge["factory"]))()
        elif kind == "llm_factory":
            # Ragas 0.4.3 requires a client.  Construct it from the worker's
            # environment only; metric parameters never carry credentials.
            from openai import AsyncOpenAI
            from ragas.llms import llm_factory

            # Disable both the OpenAI transport retry and Instructor retries;
            # the harness owns retry policy and evaluation attempts.
            client = AsyncOpenAI(max_retries=0)
            instance = llm_factory(
                model=str(judge["model"]),
                provider=str(judge.get("provider", "openai")),
                client=client,
                adapter="instructor",
                max_retries=0,
            )
        else:
            raise ValueError(f"unsupported judge kind: {kind!r}")

        from ragas.llms.base import InstructorBaseRagasLLM

        if not isinstance(instance, InstructorBaseRagasLLM):
            raise TypeError(
                f"{judge.get('factory', judge.get('model'))} did not return an "
                "InstructorBaseRagasLLM"
            )
        return instance

    @staticmethod
    def _new_metric(llm: Any) -> Any:
        """Construct the pinned Ragas metric for one case."""

        from ragas.metrics.collections import Faithfulness

        return Faithfulness(llm=llm)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        """Evaluate one stored execution without invoking the application."""

        del ctx  # Ragas 0.4.3 exposes no reliable usage/cost interface here.

        try:
            inputs = build_inputs(view)
        except _Unscorable as exc:
            return EvaluationOutcome.not_applicable(exc.reason)

        response = inputs["response"]
        if not isinstance(response, str):
            return EvaluationOutcome.not_applicable(f"unscorable_output:{type(response).__name__}")
        if not response.strip():
            return EvaluationOutcome.not_applicable("unscorable_output:blank")
        if not inputs["retrieved_contexts"]:
            return EvaluationOutcome.not_applicable("empty:execution.retrieved_context")
        if not inputs["user_input"].strip():
            return EvaluationOutcome.not_applicable("unscorable_input:blank")

        await self._ensure_ragas()
        judge = self._new_judge()
        metric = self._new_metric(judge)
        result = await metric.ascore(
            user_input=inputs["user_input"],
            response=response,
            retrieved_contexts=inputs["retrieved_contexts"],
        )
        value = _result_value(result)
        score = _finite_score(value)
        raw = _raw_result(result, self.params.get("judge", {}))
        if score is None:
            # Ragas uses NaN for an empty statement set.  It is not evidence of
            # an unfaithful answer and must never become a numeric sentinel.
            return EvaluationOutcome(
                status=ExecutionStatus.NOT_APPLICABLE,
                reason="no_statements",
                evidence=_EVIDENCE,
                raw=raw,
            )
        return EvaluationOutcome.ok(
            "scalar",
            score,
            evidence=_EVIDENCE,
            raw=raw,
        )
