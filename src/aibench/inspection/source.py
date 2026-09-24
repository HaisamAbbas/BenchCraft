"""Static repository inspection (§8 "Architecture understanding", §18 step 4, 16-T1).

Reads an approved source tree and reports what it *suggests* about the application: which
retrieval, model, tool or serving libraries it depends on or imports, each with the
evidence location (`path:line`). Nothing is executed, imported or sent anywhere:

- only roots the policy approves (`inspection_roots`) are read;
- only manifests (`pyproject.toml`, `requirements*.txt`, `package.json`, `Dockerfile`)
  and the import statements of Python (parsed with `ast`) and JavaScript/TypeScript
  (matched line by line) are read; other content is not retained;
- secret-looking files (`.env`, keys, credentials), binaries, large files and vendored or
  generated directories are skipped, and the skips are counted.

Every finding is `inferred`. A dependency in a manifest or an import in code does not show
that the application uses it at runtime: it may be unused, behind a flag, type-checking only
or test code, and those contexts are labelled. Inferred findings never make a metric
eligible; only declared configuration or observed executions do (16-G1).
"""

from __future__ import annotations

import ast
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import Field

from aibench.core.errors import PolicyError
from aibench.core.models import FrozenModel, ObservationState
from aibench.security.policy import ExecutionPolicy

SOURCE_SCOPE = (
    "manifests and import statements of the approved source tree; nothing executed; every "
    "finding is inferred, not a runtime capability"
)

# Library (top-level module or package name, lower case) -> capability it suggests.
LIBRARY_HINTS: dict[str, tuple[str, str]] = {
    # retrieval: vector stores, retrieval frameworks
    "chromadb": ("retrieval", "vector store"),
    "faiss": ("retrieval", "vector index"),
    "pinecone": ("retrieval", "vector store"),
    "qdrant_client": ("retrieval", "vector store"),
    "weaviate": ("retrieval", "vector store"),
    "pgvector": ("retrieval", "vector store"),
    "llama_index": ("retrieval", "retrieval framework"),
    "haystack": ("retrieval", "retrieval framework"),
    "langchain_community": ("retrieval", "retrieval integrations"),
    "@pinecone-database/pinecone": ("retrieval", "vector store"),
    "chromadb-client": ("retrieval", "vector store"),
    # model providers (usage may be reportable)
    "openai": ("model_provider", "OpenAI client"),
    "anthropic": ("model_provider", "Anthropic client"),
    "litellm": ("model_provider", "model router"),
    "google.generativeai": ("model_provider", "Gemini client"),
    "@anthropic-ai/sdk": ("model_provider", "Anthropic client"),
    # agents and tools
    "langgraph": ("tool_use", "agent graph"),
    "crewai": ("tool_use", "agent framework"),
    "autogen": ("tool_use", "agent framework"),
    "langchain.agents": ("tool_use", "agent framework"),
    "smolagents": ("tool_use", "agent framework"),
    # frameworks that could be either
    "langchain": ("llm_framework", "LLM framework"),
    "langchain_core": ("llm_framework", "LLM framework"),
    # serving
    "fastapi": ("http_service", "HTTP framework"),
    "flask": ("http_service", "HTTP framework"),
    "express": ("http_service", "HTTP framework"),
    # tracing
    "opentelemetry": ("tracing", "OpenTelemetry instrumentation"),
    "@opentelemetry/api": ("tracing", "OpenTelemetry instrumentation"),
}

_SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "env", "node_modules", "__pycache__", ".aibench",
     "dist", "build", ".tox", ".mypy_cache", ".ruff_cache", ".pytest_cache", "site-packages"}
)  # fmt: skip
_SECRET_NAME = re.compile(
    r"(^\.env(\..*)?$|\.pem$|\.key$|\.p12$|\.pfx$|^id_(rsa|dsa|ed25519|ecdsa)|secret|credential"
    r"|\.keystore$|\.netrc$|\.npmrc$|\.pypirc$"
    # token files (`token`, `.token`, `api_token.json`), not source such as `tokenizer.py`
    r"|(^|[._-])tokens?(\.(txt|json|ya?ml|ini|cfg|conf|toml))?$)",
    re.IGNORECASE,
)
_TEST_PATH = re.compile(
    r"(^|/)(tests?|spec|__tests__)(/|$)|(^|/)test_[^/]*\.py$|_test\.py$|(^|/)conftest\.py$"
)
_JS_TYPE_IMPORT = re.compile(r"^\s*(?:import|export)\s+type\b")
_JS_IMPORT = re.compile(r"""(?:^\s*import\s+(?:[^'"]+\s+from\s+)?|require\(\s*)['"]([^'"]+)['"]""")
_MAX_FILES = 5_000
_MAX_BYTES = 262_144


class SourceEvidence(FrozenModel):
    path: str  # relative to the inspected root, POSIX separators
    line: int | None = None
    kind: str  # "dependency" | "import"
    detail: str  # the dependency or module name, never file contents
    # manifest | code | test_code | type_checking | guarded | commented_out
    context: str = "code"


class SourceFinding(FrozenModel):
    capability: str
    state: ObservationState = ObservationState.INFERRED
    library: str
    summary: str
    evidence: tuple[SourceEvidence, ...]
    caveats: tuple[str, ...] = ()


class SourceInspection(FrozenModel):
    root: str
    scope: str = SOURCE_SCOPE
    findings: tuple[SourceFinding, ...] = ()
    files_read: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    limitations: tuple[str, ...] = (
        "static: a dependency or import does not show use at runtime",
        "only Python and JavaScript/TypeScript imports and common manifests are read",
        "dynamic imports, plugins loaded by name and non-Python/JS code are not seen",
    )

    def capability(self, name: str) -> list[SourceFinding]:
        return [f for f in self.findings if f.capability == name]


@dataclass
class _Collector:
    evidence: dict[str, list[SourceEvidence]] = field(default_factory=dict)

    def add(self, library: str, evidence: SourceEvidence) -> None:
        self.evidence.setdefault(library, []).append(evidence)


def _library(name: str) -> str | None:
    """The hint key a module or package name belongs to (longest dotted prefix first)."""
    name = name.lower().strip()
    if name in LIBRARY_HINTS:
        return name
    parts = name.replace("-", "_").split(".")
    for length in range(len(parts), 0, -1):
        candidate = ".".join(parts[:length])
        if candidate in LIBRARY_HINTS:
            return candidate
    return None


def inspect_source(root: Path, *, policy: ExecutionPolicy) -> SourceInspection:
    """Inspect `root`, which must lie inside one of the policy's `inspection_roots`."""
    root = root.resolve()
    approved = [Path(r).resolve() for r in policy.inspection_roots]
    if not any(root == a or root.is_relative_to(a) for a in approved):
        raise PolicyError(
            f"reading source under {root} is not approved (inspection_roots in the policy)"
        )
    if not root.is_dir():
        raise PolicyError(f"{root} is not a directory")
    collector = _Collector()
    skipped: dict[str, int] = {}
    files_read = 0
    for path in sorted(_walk(root, skipped)):
        if files_read >= _MAX_FILES:
            skipped["file_limit"] = skipped.get("file_limit", 0) + 1
            continue
        relative = path.relative_to(root).as_posix()
        if _SECRET_NAME.search(path.name):
            skipped["secret_like"] = skipped.get("secret_like", 0) + 1
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _MAX_BYTES:
            skipped["too_large"] = skipped.get("too_large", 0) + 1
            continue
        reader = _reader(path)
        if reader is None:
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        if b"\x00" in raw[:4096]:
            skipped["binary"] = skipped.get("binary", 0) + 1
            continue
        files_read += 1
        text = raw.decode("utf-8", errors="replace")
        reader(relative, text, collector)
    return SourceInspection(
        root=str(root),
        findings=tuple(_findings(collector)),
        files_read=files_read,
        skipped=dict(sorted(skipped.items())),
    )


def _is_link(entry: Path) -> bool:
    """A symlink, or a Windows directory junction (which `is_symlink` does not report)."""
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    return entry.is_symlink() or bool(is_junction and is_junction())


