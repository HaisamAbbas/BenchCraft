"""01-T1: model immutability, input/reference separation, JSON Schema round trips."""

import json
from pathlib import Path

import pytest
from jsonschema import validate
from pydantic import ValidationError as PydanticValidationError

from aibench.core.models import (
    ALL_MODELS,
    ArtifactRef,
    BenchmarkCase,
    EvaluationPlan,
    Fixture,
    ReferenceAnswer,
)
from aibench.core.plans import ReleaseGate
from aibench.core.schema_export import export_schemas
from aibench.core.sessions import (
    SESSION_MODELS,
    DecisionRecord,
    PlanPatch,
    SessionChoices,
    SessionReleaseGate,
)


def test_benchmark_case_is_frozen() -> None:
    case = BenchmarkCase(case_id="c1", input="hello")
    with pytest.raises(PydanticValidationError):
        case.case_id = "c2"  # type: ignore[misc]


def test_nested_input_mutation_is_blocked() -> None:
    """Regression test for the exact probe from code review: `frozen=True` alone only
    blocks attribute reassignment, not mutation of a mutable object already stored in a
    field. `FrozenValue` must deep-freeze nested containers too."""
    case = BenchmarkCase(case_id="c1", input={"nested": ["a", "b"]})
    with pytest.raises((AttributeError, TypeError)):
        case.input["nested"].append("mutated")
    with pytest.raises(TypeError):
        case.input["new_key"] = "x"
    assert case.input == {"nested": ("a", "b")}


def test_nested_metadata_and_extensions_are_deep_frozen() -> None:
    case = BenchmarkCase(
        case_id="c1",
        input="hi",
        metadata={"tags": ["a", "b"]},
        extensions={"acme.config": {"level": 1}},
    )
    with pytest.raises(TypeError):
        case.metadata["tags"] = []
    with pytest.raises(TypeError):
        case.extensions["acme.config"]["level"] = 2


def test_fixture_content_is_deep_frozen() -> None:
    fixture = Fixture(name="doc", content={"rows": [1, 2, 3]}, app_visible=True)
    with pytest.raises(AttributeError):
        fixture.content["rows"].append(4)


def test_evaluation_plan_nested_specs_are_deep_frozen() -> None:
    plan = EvaluationPlan(plan_id="p1", metric_specs=({"id": "faithfulness", "args": [1]},))
    with pytest.raises((AttributeError, TypeError)):
        plan.metric_specs[0]["args"].append(2)


def test_application_input_projection_returns_a_plain_json_safe_copy() -> None:
    """The projection is handed to a runner (Prompt 03) for real transport, so it must be
    ordinary mutable JSON-serializable data, not a leaked frozen structure, even though the
    source case itself stays deep-frozen."""
    case = BenchmarkCase(
        case_id="c1",
        input={"nested": {"deep": [1, 2, {"x": "y"}]}},
        fixtures=(Fixture(name="doc", content={"a": [1, 2]}, app_visible=True),),
    )
    projection = case.application_input_projection()
    json.dumps(projection)  # must not raise
    projection["input"]["nested"]["deep"].append("mutable-copy-is-fine")
    assert case.input["nested"]["deep"] == (1, 2, {"x": "y"})  # source untouched


def test_identity_fields_are_required() -> None:
    with pytest.raises(PydanticValidationError):
        ArtifactRef(digest="sha256:x", uri="file:///x", mime_type="text/plain", size_bytes=1)  # type: ignore[call-arg]
    ArtifactRef(
        artifact_id="a1", digest="sha256:x", uri="file:///x", mime_type="text/plain", size_bytes=1
    )


def test_reference_never_enters_application_input_projection() -> None:
    case = BenchmarkCase(
        case_id="c1",
        input="What is the refund policy?",
        reference=ReferenceAnswer(answer="Refunds within 30 days.", context=("secret doc",)),
        fixtures=(
            Fixture(name="visible_doc", content="public info", app_visible=True),
            Fixture(name="hidden_doc", content="judge-only info", app_visible=False),
        ),
    )
    projection = case.application_input_projection()
    serialized = json.dumps(projection)

    assert "secret doc" not in serialized
    assert "Refunds within 30 days" not in serialized
    assert "judge-only info" not in serialized
    assert "public info" in serialized
    assert projection["fixtures"] == {"visible_doc": "public info"}


def test_application_input_projection_excludes_reference_key_entirely() -> None:
    case = BenchmarkCase(case_id="c1", input="hi", reference=ReferenceAnswer(answer="x"))
    projection = case.application_input_projection()
    assert "reference" not in projection


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda m: m.__name__)
def test_json_schema_round_trip(model) -> None:
    schema = model.model_json_schema()
    assert schema["title"] == model.__name__
    # Must be JSON-serializable (a real schema export, not a placeholder).
    json.dumps(schema)


def test_export_schemas_writes_versioned_files(tmp_path) -> None:
    written = export_schemas(tmp_path)
    # plus the ExecutablePlan authoring format and the session records (Prompt 08)
    assert len(written) == len(ALL_MODELS) + 1 + len(SESSION_MODELS)
    assert tmp_path / "1.0.0" / "ExecutablePlan.json" in written
    for path in written:
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["$schemaVersion"] == "1.0.0"

    choices = SessionChoices(
        application="/tmp/app.yaml",
        dataset="/tmp/cases.jsonl",
        gates=(
            SessionReleaseGate(
                gate_id="correctness",
                metric="native.exact_match",
                min_pass_rate=0.9,
            ),
        ),
    )
    patch = PlanPatch(
        gates=(ReleaseGate(gate_id="correctness", binding=0, min_pass_rate=0.9),)
    )
    decision = DecisionRecord(
        decision_id="s1:d1",
        session_id="s1",
        source_turn_id=None,
        source="user",
        revision=1,
        choices=choices,
        plan_file="plan.json",
        plan_hash="sha256:abc",
        executable=False,
        draft={},
    )
    for model, value in (
        (SessionChoices, choices.model_dump(mode="json")),
        (PlanPatch, patch.model_dump(mode="json")),
        (DecisionRecord, decision.model_dump(mode="json")),
    ):
        schema = json.loads((tmp_path / "1.0.0" / f"{model.__name__}.json").read_text())
        validate(instance=value, schema=schema)
        checked_in = Path(__file__).parents[1] / "schemas" / "1.0.0" / f"{model.__name__}.json"
        validate(instance=value, schema=json.loads(checked_in.read_text(encoding="utf-8")))
