"""`aibench evaluators list|describe` and `aibench score RUN_ID --metrics FILE` (04-T2, 04-T4).

`score` evaluates recorded outputs only; it never invokes the application.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
from aibench.config.resolve import load_mapping_file
from aibench.core.errors import AibenchError
from aibench.core.models import MetricBinding
from aibench.registry import BindingValidationError, EvaluatorRegistry
from aibench.registry.discovery import discover_plugins, load_manifests
from aibench.reporting.aggregation import MetricSummary
from aibench.services.scoring import score_recorded_run
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

evaluators_app = typer.Typer(help="List and inspect available evaluators.")
console = Console()
err_console = Console(stderr=True)

_CUSTOM_OPTION = typer.Option(
    None, "--custom-evaluator", help="Python file defining EVALUATORS = (...). Executes it."
)
_TRUST_OPTION = typer.Option(
    False, "--trust-local-code", help="Allow executing --custom-evaluator code in-process."
)


def _fail(message: str, code: int = 2) -> typer.Exit:
    return error_exit(
        message, exit_code=code, json_output=False, console=console, err_console=err_console
    )


_PLUGIN_ENV_OPTION = typer.Option(
    None,
    "--plugin-env",
    help="Python interpreter of a plugin environment (e.g. plugins/deepeval/.venv/...). "
    "Its evaluators run only in worker processes using that interpreter.",
)
_PLUGIN_PATH_OPTION = typer.Option(
    [],
    "--plugin-path",
    help="Extra import path for plugin workers, e.g. a directory with a local judge factory. "
    "Code there runs in the worker.",
)
_PLUGIN_SECRET_OPTION = typer.Option(
    [],
    "--plugin-secret",
    help="NAME=source:name secret passed to plugin workers, e.g. OPENAI_API_KEY=env:OPENAI_API_KEY.",
)
_PLUGIN_STARTUP_TIMEOUT_OPTION = typer.Option(
    120.0,
    "--plugin-startup-timeout",
    min=1.0,
    max=3_600.0,
    help="Bound for plugin discovery and worker startup in seconds.",
)


def _registry(
    custom: Path | None,
    trusted: bool,
    plugin_env: Path | None = None,
    plugin_secrets: list[str] | None = None,
    plugin_paths: list[Path] | None = None,
    startup_timeout_seconds: float = 120.0,
) -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    if custom is not None:
        registry.load_local_file(custom, trusted=trusted)
    if plugin_env is not None:
        secrets: dict[str, str] = {}
        for item in plugin_secrets or []:
            name, sep, ref = item.partition("=")
            if not sep or not name or ":" not in ref:
                raise AibenchError(f"--plugin-secret must look like NAME=source:name, got {item!r}")
            secrets[name] = ref
        loads = registry.load_plugin_environment(
            plugin_env,
            secret_env=secrets,
            extra_paths=[p.resolve() for p in plugin_paths or []],
            startup_timeout_seconds=startup_timeout_seconds,
        )
        for load in loads:
            if load.error:
                err_console.print(
                    f"[yellow]plugin {escape(load.plugin.name)} not loaded:[/yellow] {escape(load.error)}"
                )
    elif plugin_secrets or plugin_paths:
        raise AibenchError("--plugin-secret and --plugin-path require --plugin-env")
    return registry


@evaluators_app.command("list")
def list_evaluators(
    custom: Path | None = _CUSTOM_OPTION,
    trust_local_code: bool = _TRUST_OPTION,
    plugin_env: Path | None = _PLUGIN_ENV_OPTION,
    plugin_secret: list[str] = _PLUGIN_SECRET_OPTION,
    plugin_path: list[Path] = _PLUGIN_PATH_OPTION,
    plugin_startup_timeout: float = _PLUGIN_STARTUP_TIMEOUT_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Built-in and trusted local evaluators, plus installed plugins found by metadata
    (plugins are listed, not imported)."""
    try:
        registry = _registry(
            custom, trust_local_code, plugin_env, plugin_secret, plugin_path, plugin_startup_timeout
        )
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    plugins = discover_plugins()
    if json_output:
        console.print_json(
            data={
                "evaluators": [m.model_dump(mode="json") for m in registry.manifests()],
                "installed_plugins": [p.__dict__ for p in plugins],
            }
        )
        return
    for manifest in registry.manifests():
        console.print(
            f"[bold]{manifest.evaluator_id}@{manifest.version}[/bold]  "
            f"{manifest.value_kind}/{manifest.aggregation}  {escape(manifest.description)}"
        )
    if plugins:
        console.print("installed plugins (not loaded; inspect with `aibench evaluators plugin`):")
        for plugin in plugins:
            console.print(
                f"  {escape(plugin.distribution)} {plugin.version}: {escape(plugin.name)}"
            )


