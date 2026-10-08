"""Optional evaluator plugins: which ones exist, whether this project has them installed
and allowed, and the plugin environments its config declares.

An optional plugin runs only in its own Python environment (ADR 0004). A project uses one
through `plugin_environments` in its config (written by `aibench plugins install`), and the
policy must allow that interpreter, the plugin's evaluators, model-backed evaluators when
it has any, and each secret its workers receive. Only `install` changes anything: the
plugin environment, the project config and the policy, after the user confirms.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

from aibench import __version__
from aibench.config.model import PluginEnvironmentConfig
from aibench.config.resolve import resolve_config
from aibench.core.errors import AibenchError
from aibench.core.plans import PluginEnvironmentRef
from aibench.security.policy import ExecutionPolicy
from aibench.services.releases import checksums, fetch, release_source, wheel_name

CONFIG_NAMES = ("aibench.json", "aibench.toml")


@dataclass(frozen=True)
class OptionalPlugin:
    name: str
    distribution: str  # the adapter's distribution
    package: str  # the pinned framework it wraps
    evaluators: str  # evaluator-ID pattern
    source: str  # adapter source directory, relative to the aibench repository
    uses_models: bool
    summary: str
    metrics: tuple[str, ...]
    not_included: str


OPTIONAL_PLUGINS: dict[str, OptionalPlugin] = {
    "deepeval": OptionalPlugin(
        name="deepeval",
        distribution="aibench-deepeval",
        package="deepeval==4.2.5",
        evaluators="deepeval.*",
        source="plugins/deepeval",
        uses_models=True,
        summary="DeepEval's single-turn and conversation metrics, judged by a model you choose",
        metrics=(
            "faithfulness",
            "answer_relevancy",
            "contextual_precision",
            "contextual_recall",
            "contextual_relevancy",
            "hallucination",
            "bias",
            "toxicity",
            "pii_leakage",
            "misuse",
            "non_advice",
            "role_violation",
            "prompt_alignment",
            "summarization",
            "task_completion",
            "argument_correctness",
            "tool_correctness",
            "tool_permission",
            "exact_match",
            "pattern_match",
            "g_eval",
            "dag",
            "conversation_completeness",
            "knowledge_retention",
            "role_adherence",
            "goal_accuracy",
            "topic_adherence",
            "tool_use",
            "turn_relevancy",
            "turn_faithfulness",
            "turn_contextual_precision",
            "turn_contextual_recall",
            "turn_contextual_relevancy",
            "conversational_g_eval",
            "conversational_dag",
            "step_efficiency",
            "plan_quality",
            "plan_adherence",
            "agent_loop_detection",
        ),
        not_included="metrics needing images, audio or MCP servers, and arena metrics",
    ),
    "ragas": OptionalPlugin(
        name="ragas",
        distribution="aibench-ragas",
        package="ragas==0.4.3",
        evaluators="ragas.*",
        source="plugins/ragas",
        uses_models=True,
        summary="Ragas text faithfulness, the second ecosystem for comparisons",
        metrics=("faithfulness",),
        not_included="every other Ragas metric",
    ),
}


def config_path(project_root: Path) -> Path | None:
    return next((project_root / n for n in CONFIG_NAMES if (project_root / n).is_file()), None)


def configured_environments(project_root: Path) -> list[PluginEnvironmentConfig]:
    path = config_path(project_root)
    if path is None:
        return []
    resolved = resolve_config(config_path=path, cli_overrides={}, env={})
    return list(resolved.config.plugin_environments)


def session_plugins(
    project_root: Path,
) -> tuple[tuple[PluginEnvironmentRef, ...], dict[str, dict[str, Any]]]:
    """The project's plugin environments, as a session and plan use them, and their
    evaluators' default parameters by ID pattern. Paths resolve against the config file."""
    path = config_path(project_root)
    base = path.parent if path else project_root
    refs: list[PluginEnvironmentRef] = []
    defaults: dict[str, dict[str, Any]] = {}
    for entry in configured_environments(project_root):
        refs.append(
            PluginEnvironmentRef(
                python=str((base / entry.python).resolve()),
                paths=tuple(str((base / p).resolve()) for p in entry.paths),
                secret_env=dict(entry.secret_env),
                startup_timeout_seconds=entry.startup_timeout_seconds,
            )
        )
        for pattern, params in entry.default_params.items():
            defaults[pattern] = {**defaults.get(pattern, {}), **params}
    return tuple(refs), defaults


