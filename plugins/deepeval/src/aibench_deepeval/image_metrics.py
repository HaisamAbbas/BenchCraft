"""DeepEval's five image-specific metrics over bounded inline image evidence."""

from __future__ import annotations

import base64
import inspect
import os
import re
from dataclasses import dataclass
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
from aibench_deepeval._version import __version__
from aibench_deepeval.faithfulness import (
    DEEPEVAL_ENVIRONMENT,
    PINNED_DEEPEVAL,
    _require_pinned_deepeval,
)
from aibench_deepeval.judges import (
    openai_compatible_multimodal_judge,
    report_judge_usage,
)
from aibench_deepeval.metrics import _jsonable, out_of_range
from aibench_deepeval.vision import (
    MAX_TEXT_CHARS,
    VISION_JUDGE_SCHEMA,
    checked_text,
    decode_inline_image,
    validate_base_url,
)

MAX_IMAGE_EVIDENCE_BYTES = 4 * 1024 * 1024
MAX_CONTEXT_CHARS = 20_000
_RESERVED_IMAGE = re.compile(r"\[DEEPEVAL:(?:IMAGE|PDF):")


@dataclass(frozen=True)
class ImageMetricSpec:
    name: str
    upstream: str
    summary: str
    concepts: tuple[str, ...]
    requires_source_image: bool = False
    max_context_size: bool = False
    parallel_judge_calls: int = 1


SPECS = (
    ImageMetricSpec(
        "image_coherence",
        "ImageCoherenceMetric",
        "whether generated images cohere with their surrounding answer text",
        ("image_coherence",),
        max_context_size=True,
    ),
    ImageMetricSpec(
        "image_helpfulness",
        "ImageHelpfulnessMetric",
        "whether generated images add useful information to their surrounding answer text",
        ("image_helpfulness",),
        max_context_size=True,
    ),
    ImageMetricSpec(
        "image_reference",
        "ImageReferenceMetric",
        "whether surrounding answer text accurately refers to each generated image",
        ("image_reference",),
        max_context_size=True,
    ),
    ImageMetricSpec(
        "text_to_image",
        "TextToImageMetric",
        "semantic consistency and perceptual quality of a generated image",
        ("image_generation", "image_quality"),
        parallel_judge_calls=2,
    ),
    ImageMetricSpec(
        "image_editing",
        "ImageEditingMetric",
        "semantic consistency and perceptual quality of an edited image",
        ("image_editing", "image_quality"),
        requires_source_image=True,
        parallel_judge_calls=2,
    ),
)


def _manifest(spec: ImageMetricSpec) -> EvaluatorManifest:
    properties: dict[str, Any] = {"judge": VISION_JUDGE_SCHEMA}
    if spec.max_context_size:
        properties["max_context_size"] = {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_CONTEXT_CHARS,
        }
    return EvaluatorManifest(
        evaluator_id=f"deepeval.{spec.name}",
        version="1.0.0",
        plugin_id="aibench-deepeval",
        plugin_version=__version__,
        package_name="deepeval",
        package_version=PINNED_DEEPEVAL,
        description=f"DeepEval {PINNED_DEEPEVAL} {spec.upstream}: {spec.summary}.",
        limitations=(
            "Requires one generated inline PNG, JPEG, or WebP image; image bytes are not fetched from URLs or filesystem paths.",
            "The configured judge must accept OpenAI-compatible image_url content; image and prompt data are sent to that endpoint.",
            "Scores depend on the configured judge and its vision capability; results from different models are not directly comparable.",
            "Image evidence is limited to 4 MiB decoded per case and one output image; ImageEditingMetric also requires one case input image.",
        ),
        concepts=spec.concepts,
        value_kind="scalar",
        direction=MetricDirection.HIGHER,
        aggregation="mean",
        requires=(
            FieldRequirement(path="case.input"),
            FieldRequirement(path="execution.output", non_empty=False),
        ),
        default_rule=DecisionRule(comparator=">=", threshold=0.5),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["judge"],
            "properties": properties,
        },
        uses_models=True,
        credentials=("the vision judge API key, passed explicitly to the plugin environment",),
        network_destinations=("the configured OpenAI-compatible vision judge endpoint",),
        # Each request may retry four times inside the judge adapter; text-to-image and
        # image-editing issue two parallel judge requests.
        internal_retries=4 * spec.parallel_judge_calls,
        internal_concurrency=spec.parallel_judge_calls,
        requires_worker=True,
    )


def _image(value: tuple[str, bytes], created: list[Any]) -> Any:
    from deepeval.test_case import MLLMImage

    mime_type, data = value
    image = MLLMImage(dataBase64=base64.b64encode(data).decode("ascii"), mimeType=mime_type)
    created.append(image)
    return image


def _task_input(value: Any, *, requires_source_image: bool) -> tuple[str, tuple[str, bytes] | None]:
    if isinstance(value, str):
        prompt = checked_text(value, "task prompt")
        if requires_source_image:
            raise ValueError("missing:case.input.image")
        return _safe_prompt(prompt), None
    if not isinstance(value, dict) or set(value) - {"prompt", "image"}:
        raise ValueError("invalid:case_input_shape")
    prompt = _safe_prompt(checked_text(value.get("prompt"), "task prompt"))
    raw_image = value.get("image")
    if raw_image is None:
        if requires_source_image:
            raise ValueError("missing:case.input.image")
        return prompt, None
    if not requires_source_image:
        raise ValueError("unsupported:case.input.image_for_metric")
    image = decode_inline_image(raw_image, "case.input.image")
    return prompt, image


