"""Judge calibration and scoring invariants (§23 "Judge and outcome validation",
"Engine and contract validation"; 12-T3).

Calibration numbers are pinned as observations of the native evaluators against the v1
labels (written by the implementing agent; unreviewed). They quantify known limitations
rather than hide them: surface matching rejects correct paraphrases.

The invariant test is a seeded randomized (property-style) check through the real scorer,
with an evaluator that misbehaves on purpose: evaluation never mutates the case, anything
but an ok result carries no score, and a decision follows the frozen rule. It asserts that
every branch (pass, fail, error, not applicable, skipped) actually occurred."""

from __future__ import annotations

import asyncio
import json
import random
import string
from collections import Counter
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.models import (
    BenchmarkCase,
    Decision,
    DecisionRule,
    EvaluatorManifest,
    ExecutionResult,
    ExecutionStatus,
    FieldRequirement,
    MetricBinding,
    MetricDirection,
    ReferenceAnswer,
    RunManifest,
)
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from aibench.services.calibration import run_calibration
from aibench.services.scoring import BindingScorer
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

SET = Path(__file__).resolve().parents[1] / "benchmarks" / "judges" / "v1"


def _frac(measure: dict[str, Any]) -> tuple[int, int]:
    return measure["numerator"], measure["denominator"]


def test_native_evaluators_against_the_labelled_calibration_set() -> None:
    report = run_calibration(SET)
    assert report["label_review"] == "unreviewed"
    strict = report["bindings"]["native.exact_match {}"]["overall"]
    normalized = report["bindings"][
        'native.exact_match {"case_sensitive": false, "collapse_whitespace": true}'
    ]["overall"]
    [schema_key] = [k for k in report["bindings"] if k.startswith("native.json_schema")]
    schema = report["bindings"][schema_key]["overall"]
    # no false acceptance anywhere: injected instructions and contradictions are rejected
    for measured in (strict, normalized, schema):
        assert measured["false_acceptance"]["numerator"] == 0
        assert measured["undecided"] == 0
        assert measured["repeat_stability"]["value"] == 1.0
    # the known limitation, quantified: correct but differently worded answers fail
    assert _frac(strict["false_rejection"]) == (5, 7)
    assert _frac(normalized["false_rejection"]) == (3, 7)
    assert _frac(schema["agreement"]) == (7, 7)
    rejected = {d["category"] for d in report["bindings"]["native.exact_match {}"]["disagreements"]}
    assert rejected == {"formatting", "paraphrase", "verbose", "non_text"}
    assert next(n.get("evaluator") for n in report["not_measured"]) == "deepeval.faithfulness@1"


def test_calibrate_command_reports_unmeasured_judges(tmp_path: Path) -> None:
    out = tmp_path / "calibration.json"
    result = CliRunner().invoke(
        app, ["evaluators", "calibrate", "--set", str(SET), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "false rejection 5/7" in text and "labels: unreviewed" in text
    assert "not measured deepeval.faithfulness@1" in text
    assert json.loads(out.read_text(encoding="utf-8"))["set"] == "judge-calibration-v1"


# --------------------------------------------------------------------------- invariants

ALPHABET = string.ascii_letters + string.digits + ' \n\t.,!?{}[]":'


class Erratic(Evaluator):
    """Misbehaves on purpose: raises, returns an error, the wrong value kind, a
    non-boolean, or a boolean. The scorer must turn every misbehaviour into a recorded
    error, never a score."""

    manifest = EvaluatorManifest(
        evaluator_id="test.erratic",
        version="1.0.0",
        plugin_id="test",
        plugin_version="0",
        description="an erratic evaluator for invariant checks",
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="case.reference.answer"),
        ),
        default_rule=DecisionRule(comparator="is_true"),
    )
    rng = random.Random(7)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        choice = self.rng.randrange(6)
        if choice == 0:
            raise RuntimeError("evaluator bug")
        if choice == 1:
            return EvaluationOutcome.error("judge unavailable")
        if choice == 2:
            return EvaluationOutcome.ok("scalar", 0.7)  # not the declared kind
        if choice == 3:
            return EvaluationOutcome.ok("boolean", "yes")  # not a boolean
        return EvaluationOutcome.ok("boolean", choice == 4)


def _random_output(rng: random.Random, reference: str | None) -> Any:
    kind = rng.randrange(6)
    if kind == 0 and reference:
        return reference  # a correct answer, so passes happen
    if kind == 1:
        return None
    if kind == 2:
        return {"answer": "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(12)))}
    if kind == 3:
        return rng.randrange(-5, 50)
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(40)))


def test_scoring_invariants_hold_for_generated_cases(tmp_path: Path) -> None:
    """Through the real scorer (`BindingScorer`), over 600 generated cases: the Golden is
    never mutated, anything but `ok` carries no score and is not evaluated, and an `ok`
    decision follows the frozen rule. Every branch must actually occur."""
    registry = EvaluatorRegistry.with_native()
    registry.register(Erratic)
    workspace = Workspace.at(tmp_path)
    workspace.ensure_directories()
    storage = Storage(Database.open_workspace(workspace))
    artifacts = ArtifactStore(workspace.artifacts_dir)
    # raw evaluator outputs are artifacts of a run, so the run must exist
    storage.commit_run(
        RunManifest(run_id="prop", dataset_hash="d", application_hash="a", plan_hash="p")
    )
    rng = random.Random(20260924)
    seen: Counter[str] = Counter()

    async def run_all() -> None:
        scorers = []
        for binding in (
            MetricBinding(metric="test.erratic"),
            MetricBinding(metric="native.exact_match"),
            MetricBinding(
                metric="native.exact_match",
                params={"case_sensitive": False, "collapse_whitespace": True},
            ),
        ):
            scorer = BindingScorer(
                storage, artifacts, "prop", registry.resolve_binding(binding), 5.0, None
            )
            await scorer.open()
            scorers.append(scorer)
        for number in range(600):
            scorer = scorers[number % len(scorers)]
            reference = rng.choice([None, "", "".join(rng.choice(ALPHABET) for _ in range(20))])
            case = BenchmarkCase(
                case_id=f"p{number}",
                input={"question": "q", "nested": {"k": [1, 2]}},
                reference=None if reference is None else ReferenceAnswer(answer=reference),
            )
            before = case.model_dump_json()
            execution = ExecutionResult(
                execution_id=f"e{number}",
                run_id="prop",
                case_id=case.case_id,
                status=rng.choice([ExecutionStatus.OK] * 5 + [ExecutionStatus.ERROR]),
                output=_random_output(rng, reference),
            )
            result = await scorer._score_one(scorer._evaluator, execution, [case])
            assert case.model_dump_json() == before  # the Golden is never mutated
            if result.status is ExecutionStatus.OK:
                assert result.value is not None and isinstance(result.value.value, bool)
                expected = Decision.PASS if result.value.value else Decision.FAIL
                assert result.decision is expected, result  # from the frozen rule
                seen[expected.value] += 1
            else:
                assert result.value is None, result  # never a score
                assert result.decision is Decision.NOT_EVALUATED, result
                seen[result.status.value] += 1
        for scorer in scorers:
            await scorer.close([])

    try:
        asyncio.run(run_all())
    finally:
        storage.db.close()
    # every branch was exercised, so the assertions above were not vacuous
    for branch in ("pass", "fail", "error", "not_applicable", "skipped"):
        assert seen[branch] > 0, seen
