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
from rich.console import Console
from rich.markup import escape

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
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=code)


def _registry(custom: Path | None, trusted: bool) -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    if custom is not None:
        registry.load_local_file(custom, trusted=trusted)
    return registry


@evaluators_app.command("list")
def list_evaluators(
    custom: Path | None = _CUSTOM_OPTION,
    trust_local_code: bool = _TRUST_OPTION,
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Built-in and trusted local evaluators, plus installed plugins found by metadata
    (plugins are listed, not imported)."""
    try:
        registry = _registry(custom, trust_local_code)
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
) -> None:
    """Full manifest of one evaluator."""
    try:
        manifest, _ = _registry(custom, trust_local_code).resolve(reference)
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
    json_output: bool = typer.Option(False, "--json", help="Machine-readable output."),
) -> None:
    """Score a run's recorded outputs. Never invokes the application."""
    try:
        registry = _registry(custom, trust_local_code)
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
        }
        console.print_json(data=payload)
        return
    console.print(f"scoring {report.scoring_id} of run {escape(report.run_id)} (recorded outputs)")
    for summary in report.summaries:
        _print_summary(summary)
    for warning in report.warnings:
        console.print(f"[yellow]warning:[/yellow] {escape(warning)}")
