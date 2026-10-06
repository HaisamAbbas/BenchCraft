"""JSON Pointer bindings, the app-visible envelope boundary (03-G2) and honest
observation states (03-G4)."""

from __future__ import annotations

import json

import pytest

from aibench.core.errors import ConfigError
from aibench.runners.bindings import (
    MISSING,
    AppInputEnvelope,
    BindingError,
    InputBinding,
    InvalidDocument,
    OutputBinding,
    extract_optional,
    parse_app_json,
    resolve_pointer,
)
from tests.runner_support import SENTINEL, golden_case


def test_resolve_pointer_follows_rfc6901_escapes_and_indexes() -> None:
    doc = {"a/b": {"m~n": [10, 20]}, "": 1}
    assert resolve_pointer(doc, "/a~1b/m~0n/1") == 20
    assert resolve_pointer(doc, "") == doc
    assert resolve_pointer(doc, "/a~1b/m~0n/5") is MISSING
    assert resolve_pointer(doc, "/nope") is MISSING
    with pytest.raises(ConfigError):
        resolve_pointer(doc, "no-leading-slash")


def test_envelope_contains_only_app_visible_data() -> None:
    envelope = AppInputEnvelope.from_case(golden_case())
    assert set(envelope.data) == {"case_id", "input", "fixtures"}
    assert envelope.data["fixtures"] == {"visible": {"note": "shown to the app"}}
    assert SENTINEL not in json.dumps(envelope.data)


def test_default_input_binding_sends_the_envelope_unchanged() -> None:
    envelope = AppInputEnvelope.from_case(golden_case())
    assert InputBinding.from_spec({}).build_payload(envelope) == dict(envelope.data)


def test_input_binding_maps_fields_into_a_static_template() -> None:
    binding = InputBinding.from_spec(
        {
            "template": {"top_k": 2, "opts": {"lang": "en"}},
            "fields": {"/question": "/input", "/opts/note": "/fixtures/visible/note"},
        }
    )
    payload = binding.build_payload(AppInputEnvelope.from_case(golden_case()))
    assert payload == {
        "top_k": 2,
        "opts": {"lang": "en", "note": "shown to the app"},
        "question": "What is your refund policy?",
    }
    # The template is copied, never mutated across invocations.
    assert binding.template == {"top_k": 2, "opts": {"lang": "en"}}


def test_input_binding_cannot_reach_judge_only_data() -> None:
    """There is no pointer that addresses `reference` or a hidden fixture: they are not in
    the envelope, so binding them is a BindingError rather than a leak."""
    envelope = AppInputEnvelope.from_case(golden_case())
    for source in ("/reference/answer", "/fixtures/hidden", "/metadata/grader_note"):
        binding = InputBinding.from_spec({"fields": {"/x": source}})
        with pytest.raises(BindingError):
            binding.build_payload(envelope)


def test_input_binding_rejects_template_without_fields_and_bad_pointers() -> None:
    with pytest.raises(ConfigError):
        InputBinding.from_spec({"template": {"a": 1}})
    with pytest.raises(ConfigError):
        InputBinding.from_spec({"fields": {"question": "/input"}})
    with pytest.raises(ConfigError):
        InputBinding.from_spec({"fields": {"/q": "/input"}, "unknown": 1})


def test_undeclared_capabilities_are_unknown_not_empty() -> None:
    extracted = extract_optional({"output": "x", "docs": ["d"]}, OutputBinding())
    assert extracted.retrieved_context is None
    assert extracted.usage is None and extracted.cost is None
    for name in ("retrieved_context", "tool_events", "usage", "cost"):
        assert extracted.completeness[name] == {"state": "unknown", "detail": "not_bound"}


def test_declared_capabilities_distinguish_present_empty_missing_invalid() -> None:
    binding = OutputBinding(
        retrieved_context="/docs",
        retrieved_context_item="/text",
        tool_events="/tools",
        usage="/usage",
        cost="/cost",
    )
    doc = {"docs": [{"text": "a"}, {"text": "b"}], "tools": [], "cost": -1}
    extracted = extract_optional(doc, binding)
    c = extracted.completeness
    assert extracted.retrieved_context == ("a", "b")
    assert (c["retrieved_context"]["state"], c["retrieved_context"]["detail"]) == (
        "observed",
        "present",
    )
    assert extracted.tool_events == ()
    assert (c["tool_events"]["state"], c["tool_events"]["detail"]) == ("observed", "empty")
    assert extracted.usage is None
    assert (c["usage"]["state"], c["usage"]["detail"]) == ("unknown", "missing")
    assert extracted.cost is None
    assert (c["cost"]["state"], c["cost"]["detail"]) == ("unknown", "invalid")


def test_passages_grouped_per_source_are_flattened_in_order() -> None:
    """LightRAG answers with one reference per file, each holding a list of chunk texts. Only
    the first file's chunks were read when the pointer was /references/0/content, so recall,
    precision and faithfulness were scored on partial evidence."""
    document = {
        "response": "x",
        "references": [
            {"file_path": "a.md", "content": ["a1", "a2"]},
            {"file_path": "b.md", "content": ["b1"]},
            {"file_path": "c.md", "content": []},  # a source with no passages adds none
        ],
    }
    binding = OutputBinding(retrieved_context="/references", retrieved_context_item="/content")
    extracted = extract_optional(document, binding)
    assert extracted.retrieved_context == ("a1", "a2", "b1")
    assert extracted.completeness["retrieved_context"]["detail"] == "present"

    # Strings and lists of strings may mix; anything else is still invalid, never stringified.
    mixed = extract_optional({"docs": ["a", ["b", "c"]]}, OutputBinding(retrieved_context="/docs"))
    assert mixed.retrieved_context == ("a", "b", "c")
    nested = extract_optional({"docs": [["b", {"x": 1}]]}, OutputBinding(retrieved_context="/docs"))
    assert nested.retrieved_context is None
    assert nested.completeness["retrieved_context"]["detail"] == "invalid"


def test_retrieved_documents_without_item_pointer_are_invalid_not_stringified() -> None:
    extracted = extract_optional(
        {"docs": [{"text": "a"}]}, OutputBinding(retrieved_context="/docs")
    )
    assert extracted.retrieved_context is None
    assert extracted.completeness["retrieved_context"]["detail"] == "invalid"


def test_redactor_catches_json_escaped_and_truncated_secrets() -> None:
    from aibench.security.secrets import Redactor

    redactor = Redactor([("env:K", 'se"cret\value')])
    escaped = json.dumps({"echo": 'se"cret\value'}).encode()
    assert b"cret" not in redactor.data(escaped)
    truncated = b'prefix se"cret\va'  # cut by a size cap mid-secret
    assert redactor.data(truncated, truncated=True) == b"prefix <redacted-partial:env:K>"
    assert redactor.data(truncated) == truncated  # only scrubbed when known to be truncated


def test_parse_app_json_rejects_exponent_overflow_as_non_finite() -> None:
    with pytest.raises(InvalidDocument):
        parse_app_json('{"output":"ok","cost":1e999}')
