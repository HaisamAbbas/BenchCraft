"""A plugin updated without the matching BenchCraft core (`pip install --no-deps` of a new
adapter into an environment holding an older aibench) failed to import there. The session
reopened with "loaded this project's plugins" in dim text, its new draft had silently lost
every DeepEval metric, and the only explanation, kept for /run, was the last line of the
traceback: pydantic's "For further information visit https://errors.pydantic.dev/...".

Now the error names the exception, says the environment holds a different BenchCraft and
how to update it, and the chat says so in yellow when the session is reopened."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from aibench import __version__
from aibench.cli.chat import _with_project_plugins
from aibench.registry.discovery import (
    discover_plugins,
    environment_site_paths,
    load_manifests,
    worker_failure,
)
from tests.session_support import SessionHarness

SRC = Path(__file__).resolve().parents[1] / "src"
PLUGIN = "aibench_core_mismatch_plugin"

# What an adapter built for a newer core does under an older one: its manifest uses a value
# the older core's schema does not have.
NEWER_ADAPTER = """
from aibench.core.models import EvaluatorManifest, MetricDirection
EvaluatorManifest(
    evaluator_id="vendor.paired", version="1.0.0", plugin_id="vendor", plugin_version="1.0.0",
    description="needs a newer core", value_kind="scalar", direction=MetricDirection.NONE,
    aggregation="mean", consumes="from_the_future",
)
EVALUATORS = ()
"""


def _dist(site: Path, name: str, version: str, entry_points: str = "") -> None:
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8"
    )
    if entry_points:
        (info / "entry_points.txt").write_text(entry_points, encoding="utf-8")


def test_a_crash_is_described_by_its_exception_not_its_last_line() -> None:
    stderr = (
        b"Traceback (most recent call last):\n"
        b'  File "x.py", line 1, in <module>\n'
        b"pydantic_core._pydantic_core.ValidationError: 1 validation error for EvaluatorManifest\n"
        b"consumes\n"
        b"  Input should be 'recorded_outputs' or 'owns_execution' [type=literal_error]\n"
        b"    For further information visit https://errors.pydantic.dev/2.13/v/literal_error\n"
    )
    said = worker_failure(stderr)
    assert said.startswith("pydantic_core._pydantic_core.ValidationError: 1 validation error")
    assert "consumes Input should be 'recorded_outputs'" in said
    assert "errors.pydantic.dev" not in said
    assert worker_failure(b"") == ""
    assert worker_failure(b"killed\n") == "killed"


def test_a_plugin_rejected_by_the_core_says_what_was_rejected(tmp_path: Path) -> None:
    site = tmp_path / "site"
    _dist(
        site,
        "core-mismatch-plugin",
        "1.0.0",
        f"[aibench.evaluators]\npaired = {PLUGIN}:EVALUATORS\n",
    )
    (site / f"{PLUGIN}.py").write_text(NEWER_ADAPTER, encoding="utf-8")
    [plugin] = discover_plugins(paths=[site])
    error = load_manifests(plugin, extra_paths=[site]).error or ""
    assert "ValidationError" in error and "consumes" in error and "Input should be" in error
    assert "errors.pydantic.dev" not in error


def test_reopening_a_session_says_its_plugin_needs_the_matching_core(
    tmp_path: Path, capsys
) -> None:  # type: ignore[no-untyped-def]
    """A real second environment whose installed aibench is an older release."""
    venv = tmp_path / "plugin-env"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True)
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    [site, *_] = environment_site_paths(python)
    _dist(site, "aibench", "0.1.0rc24")  # the old core pip left in place
    _dist(
        site,
        "core-mismatch-plugin",
        "1.0.0",
        f"[aibench.evaluators]\npaired = {PLUGIN}:EVALUATORS\n",
    )
    (site / f"{PLUGIN}.py").write_text(NEWER_ADAPTER, encoding="utf-8")
    # The environment imports aibench from this checkout and its dependencies from ours.
    host_site = next(p for p in sys.path if p.endswith("site-packages"))
    paths = [str(SRC), host_site]

    h = SessionHarness(tmp_path / "project")
    ctl = h.open_session(
        {"a": "answer"},
        objectives=("catch wrong answers",),
        policy={
            "allowed_plugin_environments": [str(python)],
            "allowed_plugin_paths": paths,
        },
    )
    try:
        (h.root / "aibench.json").write_text(
            json.dumps(
                {"plugin_environments": [{"name": "paired", "python": str(python), "paths": paths}]}
            ),
            encoding="utf-8",
        )
        capsys.readouterr()
        _with_project_plugins(ctl, h.root)
        said = " ".join(capsys.readouterr().err.split())
        assert "loaded this project's plugins into the session" in said
        assert (
            f"the plan cannot run: plugin paired: its environment has BenchCraft 0.1.0rc24, "
            f"not {__version__}; `/plugins install paired` updates it" in said
        ), said
        assert "consumes" in said and "errors.pydantic.dev" not in said
    finally:
        ctl.storage.db.close()
