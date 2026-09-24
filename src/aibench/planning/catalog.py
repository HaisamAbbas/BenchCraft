"""The planner's view of installed evaluators, with deterministic eligibility (§8, 07-T2).

§8: the LLM "ranks eligible options and explains tradeoffs"; the harness "computes
eligibility and resolves installed IDs". Every option here comes from an installed
manifest; eligibility is decided from evidence, never by a model:

- an `execution.*` field the metric reads must be declared or observed in the profile;
- a `case.*` field must be usable in at least one dataset case;
- the policy must allow the evaluator (including model-backed evaluators: data egress);
- required parameters the plan must supply are listed (the planner may not invent them).

Concepts (what an objective asks about) are derived from what a metric *reads*, so a new
evaluator is classified by its requirements, not by its name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aibench.core.models import EvaluatorManifest, FieldRequirement, deep_unfreeze
from aibench.inspection.dataset_summary import DatasetSummary
from aibench.inspection.profile import ApplicationProfile
from aibench.registry import EvaluatorRegistry
from aibench.security.policy import ExecutionPolicy, evaluator_denials

CONCEPTS: dict[str, str] = {
    "correctness": "the answer matches the reviewed reference answer",
    "groundedness": "the answer is supported by the passages the application actually retrieved",
    "format": "the output satisfies a declared structure, such as a JSON Schema",
    "tool_use": "the application called the expected tools",
    "expectations": "the output satisfies per-case domain expectations",
    "latency": "end-to-end response time",
    "reliability": "application errors, timeouts and failed calls",
}
# Recorded for every execution by the engine; no evaluator is needed.
ENGINE_RECORDED = frozenset({"latency", "reliability"})

# Keyword patterns per concept for reading objective text (the template planner, and a
# cross-check on model-assigned concepts). Regexes over lower-cased text, whole words.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "correctness": (r"correct(ness)?", r"accura(te|cy)", r"wrong", r"incorrect"),
    "groundedness": (
        r"faithful(ness)?",
        r"grounded(ness)?",
        r"hallucinat\w*",
        r"unsupported claims?",
        r"cit(e|es|ed|ation|ations)",
        r"retrieved passages?",
        r"sources?",
    ),
    "format": (r"json", r"schema", r"output format", r"format(ted|ting)?", r"structured output"),
    "tool_use": (r"tools?", r"tool calls?", r"function calls?", r"agents?"),
    "expectations": (r"expectations?", r"business rules?", r"policy rules?"),
    "latency": (r"latency", r"response times?", r"slow", r"speed"),
    "reliability": (r"errors?", r"reliab\w*", r"crash\w*", r"timeouts?", r"failures?"),
}
# A keyword preceded (within three words) by one of these is not an objective.
_NEGATIONS = r"(?:no|not|don't|dont|do not|never|without|ignore|except)"


def concepts_in(text: str) -> tuple[str, ...]:
    """Concepts an objective's wording names, skipping negated mentions ("don't care about
    latency"). Deterministic; a hint, not understanding."""
    lowered = text.lower()
    found = []
    for concept, patterns in _KEYWORDS.items():
        for pattern in patterns:
            hit = False
            for match in re.finditer(rf"\b(?:{pattern})\b", lowered):
                before = lowered[: match.start()].split()[-3:]
                if not re.search(rf"\b{_NEGATIONS}\b", " ".join(before)):
                    hit = True
                    break
            if hit:
                found.append(concept)
                break
    return tuple(found)


# Evaluators whose concept cannot be read from their requirements alone.
_DECLARED_CONCEPTS: dict[str, tuple[str, ...]] = {"native.json_schema": ("format",)}


def concepts_for(
    manifest: EvaluatorManifest, requires: tuple[FieldRequirement, ...]
) -> tuple[str, ...]:
    found: list[str] = list(_DECLARED_CONCEPTS.get(manifest.evaluator_id, ()))
    for requirement in requires:
        path = requirement.path
        if path == "case.reference.answer":
            found.append("correctness")
        elif path == "execution.retrieved_context":
            found.append("groundedness")
        elif path in ("execution.tool_events", "case.reference.tools"):
            found.append("tool_use")
        elif path.startswith("case.expectations."):
            found.append("expectations")
    return tuple(dict.fromkeys(found))


