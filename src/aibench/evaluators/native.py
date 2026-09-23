"""Native evaluators (04-T3): deterministic, dependency-light checks that run in-process.

Status discipline, applied by every evaluator here:
- a wrong answer is `ok` with a failing value (a legitimate low score);
- missing or unusable evidence is `not_applicable` with a reason;
- a problem with the evaluator's own inputs (e.g. an invalid schema) is `error`.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Mapping
from typing import Any

from referencing.exceptions import Unresolvable

from aibench import __version__
from aibench.core.models import (
    DecisionRule,
    EvaluatorManifest,
    FieldRequirement,
    MetricBinding,
    MetricDirection,
    deep_unfreeze,
)
from aibench.evaluators.protocol import (
    MISSING,
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.evaluators.validation import SchemaTimeout, schema_problems, validate_untrusted
from aibench.runners.bindings import InvalidDocument, parse_app_json

NATIVE_PLUGIN_ID = "aibench.native"
_WHITESPACE = re.compile(r"\s+")


class ExactMatch(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="native.exact_match",
        version="1.0.0",
        plugin_id=NATIVE_PLUGIN_ID,
        plugin_version=__version__,
        description="Output text equals the reference answer after optional normalization.",
        limitations=(
            "Surface comparison only: a correct paraphrase fails.",
            "A non-text output (null, object, list) is a failed match, not skipped.",
        ),
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="case.reference.answer"),
        ),
        default_rule=DecisionRule(comparator="is_true"),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "case_sensitive": {"type": "boolean"},
                "strip": {"type": "boolean"},
                "collapse_whitespace": {"type": "boolean"},
            },
        },
    )

    def _normalize(self, text: str) -> str:
        if self.params.get("strip", True):
            text = text.strip()
        if self.params.get("collapse_whitespace", False):
            text = _WHITESPACE.sub(" ", text)
        if not self.params.get("case_sensitive", True):
            text = text.casefold()
        return text

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        output = view.get("execution.output")
        reference = view.get("case.reference.answer")
        evidence = ("execution.output", "case.reference.answer")
        if not isinstance(output, str):
            # The app answered, just not with text: that is a wrong answer, not missing
            # evidence. Treating it as not applicable would let null/structured answers on
            # hard cases raise the pass rate.
            return EvaluationOutcome.ok(
                "boolean",
                False,
                evidence=evidence,
                raw={"reason": f"output_not_text:{type(output).__name__}"},
            )
        matched = self._normalize(output) == self._normalize(reference)
        return EvaluationOutcome.ok("boolean", matched, evidence=evidence)


class JsonSchemaCheck(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="native.json_schema",
        version="1.0.0",
        plugin_id=NATIVE_PLUGIN_ID,
        plugin_version=__version__,
        description=(
            "Output validates against a JSON Schema (Draft 2020-12), given inline or read "
            "from a case field. Text output is parsed as JSON first unless parse_text=false."
        ),
        limitations=(
            "Format keywords (e.g. 'email') are annotations only; they are not asserted.",
            "Remote $ref targets are never fetched; a schema that needs one is an error.",
            "Schemas with regex keywords are validated in a worker process (slower).",
        ),
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(FieldRequirement(path="execution.output", non_empty=False),),
        default_rule=DecisionRule(comparator="is_true"),
        parameters_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "schema": {"type": "object"},
                "schema_field": {
                    "type": "string",
                    "pattern": r"^case\.(expectations|metadata)\.[A-Za-z0-9_.]+$",
                },
                "parse_text": {"type": "boolean"},
            },
            "oneOf": [{"required": ["schema"]}, {"required": ["schema_field"]}],
        },
    )

    def validate_binding(self, binding: MetricBinding) -> list[str]:
        problems = super().validate_binding(binding)
        params = deep_unfreeze(binding.params) or {}
        if not problems and "schema" in params:
            problems += schema_problems(params["schema"])
        return problems

    def required_fields(self, params: Mapping[str, Any]) -> tuple[FieldRequirement, ...]:
        extra = (FieldRequirement(path=params["schema_field"]),) if "schema_field" in params else ()
        return self.manifest.requires + extra

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        schema = self.params.get("schema")
        evidence = ["execution.output"]
        if schema is None:
            field = self.params["schema_field"]
            schema = view.get(field)
            evidence.append(field)
            problems = schema_problems(schema) if schema is not MISSING else ["missing"]
            if problems:
                return EvaluationOutcome.error(f"schema from {field} is unusable: {problems[0]}")

        output = view.get("execution.output")
        if isinstance(output, str) and self.params.get("parse_text", True):
            try:
                output = parse_app_json(output)
            except InvalidDocument as exc:
                errors = [{"path": "/", "message": f"output is not valid JSON: {exc}"}]
                return EvaluationOutcome.ok(
                    "boolean", False, evidence=evidence, raw={"errors": errors}
                )
        try:
            # Off the event loop: a regex schema runs in a killable worker (bounded by
            # SCHEMA_WORKER_TIMEOUT_SECONDS), so the scoring timeout can also fire.
            errors = await asyncio.to_thread(validate_untrusted, schema, output)
        except SchemaTimeout as exc:
            return EvaluationOutcome.error(f"timeout:{exc}")
        except (Unresolvable, RuntimeError) as exc:  # the schema's fault, not the output's
            return EvaluationOutcome.error(
                f"schema could not be applied: {type(exc).__name__}: {exc}"[:300]
            )
        return EvaluationOutcome.ok(
            "boolean", not errors, evidence=evidence, raw={"errors": errors}
        )


NATIVE_EVALUATORS: tuple[type[Evaluator], ...] = (ExactMatch, JsonSchemaCheck)
