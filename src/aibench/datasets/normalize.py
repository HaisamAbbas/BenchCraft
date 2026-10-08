"""Dataset shorthand normalization (§6). Converts the brief's shorthand JSONL examples into
the stricter internal `BenchmarkCase` schema, reporting line-precise errors and warnings.

Every malformed input on a line — whether caught by an explicit check here or by the
underlying Pydantic model construction (nested type mismatches, unexpected fields inside
`reference`/`provenance`, non-object `fixtures` entries, etc.) — must surface as our own
`ValidationError` carrying the line number, never as a raw `pydantic.ValidationError` or a
`TypeError`/`KeyError` bubbling out of this module. `ingest.py` also catches those framework
exceptions as a second line of defense, but the primary, well-messaged conversion happens
here, close to the data that caused it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from aibench.core.errors import ValidationError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    BenchmarkCase,
    Fixture,
    Provenance,
    ReferenceAnswer,
    ReferenceStatus,
    RepositoryFixture,
    ToolExpectation,
    ToolMatchMode,
)

# Top-level keys understood by the shorthand normalizer. Anything else must live under
# "extensions" so misspellings stay visible instead of silently becoming ignored data.
KNOWN_TOP_LEVEL_KEYS = {
    "case_id",
    "input",
    "expected_output",
    "context",
    "expected_tools",
    "repository",
    "reference",
    "expectations",
    "fixtures",
    "group_id",
    "metadata",
    "extensions",
    "provenance",
}

# `extensions` keys must be namespaced ("vendor.field" or "vendor:field") so a bare typo like
# "made_up_field" is as visible inside extensions as an unknown top-level key would be.
EXTENSION_KEY_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]*[.:][A-Za-z][A-Za-z0-9_]*$")


@dataclass
class NormalizationResult:
    case: BenchmarkCase | None
    warnings: list[str] = field(default_factory=list)


def _generate_case_id(raw: dict[str, Any], occurrence_index: int) -> str:
    digest = content_hash({"input": raw.get("input"), "occurrence": occurrence_index})
    short = digest.split(":", 1)[1][:12]
    return f"generated-{short}"


def _validate_extension_keys(extensions: Any, *, line: int) -> dict[str, Any]:
    if not isinstance(extensions, dict):
        raise ValidationError("`extensions` must be an object", line=line, field="extensions")
    for key in extensions:
        if not EXTENSION_KEY_PATTERN.match(key):
            raise ValidationError(
                f"extensions key {key!r} must be namespaced, e.g. 'vendor.field' or "
                "'vendor:field', so a misspelling stays visible",
                line=line,
                field="extensions",
            )
    return extensions


def _normalize_repository(raw_repo: Any, warnings: list[str], line: int) -> RepositoryFixture:
    if isinstance(raw_repo, str):
        repo = RepositoryFixture(path=raw_repo)
    elif isinstance(raw_repo, dict):
        repo = RepositoryFixture(
            path=raw_repo.get("path", ""),
            commit=raw_repo.get("commit"),
            setup_recipe=raw_repo.get("setup_recipe"),
            hidden_tests_ref=raw_repo.get("hidden_tests_ref"),
            success_criteria=raw_repo.get("success_criteria"),
        )
    else:
        raise ValidationError(
            "`repository` must be a string path or an object", line=line, field="repository"
        )
    if not repo.is_execution_ready:
        missing = [
            name
            for name, value in (
                ("commit", repo.commit),
                ("setup_recipe", repo.setup_recipe),
                ("hidden_tests_ref", repo.hidden_tests_ref),
                ("success_criteria", repo.success_criteria),
            )
            if not value
        ]
        warnings.append(
            "coding case is missing execution prerequisites and cannot be run yet: "
            + ", ".join(missing)
        )
    return repo


def _normalize_fixtures(fixtures_raw: Any, *, line: int) -> tuple[Fixture, ...]:
    if not isinstance(fixtures_raw, list):
        raise ValidationError("`fixtures` must be a list of objects", line=line, field="fixtures")
    fixtures: list[Fixture] = []
    for idx, entry in enumerate(fixtures_raw):
        if not isinstance(entry, dict) or "name" not in entry:
            raise ValidationError(
                f"fixtures[{idx}] must be an object with a 'name' field",
                line=line,
                field="fixtures",
            )
        try:
            fixtures.append(
                Fixture(
                    name=entry["name"],
                    content=entry.get("content"),
                    app_visible=entry.get("app_visible", False),
                )
            )
        except PydanticValidationError as exc:
            raise ValidationError(
                f"invalid fixtures[{idx}]: {exc}", line=line, field=f"fixtures[{idx}]"
            ) from exc
    return tuple(fixtures)


def normalize_case(raw: dict[str, Any], *, line: int, occurrence_index: int) -> NormalizationResult:
    """Normalize one parsed JSONL object into a `BenchmarkCase`.

    Raises `ValidationError` for structurally invalid input. Returns warnings for
    compatibility behaviors (legacy `context`, `expected_tools` proxy matching, incomplete
    coding prerequisites) so callers can surface them without failing validation.
    """
    if not isinstance(raw, dict):
        raise ValidationError("each dataset line must be a JSON object", line=line)

    unknown = set(raw.keys()) - KNOWN_TOP_LEVEL_KEYS
    if unknown:
        raise ValidationError(
            "unknown top-level field(s) "
            + ", ".join(sorted(unknown))
            + "; move custom data under a namespaced `extensions` object instead",
            line=line,
        )

    if "input" not in raw:
        raise ValidationError("missing required field `input`", line=line, field="input")

    warnings: list[str] = []

    case_id = raw.get("case_id")
    if not case_id:
        case_id = _generate_case_id(raw, occurrence_index)

    reference: ReferenceAnswer | None = None
    ref_kwargs: dict[str, Any] = {}
    if "reference" in raw:
        if not isinstance(raw["reference"], dict):
            raise ValidationError("`reference` must be an object", line=line, field="reference")
        ref_kwargs.update(raw["reference"])
    if "expected_output" in raw:
        ref_kwargs["answer"] = raw["expected_output"]
    if "context" in raw:
        ctx = raw["context"]
        if not isinstance(ctx, list):
            raise ValidationError("`context` must be a list of strings", line=line, field="context")
        ref_kwargs["context"] = tuple(ctx)
        warnings.append(
            "`context` was normalized to `reference.context`; this is a judge-only reference, "
            "not observed retrieval. Mark app-visible fixtures explicitly if the app should "
            "receive this content."
        )
    if "expected_tools" in raw:
        tools = raw["expected_tools"]
        if not isinstance(tools, list):
            raise ValidationError(
                "`expected_tools` must be a list of tool names", line=line, field="expected_tools"
            )
        ref_kwargs["tools"] = ToolExpectation(
            tool_names=tuple(tools), match_mode=ToolMatchMode.CONTAINS_ALL
        )
        warnings.append(
            "`expected_tools` was normalized to a `contains_all` tool-name expectation "
            "(extras allowed, no order enforced); choose `exact` or `ordered_subsequence` "
            "explicitly via `reference.tools` for a stricter contract."
        )
    if ref_kwargs:
        ref_kwargs.setdefault("status", ReferenceStatus.HUMAN_AUTHORED)

    repository: RepositoryFixture | None = None
    if "repository" in raw:
        repository = _normalize_repository(raw["repository"], warnings, line)

    fixtures = _normalize_fixtures(raw.get("fixtures", []), line=line)

    if "provenance" in raw and not isinstance(raw["provenance"], dict):
        raise ValidationError("`provenance` must be an object", line=line, field="provenance")

    extensions = _validate_extension_keys(raw.get("extensions", {}), line=line)

    expectations = raw.get("expectations", {})
    if not isinstance(expectations, dict):
        raise ValidationError("`expectations` must be an object", line=line, field="expectations")

    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValidationError("`metadata` must be an object", line=line, field="metadata")

    try:
        if ref_kwargs:
            reference = ReferenceAnswer(**ref_kwargs)
        provenance = (
            Provenance(**raw["provenance"]) if isinstance(raw.get("provenance"), dict) else Provenance()
        )
        case = BenchmarkCase(
            case_id=case_id,
            input=raw["input"],
            reference=reference,
            expectations=expectations,
            fixtures=fixtures,
            repository=repository,
            group_id=raw.get("group_id"),
            metadata=metadata,
            extensions=extensions,
            provenance=provenance,
            source_line=line,
        )
    except PydanticValidationError as exc:
        raise ValidationError(f"invalid case data: {exc}", line=line) from exc

    return NormalizationResult(case=case, warnings=warnings)
