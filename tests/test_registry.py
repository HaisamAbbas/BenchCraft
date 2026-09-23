"""Registry resolution, schema compatibility and pre-execution binding validation
(04-T2, 04-G2)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aibench.core.errors import PolicyError
from aibench.core.models import ApplicationSpec, FieldRequirement, MetricBinding
from aibench.evaluators.native import ExactMatch
from aibench.evaluators.protocol import EvaluationOutcome, Evaluator
from aibench.registry import (
    BindingValidationError,
    EvaluatorRegistry,
    RegistryError,
    schema_compatible,
)
from tests.runner_support import REPO_ROOT


def _variant(version: str, core_schema: str = ">=1.0.0,<2.0.0") -> type[Evaluator]:
    # `native.*` is reserved for built-ins, so test variants live in `tests.*`.
    manifest = ExactMatch.manifest.model_copy(
        update={
            "evaluator_id": "tests.exact_match",
            "plugin_id": "tests",
            "version": version,
            "core_schema": core_schema,
        }
    )

    class Variant(ExactMatch):
        pass

    Variant.manifest = manifest
    return Variant


def test_resolution_by_id_major_minor_and_exact_version() -> None:
    registry = EvaluatorRegistry.with_native()
    for version in ("1.0.0", "1.1.0", "1.10.2", "2.0.0"):
        registry.register(_variant(version))
    assert registry.resolve("tests.exact_match")[0].version == "2.0.0"
    assert registry.resolve("tests.exact_match@1")[0].version == "1.10.2"  # numeric, not lexical
    assert registry.resolve("tests.exact_match@1.1")[0].version == "1.1.0"
    assert registry.resolve("tests.exact_match@1.0.0")[0].version == "1.0.0"
    for bad, message in (
        ("tests.exact_match@3", "no version"),
        ("native.nope", "unknown evaluator"),
        ("exact_match", "invalid metric reference"),
        ("native.exact_match@latest", "invalid metric reference"),
    ):
        with pytest.raises(RegistryError, match=message):
            registry.resolve(bad)


def test_core_schema_range_is_enforced() -> None:
    assert schema_compatible(">=1.0.0,<2.0.0", "1.0.0")
    assert not schema_compatible(">=1.1,<2", "1.0.0")
    assert not schema_compatible("^1.0", "1.0.0")  # unsupported syntax is incompatible
    registry = EvaluatorRegistry()
    registry.register(_variant("9.0.0", core_schema=">=2.0.0"))
    with pytest.raises(RegistryError, match="supports core schema"):
        registry.resolve("tests.exact_match")


def test_duplicate_and_worker_only_registrations_are_refused() -> None:
    registry = EvaluatorRegistry.with_native()
    registry.register(_variant("1.0.0"))
    with pytest.raises(RegistryError, match="already registered"):
        registry.register(_variant("1.0.0"))
    external = ExactMatch.manifest.model_copy(
        update={"evaluator_id": "vendor.metric", "plugin_id": "vendor"}
    )
    registry.register_external(external)
    assert any(
        m.evaluator_id == "vendor.metric" and m.requires_worker for m in registry.manifests()
    )
    with pytest.raises(RegistryError, match="isolated worker"):
        registry.resolve("vendor.metric")


def test_validation_reports_every_problem_before_anything_runs() -> None:
    registry = EvaluatorRegistry.with_native()
    bindings = [
        MetricBinding(metric="native.exact_match", params={"case_sensitiv": True}),  # typo
        MetricBinding(metric="native.exact_match", rule={"comparator": ">=", "threshold": 0.5}),
        MetricBinding(metric="native.json_schema", params={"schema": {"type": "wat"}}),
        MetricBinding(metric="native.json_schema", params={}),  # neither schema nor field
        MetricBinding(metric="vendor.unknown"),
        MetricBinding(metric="native.exact_match"),
        MetricBinding(metric="native.exact_match"),  # duplicate of the previous one
    ]
    with pytest.raises(BindingValidationError) as info:
        registry.validate(bindings)
    problems = [str(p) for p in info.value.problems]
    assert len(problems) == 6, problems
    joined = "\n".join(problems)
    for expected in (
        "Additional properties are not allowed ('case_sensitiv'",
        "cannot decide a 'boolean' value",
        "invalid JSON Schema",
        "is not valid under any of the given schemas",
        "unknown evaluator 'vendor.unknown'",
        "duplicate binding",
    ):
        assert expected in joined


def test_metric_needing_an_unexposed_observation_is_refused_up_front() -> None:
    class NeedsRetrieval(Evaluator):
        manifest = ExactMatch.manifest.model_copy(
            update={
                "evaluator_id": "test.needs_retrieval",
                "requires": (FieldRequirement(path="execution.retrieved_context"),),
            }
        )

        async def evaluate(self, view, ctx):  # type: ignore[no-untyped-def]
            return EvaluationOutcome.ok("boolean", True)

    registry = EvaluatorRegistry.with_native()
    registry.register(NeedsRetrieval)
    blind_app = ApplicationSpec(application_id="bot", runner="cli", target="bot.py")
    rag_app = ApplicationSpec(
        application_id="rag",
        runner="http",
        target="u",
        output_binding={"retrieved_context": "/docs"},
    )
    binding = [MetricBinding(metric="test.needs_retrieval")]
    with pytest.raises(BindingValidationError, match="does not expose it"):
        registry.validate(binding, application=blind_app)
    assert registry.validate(binding, application=rag_app)


def test_local_evaluator_files_require_explicit_trust(tmp_path: Path) -> None:
    path = REPO_ROOT / "examples" / "evaluators" / "refund_window.py"
    registry = EvaluatorRegistry.with_native()
    with pytest.raises(PolicyError, match="trust"):
        registry.load_local_file(path, trusted=False)
    assert [m.evaluator_id for m in registry.load_local_file(path, trusted=True)] == [
        "acme.refund_window"
    ]
    empty = tmp_path / "empty.py"
    empty.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(RegistryError, match="EVALUATORS"):
        EvaluatorRegistry().load_local_file(empty, trusted=True)