@evaluators_app.command("describe")
def describe_evaluator(
    reference: str = typer.Argument(..., help="namespace.name[@version]"),
    custom: Path | None = _CUSTOM_OPTION,
    trust_local_code: bool = _TRUST_OPTION,
    plugin_env: Path | None = _PLUGIN_ENV_OPTION,
    plugin_secret: list[str] = _PLUGIN_SECRET_OPTION,
    plugin_path: list[Path] = _PLUGIN_PATH_OPTION,
    plugin_startup_timeout: float = _PLUGIN_STARTUP_TIMEOUT_OPTION,
) -> None:
    """Full manifest of one evaluator."""
    try:
        manifest, _ = _registry(
            custom,
            trust_local_code,
            plugin_env,
            plugin_secret,
            plugin_path,
            plugin_startup_timeout,
        ).resolve(reference)
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    console.print_json(data=manifest.model_dump(mode="json"))


@evaluators_app.command("plugin")
def inspect_plugin(
    name: str = typer.Argument(..., help="Entry-point name from `aibench evaluators list`."),
) -> None:
    """Read an installed plugin's manifests in an isolated worker process. This runs the
    plugin's code in that process, never in this one."""
    matches = [p for p in discover_plugins() if p.name == name]
    if not matches:
        raise _fail(f"no installed plugin named {name!r}")
    loaded = load_manifests(matches[0])
    if loaded.error:
        raise _fail(loaded.error, code=1)
    console.print_json(data=[m.model_dump(mode="json") for m in loaded.manifests])


def _load_bindings(path: Path) -> list[MetricBinding]:
    raw = load_mapping_file(path)
    items = raw.get("metrics")
    if not isinstance(items, list) or not items:
        raise AibenchError(f"{path} must contain a non-empty 'metrics' list")
    try:
        return [MetricBinding.model_validate(item) for item in items]
    except PydanticValidationError as exc:
        raise AibenchError(f"invalid metric binding in {path}: {exc}") from exc


def _print_summary(summary: MetricSummary) -> None:
    head = f"[bold]{summary.metric_id}@{summary.metric_version}[/bold] ({summary.value_kind})"
    if summary.params:
        head += f" params={escape(json.dumps(summary.params, sort_keys=True))}"
    console.print(head)
    console.print(
        f"  selected={summary.selected} eligible={summary.eligible} "
        f"completed={summary.completed} errors={summary.errors} "
        f"not_applicable={summary.not_applicable} unavailable={summary.unavailable}"
    )
    console.print(
        f"  coverage: eligible={summary.eligible_coverage} completed={summary.completed_coverage}"
    )
    d = summary.decisions
    console.print(
        f"  decisions: pass={d['pass']} fail={d['fail']} indeterminate={d['indeterminate']} "
        f"not_evaluated={d['not_evaluated']}"
    )
    if summary.value_summary:
        console.print(f"  values: {escape(json.dumps(summary.value_summary))}")
    if summary.reasons:
        console.print(f"  reasons: {escape(json.dumps(summary.reasons))}")


