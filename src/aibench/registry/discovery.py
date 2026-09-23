"""Controlled discovery of installed evaluator plugins (§9: "read manifests without
importing untrusted code into the CLI process. Run third-party discovery/loading inside a
controlled worker. An installed package is executable code, not harmless metadata.").

- `discover_plugins` reads entry-point metadata (`dist-info/entry_points.txt`) only.
  Nothing is imported.
- `load_manifests` runs the plugin's entry point in a separate Python process
  (`python -m aibench.registry.worker`) with a minimal environment, a hard timeout that
  kills the worker's whole process tree, and an output cap, then validates what it prints.
  A crash, hang or malformed reply is a reported error.

The worker is process isolation only — not a sandbox. Plugin code there can still read the
filesystem and use the network; installed plugins are trusted code the user chose to
install.

Installing packages is out of scope: only already-installed distributions are seen.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from aibench.core.models import EvaluatorManifest
from aibench.runners.process_tree import run_contained

ENTRY_POINT_GROUP = "aibench.evaluators"
WORKER_TIMEOUT_SECONDS = 30.0
MAX_WORKER_OUTPUT_BYTES = 1_048_576
_WORKER_ENV_KEEP = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME")


@dataclass(frozen=True)
class DiscoveredPlugin:
    name: str
    target: str  # "module:attribute" — not imported here
    distribution: str
    version: str


@dataclass(frozen=True)
class ManifestLoad:
    plugin: DiscoveredPlugin
    manifests: tuple[EvaluatorManifest, ...] = ()
    error: str | None = None


def discover_plugins(paths: Sequence[Path] | None = None) -> list[DiscoveredPlugin]:
    """Entry points in the `aibench.evaluators` group, from metadata only."""
    distributions = (
        metadata.distributions(path=[str(p) for p in paths])
        if paths is not None
        else metadata.distributions()
    )
    found = {}
    for dist in distributions:
        for ep in dist.entry_points:
            if ep.group == ENTRY_POINT_GROUP:
                plugin = DiscoveredPlugin(ep.name, ep.value, dist.metadata["Name"], dist.version)
                found[(plugin.distribution, plugin.name)] = plugin
    return sorted(found.values(), key=lambda p: (p.distribution, p.name))


def load_manifests(
    plugin: DiscoveredPlugin,
    *,
    extra_paths: Sequence[Path] = (),
    timeout: float = WORKER_TIMEOUT_SECONDS,
) -> ManifestLoad:
    """Ask a worker process for the plugin's manifests. The plugin's code runs only in
    that process."""
    env = {k: os.environ[k] for k in _WORKER_ENV_KEEP if k in os.environ}
    if extra_paths:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in extra_paths)
    result = run_contained(
        [sys.executable, "-m", "aibench.registry.worker", plugin.target],
        timeout=timeout,
        env=env,
        max_output_bytes=MAX_WORKER_OUTPUT_BYTES,
    )
    if result.timed_out:
        return ManifestLoad(plugin, error=f"manifest worker timed out after {timeout}s")
    if result.truncated:
        return ManifestLoad(plugin, error="manifest worker output exceeded the size limit")
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        return ManifestLoad(
            plugin,
            error=f"manifest worker exited {result.returncode}: {detail[-1] if detail else ''}",
        )
    try:
        raw = json.loads(result.stdout)
        manifests = tuple(EvaluatorManifest.model_validate(item) for item in raw)
    except (ValueError, TypeError, PydanticValidationError) as exc:
        return ManifestLoad(
            plugin, error=f"manifest worker returned invalid manifests: {exc}"[:500]
        )
    return ManifestLoad(plugin, manifests=manifests)
