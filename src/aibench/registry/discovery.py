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

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from aibench.core.hashes import content_hash
from aibench.core.models import EvaluatorManifest
from aibench.runners.process_tree import run_contained

ENTRY_POINT_GROUP = "aibench.evaluators"
# Ragas' adapter imports a large metric catalogue during manifest discovery on
# Windows.  A bounded two-minute ceiling keeps discovery reliable without making a
# hung plugin unbounded; callers with a measured cold-start budget may pass a
# larger value explicitly.
WORKER_TIMEOUT_SECONDS = 120.0
MAX_WORKER_OUTPUT_BYTES = 1_048_576
_WORKER_ENV_KEEP = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME")
_EXCEPTION_LINE = re.compile(r"^[A-Za-z_][\w.]*(Error|Exception)(:|$)")


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


def core_version(paths: Sequence[Path]) -> str | None:
    """The version of aibench installed on these paths (another environment's site
    directories), from metadata only; None when it is not installed there."""
    for dist in metadata.distributions(path=[str(p) for p in paths]):
        if (dist.metadata["Name"] or "").lower() == "aibench":
            return dist.version
    return None


def dependency_lock_hash(paths: Sequence[Path]) -> str | None:
    """Hash installed distribution names and versions in a plugin environment."""
    installed = set()
    for dist in metadata.distributions(path=[str(path) for path in paths]):
        try:
            name = dist.metadata["Name"]
        except KeyError:
            continue
        installed.add((re.sub(r"[-_.]+", "-", name).casefold(), dist.version))
    if not installed:
        return None
    return content_hash([{"name": name, "version": version} for name, version in sorted(installed)])


def worker_python_identity(
    python: Path, *, timeout: float = WORKER_TIMEOUT_SECONDS
) -> str | None:
    """Identify the worker interpreter implementation, version, ABI and platform."""
    probe = (
        "import json, platform, sys, sysconfig; "
        "print(json.dumps({'implementation': platform.python_implementation(), "
        "'version': list(sys.version_info[:3]), 'cache_tag': sys.implementation.cache_tag, "
        "'platform': sysconfig.get_platform(), 'machine': platform.machine()}))"
    )
    env = {key: os.environ[key] for key in _WORKER_ENV_KEEP if key in os.environ}
    try:
        result = run_contained([str(python), "-I", "-c", probe], timeout=timeout, env=env)
        if result.timed_out or result.returncode != 0:
            return None
        identity = json.loads(result.stdout)
        if not isinstance(identity, dict) or not all(identity.values()):
            return None
        return content_hash(identity)
    except (OSError, ValueError, TypeError):
        return None


def plugin_paths_hash(paths: Sequence[Path]) -> str | None:
    """Hash code and data visible on plugin ``PYTHONPATH``; fail closed on unreadable/large paths."""
    max_bytes = 64 * 1024 * 1024
    max_entries = 10_000
    entries: list[dict[str, str]] = []
    total_bytes = 0
    visited_entries = 0
    visited: set[Path] = set()

    def files_under(root: Path):
        nonlocal visited_entries
        if root.is_file():
            visited_entries += 1
            if visited_entries > max_entries:
                raise OverflowError("plugin path contains too many entries")
            yield root
            return
        if not root.is_dir():
            return
        pending = [root]
        while pending:
            directory = pending.pop()
            resolved_dir = directory.resolve()
            if resolved_dir in visited:
                continue
            visited.add(resolved_dir)
            child_dirs: list[Path] = []
            with os.scandir(directory) as children:
                for child in children:
                    visited_entries += 1
                    if visited_entries > max_entries:
                        raise OverflowError("plugin path contains too many entries")
                    if child.name in {
                        ".git",
                        "__pycache__",
                        ".mypy_cache",
                        ".ruff_cache",
                    }:
                        continue
                    if child.is_dir(follow_symlinks=True):
                        child_dirs.append(Path(child.path))
                    elif child.is_file(follow_symlinks=True):
                        yield Path(child.path)
            pending.extend(sorted(child_dirs, reverse=True))

    try:
        for configured in paths:
            root = configured.resolve(strict=True)
            if not root.is_file() and not root.is_dir():
                return None
            for candidate in files_under(root):
                if not candidate.is_file():
                    continue
                remaining = max_bytes - total_bytes
                if len(entries) >= max_entries or candidate.stat().st_size > remaining:
                    return None
                digest = hashlib.sha256()
                file_bytes = 0
                with candidate.open("rb") as stream:
                    while chunk := stream.read(min(64 * 1024, remaining - file_bytes + 1)):
                        file_bytes += len(chunk)
                        if file_bytes > remaining:
                            return None
                        digest.update(chunk)
                total_bytes += file_bytes
                entries.append(
                    {
                        "root": str(root),
                        "path": candidate.relative_to(root).as_posix()
                        if root.is_dir()
                        else root.name,
                        "hash": "sha256:" + digest.hexdigest(),
                    }
                )
    except (OSError, OverflowError, RuntimeError):
        return None
    return content_hash(sorted(entries, key=lambda entry: (entry["root"], entry["path"])))


