"""The stored-output contract with the hosted OpenAI Evals API (§11B, 17-T2, 17-G2).

Checked against the official `openai==3.19.2` SDK types (generated from the OpenAPI
spec): `evals.create(data_source_config={"type": "custom", ...}, testing_criteria=[...])`
and `evals.runs.create(data_source={"type": "jsonl", "source": {"type": "file_content",
"content": [{"item": {...}}]}})`.

Scoring recorded outputs means the grader reads the application's output from the
uploaded item: templates may reference `{{item.input}}`, `{{item.output}}` and
`{{item.reference}}` only. The run's data source is always `jsonl` built from recorded
executions. A `completions` or `responses` data source, or any `{{sample.*}}` template,
would have the service generate a replacement answer instead of grading the recorded
one, so they are refused here, not sent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

PINNED_OPENAI = "3.19.2"
ITEM_FIELDS = ("input", "output", "reference")
# Rule graders grade text without a model; label_model grades with one (a judge).
RULE_GRADERS = ("string_check", "text_similarity")
MODEL_GRADERS = ("label_model",)
_STRING_OPERATIONS = ("eq", "ne", "like", "ilike")
_SIMILARITY_METRICS = (
    "cosine", "fuzzy_match", "bleu", "gleu", "meteor",
    "rouge_1", "rouge_2", "rouge_3", "rouge_4", "rouge_5", "rouge_l",
)  # fmt: skip
_TEMPLATE = re.compile(r"\{\{\s*([^}]*?)\s*\}\}")
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")


class ContractError(ValueError):
    """A grader or data source the stored-output contract does not allow."""


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)


def _check_templates(criterion: Mapping[str, Any]) -> list[str]:
    problems = []
    for text in _strings(criterion):
        for reference in _TEMPLATE.findall(text):
            if reference.startswith("sample."):
                problems.append(
                    f"{{{{{reference}}}}} reads a model-generated sample; stored-output "
                    "scoring grades the recorded output ({{item.output}})"
                )
            elif reference not in {f"item.{f}" for f in ITEM_FIELDS}:
                problems.append(
                    f"{{{{{reference}}}}} is not an uploaded field; use one of "
                    + ", ".join(f"{{{{item.{f}}}}}" for f in ITEM_FIELDS)
                )
    return problems


def check_criteria(criteria: Sequence[Mapping[str, Any]], *, models_allowed: bool) -> list[str]:
    """Problems with testing criteria under the stored-output contract."""
    problems: list[str] = []
    if not criteria:
        return ["at least one testing criterion is required"]
    names = [c.get("name") for c in criteria]
    if len(set(names)) != len(names):
        problems.append("testing criterion names must be unique")
    for criterion in criteria:
        name, kind = criterion.get("name"), criterion.get("type")
        label = f"criterion {name!r}"
        if not isinstance(name, str) or not _NAME.match(name):
            problems.append(f"{label}: name must match {_NAME.pattern}")
        if kind in RULE_GRADERS:
            required = {"string_check": ("input", "operation", "reference"),
                        "text_similarity": ("input", "reference", "evaluation_metric",
                                            "pass_threshold")}[kind]  # fmt: skip
            missing = [k for k in required if k not in criterion]
            if missing:
                problems.append(f"{label}: missing {', '.join(missing)}")
            if kind == "string_check" and criterion.get("operation") not in _STRING_OPERATIONS:
                problems.append(f"{label}: operation must be one of {_STRING_OPERATIONS}")
            if (
                kind == "text_similarity"
                and criterion.get("evaluation_metric") not in _SIMILARITY_METRICS
            ):
                problems.append(f"{label}: evaluation_metric must be one of {_SIMILARITY_METRICS}")
        elif kind in MODEL_GRADERS:
            if not models_allowed:
                problems.append(
                    f"{label}: {kind} grades with a model; the policy must allow model "
                    "evaluators (allow_model_evaluators)"
                )
            missing = [
                k for k in ("input", "labels", "passing_labels", "model") if k not in criterion
            ]
            if missing:
                problems.append(f"{label}: missing {', '.join(missing)}")
        else:
            problems.append(
                f"{label}: grader type {kind!r} is not supported for stored-output scoring; "
                f"supported: {', '.join(RULE_GRADERS + MODEL_GRADERS)}"
            )
        allowed_keys = {"type", "name", "input", "operation", "reference", "evaluation_metric",
                        "pass_threshold", "labels", "passing_labels", "model"}  # fmt: skip
        extra = sorted(set(criterion) - allowed_keys)
        if extra:
            problems.append(f"{label}: unsupported fields {', '.join(extra)}")
        problems.extend(f"{label}: {p}" for p in _check_templates(criterion))
    return problems


def uses_models(criteria: Sequence[Mapping[str, Any]]) -> bool:
    return any(c.get("type") in MODEL_GRADERS for c in criteria)


def eval_request(
    name: str, criteria: Sequence[Mapping[str, Any]], metadata: Mapping[str, str]
) -> dict[str, Any]:
    """`evals.create` parameters: a custom item schema of exactly the uploaded fields."""
    return {
        "name": name,
        "data_source_config": {
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {
                    "aibench_case_id": {"type": "string"},
                    **{f: {"type": "string"} for f in ITEM_FIELDS},
                },
                "required": ["aibench_case_id", "input", "output"],
            },
            "include_sample_schema": False,
        },
        "testing_criteria": [dict(c) for c in criteria],
        "metadata": dict(metadata),
    }


def run_request(
    name: str, items: Sequence[Mapping[str, str]], metadata: Mapping[str, str]
) -> dict[str, Any]:
    """`evals.runs.create` parameters: always a `jsonl` data source of recorded items."""
    for item in items:
        if set(item) - {"aibench_case_id", *ITEM_FIELDS}:
            raise ContractError(f"unexpected item fields: {sorted(item)}")
    return {
        "name": name,
        "data_source": {
            "type": "jsonl",
            "source": {"type": "file_content", "content": [{"item": dict(i)} for i in items]},
        },
        "metadata": dict(metadata),
    }


def check_run_request(request: Mapping[str, Any]) -> None:
    """The last check before sending: the data source can only grade recorded outputs."""
    source = request.get("data_source") or {}
    if source.get("type") != "jsonl":
        raise ContractError(
            f"data source {source.get('type')!r} would have the service generate outputs; "
            "stored-output scoring only uploads recorded outputs (jsonl)"
        )
    content = (source.get("source") or {}).get("content")
    if (source.get("source") or {}).get("type") != "file_content" or not isinstance(content, list):
        raise ContractError("the jsonl data source must carry the recorded items inline")
    if any("sample" in entry for entry in content):
        raise ContractError("items must not carry a `sample`: the output is the recorded one")
