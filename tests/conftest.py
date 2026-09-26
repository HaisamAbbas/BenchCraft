"""Best-effort selection of pytest's `tmp_path`/`tmp_path_factory` base directory.

A code review of this project ran in a sandboxed environment where pytest's default
temp-directory mechanism failed with permission errors ("62 passed, 12 errors", all in tests
using `tmp_path`). An earlier fix pointed `basetemp` at a fixed repo-local `.pytest-tmp/`
directory, which did not resolve it either — in a sandbox that restricts filesystem writes to
a specific allow-listed location, no path chosen from *inside* this repository can reliably
be writable, because the restriction is about which paths the sandbox permits, not about
directory ownership or staleness.

This version tries, in order, to find a location that is *actually* writable right now
(verified with a real write, not assumed), and only overrides `basetemp` if one is found:

1. `AIBENCH_TEST_TMPDIR` environment variable, if set — the explicit override for a sandbox
   that knows which directory it allows. Set this before invoking pytest if the sandbox
   confines writes to a specific path, e.g. `AIBENCH_TEST_TMPDIR=/allowed/scratch pytest -q`.
2. A fresh, uniquely named directory under `<repo>/.pytest-tmp/` (gitignored). Unique per
   process so a directory from a previous run (possibly created under different sandbox
   permissions) is never reused.
3. A fresh directory under the system temp directory via `tempfile.mkdtemp()`.

If none of these can be created and write-probed successfully, `basetemp` is left untouched
and pytest falls back to its own default behavior — this function never raises, so a fully
restricted sandbox still gets pytest's normal (if still failing) default rather than a
conftest crash on top of it.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest


def _probe_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / f".write-probe-{uuid.uuid4().hex}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def pytest_configure(config: pytest.Config) -> None:
    if config.option.basetemp:
        return

    env_override = os.environ.get("AIBENCH_TEST_TMPDIR")
    if env_override:
        candidate = Path(env_override)
        if _probe_writable(candidate):
            config.option.basetemp = str(candidate)
            return

    repo_candidate = Path(str(config.rootdir)) / ".pytest-tmp" / uuid.uuid4().hex[:8]
    if _probe_writable(repo_candidate):
        config.option.basetemp = str(repo_candidate)
        return

    try:
        system_candidate = Path(tempfile.mkdtemp(prefix="aibench-pytest-"))
    except OSError:
        return  # no writable location found anywhere; leave pytest's own default in place
    if _probe_writable(system_candidate):
        config.option.basetemp = str(system_candidate)


@pytest.fixture(autouse=True)
def _isolated_user_settings(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every test, and every process it starts, sees its own per-user settings with
    first-run setup already skipped: no real ~/.benchcraft, no setup questions in a chat.
    Tests of setup itself point BENCHCRAFT_HOME elsewhere."""
    home = tmp_path_factory.mktemp("benchcraft-home")
    (home / "config.json").write_text('{"provider": null}\n', encoding="utf-8")
    monkeypatch.setenv("BENCHCRAFT_HOME", str(home))
