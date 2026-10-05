"""A metric's criteria can name what they read ("the same facts as the expected answer"). The
manifest's `parameter_patterns` turn that into a required field, so the harness sends it to a
worker; without it a G-Eval judge never saw the expected answer and scored every case 0."""

from __future__ import annotations

from aibench.core.models import EvaluatorManifest, FieldRequirement, MetricDirection
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)

EXPECTED = {"path": "case.reference.answer", "non_empty": True}


class Judged(Evaluator):
    manifest = EvaluatorManifest.model_validate(
        {
            "evaluator_id": "tests.judged",
            "version": "1.0.0",
            "plugin_id": "tests",
            "plugin_version": "0",
            "description": "criteria that may name the expected answer",
            "value_kind": "scalar",
            "direction": MetricDirection.HIGHER,
            "aggregation": "mean",
            "requires": (FieldRequirement(path="execution.output", non_empty=False),),
            "parameter_patterns": {
                name: {
                    "pattern": r"\bexpected\s+answer\b",
                    "requires": EXPECTED,
                    "unless_set": "evaluation_params",
                }
                for name in ("criteria", "evaluation_steps")
            },
        }
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        return EvaluationOutcome.ok("scalar", 1.0)


def _paths(params: dict[str, object]) -> list[str]:
    return [r.path for r in Judged().required_fields(params)]


def test_criteria_that_name_the_expected_answer_require_it() -> None:
    assert _paths({"criteria": "Is it polite?"}) == ["execution.output"]
    assert _paths({"criteria": "Same facts as the Expected Answer."}) == [
        "execution.output",
        "case.reference.answer",
    ]
    steps = {"evaluation_steps": ["Read it.", "Compare with the expected answer."]}
    assert "case.reference.answer" in _paths(steps)


def test_explicit_fields_win_and_a_similar_word_is_not_a_match() -> None:
    named = {"criteria": "the expected answer", "evaluation_params": ["input", "actual_output"]}
    assert "case.reference.answer" not in _paths(named)  # the user chose the fields
    assert _paths({"criteria": "answers should not be expected to be long"}) == ["execution.output"]
