"""Controlled discovery (04-T2): installed-plugin metadata is read without importing plugin
code into this process; manifests are loaded in a separate worker process."""

from __future__ import annotations

import sys
from pathlib import Path

from aibench.registry.discovery import DiscoveredPlugin, discover_plugins, load_manifests

PLUGIN_MODULE = "aibench_test_plugin_xyz"


def _install_fake_plugin(root: Path, body: str) -> Path:
    """A real distribution layout (dist-info + module) on a path of our choosing — the same
    shape pip leaves behind, without running any installer."""
    dist = root / "aibench_test_plugin_xyz-0.3.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: aibench-test-plugin-xyz\nVersion: 0.3.0\n", encoding="utf-8"
    )
    (dist / "entry_points.txt").write_text(
        f"[aibench.evaluators]\nlength = {PLUGIN_MODULE}:EVALUATORS\n", encoding="utf-8"
    )
    (root / f"{PLUGIN_MODULE}.py").write_text(body, encoding="utf-8")
    return root


PLUGIN_BODY = f'''
import os, pathlib
# Import side effect: records which process imported this module.
pathlib.Path(os.environ.get("TEMP", "."), "{PLUGIN_MODULE}.imported").write_text(str(os.getpid()))
from aibench.core.models import EvaluatorManifest, MetricDirection
from aibench.evaluators.protocol import Evaluator, EvaluationOutcome

class Length(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="vendor.length", version="0.3.0", plugin_id="vendor", plugin_version="0.3.0",
        description="output length", value_kind="scalar", direction=MetricDirection.NONE,
        aggregation="mean",
    )
    async def evaluate(self, view, ctx):
        return EvaluationOutcome.ok("scalar", len(view.get("execution.output")))

EVALUATORS = (Length,)
'''


def test_discovery_reads_metadata_without_importing_the_plugin(tmp_path: Path) -> None:
    root = _install_fake_plugin(tmp_path / "site", PLUGIN_BODY)
    plugins = discover_plugins(paths=[root])
    assert plugins == [
        DiscoveredPlugin(
            name="length",
            target=f"{PLUGIN_MODULE}:EVALUATORS",
            distribution="aibench-test-plugin-xyz",
            version="0.3.0",
        )
    ]
    assert PLUGIN_MODULE not in sys.modules


def test_manifests_are_loaded_in_a_worker_process_only(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("TEMP", str(tmp_path))
    root = _install_fake_plugin(tmp_path / "site", PLUGIN_BODY)
    [plugin] = discover_plugins(paths=[root])
    loaded = load_manifests(plugin, extra_paths=[root])
    assert loaded.error is None, loaded.error
    assert [m.evaluator_id for m in loaded.manifests] == ["vendor.length"]
    marker = tmp_path / f"{PLUGIN_MODULE}.imported"
    import os

    assert marker.exists() and int(marker.read_text()) != os.getpid()  # imported elsewhere
    assert PLUGIN_MODULE not in sys.modules


def test_broken_and_hanging_plugins_are_contained(tmp_path: Path) -> None:
    broken = _install_fake_plugin(tmp_path / "broken", "raise RuntimeError('boom at import')\n")
    [plugin] = discover_plugins(paths=[broken])
    result = load_manifests(plugin, extra_paths=[broken])
    assert result.manifests == () and "boom at import" in (result.error or "")

    hanging = _install_fake_plugin(tmp_path / "hanging", "import time\ntime.sleep(60)\n")
    [plugin] = discover_plugins(paths=[hanging])
    result = load_manifests(plugin, extra_paths=[hanging], timeout=2)
    assert "timed out" in (result.error or "")

    liar = _install_fake_plugin(tmp_path / "liar", "EVALUATORS = (object,)\n")
    [plugin] = discover_plugins(paths=[liar])
    result = load_manifests(plugin, extra_paths=[liar])
    assert "not an Evaluator subclass" in (result.error or "")


def test_worker_timeout_kills_plugin_grandchildren(tmp_path: Path) -> None:
    """Review #4: a plugin that spawns a long-lived grandchild must not stall discovery
    past the timeout, and the grandchild must not survive it."""
    from tests.runner_support import read_pid, wait_until_dead

    pidfile = tmp_path / "grandchild.pid"
    body = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(60)\n"
    )
    root = _install_fake_plugin(tmp_path / "spawner", body)
    [plugin] = discover_plugins(paths=[root])
    import time

    # The timeout must leave the worker time to start and import the plugin (which spawns
    # the grandchild) even on a loaded machine; it is still far below the grandchild's 60s.
    started = time.monotonic()
    result = load_manifests(plugin, extra_paths=[root], timeout=10)
    assert time.monotonic() - started < 30
    assert "timed out" in (result.error or "")
    assert wait_until_dead(read_pid(pidfile))


def test_worker_output_cap_is_enforced_while_the_worker_runs(tmp_path: Path) -> None:
    """Second review P3: a worker flooding stdout is stopped at the cap, not at the timeout,
    so it cannot fill the disk or memory in the meantime."""
    import os
    import sys
    import time

    from aibench.runners.process_tree import run_contained

    flood = "import sys\nwhile True:\n    sys.stdout.write('x' * 65536)\n"
    started = time.monotonic()
    result = run_contained(
        [sys.executable, "-c", flood],
        timeout=60,
        env={k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")},
        max_output_bytes=1_000_000,
    )
    assert time.monotonic() - started < 20
    assert result.truncated and not result.timed_out
    assert len(result.stdout) == 1_000_000
