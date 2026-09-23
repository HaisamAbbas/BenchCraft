"""`aibench init`, `aibench doctor`, `aibench plugins list`, `aibench compare` (§13, 11-T3).

- `init [DIR]` writes the packaged quickstart project (config, 10-case dataset, fixture app,
  plan with release gates, local policy). It copies files only: it installs nothing, runs
  nothing, and refuses to overwrite any existing file.
- `doctor` checks the environment and the project without executing the application
  (a plan's plugin environments are started to list their evaluators, only when the policy
  permits them):
  Python and platform support, the workspace, the project config and the files it names,
  runner prerequisites, the plan (including its plugin environments, started only when the
  policy permits them) and whether each secret reference is set. Secret values are never
  printed. Exit codes: 0 no problems (warnings allowed); 2 invalid configuration, dataset or
  plan; 3 a missing prerequisite (interpreter, executable, secret, unsupported Python).
- `plugins list` lists installed plugin kinds: built-in runners and planners, native
  evaluators, and evaluator plugins found in package metadata (listed, not imported).
- `compare` is not implemented in this version; it says so and exits 2 (Prompt 14).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from aibench import __version__
from aibench.core.errors import AibenchError
from aibench.core.models import RunnerKind
from aibench.quickstart import ProjectExists, create_project
from aibench.tui.render import safe

console = Console(highlight=False, emoji=False)
err_console = Console(stderr=True, highlight=False, emoji=False)
plugins_app = typer.Typer(help="Inspect installed plugin kinds.")

EXIT_OK, EXIT_INVALID, EXIT_MISSING = 0, 2, 3
SUPPORTED_PYTHON = ((3, 11), (3, 12))
TESTED_PLATFORMS = ("win32", "linux")
_JSON = typer.Option(False, "--json", help="Machine-readable output on stdout.")


# --------------------------------------------------------------------------- init


def init(
    directory: Path = typer.Argument(Path("."), help="Project directory (default: cwd)."),  # noqa: B008
    json_output: bool = _JSON,
) -> None:
    """Create a quickstart project: config, 10-case dataset, fixture app, plan and policy."""
    target = directory.resolve()
    if target.exists() and not target.is_dir():
        err_console.print(f"[red]{safe(str(target))} exists and is not a directory[/red]")
        raise typer.Exit(code=EXIT_INVALID)
    try:
        written = create_project(target, python=sys.executable)
    except ProjectExists as exc:
        err_console.print(
            "[red]not written: these files already exist (nothing was changed):[/red]"
        )
        for path in exc.paths:
            err_console.print(f"  {safe(str(path))}")
        raise typer.Exit(code=EXIT_INVALID) from exc
    except OSError as exc:
        err_console.print(f"[red]could not create the project: {safe(str(exc))}[/red]")
        raise typer.Exit(code=EXIT_INVALID) from exc
    if json_output:
        console.print_json(data={"project": str(target), "files": [str(p) for p in written]})
        return
    console.print(f"created a quickstart project in {safe(str(target))}:")
    for path in written:
        console.print(f"  {safe(path.name)}")
    console.print(
        "nothing was installed or run. Next:\n"
        f"  cd {safe(str(target))}\n"
        "  aibench doctor            check the environment and the project\n"
        "  aibench                   plan and run it in conversation (/plan, /run)\n"
        "  aibench run               or run the plan headlessly\n"
        "  aibench report RUN_ID     then render its report"
    )


# --------------------------------------------------------------------------- doctor


@dataclass
class Check:
    name: str
    status: str  # ok | warn | invalid | missing
    detail: str


def _python_check() -> Check:
    version = sys.version_info[:2]
    label = f"Python {sys.version.split()[0]} ({sys.executable})"
    if version in SUPPORTED_PYTHON:
        return Check("python", "ok", label)
    return Check("python", "missing", f"{label}: supported versions are 3.11 and 3.12")


def _platform_check() -> Check:
    if sys.platform in TESTED_PLATFORMS:
        return Check("platform", "ok", sys.platform)
    return Check("platform", "warn", f"{sys.platform}: not in the tested matrix (Windows, Linux)")


def _workspace_check(root: Path) -> Check:
    probe = root / ".aibench" if (root / ".aibench").is_dir() else root
    try:
        with tempfile.NamedTemporaryFile(dir=probe, prefix=".aibench-doctor-"):
            pass
    except OSError as exc:
        return Check("workspace", "missing", f"{probe} is not writable: {exc}")
    state = "exists" if (root / ".aibench").is_dir() else "created on first run"
    return Check(
        "workspace", "ok", f"{root / '.aibench'} ({state}); SQLite {sqlite3.sqlite_version}"
    )


def _display_url(url: str) -> str:
    """A URL without userinfo or query string (either may carry a credential)."""
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        netloc = f"{host}:{parts.port}" if parts.port else host
        return urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    except ValueError:
        return "(unparseable URL)"


def _secret_check(ref: str, where: str, environ: dict[str, str]) -> Check:
    source, _, name = ref.partition(":")
    if source != "env":
        return Check(f"secret {ref}", "missing", f"{where}: only env: references are supported")
    if environ.get(name):
        return Check(f"secret {ref}", "ok", f"{where}: set (value not shown)")
    return Check(f"secret {ref}", "missing", f"{where}: environment variable {name} is not set")


def _application_checks(path: Path, policy: Any, environ: dict[str, str]) -> list[Check]:
    from aibench.runners import load_application
    from aibench.security.policy import application_denials

    try:
        app = load_application(path)
    except AibenchError as exc:
        return [Check("application", "invalid", str(exc))]
    spec = app.spec
    checks = [
        Check("application", "ok", f"{spec.application_id} ({spec.runner.value}) from {path}")
    ]
    transport = spec.transport
    if spec.runner is RunnerKind.CLI and transport is not None and transport.kind == "cli":
        program = transport.argv[0]
        candidate = Path(program)
        cwd = app.base_dir / (transport.cwd or ".")
        if candidate.is_absolute() or len(candidate.parts) > 1:
            found = (cwd / candidate) if not candidate.is_absolute() else candidate
            ok = found.is_file()
        else:
            found = Path(shutil.which(program) or program)
            ok = shutil.which(program) is not None
        checks.append(
            Check(
                "cli executable",
                "ok" if ok else "missing",
                f"{found}" if ok else f"{program!r} not found (resolved from {cwd})",
            )
        )
        if not policy.allow_trusted_local:
            checks.append(
                Check(
                    "trusted local",
                    "warn",
                    "a CLI application runs only with trusted-local mode: set "
                    "allow_trusted_local in the policy or pass --trust-local-app",
                )
            )
        for name, ref in transport.secret_env.items():
            checks.append(_secret_check(ref, f"application env {name}", environ))
    elif transport is not None and transport.kind == "http":
        checks.append(
            Check("http endpoint", "ok", f"{_display_url(transport.url)} (not contacted)")
        )
        for header, secret in transport.secret_headers.items():
            checks.append(_secret_check(secret.ref, f"application header {header}", environ))
    for denial in application_denials(policy, spec):
        checks.append(Check("application policy", "warn", denial))
    return checks


def _dataset_check(path: Path) -> Check:
    from aibench.datasets.ingest import ingest_dataset

    try:
        report = ingest_dataset(path)
    except AibenchError as exc:
        return Check("dataset", "invalid", str(exc))
    if not report.is_valid or report.manifest is None:
        errors = "; ".join(str(e) for e in report.errors[:3])
        return Check("dataset", "invalid", f"{path}: {errors}")
    return Check("dataset", "ok", f"{path}: {len(report.cases)} valid case(s)")


def _plan_checks(path: Path, policy: Any, environ: dict[str, str]) -> list[Check]:
    from aibench.engine.compile import PlanInvalid, analyze_plan, load_plan

    try:
        plan = load_plan(path)
    except PlanInvalid as exc:
        return [Check("plan", "invalid", "; ".join(exc.problems))]
    checks = []
    for env in plan.plugin_environments:
        python = Path(env.python) if Path(env.python).is_absolute() else path.parent / env.python
        checks.append(
            Check(
                "plugin environment",
                "ok" if python.is_file() else "missing",
                f"{python}" if python.is_file() else f"interpreter not found: {python}",
            )
        )
        for name, ref in env.secret_env.items():
            checks.append(_secret_check(ref, f"plugin env {name}", environ))
    analysis = analyze_plan(plan, path.resolve().parent, policy=policy)
    blocking = [f for f in analysis.findings if f.blocking]
    for finding in blocking:
        status = "warn" if finding.kind == "missing_permission" else "invalid"
        checks.append(Check(f"plan {finding.subject}", status, finding.message))
    if not blocking:
        checks.append(
            Check(
                "plan",
                "ok",
                f"{plan.plan_id}: {len(analysis.cases)} case(s) x {plan.repetitions}, "
                f"{len(analysis.metrics)} metric(s), {len(plan.gates)} release gate(s)",
            )
        )
    return checks


def _provider_checks(path: Path, environ: dict[str, str]) -> list[Check]:
    from pydantic import ValidationError

    from aibench.config.resolve import load_mapping_file
    from aibench.planning.openai_provider import OpenAICompatibleConfig

    try:
        config = OpenAICompatibleConfig.model_validate(load_mapping_file(path))
    except ValidationError as exc:
        # Field names and error types only: pydantic's messages can quote the offending
        # input, which may be a pasted key.
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: invalid ({e['type']})" for e in exc.errors()
        )
        return [Check("assistant model", "invalid", f"{path}: {problems}")]
    except (AibenchError, OSError) as exc:
        return [Check("assistant model", "invalid", f"{path}: {type(exc).__name__}")]
    checks = [
        Check(
            "assistant model",
            "ok",
            f"{config.model} at {_display_url(config.base_url)} (not contacted)",
        )
    ]
    if config.api_key is not None:
        checks.append(_secret_check(config.api_key, "assistant model api_key", environ))
    return checks


def doctor(
    project: Path | None = typer.Option(None, "--project", help="Project directory."),  # noqa: B008
    provider_config: Path | None = typer.Option(  # noqa: B008
        None, "--provider-config", help="Also check this assistant model config."
    ),
    json_output: bool = _JSON,
) -> None:
    """Check the environment, the project and credential availability. The application
    is never run; a plan's plugin environments start only if the policy permits them."""
    from aibench.cli.chat import project_settings
    from aibench.engine.compile import load_policy

    root = (project or Path.cwd()).resolve()
    environ = dict(os.environ)
    checks = [
        Check("aibench", "ok", __version__),
        _python_check(),
        _platform_check(),
        _workspace_check(root),
    ]
    try:
        settings = project_settings(root, None, None, None)
    except AibenchError as exc:
        checks.append(Check("config", "invalid", str(exc)))
        settings = {}
    if settings:
        config = settings.get("config")
        checks.append(
            Check("config", "ok", str(config))
            if config
            else Check("config", "warn", f"no aibench.json in {root} (`aibench init` creates one)")
        )
    policy = None
    try:
        policy = load_policy(settings.get("policy"))
        where = settings.get("policy") or "built-in conservative policy"
        checks.append(Check("policy", "ok", str(where)))
    except AibenchError as exc:
        checks.append(Check("policy", "invalid", str(exc)))
    for key, check in (("application", "application"), ("dataset", "dataset"), ("plan", "plan")):
        path = settings.get(key)
        if path is None:
            if settings.get("config"):
                checks.append(Check(check, "warn", f"not set in {settings['config']}"))
            continue
        if not path.is_file():
            checks.append(Check(check, "invalid", f"{path} does not exist"))
            continue
        if key == "dataset":
            checks.append(_dataset_check(path))
        elif policy is not None and key == "application":
            checks += _application_checks(path, policy, environ)
        elif policy is not None:
            checks += _plan_checks(path, policy, environ)
    if provider_config is not None:
        checks += _provider_checks(provider_config, environ)
    code = (
        EXIT_INVALID
        if any(c.status == "invalid" for c in checks)
        else EXIT_MISSING
        if any(c.status == "missing" for c in checks)
        else EXIT_OK
    )
    if json_output:
        console.print_json(
            data={"project": str(root), "checks": [asdict(c) for c in checks], "exit_code": code}
        )
        raise typer.Exit(code=code)
    colours = {"ok": "green", "warn": "yellow", "invalid": "red", "missing": "red"}
    for c in checks:
        colour = colours[c.status]
        console.print(f"[{colour}]{c.status:>7}[/{colour}]  {safe(c.name)}: {safe(c.detail)}")
    raise typer.Exit(code=code)