def plugin_status(project_root: Path, policy: ExecutionPolicy) -> list[dict[str, Any]]:
    """Each optional plugin: whether this project has an environment for it, whether the
    policy allows it, and how to enable it. Reads files only."""
    configured = {e.name: e for e in configured_environments(project_root)}
    path = config_path(project_root)
    base = path.parent if path else project_root
    rows = []
    for plugin in OPTIONAL_PLUGINS.values():
        entry = configured.get(plugin.name)
        python = (base / entry.python).resolve() if entry else None
        allowed_evaluators = any(
            fnmatchcase(f"{plugin.name}.x", pattern) for pattern in policy.allowed_evaluators
        )
        missing: list[str] = []
        if python is None:
            state = "not_installed"
        elif not python.is_file():
            state = "broken"
            missing.append(f"the interpreter {python} no longer exists")
        else:
            state = "installed"
        if entry is not None:
            if python not in {Path(p).resolve() for p in policy.allowed_plugin_environments}:
                missing.append("allowed_plugin_environments does not list its interpreter")
            if not allowed_evaluators:
                missing.append(f"allowed_evaluators does not include {plugin.evaluators}")
            if plugin.uses_models and not policy.allow_model_evaluators:
                missing.append("allow_model_evaluators is false (its metrics call a judge)")
            for ref in entry.secret_env.values():
                if ref not in policy.allowed_secret_refs:
                    missing.append(f"allowed_secret_refs does not include {ref}")
        rows.append(
            {
                "name": plugin.name,
                "summary": plugin.summary,
                "package": plugin.package,
                "evaluators": plugin.evaluators,
                "metrics": list(plugin.metrics),
                "not_included": plugin.not_included,
                "uses_models": plugin.uses_models,
                "state": state if not (state == "installed" and missing) else "not_allowed",
                "missing": missing,
                "enable": f"/plugins install {plugin.name}",
            }
        )
    return rows


# --------------------------------------------------------------------------- install


class PluginInstallError(AibenchError):
    """An optional plugin could not be installed or enabled."""


@dataclass
class InstallPlan:
    """Everything `install` will do, shown to the user before anything changes."""

    plugin: OptionalPlugin
    project_root: Path
    python: Path  # the plugin environment's interpreter
    create: Path | None  # the environment to create, or None to adopt an existing one
    source: Path | None  # the aibench checkout the adapter installs from, if any
    release: str | None  # otherwise the release (URL or directory) its wheels come from
    config_path: Path
    config: dict[str, Any]  # the project config after the change
    policy_path: Path | None
    policy: dict[str, Any] | None  # the policy after the change (None: unchanged)
    policy_changes: list[str]
    judge: dict[str, Any] | None
    secret_env: dict[str, str]
    judge_kept: bool = False  # the project already had a judge for this plugin; it is kept

    def summary(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin.name,
            "package": self.plugin.package,
            "environment": str(self.python),
            "creates_environment": self.create is not None,
            "installs_from": (
                f"source checkout {self.source}" if self.source else f"release {self.release}"
            ),
            "config": str(self.config_path),
            "policy": str(self.policy_path) if self.policy_path else None,
            "policy_changes": self.policy_changes,
            "judge": _judge_label(self.judge),
            "judge_kept": self.judge_kept,
            "secret_env": self.secret_env,
            "metrics": list(self.plugin.metrics),
        }


def _configured_judge(entry: dict[str, Any] | None, evaluators: str) -> dict[str, Any] | None:
    """The judge an existing plugin environment entry already names for these evaluators."""
    if not entry:
        return None
    judge = ((entry.get("default_params") or {}).get(evaluators) or {}).get("judge")
    return judge if isinstance(judge, dict) and judge else None


def _judge_label(judge: dict[str, Any] | None) -> str | None:
    if judge is None:
        return None
    if judge["kind"] == "openai_compatible":
        return f"{judge['model']} at {judge['base_url']}"
    return str(judge.get("model") or judge.get("factory"))


JUDGE_KEY_ENV = "AIBENCH_JUDGE_KEY"


