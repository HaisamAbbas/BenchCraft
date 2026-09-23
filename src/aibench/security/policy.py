"""Execution policy (§16, 06-T2): what a run may do, decided by code before any dispatch.

The policy is independent of any plan author, human or LLM: a plan asks, the policy
decides. Every denial is collected, and a run with any denial dispatches nothing (06-G2).
Defaults deny everything a run could need special trust for:

- no trusted-local (subprocess) execution, unless the user grants it explicitly;
- only HTTP targets on loopback, unless origins are allowed;
- no application with declared external effects;
- only built-in `native.*` evaluators, and no model-backed evaluator — those send case data
  to a judge provider (data egress);
- no plugin environments, no extra plugin import paths and no secrets, unless listed;
- data scope: when `data_roots` is set, the plan's dataset and application config must
  resolve inside one of them (unset means local files are not restricted).

Relative paths in a policy file resolve against the policy file's directory.
"""

from __future__ import annotations

import fnmatch
import os
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field

from aibench.core.models import (
    ApplicationSpec,
    CliTransport,
    EffectLevel,
    EvaluatorManifest,
    FrozenModel,
    HttpTransport,
)
from aibench.core.plans import BudgetLimits, ExecutablePlan
from aibench.security.endpoints import is_loopback, origin_of

_EFFECT_ORDER = (EffectLevel.NONE, EffectLevel.REVERSIBLE, EffectLevel.IRREVERSIBLE)


class ExecutionPolicy(FrozenModel):
    allowed_applications: tuple[str, ...] = ("*",)  # application_id glob patterns
    allow_trusted_local: bool = False
    allowed_http_origins: tuple[str, ...] = ()  # beyond loopback, e.g. "https://rag.internal"
    max_effects: EffectLevel = EffectLevel.NONE
    allowed_evaluators: tuple[str, ...] = ("native.*",)  # evaluator_id glob patterns
    allow_model_evaluators: bool = False
    allowed_plugin_environments: tuple[str, ...] = ()  # interpreter paths
    # Extra import paths a plugin environment may add (they run code in the worker).
    allowed_plugin_paths: tuple[str, ...] = ()
    data_roots: tuple[str, ...] = ()  # directories the plan's data may come from
    allowed_secret_refs: tuple[str, ...] = ()  # e.g. "env:RAG_TOKEN"
    ceilings: BudgetLimits = Field(default_factory=BudgetLimits)

    def with_trusted_local(self, granted: bool) -> ExecutionPolicy:
        """The user's explicit per-run grant (`--trust-local-app`)."""
        return self if not granted else self.model_copy(update={"allow_trusted_local": True})

    def resolved_against(self, base: Path) -> ExecutionPolicy:
        """Make every path in the policy absolute, relative to the policy file's directory."""

        def absolute(paths: tuple[str, ...]) -> tuple[str, ...]:
            return tuple(str((base / p).resolve()) for p in paths)

        return self.model_copy(
            update={
                "allowed_plugin_environments": absolute(self.allowed_plugin_environments),
                "allowed_plugin_paths": absolute(self.allowed_plugin_paths),
                "data_roots": absolute(self.data_roots),
            }
        )


def _matches(value: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in patterns)


def _same_path(a: str, b: str) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _inside(path: Path, root: str) -> bool:
    return path.resolve().is_relative_to(Path(root).resolve())


def _plan_path(plan_dir: Path, value: str) -> Path:
    return Path(value) if Path(value).is_absolute() else plan_dir / value


def application_denials(policy: ExecutionPolicy, spec: ApplicationSpec) -> list[str]:
    denials = []
    if not _matches(spec.application_id, policy.allowed_applications):
        denials.append(f"application {spec.application_id!r} is not an approved target")
    if _EFFECT_ORDER.index(spec.effects) > _EFFECT_ORDER.index(policy.max_effects):
        denials.append(
            f"application declares {spec.effects.value} effects; policy allows at most "
            f"{policy.max_effects.value}"
        )
    transport = spec.transport
    secret_refs: list[str] = []
    if isinstance(transport, CliTransport):
        if not policy.allow_trusted_local:
            denials.append(
                "executing a local application requires trusted-local mode "
                "(--trust-local-app or allow_trusted_local in the policy)"
            )
        secret_refs = list(transport.secret_env.values())
    elif isinstance(transport, HttpTransport):
        allowed = {origin_of(o) for o in policy.allowed_http_origins}
        for url in (
            transport.url,
            transport.healthcheck_url,
            transport.reset_url,
            *transport.allowed_endpoints,
        ):
            if url is None:
                continue
            origin = origin_of(url)
            if not is_loopback(urlsplit(url).hostname or "") and origin not in allowed:
                denials.append(f"HTTP origin {origin} is not an approved target")
        secret_refs = [header.ref for header in transport.secret_headers.values()]
    denials.extend(
        f"secret {ref} is not allowed by the policy"
        for ref in secret_refs
        if ref not in policy.allowed_secret_refs
    )
    return sorted(set(denials))


def evaluator_denials(policy: ExecutionPolicy, manifests: Iterable[EvaluatorManifest]) -> list[str]:
    denials = []
    for manifest in manifests:
        label = f"{manifest.evaluator_id}@{manifest.version}"
        if not _matches(manifest.evaluator_id, policy.allowed_evaluators):
            denials.append(f"evaluator {label} is not allowed by the policy")
        if manifest.uses_models and not policy.allow_model_evaluators:
            denials.append(
                f"evaluator {label} sends case data to a model judge; the policy does not "
                "allow model-backed evaluators"
            )
    return denials


def plan_denials(policy: ExecutionPolicy, plan: ExecutablePlan, plan_dir: Path) -> list[str]:
    denials = []
    if policy.data_roots:
        for label, value in (("dataset", plan.dataset), ("application config", plan.application)):
            if not any(_inside(_plan_path(plan_dir, value), r) for r in policy.data_roots):
                denials.append(f"{label} {value} is outside the policy's data_roots")
    for env in plan.plugin_environments:
        python = str(_plan_path(plan_dir, env.python))
        if not any(_same_path(python, allowed) for allowed in policy.allowed_plugin_environments):
            denials.append(f"plugin environment {env.python} is not allowed by the policy")
        denials.extend(
            f"plugin path {extra} is not allowed by the policy"
            for extra in env.paths
            if not any(
                _same_path(str(_plan_path(plan_dir, extra)), allowed)
                for allowed in policy.allowed_plugin_paths
            )
        )
        denials.extend(
            f"secret {ref} is not allowed by the policy"
            for ref in env.secret_env.values()
            if ref not in policy.allowed_secret_refs
        )
    ceilings, budgets = policy.ceilings, plan.budgets
    for field in (
        "max_application_calls",
        "max_evaluator_calls",
        "max_judge_tokens",
        "max_wall_seconds",
        "max_cost_usd",
    ):
        ceiling = getattr(ceilings, field)
        if ceiling is None:
            continue
        requested = getattr(budgets, field)
        if requested is None:
            denials.append(f"policy caps {field} at {ceiling}; the plan must set it")
        elif requested > ceiling:
            denials.append(f"plan {field}={requested} exceeds the policy ceiling {ceiling}")
    return denials