@dataclass
class MetricOption:
    metric: str  # "evaluator_id@version"
    evaluator_id: str
    version: str
    description: str
    concepts: tuple[str, ...]
    requires: tuple[str, ...]
    value_kind: str
    uses_models: bool
    default_rule: dict[str, object] | None
    required_params: tuple[str, ...]
    limitations: tuple[str, ...]
    eligible: bool
    reasons: list[str] = field(default_factory=list)  # why not eligible
    usable_cases: int | None = None  # dataset cases with every case.* field it reads

    def as_dict(self) -> dict[str, object]:
        return {
            "metric": self.metric,
            "description": self.description,
            "concepts": list(self.concepts),
            "requires": list(self.requires),
            "value_kind": self.value_kind,
            "uses_models": self.uses_models,
            "default_rule": self.default_rule,
            "required_params": list(self.required_params),
            "limitations": list(self.limitations),
            "eligible": self.eligible,
            "reasons": self.reasons,
            "usable_cases": self.usable_cases,
        }


def _required_params(manifest: EvaluatorManifest) -> tuple[str, ...]:
    """Parameters the plan must supply. Alternatives declared with `oneOf`/`anyOf` branches
    of `required` are joined with "|" (e.g. "schema|schema_field": one of them)."""
    schema = deep_unfreeze(manifest.parameters_schema) or {}
    if not isinstance(schema, dict):
        return ()
    required = [str(r) for r in schema.get("required", [])]
    for keyword in ("oneOf", "anyOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            options = [
                "+".join(str(r) for r in branch.get("required", []))
                for branch in branches
                if isinstance(branch, dict) and branch.get("required")
            ]
            if options:
                required.append("|".join(options))
    return tuple(required)


def missing_params(required: tuple[str, ...], supplied: dict[str, object]) -> list[str]:
    """Required entries (see `_required_params`) the supplied parameters do not satisfy."""
    missing = []
    for entry in required:
        alternatives = [alt.split("+") for alt in entry.split("|")]
        if not any(all(name in supplied for name in alt) for alt in alternatives):
            missing.append(entry)
    return missing


def build_catalog(
    registry: EvaluatorRegistry,
    profile: ApplicationProfile,
    dataset: DatasetSummary,
    policy: ExecutionPolicy,
) -> list[MetricOption]:
    options = []
    for manifest in registry.manifests():
        # Declared requirements; parameter-dependent extras (e.g. a schema field) are
        # checked again when the plan is validated. No evaluator is instantiated here.
        requires = manifest.requires
        reasons = list(evaluator_denials(policy, [manifest]))
        usable: int | None = None
        for requirement in requires:
            head, _, name = requirement.path.partition(".")
            if head == "execution" and name != "output" and not profile.available(name):
                reasons.append(
                    f"reads {requirement.path}, which application {profile.application_id!r} "
                    "does not expose (not declared or observed)"
                )
            elif head == "execution" and requirement.non_empty and name in profile.always_empty:
                reasons.append(
                    f"needs a non-empty {requirement.path}, which was empty in every recorded "
                    f"execution of {profile.application_id!r}"
                )
            if head == "case":
                count = dataset.usable(requirement.path, non_empty=requirement.non_empty)
                usable = count if usable is None else min(usable, count)
                if count == 0:
                    reasons.append(f"reads {requirement.path}, which no dataset case has")
        rule = manifest.default_rule.model_dump(mode="json") if manifest.default_rule else None
        options.append(
            MetricOption(
                metric=f"{manifest.evaluator_id}@{manifest.version}",
                evaluator_id=manifest.evaluator_id,
                version=manifest.version,
                description=manifest.description,
                concepts=concepts_for(manifest, requires),
                requires=tuple(r.path for r in requires),
                value_kind=manifest.value_kind,
                uses_models=manifest.uses_models,
                default_rule=rule,
                required_params=_required_params(manifest),
                limitations=tuple(manifest.limitations),
                eligible=not reasons,
                reasons=reasons,
                usable_cases=usable if usable is not None else dataset.case_count,
            )
        )
    return sorted(options, key=lambda o: (not o.evaluator_id.startswith("native."), o.metric))