def worker_failure(stderr: bytes) -> str:
    """What a crashed worker's traceback says went wrong: the exception and the lines it
    printed after it, not just the last line (pydantic's last line is a help link)."""
    lines = [
        line.strip()
        for line in stderr.decode("utf-8", "replace").splitlines()
        if line.strip() and not line.strip().startswith("For further information visit")
    ]
    for index in range(len(lines) - 1, -1, -1):
        if _EXCEPTION_LINE.match(lines[index]):
            return " ".join(lines[index:])[:400]
    return lines[-1][:400] if lines else ""


def environment_site_paths(python: Path, *, timeout: float = WORKER_TIMEOUT_SECONDS) -> list[Path]:
    """The site-packages directories of another Python environment, asked of that
    environment's own interpreter (nothing is imported from it here)."""
    probe = "import json, sysconfig; p = sysconfig.get_paths(); print(json.dumps([p['purelib'], p['platlib']]))"
    env = {k: os.environ[k] for k in _WORKER_ENV_KEEP if k in os.environ}
    result = run_contained([str(python), "-I", "-c", probe], timeout=timeout, env=env)
    try:
        if result.timed_out or result.returncode != 0:
            raise ValueError("probe failed")
        return sorted({Path(p) for p in json.loads(result.stdout)})
    except (ValueError, TypeError):
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OSError(
            f"could not inspect Python environment {python}: "
            f"{detail[-1] if detail else result.returncode}"
        ) from None


def load_manifests(
    plugin: DiscoveredPlugin,
    *,
    extra_paths: Sequence[Path] = (),
    timeout: float = WORKER_TIMEOUT_SECONDS,
    python: Path | None = None,
) -> ManifestLoad:
    """Ask a worker process for the plugin's manifests. The plugin's code runs only in
    that process."""
    env = {k: os.environ[k] for k in _WORKER_ENV_KEEP if k in os.environ}
    if extra_paths:
        env["PYTHONPATH"] = os.pathsep.join(str(p) for p in extra_paths)
    # A private working directory, so a module in the user's project (first on sys.path
    # under `-m`) can never shadow the plugin.
    workdir = tempfile.mkdtemp(prefix="aibench-manifest-")
    result = run_contained(
        [str(python or sys.executable), "-m", "aibench.registry.worker", plugin.target],
        cwd=workdir,
        timeout=timeout,
        env=env,
        max_output_bytes=MAX_WORKER_OUTPUT_BYTES,
    )
    shutil.rmtree(workdir, ignore_errors=True)
    if result.timed_out:
        return ManifestLoad(plugin, error=f"manifest worker timed out after {timeout}s")
    if result.truncated:
        return ManifestLoad(plugin, error="manifest worker output exceeded the size limit")
    if result.returncode != 0:
        return ManifestLoad(
            plugin,
            error=f"manifest worker exited {result.returncode}: {worker_failure(result.stderr)}",
        )
    try:
        raw = json.loads(result.stdout)
        manifests = tuple(EvaluatorManifest.model_validate(item) for item in raw)
    except (ValueError, TypeError, PydanticValidationError) as exc:
        return ManifestLoad(
            plugin, error=f"manifest worker returned invalid manifests: {exc}"[:500]
        )
    return ManifestLoad(plugin, manifests=manifests)