def _walk(root: Path, skipped: dict[str, int]) -> list[Path]:
    """Every file under `root`, never leaving it: links are skipped, and so is any entry
    that resolves outside the root (a junction or mount on an older Python)."""
    found = []
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = list(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                outside = _is_link(entry) or not entry.resolve().is_relative_to(root)
            except OSError:
                outside = True
            if outside:
                skipped["symlink"] = skipped.get("symlink", 0) + 1  # never leave the root
                continue
            if entry.is_dir():
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    skipped["directory"] = skipped.get("directory", 0) + 1
                    continue
                stack.append(entry)
            elif entry.is_file():
                found.append(entry)
    return found


def _reader(path: Path) -> Any:
    name = path.name.lower()
    if name == "pyproject.toml":
        return _pyproject
    if name.startswith("requirements") and name.endswith(".txt"):
        return _requirements
    if name == "package.json":
        return _package_json
    if name == "dockerfile" or name.endswith(".dockerfile"):
        return _dockerfile
    if name.endswith(".py"):
        return _python
    if name.endswith((".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx")):
        return _javascript
    return None


def _requirement_name(spec: str) -> str:
    return re.split(r"[\s\[<>=!~;@]", spec.strip(), maxsplit=1)[0]


def _dependency(collector: _Collector, relative: str, line: int | None, name: str) -> None:
    library = _library(name)
    if library is not None:
        collector.add(
            library,
            SourceEvidence(
                path=relative, line=line, kind="dependency", detail=name, context="manifest"
            ),
        )


def _pyproject(relative: str, text: str, collector: _Collector) -> None:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    specs = list(project.get("dependencies", []) or [])
    for extra in (project.get("optional-dependencies", {}) or {}).values():
        specs.extend(extra or [])
    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    specs.extend((poetry.get("dependencies", {}) or {}).keys())
    lines = text.splitlines()
    for spec in specs:
        if not isinstance(spec, str):
            continue
        name = _requirement_name(spec)
        line = next((i for i, row in enumerate(lines, 1) if name in row), None)
        _dependency(collector, relative, line, name)


def _requirements(relative: str, text: str, collector: _Collector) -> None:
    for number, row in enumerate(text.splitlines(), 1):
        row = row.split("#", 1)[0].strip()
        if row and not row.startswith("-"):
            _dependency(collector, relative, number, _requirement_name(row))


def _package_json(relative: str, text: str, collector: _Collector) -> None:
    try:
        data = json.loads(text)
    except ValueError:
        return
    lines = text.splitlines()
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name in (data.get(section) or {}) if isinstance(data, dict) else {}:
            line = next((i for i, row in enumerate(lines, 1) if f'"{name}"' in row), None)
            _dependency(collector, relative, line, name)


def _dockerfile(relative: str, text: str, collector: _Collector) -> None:
    for number, row in enumerate(text.splitlines(), 1):
        match = re.match(r"\s*RUN\s+.*pip\s+install\s+(.*)", row, re.IGNORECASE)
        if match:
            for token in match.group(1).split():
                if not token.startswith("-"):
                    _dependency(collector, relative, number, _requirement_name(token))


class _ImportVisitor(ast.NodeVisitor):
    """Import statements with their context: type-checking only, or guarded by a
    condition, is recorded as such."""

    def __init__(self) -> None:
        self.found: list[tuple[str, int, str]] = []
        self._contexts: list[str] = []

    def visit_If(self, node: ast.If) -> None:
        test = ast.unparse(node.test).replace(" ", "")
        if test in ("TYPE_CHECKING", "typing.TYPE_CHECKING"):
            body, orelse = "type_checking", self._context()  # the else branch runs
        elif test in ("notTYPE_CHECKING", "nottyping.TYPE_CHECKING"):
            body, orelse = self._context(), "type_checking"  # the body runs at runtime
        else:
            body = orelse = "guarded"
        for context, children in ((body, node.body), (orelse, node.orelse)):
            self._contexts.append(context)
            for child in children:
                self.visit(child)
            self._contexts.pop()

    def visit_Try(self, node: ast.Try) -> None:
        self._contexts.append("guarded")  # an optional import: try / except ImportError
        self.generic_visit(node)
        self._contexts.pop()

    def visit_With(self, node: ast.With) -> None:
        # `with suppress(ImportError):` is an optional import too.
        guarded = any("suppress" in ast.unparse(item.context_expr) for item in node.items)
        self._contexts.append("guarded" if guarded else self._context())
        self.generic_visit(node)
        self._contexts.pop()

    def _context(self) -> str:
        return self._contexts[-1] if self._contexts else "code"

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.found.append((alias.name, node.lineno, self._context()))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module and not node.level:
            self.found.append((node.module, node.lineno, self._context()))
            for alias in node.names:
                self.found.append((f"{node.module}.{alias.name}", node.lineno, self._context()))


def _python(relative: str, text: str, collector: _Collector) -> None:
    test_code = bool(_TEST_PATH.search(relative))
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    visitor = _ImportVisitor()
    visitor.visit(tree)
    seen: set[tuple[str, int]] = set()
    for module, line, context in visitor.found:
        library = _library(module)
        if library is None or (library, line) in seen:
            continue
        seen.add((library, line))
        collector.add(
            library,
            SourceEvidence(
                path=relative,
                line=line,
                kind="import",
                detail=module,
                context="test_code" if test_code else context,
            ),
        )
    for number, row in enumerate(text.splitlines(), 1):
        match = re.match(r"\s*#\s*(?:from\s+(\S+)\s+import|import\s+(\S+))", row)
        if match:
            library = _library(match.group(1) or match.group(2))
            if library is not None:
                collector.add(
                    library,
                    SourceEvidence(
                        path=relative,
                        line=number,
                        kind="import",
                        detail=match.group(1) or match.group(2),
                        context="commented_out",
                    ),
                )


def _javascript(relative: str, text: str, collector: _Collector) -> None:
    test_code = bool(_TEST_PATH.search(relative)) or ".test." in relative or ".spec." in relative
    in_block = False  # inside /* ... */
    for number, row in enumerate(text.splitlines(), 1):
        stripped = row.strip()
        commented = in_block or stripped.startswith(("//", "/*", "*"))
        if "/*" in stripped and "*/" not in stripped.split("/*", 1)[1]:
            in_block = True
        elif in_block and "*/" in stripped:
            in_block = False
        for match in _JS_IMPORT.finditer(stripped.lstrip("/* ")):
            library = _library(match.group(1))
            if library is not None:
                if commented:
                    context = "commented_out"
                elif _JS_TYPE_IMPORT.match(stripped):
                    context = "type_checking"  # `import type` is erased at compile time
                else:
                    context = "test_code" if test_code else "code"
                collector.add(
                    library,
                    SourceEvidence(
                        path=relative, line=number, kind="import", detail=match.group(1),
                        context=context,
                    ),
                )  # fmt: skip


_CONTEXT_WORDS = {
    "test_code": "in test code",
    "type_checking": "for type checking",
    "guarded": "under a condition or optional-import guard",
    "commented_out": "in commented-out code",
    "code": "in code",
}


def _findings(collector: _Collector) -> list[SourceFinding]:
    findings = []
    for library, evidence in sorted(collector.evidence.items()):
        capability, what = LIBRARY_HINTS[library]
        imports = [e for e in evidence if e.kind == "import"]
        live = [e for e in imports if e.context == "code"]
        caveats = ["inferred from source; not a runtime observation"]
        if not imports:
            caveats.append("listed as a dependency but never imported by the code read")
        elif not live:
            contexts = sorted({_CONTEXT_WORDS[e.context] for e in imports})
            caveats.append(f"only imported {' or '.join(contexts)}")
        summary = f"{what} ({library}): " + (
            f"imported in {len({e.path for e in live})} file(s)" if live else "no live import found"
        )
        findings.append(
            SourceFinding(
                capability=capability,
                library=library,
                summary=summary,
                evidence=tuple(evidence[:20]),
                caveats=tuple(caveats),
            )
        )
    return findings