# --------------------------------------------------------------------------- plugins / compare


@plugins_app.command("list")
def list_plugins(json_output: bool = _JSON) -> None:
    """Installed plugin kinds: runners, planners, native evaluators, evaluator plugins."""
    from aibench.registry import EvaluatorRegistry
    from aibench.registry.discovery import discover_plugins

    data: dict[str, Any] = {
        "runners": [kind.value for kind in RunnerKind],
        "planners": ["template", "model (OpenAI-compatible endpoint, policy-gated)"],
        "evaluators": [
            f"{m.evaluator_id}@{m.version}" for m in EvaluatorRegistry.with_native().manifests()
        ],
        "evaluator_plugins": [p.__dict__ for p in discover_plugins()],
        "note": "evaluator plugins are listed from package metadata, not imported; "
        "plugin environments (e.g. DeepEval) are loaded per plan with --plugin-env",
    }
    if json_output:
        console.print_json(data=data)
        return
    console.print("runners: " + ", ".join(data["runners"]))
    console.print("planners: " + ", ".join(data["planners"]))
    console.print("evaluators (built in): " + ", ".join(data["evaluators"]))
    plugins = data["evaluator_plugins"]
    if plugins:
        console.print("evaluator plugins (installed, not loaded):")
        for p in plugins:
            console.print(f"  {safe(p['distribution'])} {safe(p['version'])}: {safe(p['name'])}")
    else:
        console.print("evaluator plugins: none installed in this environment")
    console.print(safe(data["note"]))


def compare(
    baseline: str = typer.Argument(..., help="Baseline run ID."),
    current: str = typer.Argument(..., help="Current run ID."),
    json_output: bool = _JSON,
) -> None:
    """Not available in this version (planned: paired comparison, Prompt 14)."""
    message = (
        "run comparison is not implemented in this version; nothing was compared. It is "
        "planned (paired, uncertainty-aware comparison of compatible runs). Until then, "
        "render each run with `aibench report RUN_ID` and compare their stated denominators."
    )
    if json_output:
        console.print_json(
            data={
                "status": "unsupported",
                "baseline": baseline,
                "current": current,
                "message": message,
            }
        )
    else:
        err_console.print(f"[yellow]{safe(message)}[/yellow]")
    raise typer.Exit(code=EXIT_INVALID)
