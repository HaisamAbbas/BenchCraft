"""Evaluator registry (§9, 04-T2): versioned namespaced IDs, core-schema compatibility,
binding validation and applicability — all checked before anything is evaluated (04-G2).

In-process execution is limited to first-party native evaluators and local evaluator
files the user explicitly trusts. Installed third-party plugins are discovered from
entry-point metadata without importing them (`aibench.registry.discovery`); their manifests
are read by a subprocess worker, and they are never instantiated in this process.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from aibench.core.errors import AibenchError, PolicyError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    SCHEMA_VERSION,
    ApplicationSpec,
    EvaluatorManifest,
    FieldRequirement,
    MetricBinding,
    deep_unfreeze,
)
from aibench.evaluators.native import NATIVE_EVALUATORS, NATIVE_PLUGIN_ID
from aibench.evaluators.protocol import EvaluationView, Evaluator, rule_for

RESERVED_NAMESPACE = "native."
_SPEC = re.compile(
    r"^(?P<id>[a-z][a-z0-9_]*\.[a-z][a-z0-9_.]*?)(?:@(?P<version>\d+(?:\.\d+){0,2}))?$"
)
# Execution fields that exist only when the application exposes them (§7).
_OBSERVATION_FIELDS = ("retrieved_context", "tool_events", "usage", "cost")


class RegistryError(AibenchError):
    """An evaluator reference cannot be resolved or is not usable here."""


@dataclass(frozen=True)
class ResolvedMetric:
    binding: MetricBinding
    manifest: EvaluatorManifest
    factory: type[Evaluator]
    binding_hash: str
    requirements: tuple[FieldRequirement, ...]


@dataclass(frozen=True)
class BindingProblem:
    metric: str
    problem: str

    def __str__(self) -> str:
        return f"{self.metric}: {self.problem}"


class BindingValidationError(AibenchError):
    def __init__(self, problems: Sequence[BindingProblem]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(str(p) for p in problems))


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def schema_compatible(core_schema: str, version: str = SCHEMA_VERSION) -> bool:
    """Evaluate a comma-separated range such as `>=1.0.0,<2.0.0` against `version`."""
    current = _version_tuple(version)
    for clause in (c.strip() for c in core_schema.split(",") if c.strip()):
        match = re.fullmatch(r"(>=|<=|==|>|<)\s*(\d+(?:\.\d+){0,2})", clause)
        if match is None:
            return False
        op, bound = match.group(1), _version_tuple(match.group(2))
        bound = bound + (0,) * (3 - len(bound))
        ok = {
            ">=": current >= bound,
            "<=": current <= bound,
            "==": current == bound,
            ">": current > bound,
            "<": current < bound,
        }[op]
        if not ok:
            return False
    return True


class EvaluatorRegistry:
    def __init__(self) -> None:
        self._factories: dict[tuple[str, str], type[Evaluator]] = {}
        self._external: dict[tuple[str, str], EvaluatorManifest] = {}

    @classmethod
    def with_native(cls) -> EvaluatorRegistry:
        registry = cls()
        for factory in NATIVE_EVALUATORS:
            registry._add(factory, allow_native=True)
        return registry

    # ------------------------------------------------------------------ registration

    def register(self, factory: type[Evaluator]) -> None:
        self._add(factory, allow_native=False)

    def _check_namespace(self, manifest: EvaluatorManifest, *, allow_native: bool) -> None:
        if manifest.evaluator_id.startswith(RESERVED_NAMESPACE) and not (
            allow_native and manifest.plugin_id == NATIVE_PLUGIN_ID
        ):
            raise RegistryError(
                f"{manifest.evaluator_id}: the {RESERVED_NAMESPACE!r} namespace is reserved for "
                "built-in evaluators"
            )
        key = (manifest.evaluator_id, manifest.version)
        if key in self._factories or key in self._external:
            raise RegistryError(f"{manifest.evaluator_id}@{manifest.version} is already registered")

    def _add(self, factory: type[Evaluator], *, allow_native: bool) -> None:
        manifest = factory.manifest
        self._check_namespace(manifest, allow_native=allow_native)
        key = (manifest.evaluator_id, manifest.version)
        if manifest.requires_worker:
            raise RegistryError(
                f"{manifest.evaluator_id} declares requires_worker; it cannot run in-process"
            )
        self._factories[key] = factory

    def register_external(self, manifest: EvaluatorManifest) -> None:
        """Record a third-party manifest read by the discovery worker: listable and
        validatable, never executable in this process."""
        self._check_namespace(manifest, allow_native=False)
        external = manifest.model_copy(update={"requires_worker": True})
        self._external[(external.evaluator_id, external.version)] = external

    def load_local_file(self, path: Path, *, trusted: bool) -> list[EvaluatorManifest]:
        """Import evaluator classes from a project file. This executes the file, so it
        requires explicit trust, like running a local application (§16)."""
        if not trusted:
            raise PolicyError(
                f"loading {path} executes its code; pass explicit trust (--trust-local-code)"
            )
        if not path.is_file():
            raise RegistryError(f"evaluator file not found: {path}")
        # Unique per resolved path, so two same-named files never share a module slot.
        module_name = f"aibench_local_{path.stem}_{content_hash(str(path.resolve()))[7:19]}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise RegistryError(f"cannot load evaluator file {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        factories = getattr(module, "EVALUATORS", None)
        if not factories:
            raise RegistryError(f"{path} must define EVALUATORS = (EvaluatorClass, ...)")
        for factory in factories:
            if not (isinstance(factory, type) and issubclass(factory, Evaluator)):
                raise RegistryError(f"{path}: {factory!r} is not an Evaluator subclass")
            self.register(factory)
        return [f.manifest for f in factories]

    # ------------------------------------------------------------------ lookup

    def manifests(self) -> list[EvaluatorManifest]:
        items = [f.manifest for f in self._factories.values()] + list(self._external.values())
        return sorted(items, key=lambda m: (m.evaluator_id, _version_tuple(m.version)))

    def resolve(self, reference: str) -> tuple[EvaluatorManifest, type[Evaluator]]:
        """`id` (latest), `id@1` (latest 1.x), `id@1.2` or `id@1.2.3`."""
        match = _SPEC.match(reference)
        if match is None:
            raise RegistryError(
                f"invalid metric reference {reference!r}; expected namespace.name[@version]"
            )
        evaluator_id, wanted = match.group("id"), match.group("version")
        known = [m for m in self.manifests() if m.evaluator_id == evaluator_id]
        if not known:
            raise RegistryError(f"unknown evaluator {evaluator_id!r}")
        if wanted is not None:
            prefix = _version_tuple(wanted)
            known = [m for m in known if _version_tuple(m.version)[: len(prefix)] == prefix]
            if not known:
                raise RegistryError(f"no version of {evaluator_id} matches @{wanted}")
        manifest = known[-1]
        if not schema_compatible(manifest.core_schema):
            raise RegistryError(
                f"{evaluator_id}@{manifest.version} supports core schema {manifest.core_schema!r}, "
                f"not {SCHEMA_VERSION}"
            )
        factory = self._factories.get((manifest.evaluator_id, manifest.version))
        if factory is None:
            raise RegistryError(
                f"{evaluator_id}@{manifest.version} is a third-party plugin that must run in an "
                "isolated worker; worker execution is not available yet"
            )
        return manifest, factory

    # ------------------------------------------------------------------ validation

    def validate(
        self,
        bindings: Iterable[MetricBinding],
        *,
        application: ApplicationSpec | None = None,
    ) -> list[ResolvedMetric]:
        """Resolve and check every binding; raise `BindingValidationError` listing *all*
        problems. Nothing is evaluated if any binding is invalid (04-G2)."""
        resolved: list[ResolvedMetric] = []
        problems: list[BindingProblem] = []
        seen: set[str] = set()
        for binding in bindings:
            try:
                manifest, factory = self.resolve(binding.metric)
            except RegistryError as exc:
                problems.append(BindingProblem(binding.metric, str(exc)))
                continue
            evaluator = factory()
            issues = evaluator.validate_binding(binding)
            if issues:
                problems.extend(BindingProblem(binding.metric, issue) for issue in issues)
                continue
            requirements = evaluator.required_fields(deep_unfreeze(binding.params) or {})
            requirement_issues = _requirement_problems(requirements)
            if requirement_issues:
                problems.extend(BindingProblem(binding.metric, i) for i in requirement_issues)
                continue
            problems.extend(
                BindingProblem(binding.metric, issue)
                for issue in _applicability_problems(requirements, application)
            )
            # Identity of what will actually run — not how the reference was spelled — so
            # `x`, `x@1` and `x@1.0.0` with the default rule are recognised as duplicates.
            effective_rule = rule_for(binding, manifest)
            binding_hash = content_hash(
                {
                    "evaluator_id": manifest.evaluator_id,
                    "version": manifest.version,
                    "params": deep_unfreeze(binding.params) or {},
                    "rule": effective_rule.model_dump(mode="json") if effective_rule else None,
                }
            )
            if binding_hash in seen:
                problems.append(BindingProblem(binding.metric, "duplicate binding"))
                continue
            seen.add(binding_hash)
            resolved.append(ResolvedMetric(binding, manifest, factory, binding_hash, requirements))
        if problems:
            raise BindingValidationError(problems)
        return resolved


def _requirement_problems(requirements: Sequence[FieldRequirement]) -> list[str]:
    problems = []
    for requirement in requirements:
        issue = EvaluationView.path_problem(requirement.path)
        if issue:
            problems.append(issue)
        elif requirement.path == "execution.output" and requirement.non_empty:
            # An empty answer is the application's answer; skipping it as not applicable
            # would drop failures from the denominator.
            problems.append(
                "execution.output cannot be a non_empty requirement: score empty output, "
                "do not skip it"
            )
    return problems


def _applicability_problems(
    requirements: Sequence[FieldRequirement], application: ApplicationSpec | None
) -> list[str]:
    """A metric that needs an observation the application does not expose would be
    not_applicable for every case; refuse it up front instead of producing empty coverage."""
    if application is None:
        return []
    declared = deep_unfreeze(application.output_binding) or {}
    problems = []
    for requirement in requirements:
        head, _, name = requirement.path.partition(".")
        if head == "execution" and name in _OBSERVATION_FIELDS and not declared.get(name):
            problems.append(
                f"requires {requirement.path}, but application {application.application_id!r} "
                f"does not expose it (output_binding.{name} is not declared)"
            )
    return problems
