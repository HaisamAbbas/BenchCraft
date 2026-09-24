"""Validation for multi-turn text application episode manifests (18-T3)."""

# Typer intentionally builds CLI parameters from `typer.Option` annotations.
# ruff: noqa: B008

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.markup import escape

from aibench.core.errors import AibenchError
from aibench.datasets.episodes import validate_episode_manifest

app = typer.Typer(help="Validate multi-turn application episode manifests.")
console = Console()
err_console = Console(stderr=True)


@app.command("validate")
def validate(
    dataset: Path = typer.Argument(..., help="Episode turns in dataset JSONL."),
    manifest: Path = typer.Argument(..., help="TextEpisodeManifest JSON file."),
    plan: Path = typer.Option(
        ..., "--plan", help="Plan selecting this dataset, app, and test world."
    ),
    json_output: bool = typer.Option(False, "--json"),
) -> None:
    """Check episode ordering, user-simulator provenance and independent success criteria."""
    try:
        episode_manifest, cases = validate_episode_manifest(dataset, manifest, plan)
    except (AibenchError, OSError, ValueError) as exc:
        err_console.print(f"[red]{escape(str(exc))}[/red]")
        raise typer.Exit(code=2) from exc
    payload = {
        "valid": True,
        "episode_count": len(episode_manifest.episodes),
        "turn_count": sum(len(item.case_ids) for item in episode_manifest.episodes),
        "case_count": len(cases),
        "simulators": sorted({item.simulator.kind for item in episode_manifest.episodes}),
        "independent_success_evaluator": "native.final_state",
    }
    if json_output:
        console.print_json(data=payload)
    else:
        console.print(
            f"valid episode manifest: {payload['episode_count']} episode(s), "
            f"{payload['turn_count']} turn(s); reset policy and final-state checks are declared"
        )
