"""Registry resolution, schema compatibility and pre-execution binding validation
(04-T2, 04-G2)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
from aibench.registry.discovery import (
    dependency_lock_hash,
    environment_paths,
    plugin_paths_hash,
    worker_python_identity,
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


def test_worker_dependency_lock_hash_tracks_installed_versions(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    for root, version in ((first, "1.0"), (second, "2.0")):
        dist_info = root / "fixture_dependency-1.0.dist-info"
        dist_info.mkdir(parents=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: Fixture_Dependency\nVersion: {version}\n",
            encoding="utf-8",
        )

    first_hash = dependency_lock_hash([first])
    assert first_hash is not None
    assert first_hash == dependency_lock_hash([first])
    assert first_hash != dependency_lock_hash([second])


def test_worker_python_identity_reads_the_interpreter_runtime() -> None:
    assert worker_python_identity(Path(sys.executable)) is not None


def test_environment_paths_tracks_plain_pth_roots_without_running_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.registry import discovery

    site = tmp_path / "site"
    user_site = tmp_path / "user-site"
    editable = tmp_path / "editable-source"
    alternate = tmp_path / "alternate-source"
    site.mkdir()
    user_site.mkdir()
    editable.mkdir()
    alternate.mkdir()
    (site / "one.pth").write_text(str(editable) + "\n", encoding="utf-8")
    (site / "two.pth").write_text(str(alternate) + "\n", encoding="utf-8")
    (editable / "module.py").write_text("VALUE = 'first'\n", encoding="utf-8")
    (alternate / "module.py").write_text("VALUE = 'second'\n", encoding="utf-8")
    monkeypatch.setattr(
        discovery,
        "run_contained",
        lambda *_args, **_kwargs: SimpleNamespace(
            timed_out=False,
            returncode=0,
            stdout=json.dumps(
                {"site": [str(site), str(site)], "user": str(user_site), "stdlib": []}
            ).encode(),
            stderr=b"",
        ),
    )

    all_sites, imports, complete = environment_paths(Path(sys.executable))

    assert all_sites == [user_site, site, editable, alternate]
    assert imports == [user_site, site, editable, alternate]
    assert complete is True

    (site / "one.pth").write_text(str(alternate) + "\n", encoding="utf-8")
    (site / "two.pth").write_text(str(editable) + "\n", encoding="utf-8")
    _sites_after_reorder, imports_after_reorder, complete_after_reorder = environment_paths(
        Path(sys.executable)
    )
    assert complete_after_reorder is True
    assert imports_after_reorder == [user_site, site, alternate, editable]


def test_environment_paths_marks_executable_pth_opaque_without_running_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aibench.registry import discovery

    site = tmp_path / "site"
    site.mkdir()
    (site / "custom.pth").write_text("import sys; sys.path.append('elsewhere')\n", encoding="utf-8")
    monkeypatch.setattr(
        discovery,
        "run_contained",
        lambda *_args, **_kwargs: SimpleNamespace(
            timed_out=False,
            returncode=0,
            stdout=json.dumps(
                {"site": [str(site)], "user": str(tmp_path / "user"), "stdlib": []}
            ).encode(),
            stderr=b"",
        ),
    )

    _site_paths, imports, complete = environment_paths(Path(sys.executable))

    assert imports == [tmp_path / "user", site]
    assert complete is False


def test_plugin_import_path_identity_tracks_in_place_source_changes(tmp_path: Path) -> None:
    plugin_path = tmp_path / "local-plugin"
    plugin_path.mkdir()
    source = plugin_path / "judge.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")

    first_hash = plugin_paths_hash([plugin_path])
    assert first_hash is not None
    assert first_hash == plugin_paths_hash([plugin_path])
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert first_hash != plugin_paths_hash([plugin_path])


def test_plugin_import_path_identity_rejects_oversized_files_before_reading(
    tmp_path: Path,
) -> None:
    plugin_path = tmp_path / "large-plugin"
    plugin_path.mkdir()
    oversized = plugin_path / "large.bin"
    with oversized.open("wb") as stream:
        stream.truncate(64 * 1024 * 1024 + 1)
    assert plugin_paths_hash([plugin_path]) is None


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