def _safe_prompt(value: str) -> str:
    if _RESERVED_IMAGE.search(value):
        raise ValueError("invalid:reserved_deepeval_image_marker")
    return value


def _output(value: Any) -> tuple[str, tuple[str, bytes]]:
    """Return the optional caption and the one required inline generated image."""
    caption = ""
    if isinstance(value, dict) and set(value) == {"image", "text"}:
        caption_value = value["text"]
        if not isinstance(caption_value, str) or len(caption_value) > MAX_TEXT_CHARS:
            raise ValueError("invalid:execution_output_text")
        if _RESERVED_IMAGE.search(caption_value):
            raise ValueError("invalid:reserved_deepeval_image_marker")
        caption = caption_value.strip()
        image_value = value["image"]
    else:
        image_value = value
    return caption, decode_inline_image(image_value, "execution.output.image")


class DeepEvalImageMetric(Evaluator):
    """One pinned DeepEval image metric with a worker-local multimodal judge."""

    spec: ClassVar[ImageMetricSpec]

    async def prepare(self, params: Any) -> None:
        self.params = dict(params)
        os.environ.update(DEEPEVAL_ENVIRONMENT)
        _require_pinned_deepeval()
        import deepeval.metrics  # noqa: F401 - surface incompatible installs during prepare

        judge = self._judge_config()
        validate_base_url(judge.get("base_url"))
        openai_compatible_multimodal_judge(judge)  # validate the explicitly named secret

    def _judge_config(self) -> dict[str, Any]:
        judge = dict(self.params["judge"])
        judge.setdefault("max_output_tokens", 1024)
        judge.setdefault("max_concurrent_calls", 2)
        return judge

    def _new_metric(self, judge: Any) -> Any:
        import deepeval.metrics

        upstream = getattr(deepeval.metrics, self.spec.upstream)
        supported = inspect.signature(upstream.__init__).parameters
        arguments: dict[str, Any] = {
            "model": judge,
            "threshold": 0.5,
            "async_mode": True,
            "strict_mode": False,
            "verbose_mode": False,
        }
        if self.spec.max_context_size:
            arguments["max_context_size"] = self.params.get("max_context_size", 4000)
        return upstream(**{key: value for key, value in arguments.items() if key in supported})

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        from deepeval.test_case import LLMTestCase
        from deepeval.test_case.llm_test_case import _MLLM_IMAGE_REGISTRY

        try:
            prompt, input_image = _task_input(
                view.get("case.input"), requires_source_image=self.spec.requires_source_image
            )
            caption, output_image = _output(view.get("execution.output"))
        except (TypeError, ValueError) as exc:
            return EvaluationOutcome.not_applicable(str(exc))

        images = [size for size in (input_image, output_image) if size is not None]
        if sum(len(data) for _, data in images) > MAX_IMAGE_EVIDENCE_BYTES:
            return EvaluationOutcome.not_applicable("image_evidence_too_large")

        created: list[Any] = []
        judge = None
        metric = None
        try:
            input_text = prompt
            if input_image is not None:
                input_text = f"{prompt}\n{_image(input_image, created)}"
            output_marker = str(_image(output_image, created))
            actual_output = f"{caption}\n{output_marker}" if caption else output_marker
            judge = openai_compatible_multimodal_judge(self._judge_config())
            metric = self._new_metric(judge)
            test_case = LLMTestCase(input=input_text, actual_output=actual_output, multimodal=True)
            score = await metric.a_measure(test_case, _show_indicator=False)
        except Exception as exc:  # noqa: BLE001 - translate upstream metric failures to evaluator outcomes
            if metric is not None and judge is not None:
                await judge.settle_pending_calls()
                report_judge_usage(ctx, metric, judge)
            return EvaluationOutcome.error(f"deepeval_{self.spec.name}_failed:{type(exc).__name__}")
        finally:
            for image in created:
                _MLLM_IMAGE_REGISTRY.pop(image._id, None)
        assert metric is not None and judge is not None
        report_judge_usage(ctx, metric, judge)
        bad = out_of_range(score)
        if bad is not None:
            return bad

        raw: dict[str, Any] = {
            "deepeval_version": PINNED_DEEPEVAL,
            "metric": self.spec.upstream,
            "judge": getattr(metric, "evaluation_model", None),
            "score": metric.score,
            "reason": str(getattr(metric, "reason", ""))[:4000],
            "upstream_success": getattr(metric, "success", None),
            "upstream_threshold": getattr(metric, "threshold", None),
        }
        for key in ("SC_scores", "SC_reasoning", "PQ_scores", "PQ_reasoning", "scores", "reasons"):
            value = getattr(metric, key, None)
            if isinstance(value, (list, tuple)):
                raw[key] = _jsonable(value)
        return EvaluationOutcome.ok(
            "scalar",
            float(score),
            evidence=("case.input", "execution.output"),
            raw=raw,
        )


def _evaluator_class(spec: ImageMetricSpec) -> type[DeepEvalImageMetric]:
    class_name = "".join(part.title() for part in spec.name.split("_"))
    return type(
        class_name,
        (DeepEvalImageMetric,),
        {"spec": spec, "manifest": _manifest(spec)},
    )


IMAGE_EVALUATORS: tuple[type[DeepEvalImageMetric], ...] = tuple(
    _evaluator_class(spec) for spec in SPECS
)