def score(
    run_id: str = typer.Argument(..., help="Run whose recorded outputs to score."),
    metrics: Path = typer.Option(..., "--metrics", help="JSON/YAML file with a 'metrics' list."),  # noqa: B008
    workspace: Path | None = typer.Option(  # noqa: B008
        None, "--workspace", help="Project root containing .aibench/ (default: cwd)."
    ),
    custom: Path | None = _CUSTOM_OPTION,
    trust_local_code: bool = _TRUST_OPTION,
    plugin_env: Path | None = _PLUGIN_ENV_OPTION,
    plugin_secret: list[str] = _PLUGIN_SECRET_OPTION,
    plugin_path: list[Path] = _PLUGIN_PATH_OPTION,
    plugin_startup_timeout: float = _PLUGIN_STARTUP_TIMEOUT_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Score a run's recorded outputs. Never invokes the application."""
    try:
        registry = _registry(
            custom, trust_local_code, plugin_env, plugin_secret, plugin_path, plugin_startup_timeout
        )
        bindings = _load_bindings(metrics)
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    ws = Workspace.at(workspace or Path.cwd())
    if not ws.db_path.exists():
        raise _fail(f"no aibench workspace at {ws.root}")
    storage = Storage(Database.open_workspace(ws))
    try:
        report = asyncio.run(
            score_recorded_run(
                storage=storage,
                artifacts=ArtifactStore(ws.artifacts_dir),
                registry=registry,
                run_id=run_id,
                bindings=bindings,
            )
        )
    except BindingValidationError as exc:
        for problem in exc.problems:
            err_console.print(f"[red]{escape(str(problem))}[/red]")
        raise _fail("no cases were evaluated: fix the metric bindings above") from exc
    except AibenchError as exc:
        raise _fail(str(exc)) from exc
    finally:
        storage.db.close()

    if json_output:
        payload: dict[str, Any] = {
            "scoring_id": report.scoring_id,
            "run_id": report.run_id,
            "summaries": [s.as_dict() for s in report.summaries],
            "warnings": report.warnings,
            "budget": report.budget,
            "quotas": report.quotas,
            "stop_reason": report.stop_reason,
            "outcome": report.outcome,
            "gates": report.gates,
            "exit_code": report.exit_code,
        }
        console.print_json(data=payload, cli_exit_code=report.exit_code)
        raise typer.Exit(code=report.exit_code)
    console.print(f"scoring {report.scoring_id} of run {escape(report.run_id)} (recorded outputs)")
    console.print(
        f"  outcome: {'complete' if report.outcome.get('complete') else 'incomplete'} "
        f"(exit code {report.exit_code})"
    )
    for summary in report.summaries:
        _print_summary(summary)
    if report.stop_reason:
        console.print(f"  stopped: {escape(report.stop_reason)}")
    for warning in report.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
    if report.exit_code:
        raise typer.Exit(code=report.exit_code)


@evaluators_app.command("calibrate")
def calibrate(
    set_dir: Path = typer.Option(  # noqa: B008
        Path("benchmarks/judges/v1"), "--set", help="Calibration set directory."
    ),
    out: Path | None = typer.Option(None, "--out", help="Write the full report (JSON) here."),  # noqa: B008
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Measure the in-process (native) evaluators against labelled outputs: agreement,
    false acceptance, false rejection and repeat stability, per category (§23)."""
    from aibench.services.calibration import CalibrationError, run_calibration
    from aibench.services.reports import write_text_atomic

    try:
        report = run_calibration(set_dir)
    except (CalibrationError, BindingValidationError) as exc:
        raise _fail(str(exc)) from exc
    if out is not None:
        write_text_atomic(out, json.dumps(report, indent=2) + "\n")
    if json_output:
        console.print_json(data=report)
        return
    console.print(
        f"calibration set {escape(str(report['set']))}; labels: {escape(report['label_review'])}"
    )
    for binding, data in report["bindings"].items():
        o = data["overall"]

        def frac(m: dict[str, Any]) -> str:
            return f"{m['numerator']}/{m['denominator']}"

        console.print(
            f"[bold]{escape(binding)}[/bold]: agreement {frac(o['agreement'])}, "
            f"false acceptance {frac(o['false_acceptance'])}, false rejection "
            f"{frac(o['false_rejection'])}, undecided {o['undecided']}, repeat stability "
            f"{frac(o['repeat_stability'])}"
        )
        for d in data["disagreements"]:
            console.print(
                f"  disagrees on {escape(d['id'])} ({escape(d['category'])}): labelled "
                f"{d['label']}, decided {d['decision']}"
            )
    for item in report["not_measured"]:
        subject = item.get("evaluator") or item.get("measure")
        console.print(
            f"[yellow]not measured[/yellow] {escape(str(subject))}: {escape(item['reason'])}"
        )
