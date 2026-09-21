"""01-T2: shorthand normalization rules."""

import pytest

from aibench.core.errors import ValidationError
from aibench.core.models import ReferenceStatus, ToolMatchMode
from aibench.datasets.normalize import normalize_case


def test_expected_output_becomes_reference_answer() -> None:
    result = normalize_case(
        {"case_id": "c1", "input": "q", "expected_output": "a"}, line=1, occurrence_index=1
    )
    assert result.case.reference.answer == "a"


def test_legacy_context_becomes_reference_context_with_warning() -> None:
    result = normalize_case(
        {"case_id": "c1", "input": "q", "context": ["doc one"]}, line=1, occurrence_index=1
    )
    assert result.case.reference.context == ("doc one",)
    assert any("not observed retrieval" in w for w in result.warnings)


def test_expected_tools_becomes_contains_all_expectation_with_warning() -> None:
    result = normalize_case(
        {"case_id": "c1", "input": "q", "expected_tools": ["a", "b"]}, line=1, occurrence_index=1
    )
    assert result.case.reference.tools.tool_names == ("a", "b")
    assert result.case.reference.tools.match_mode == ToolMatchMode.CONTAINS_ALL
    assert any("contains_all" in w for w in result.warnings)


def test_repository_shorthand_flags_missing_execution_prerequisites() -> None:
    result = normalize_case(
        {"case_id": "code-001", "input": "fix bug", "repository": "./repos/auth-service"},
        line=1,
        occurrence_index=1,
    )
    assert result.case.repository is not None
    assert not result.case.repository.is_execution_ready
    assert any("missing execution prerequisites" in w for w in result.warnings)


def test_missing_case_id_generates_stable_documented_id() -> None:
    raw = {"input": "same content"}
    r1 = normalize_case(raw, line=1, occurrence_index=1)
    r2 = normalize_case(raw, line=1, occurrence_index=1)
    assert r1.case.case_id == r2.case.case_id
    assert r1.case.case_id.startswith("generated-")


def test_missing_case_id_differs_by_occurrence_index() -> None:
    raw = {"input": "same content"}
    r1 = normalize_case(raw, line=1, occurrence_index=1)
    r2 = normalize_case(raw, line=2, occurrence_index=2)
    assert r1.case.case_id != r2.case.case_id


def test_unknown_top_level_key_raises_with_migration_advice() -> None:
    with pytest.raises(ValidationError, match="extensions"):
        normalize_case({"input": "q", "made_up_field": 1}, line=3, occurrence_index=1)


def test_namespaced_extension_key_is_accepted() -> None:
    result = normalize_case(
        {"input": "q", "extensions": {"acme.priority": 1}}, line=1, occurrence_index=1
    )
    assert result.case.extensions == {"acme.priority": 1}


def test_non_namespaced_extension_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="namespaced"):
        normalize_case(
            {"input": "q", "extensions": {"made_up_field": 1}}, line=7, occurrence_index=1
        )


def test_colon_namespaced_extension_key_is_accepted() -> None:
    result = normalize_case(
        {"input": "q", "extensions": {"acme:priority": 1}}, line=1, occurrence_index=1
    )
    assert result.case.extensions == {"acme:priority": 1}


def test_missing_input_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="input"):
        normalize_case({"case_id": "c1"}, line=5, occurrence_index=1)


def test_non_list_context_is_rejected() -> None:
    with pytest.raises(ValidationError):
        normalize_case({"input": "q", "context": "not-a-list"}, line=1, occurrence_index=1)


def test_error_includes_line_number() -> None:
    with pytest.raises(ValidationError, match=r"line 42"):
        normalize_case({"case_id": "c1"}, line=42, occurrence_index=1)


def test_provenance_defaults_to_human_authored() -> None:
    result = normalize_case({"input": "q"}, line=1, occurrence_index=1)
    assert result.case.provenance.origin == ReferenceStatus.HUMAN_AUTHORED


# --- malformed nested input must become a line-precise ValidationError, never a raw
# --- pydantic.ValidationError / TypeError / KeyError bubbling out of this module.


def test_non_string_context_items_raise_validation_error_not_pydantic_error() -> None:
    with pytest.raises(ValidationError, match=r"line 9"):
        normalize_case({"input": "q", "context": [1, 2]}, line=9, occurrence_index=1)


def test_non_object_fixture_entry_raises_validation_error_not_type_error() -> None:
    with pytest.raises(ValidationError, match=r"fixtures"):
        normalize_case({"input": "q", "fixtures": [1]}, line=1, occurrence_index=1)


def test_fixture_entry_missing_name_raises_validation_error() -> None:
    with pytest.raises(ValidationError, match=r"fixtures"):
        normalize_case(
            {"input": "q", "fixtures": [{"content": "x"}]}, line=1, occurrence_index=1
        )


def test_non_object_reference_is_rejected_not_silently_ignored() -> None:
    with pytest.raises(ValidationError, match=r"reference"):
        normalize_case({"input": "q", "reference": "not-an-object"}, line=1, occurrence_index=1)


def test_reference_with_unknown_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        normalize_case(
            {"input": "q", "reference": {"not_a_real_field": 1}}, line=1, occurrence_index=1
        )


def test_non_object_provenance_is_rejected_not_silently_ignored() -> None:
    with pytest.raises(ValidationError, match=r"provenance"):
        normalize_case({"input": "q", "provenance": "nope"}, line=1, occurrence_index=1)


def test_non_object_expectations_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"expectations"):
        normalize_case({"input": "q", "expectations": "nope"}, line=1, occurrence_index=1)


def test_non_object_metadata_is_rejected() -> None:
    with pytest.raises(ValidationError, match=r"metadata"):
        normalize_case({"input": "q", "metadata": "nope"}, line=1, occurrence_index=1)
