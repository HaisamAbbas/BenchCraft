"""Repository inspection and controlled probes (16-T1, 16-G1).

End to end through `aibench inspect` on fixture repositories in `examples/inspection/`:
a support bot whose manifest and imports suggest retrieval, tools and a model provider it
never uses, and an instrumented RAG app that reports what it retrieved. Source findings
are always inferred; only declared configuration or observed executions make a
capability available to planning."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.core.errors import PolicyError
from aibench.inspection.dataset_summary import summarize_dataset
from aibench.inspection.profile import inspect_application
from aibench.inspection.source import inspect_source
from aibench.planning.catalog import build_catalog
from aibench.security.policy import ExecutionPolicy
from tests.planning_support import GROUNDED, registry_with

REPO = Path(__file__).resolve().parents[1]
FIXTURES = REPO / "examples" / "inspection"
cli = CliRunner()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "inspection"
    shutil.copytree(FIXTURES, root)
    return root


def _inspect(*args: str, code: int = 0) -> dict[str, Any]:
    result = cli.invoke(app, ["inspect", *args, "--json"])
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if code == 0 else {}


def test_misleading_imports_stay_inferred_and_never_confirm_a_capability(repo: Path) -> None:
    data = _inspect(
        str(repo / "misleading_rag" / "app.json"),
        "--source", str(repo / "misleading_rag"),
        "--policy", str(repo / "policy.json"),
    )  # fmt: skip
    profile = data["profile"]
    findings = {f["library"]: f for f in profile["source_findings"]}
    assert set(findings) == {"chromadb", "faiss", "fastapi", "langchain.agents", "openai"}
    assert {f["state"] for f in findings.values()} == {"inferred"}
    contexts = {lib: [(e["path"], e["line"], e["context"]) for e in f["evidence"]]
                for lib, f in findings.items()}  # fmt: skip
    assert contexts["chromadb"] == [
        ("app.py", 14, "type_checking"),
        ("pyproject.toml", 6, "manifest"),
    ]
    assert contexts["faiss"] == [("tests/test_retrieval.py", 1, "test_code")]
    assert contexts["langchain.agents"] == [("app.py", 16, "commented_out")]
    assert findings["chromadb"]["caveats"][1] == "only imported for type checking"
    assert findings["openai"]["caveats"][1] == (
        "listed as a dependency but never imported by the code read"
    )
    # The claims are untouched: retrieval is still unknown, and the gap says why.
    claims = {c["capability"]: c["state"] for c in profile["claims"]}
    assert claims["retrieved_context"] == claims["tool_events"] == "unknown"
    assert any("source suggests retrieval (chromadb at app.py:14" in g for g in profile["gaps"])


def test_secret_files_are_skipped_and_never_echoed(repo: Path) -> None:
    tree = inspect_source(
        repo / "misleading_rag",
        policy=ExecutionPolicy(inspection_roots=(str(repo),)),
    )
    assert tree.skipped.get("secret_like", 0) >= 1
    assert "sk-this-file-must-never-be-read" not in tree.model_dump_json()
    assert all(e.path != ".env" for f in tree.findings for e in f.evidence)


def test_inferred_findings_never_make_a_metric_eligible(repo: Path) -> None:
    """16-G1: a groundedness judge needs retrieval the application reports; a source tree
    that imports a vector store does not make it computable."""
    policy = ExecutionPolicy(
        inspection_roots=(str(repo),), allowed_evaluators=("*",), allow_model_evaluators=True
    )
    tree = inspect_source(repo / "misleading_rag", policy=policy)
    profile = inspect_application(repo / "misleading_rag" / "app.json", source_tree=tree)
    dataset = summarize_dataset(repo / "questions.jsonl")
    [grounded] = [
        o for o in build_catalog(registry_with(GROUNDED), profile, dataset, policy)
        if o.evaluator_id == GROUNDED.evaluator_id
    ]  # fmt: skip
    assert grounded.eligible is False
    assert profile.available("retrieved_context") is False


def test_reading_source_needs_an_approved_root(repo: Path) -> None:
    with pytest.raises(PolicyError, match="not approved"):
        inspect_source(repo / "misleading_rag", policy=ExecutionPolicy())
    result = cli.invoke(
        app,
        ["inspect", str(repo / "misleading_rag" / "app.json"), "--source",
         str(repo / "misleading_rag")],
    )  # fmt: skip
    assert result.exit_code == 2 and "inspection_roots" in result.output


def test_a_probe_turns_a_declaration_into_an_observation(repo: Path, tmp_path: Path) -> None:
    data = _inspect(
        str(repo / "instrumented_rag" / "app.json"),
        "--source", str(repo / "instrumented_rag"),
        "--policy", str(repo / "policy.json"),
        "--dataset", str(repo / "questions.jsonl"),
        "--probe", "2",
        "--workspace", str(tmp_path / "ws"),
    )  # fmt: skip
    profile = data["profile"]
    claim = next(c for c in profile["claims"] if c["capability"] == "retrieved_context")
    assert claim["state"] == "observed" and claim["scope"].startswith("2 of 2")
    assert any(note.startswith("probed 2 case(s) in run smoke-") for note in data["notes"])
    [finding] = profile["source_findings"]
    assert finding["library"] == "chromadb" and finding["state"] == "inferred"
    assert "optional-import guard" in finding["caveats"][1]


def test_a_probe_the_policy_does_not_permit_invokes_nothing(repo: Path, tmp_path: Path) -> None:
    (repo / "strict.json").write_text(json.dumps({"inspection_roots": ["."]}), encoding="utf-8")
    result = cli.invoke(
        app,
        ["inspect", str(repo / "instrumented_rag" / "app.json"), "--policy",
         str(repo / "strict.json"), "--dataset", str(repo / "questions.jsonl"), "--probe", "1",
         "--workspace", str(tmp_path / "ws")],
    )  # fmt: skip
    assert result.exit_code == 4 and "trusted-local" in result.output
    assert not (tmp_path / "ws" / ".aibench" / "aibench.db").exists()


def test_javascript_manifests_and_imports(tmp_path: Path) -> None:
    root = tmp_path / "js"
    root.mkdir()
    (root / "package.json").write_text(
        json.dumps({"dependencies": {"@pinecone-database/pinecone": "1", "express": "4"}}),
        encoding="utf-8",
    )
    (root / "index.js").write_text(
        'const OpenAI = require("openai");\n// import { graph } from "langgraph";\n',
        encoding="utf-8",
    )
    tree = inspect_source(root, policy=ExecutionPolicy(inspection_roots=(str(root),)))
    found = {f.library: [e.context for e in f.evidence] for f in tree.findings}
    assert found == {
        "@pinecone-database/pinecone": ["manifest"],
        "express": ["manifest"],
        "langgraph": ["commented_out"],
        "openai": ["code"],
    }


def test_a_directory_junction_or_link_never_leads_outside_the_root(tmp_path: Path) -> None:
    """Windows junctions are not symlinks to `is_symlink()`; following one would read files
    outside the approved root (review finding)."""
    import subprocess
    import sys

    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "private.py").write_text("import openai\n", encoding="utf-8")
    link = root / "linked"
    if sys.platform == "win32":
        made = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True,
            check=False,
        )
        assert made.returncode == 0, made.stderr
    else:
        link.symlink_to(outside, target_is_directory=True)
    tree = inspect_source(root, policy=ExecutionPolicy(inspection_roots=(str(root),)))
    assert tree.files_read == 0 and not tree.findings
    assert tree.skipped == {"symlink": 1}
    with pytest.raises(PolicyError, match="not approved"):
        inspect_source(link, policy=ExecutionPolicy(inspection_roots=(str(root),)))


def test_import_contexts_that_do_not_show_runtime_use(tmp_path: Path) -> None:
    root = tmp_path / "contexts"
    (root / "pkg").mkdir(parents=True)
    (root / "conftest.py").write_text("import openai\n", encoding="utf-8")
    (root / "pkg" / "optional.py").write_text(
        "from contextlib import suppress\nwith suppress(ImportError):\n    import chromadb\n",
        encoding="utf-8",
    )
    (root / "pkg" / "runtime.py").write_text(
        "from typing import TYPE_CHECKING\nif not TYPE_CHECKING:\n    import fastapi\n"
        "else:\n    import langgraph\n",
        encoding="utf-8",
    )
    (root / "pkg" / "tokenizer.py").write_text("import anthropic\n", encoding="utf-8")
    (root / "pkg" / "api_token.json").write_text('{"t": "x"}', encoding="utf-8")
    (root / "types.ts").write_text(
        "import type { Index } from '@pinecone-database/pinecone';\n"
        "/*\nimport express from 'express';\n*/\n",
        encoding="utf-8",
    )
    tree = inspect_source(root, policy=ExecutionPolicy(inspection_roots=(str(root),)))
    found = {f.library: [e.context for e in f.evidence] for f in tree.findings}
    assert found == {
        "openai": ["test_code"],
        "chromadb": ["guarded"],
        "fastapi": ["code"],  # `if not TYPE_CHECKING:` runs at runtime
        "langgraph": ["type_checking"],
        "anthropic": ["code"],  # tokenizer.py is source, not a token file
        "@pinecone-database/pinecone": ["type_checking"],
        "express": ["commented_out"],
    }
    assert tree.skipped.get("secret_like") == 1  # api_token.json
