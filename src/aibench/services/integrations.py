"""What external integrations can do here, where they send data, and whether they can run
now (§9, 17-T4). Shared by `aibench integrations list`, chat (`/integrations` and the
assistant's `list_integrations` tool) and planning.

Nothing here starts plugin code or contacts a service. Plugins are found from their
installed metadata in the environments the policy approves. An integration whose plugin,
destination approval or credentials are missing is reported as unavailable with the exact
reasons, never as working. No integration here has been verified against a live service,
and each says so.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aibench.core.errors import AibenchError
from aibench.registry.discovery import discover_plugins, environment_site_paths
from aibench.security.policy import ExecutionPolicy, egress_denials, policy_matches

OPENAI_BASE_URL = "https://api.openai.com/v1"
_LIVE = (
    "not verified against the live service (no authorized credentials); local contract tests only"
)


def _plugin_environments(policy: ExecutionPolicy | None) -> dict[str, str]:
    """Installed aibench plugin distributions -> the approved interpreter providing them."""
    found: dict[str, str] = {}
    for python in policy.allowed_plugin_environments if policy else ():
        if not Path(python).is_file():
            continue
        try:
            plugins = discover_plugins(environment_site_paths(Path(python)))
        except (AibenchError, OSError):
            continue
        for plugin in plugins:
            found.setdefault(plugin.distribution, python)
    return found


def _secret_problems(
    refs: list[str], policy: ExecutionPolicy | None, env: Mapping[str, str]
) -> list[str]:
    problems = []
    for ref in refs:
        if policy is not None and ref not in policy.allowed_secret_refs:
            problems.append(f"secret {ref} is not allowed by the policy")
        name = ref.partition(":")[2]
        if ref.startswith("env:") and not env.get(name):
            problems.append(f"credential {ref} is not set")
    return problems


def _missing_plugin(distribution: str, found: str | None) -> list[str]:
    if found:
        return []
    return [
        (
            f"plugin {distribution} is not installed in an approved plugin environment "
            "(allowed_plugin_environments)"
        )
    ]


def integrations(
    policy: ExecutionPolicy | None,
    *,
    environ: Mapping[str, str] | None = None,
    langfuse_host: str | None = None,
    openai_base_url: str = OPENAI_BASE_URL,
) -> list[dict[str, Any]]:
    env = dict(environ if environ is not None else os.environ)
    langfuse_host = langfuse_host or env.get("LANGFUSE_HOST")
    plugins = _plugin_environments(policy)
    no_policy = ["no policy given: nothing is approved"] if policy is None else []

    def evaluator_problem(pattern: str) -> list[str]:
        if policy is None or policy_matches(pattern, policy.allowed_evaluators):
            return []
        return [f"{pattern} evaluators are not allowed (allowed_evaluators)"]

    oss_env = plugins.get("aibench-openai-evals-oss")
    oss_problems = no_policy + _missing_plugin("aibench-openai-evals-oss", oss_env)
    oss_problems += evaluator_problem("openai_evals_oss.match")

    api_env = plugins.get("aibench-openai-evals-api")
    api_key = "env:OPENAI_API_KEY"
    api_problems = no_policy + _missing_plugin("aibench-openai-evals-api", api_env)
    api_problems += evaluator_problem("openai_evals_api.criterion")
    if policy is not None:
        api_problems += egress_denials(
            policy, openai_base_url, sends="case inputs, outputs and references"
        )
    api_problems += _secret_problems([api_key], policy, env)

    lf_keys = ["env:LANGFUSE_PUBLIC_KEY", "env:LANGFUSE_SECRET_KEY"]
    lf_problems = list(no_policy)
    if not langfuse_host:
        lf_problems.append("no Langfuse host configured (LANGFUSE_HOST or --host)")
    elif policy is not None:
        lf_problems += egress_denials(policy, langfuse_host, sends="metric results")
    lf_problems += _secret_problems(lf_keys, policy, env)

    def status(problems: list[str]) -> dict[str, Any]:
        return {"available": not problems, "reasons": problems}

    return [
        {
            "id": "openai_evals_oss",
            "name": "openai/evals (open-source framework)",
            "plugin": "aibench-openai-evals-oss",
            "upstream": "evals==3.0.1.post1",
            "kind": ["evaluator", "delegated_suite"],
            "modes": [
                {
                    "mode": "recorded_replay",
                    "how": "bind openai_evals_oss.<type> metrics",
                    "runs_application": False,
                    "supported": True,
                },
                {
                    "mode": "live_bridge",
                    "how": "aibench openai-evals-oss run",
                    "runs_application": True,
                    "supported": True,
                },
            ],
            "eval_types": ["match", "includes", "fuzzy_match", "json_match"],
            "unsupported": [
                "model-graded, solver, multi-turn and tool evals",
                "replay of a request that differs from the recorded input, or a follow-up",
            ],
            "data_destinations": [],
            "credentials": [],
            "plugin_environment": oss_env,
            "status": status(oss_problems),
            "live_verification": "runs locally; no service involved",
        },
        {
            "id": "openai_evals_api",
            "name": "OpenAI Evals API (hosted)",
            "plugin": "aibench-openai-evals-api",
            "upstream": "openai==3.19.2",
            "kind": ["remote_job"],
            "modes": [
                {
                    "mode": "stored_output_grading",
                    "how": "aibench openai-evals-api submit / status / fetch / cancel / resume",
                    "runs_application": False,
                    "supported": True,
                },
                {
                    "mode": "model_generation",
                    "how": None,
                    "runs_application": False,
                    "supported": False,
                    "why": "not implemented: only recorded outputs are graded",
                },
            ],
            "graders": ["string_check", "text_similarity", "label_model (model judge)"],
            "unsupported": ["completions/responses data sources", "{{sample.*}} templates"],
            "data_destinations": [
                {
                    "url": openai_base_url,
                    "sends": "case inputs, recorded outputs, reference answers",
                },
            ],
            "credentials": [api_key],
            "plugin_environment": api_env,
            "status": status(api_problems),
            "live_verification": _LIVE,
        },
        {
            "id": "langfuse",
            "name": "Langfuse",
            "plugin": None,
            "upstream": "Langfuse public API v4 endpoints (shapes from langfuse==4.15.6)",
            "kind": ["dataset_connector", "trace_importer", "result_exporter"],
            "modes": [
                {
                    "mode": "import-dataset",
                    "how": "aibench langfuse import-dataset",
                    "runs_application": False,
                    "supported": True,
                },
                {
                    "mode": "import-traces",
                    "how": "aibench langfuse import-traces",
                    "runs_application": False,
                    "supported": True,
                },
                {
                    "mode": "export-scores",
                    "how": "aibench langfuse export-scores",
                    "runs_application": False,
                    "supported": True,
                },
            ],
            "unsupported": ["metric evaluation (import/export only)", "deprecated v3 dataset runs"],
            "data_destinations": [
                {
                    "url": langfuse_host,
                    "sends": "credentials; trace IDs; exported metric results and IDs",
                },
            ],
            "credentials": lf_keys,
            "plugin_environment": None,
            "status": status(lf_problems),
            "live_verification": _LIVE,
        },
    ]