def judge_from_provider(
    base_url: str, model: str, api_key: str | None
) -> tuple[dict[str, Any], dict[str, str]]:
    """A judge on the same OpenAI-compatible endpoint as the assistant (e.g. GLM on Z.ai),
    and the secret its workers need: the key reference is passed as `AIBENCH_JUDGE_KEY`."""
    judge: dict[str, Any] = {
        "kind": "openai_compatible",
        "base_url": base_url,
        "model": model,
        "api_key_env": JUDGE_KEY_ENV,
    }
    return judge, ({JUDGE_KEY_ENV: api_key} if api_key else {})


def _source_checkout(plugin: OptionalPlugin) -> Path | None:
    """The aibench checkout this copy runs from, when it has the adapter's source (a
    developer install); None for an installed package, which uses release wheels."""
    import aibench

    root = Path(aibench.__file__).resolve().parents[2]  # src/aibench -> the checkout
    return root if (root / plugin.source / "pyproject.toml").is_file() else None


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _relative_or_absolute(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def plan_install(
    name: str,
    project_root: Path,
    *,
    policy_path: Path | None,
    judge: dict[str, Any] | None = None,
    secret_env: dict[str, str] | None = None,
    existing_python: Path | None = None,
) -> InstallPlan:
    """What installing `name` for this project involves. Reads files only."""
    plugin = OPTIONAL_PLUGINS.get(name)
    if plugin is None:
        known = ", ".join(OPTIONAL_PLUGINS)
        raise PluginInstallError(f"no optional plugin {name!r}; known: {known}")
    source = _source_checkout(plugin)
    release = None if source is not None or existing_python is not None else release_source()
    path = config_path(project_root)
    config: dict[str, Any] = {}
    if path is None:
        path = project_root / "aibench.json"
    elif path.suffix != ".json":
        raise PluginInstallError(f"{path.name} is not JSON; add plugin_environments by hand")
    else:
        config = _read_json(path, "project config")
    create: Path | None = None
    if existing_python is not None:
        python = existing_python.resolve()
        if not python.is_file():
            raise PluginInstallError(f"no interpreter at {python}")
    else:
        create = project_root / ".aibench" / "plugins" / name / "venv"
        python = _venv_python(create)
    secrets = dict(secret_env or {})
    previous = next(
        (e for e in config.get("plugin_environments", []) if e.get("name") == name), None
    )
    kept = _configured_judge(previous, plugin.evaluators)
    if kept is not None:
        # Installing again (to upgrade the plugin) must not swap the judge the user chose for
        # the assistant's model: that turned a paid glm-4.7-flashx judge into the free
        # glm-4.5-flash without a word. Change the judge by editing the project config.
        judge = kept
        secrets = {**secrets, **dict(previous.get("secret_env") or {})}  # type: ignore[union-attr]
    if plugin.uses_models and judge is None:
        raise PluginInstallError(
            f"{name} metrics are judged by a model: open the chat with --provider-config to "
            "use the assistant's model as judge, or pass --judge-provider"
        )
    entry: dict[str, Any] = {
        "name": name,
        "python": _relative_or_absolute(python, path.parent),
        "secret_env": secrets,
    }
    if kept is not None:
        entry["default_params"] = previous["default_params"]  # type: ignore[index]
    elif judge is not None:
        entry["default_params"] = {plugin.evaluators: {"judge": judge}}
    others = [e for e in config.get("plugin_environments", []) if e.get("name") != name]
    config = {**config, "plugin_environments": [*others, entry]}

    policy: dict[str, Any] | None = None
    changes: list[str] = []
    if policy_path is not None:
        current = _read_json(policy_path, "policy")
        policy = json.loads(json.dumps(current))
        interpreter = _relative_or_absolute(python, policy_path.parent)
        listed = {
            (policy_path.parent / p).resolve()
            for p in policy.get("allowed_plugin_environments", [])
        }
        if python.resolve() not in listed:
            policy.setdefault("allowed_plugin_environments", []).append(interpreter)
            changes.append(f"allowed_plugin_environments: + {interpreter}")
        evaluators = policy.get("allowed_evaluators", ["native.*"])
        if not any(fnmatchcase(f"{name}.x", p) for p in evaluators):
            policy["allowed_evaluators"] = [*evaluators, plugin.evaluators]
            changes.append(f"allowed_evaluators: + {plugin.evaluators}")
        if plugin.uses_models and not policy.get("allow_model_evaluators", False):
            policy["allow_model_evaluators"] = True
            changes.append("allow_model_evaluators: false -> true (its metrics call a judge)")
        for ref in secrets.values():
            if ref not in policy.get("allowed_secret_refs", []):
                policy.setdefault("allowed_secret_refs", []).append(ref)
                changes.append(f"allowed_secret_refs: + {ref}")
        if policy == current:
            policy = None
    return InstallPlan(
        plugin=plugin,
        project_root=project_root,
        python=python,
        create=create,
        source=source,
        release=release,
        config_path=path,
        config=config,
        policy_path=policy_path,
        policy=policy,
        policy_changes=changes,
        judge=judge,
        secret_env=secrets,
        judge_kept=kept is not None,
    )


def install(plan: InstallPlan, progress: Callable[[str], None]) -> dict[str, Any]:
    """Create (or adopt) the environment, check the adapter loads there, then write the
    project config and the policy (the old policy kept as `.bak`). Neither file is written
    if the environment cannot be built or does not load."""
    if plan.create is not None:
        if not plan.python.is_file():
            progress(f"creating {plan.create}")
            _run([sys.executable, "-m", "venv", str(plan.create)], progress)
        progress(f"installing {plan.plugin.distribution} and {plan.plugin.package}")
        _run(
            [
                str(plan.python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                *_adapter_requirements(plan, progress),
            ],
            progress,
        )
    progress("checking the plugin loads in its environment")
    from aibench.registry import EvaluatorRegistry

    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(plan.python)
    errors = [load.error for load in loads if load.error]
    if errors:
        raise PluginInstallError(f"the plugin environment does not load: {errors[0]}")
    found = sorted(
        m.evaluator_id
        for m in registry.manifests()
        if fnmatchcase(m.evaluator_id, plan.plugin.evaluators)
    )
    if not found:
        raise PluginInstallError(f"{plan.python} has no {plan.plugin.evaluators} evaluators")
    _write_json(plan.config_path, plan.config)
    if plan.policy is not None and plan.policy_path is not None:
        backup = plan.policy_path.with_name(plan.policy_path.name + ".bak")
        backup.write_text(plan.policy_path.read_text(encoding="utf-8"), encoding="utf-8")
        _write_json(plan.policy_path, plan.policy)
        progress(f"policy updated; the previous one is {backup.name}")
    return {**plan.summary(), "evaluators": found}


def _adapter_requirements(plan: InstallPlan, progress: Callable[[str], None]) -> list[str]:
    """What pip installs: the checkout's sources (editable), or this aibench version's and
    the adapter's wheels from the release, verified against its SHA256SUMS. The framework
    the adapter pins (e.g. deepeval) and its dependencies come from the package index."""
    if plan.source is not None:
        return ["-e", str(plan.source), "-e", str(plan.source / plan.plugin.source)]
    assert plan.release is not None and plan.create is not None
    listed = checksums(plan.release)
    names = [
        wheel_name(listed, "aibench", __version__),
        wheel_name(listed, plan.plugin.distribution),
    ]
    wheels = fetch(plan.release, names, plan.create.parent / "wheels", listed, progress)
    return [str(p) for p in wheels]


_PROGRESS = ("Collecting", "Successfully", "ERROR", "error:", "Installing collected")


def _run(argv: list[str], progress: Callable[[str], None]) -> None:
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail: list[str] = []
    assert process.stdout is not None
    for raw in process.stdout:
        line = raw.rstrip()
        tail = [*tail[-19:], line]
        if line.startswith(_PROGRESS):
            progress(line[:160])
    if process.wait() != 0:
        command = " ".join([Path(argv[0]).name, *argv[1:3]])
        raise PluginInstallError(f"{command} failed:\n" + "\n".join(tail))


def _read_json(path: Path, what: str) -> dict[str, Any]:
    if path.suffix != ".json":
        raise PluginInstallError(f"the {what} {path.name} is not JSON; change it by hand")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PluginInstallError(f"cannot read the {what} {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PluginInstallError(f"the {what} {path} is not a JSON object")
    return data


def _write_json(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
