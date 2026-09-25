"""Shared setup for tests against the REAL DeepEval plugin environment
(plugins/deepeval/.venv, or AIBENCH_DEEPEVAL_PYTHON). Tests using it skip when it is absent."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.runner_support import REPO_ROOT

PLUGIN_ENV = Path(
    os.environ.get("AIBENCH_DEEPEVAL_PYTHON")
    or REPO_ROOT
    / "plugins"
    / "deepeval"
    / ".venv"
    / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
)
JUDGES = REPO_ROOT / "tests" / "fixtures" / "deepeval_judges"

requires_plugin_env = pytest.mark.skipif(
    not PLUGIN_ENV.is_file(),
    reason=f"DeepEval plugin environment not installed at {PLUGIN_ENV} (see plugins/deepeval/README.md)",
)


def plugin_python(code: str) -> Any:
    """Run a snippet in the plugin environment and return the JSON its last line prints."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    env.update(PYTHONPATH=str(JUDGES), DEEPEVAL_TELEMETRY_OPT_OUT="1", DEEPEVAL_DISABLE_DOTENV="1")
    done = subprocess.run(
        [str(PLUGIN_ENV), "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
        cwd=JUDGES.parent,
        check=False,  # the returncode is asserted below with stderr for context
    )
    assert done.returncode == 0, done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])
