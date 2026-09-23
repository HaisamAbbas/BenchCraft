"""Binding validation shared by evaluators and the registry (04-G2): parameters against the
manifest's JSON Schema, and decision rules against the metric's value kind."""

from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import NoSuchResource

from aibench.core.models import DecisionRule, EvaluatorManifest, deep_unfreeze


def _refuse_retrieval(uri: str) -> Any:
    raise NoSuchResource(ref=uri)  # type: ignore[call-arg]  # attrs alias, see below


# Schemas come from metric params and case data, so they are untrusted. Without an
# explicit registry, jsonschema falls back to a legacy resolver that fetches remote `$ref`
# URLs over the network. This registry refuses every retrieval; references inside the
# schema itself (`#/$defs/...`) still resolve.
# `retrieve`/`ref` are attrs field aliases: valid at runtime, invisible to mypy.
_NO_RETRIEVAL: Registry[Any] = Registry(retrieve=_refuse_retrieval)  # type: ignore[call-arg]

_RULES_BY_KIND: dict[str, tuple[str, ...]] = {
    "boolean": ("is_true",),
    "scalar": (">=", ">", "<=", "<", "=="),
    "category": ("in",),
}


def schema_problems(schema: Any) -> list[str]:
    """Problems with a JSON Schema document itself (Draft 2020-12)."""
    if not isinstance(schema, dict):
        return ["JSON Schema must be an object"]
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        return [f"invalid JSON Schema: {exc.message}"]
    return []


def instance_errors(
    schema: dict[str, Any], instance: Any, *, limit: int = 20
) -> list[dict[str, Any]]:
    """Validation errors for `instance`, sorted deterministically, at most `limit`."""
    validator = Draft202012Validator(schema, registry=_NO_RETRIEVAL)
    errors = sorted(
        validator.iter_errors(instance), key=lambda e: (list(e.absolute_path), e.message)
    )
    return [
        {"path": "/" + "/".join(str(p) for p in error.absolute_path), "message": error.message}
        for error in errors[:limit]
    ]


def check_params(manifest: EvaluatorManifest, params: dict[str, Any]) -> list[str]:
    schema = deep_unfreeze(manifest.parameters_schema) or {"type": "object", "maxProperties": 0}
    return [
        f"params{error['path'] if error['path'] != '/' else ''}: {error['message']}"
        for error in instance_errors(schema, params)
    ]


def check_rule(manifest: EvaluatorManifest, rule: DecisionRule | None) -> list[str]:
    if rule is None:
        return []
    allowed = _RULES_BY_KIND.get(manifest.value_kind, ())
    if rule.comparator not in allowed:
        allowed_text = ", ".join(allowed) or "none"
        return [
            (
                f"rule comparator {rule.comparator!r} cannot decide a "
                f"{manifest.value_kind!r} value (allowed: {allowed_text})"
            )
        ]
    return []


SCHEMA_WORKER_TIMEOUT_SECONDS = 5.0
_REGEX_KEYWORDS = ("pattern", "patternProperties")


class SchemaTimeout(Exception):
    """Validation exceeded its hard time limit (e.g. catastrophic regex backtracking)."""


def contains_regex(schema: Any) -> bool:
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(key in node for key in _REGEX_KEYWORDS):
                return True
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return False


def validate_untrusted(
    schema: dict[str, Any], instance: Any, *, timeout: float = SCHEMA_WORKER_TIMEOUT_SECONDS
) -> list[dict[str, Any]]:
    """`instance_errors` for an untrusted schema. Regex-free schemas run in-process; a
    schema with regex keywords runs in a killable worker with a hard timeout."""
    if not contains_regex(schema):
        return instance_errors(schema, instance)
    import os
    import sys

    from aibench.runners.process_tree import run_contained

    env = {k: os.environ[k] for k in ("PATH", "SYSTEMROOT", "TEMP", "TMP") if k in os.environ}
    result = run_contained(
        [sys.executable, "-m", "aibench.evaluators.schema_worker"],
        timeout=timeout,
        env=env,
        input_bytes=json.dumps({"schema": schema, "instance": instance}).encode("utf-8"),
    )
    if result.timed_out:
        raise SchemaTimeout(f"schema validation exceeded {timeout}s (worker killed)")
    if result.returncode != 0 or result.truncated:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(f"schema worker failed: {detail[-1] if detail else result.returncode}")
    errors: list[dict[str, Any]] = json.loads(result.stdout)
    return errors
