"""Dependency direction (ADR 0001, 04-G4): core models never import evaluator frameworks
or terminal UI packages, and no first-party module imports an evaluator framework
(framework adapters live in separately packaged plugins).

Checked two ways: statically (every import statement in the source) and at runtime
(importing `aibench.core` in a fresh interpreter must not load forbidden modules)."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "aibench"
EVALUATOR_FRAMEWORKS = {"deepeval", "hermes", "evals", "openai_evals", "ragas", "promptfoo"}
UI_PACKAGES = {"rich", "typer", "click", "prompt_toolkit", "textual", "curses"}
CORE_ALLOWED_THIRD_PARTY = {"pydantic"}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
    return names


def _top(name: str) -> str:
    return name.split(".", 1)[0]


def test_core_imports_only_stdlib_pydantic_and_core() -> None:
    offenders = {}
    for path in (SRC / "core").rglob("*.py"):
        for name in _imports(path):
            top = _top(name)
            if top == "aibench":
                if not name.startswith("aibench.core"):
                    offenders.setdefault(path.name, []).append(name)
            elif top not in sys.stdlib_module_names and top not in CORE_ALLOWED_THIRD_PARTY:
                offenders.setdefault(path.name, []).append(name)
    assert offenders == {}


def test_no_first_party_module_imports_an_evaluator_framework() -> None:
    offenders = {
        str(path.relative_to(SRC)): sorted(
            name for name in _imports(path) if _top(name) in EVALUATOR_FRAMEWORKS
        )
        for path in SRC.rglob("*.py")
    }
    assert {k: v for k, v in offenders.items() if v} == {}


def test_importing_core_loads_no_framework_or_ui_module() -> None:
    probe = (
        "import json, sys; import aibench.core.models, aibench.core.hashes, aibench.core.errors; "
        "print(json.dumps(sorted({m.split('.')[0] for m in sys.modules})))"
    )
    loaded = set(
        json.loads(
            subprocess.run(
                [sys.executable, "-c", probe], capture_output=True, check=True, text=True
            ).stdout
        )
    )
    assert loaded & (EVALUATOR_FRAMEWORKS | UI_PACKAGES) == set()


def test_evaluator_protocol_and_registry_stay_free_of_ui_packages() -> None:
    for package in ("evaluators", "registry", "reporting", "services"):
        for path in (SRC / package).rglob("*.py"):
            ui = {name for name in _imports(path) if _top(name) in UI_PACKAGES}
            assert ui == set(), f"{path.relative_to(SRC)} imports {ui}"
