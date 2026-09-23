"""The packaged quickstart project that `aibench init` writes (11-T4).

Ten support cases, a standard-library CLI support assistant with observable retrieval,
an executable plan with two predeclared release gates, a conservative local policy and a
project config. Creating it copies files only: nothing is installed or executed.

The app config's interpreter is written as the absolute path of the Python running
`aibench init`, so the fixture runs without relying on `python` being on PATH.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

FILES = (
    "aibench.json",
    "support.app.json",
    "support_app.py",
    "dataset.jsonl",
    "plan.json",
    "policy.json",
)


class ProjectExists(Exception):
    """Some quickstart files already exist; nothing was written."""

    def __init__(self, paths: list[Path]) -> None:
        super().__init__(", ".join(str(p) for p in paths))
        self.paths = paths


def _source(name: str) -> str:
    return (resources.files(__package__) / "files" / name).read_text(encoding="utf-8")


def create_project(target: Path, *, python: str) -> list[Path]:
    """Write the quickstart into `target`. Refuses, writing nothing, if any file exists."""
    existing = [target / name for name in FILES if (target / name).exists()]
    if existing:
        raise ProjectExists(existing)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name in FILES:
        text = _source(name)
        if name == "support.app.json":
            config = json.loads(text)
            config["transport"]["argv"][0] = python
            text = json.dumps(config, indent=2) + "\n"
        path = target / name
        with path.open("x", encoding="utf-8", newline="\n") as handle:  # never overwrite
            handle.write(text)
        written.append(path)
    return written
