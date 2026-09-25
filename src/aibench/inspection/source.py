"""Static repository inspection (16-T1, extended for Prompt 25).

Reads an approved source tree and reports what it *suggests* about the application: which
retrieval, model, tool or serving libraries it depends on or imports, each with the
evidence location (`path:line`). Nothing is executed, imported or sent anywhere:

- only roots the policy approves (`inspection_roots`) are read;
- manifests (`pyproject.toml`, `requirements*.txt`, `package.json`, Dockerfiles), Python
  imports/main guards, and bounded JavaScript/TypeScript import patterns are parsed;
  dataset/test/evaluator files are path-only candidates;
- secret-looking files (`.env`, keys, credentials), binaries, large files and vendored or
  generated directories are skipped, and the skips are counted; file, byte, depth,
  directory-entry and result budgets are explicit.

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
from typing import Any, Literal

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
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        ".aibench",
        "dist",
        "build",
        ".tox",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "site-packages",
        "vendor",
        "bower_components",
        "target",
        "coverage",
        "out",
        "generated",
        ".next",
        ".nuxt",
        "__pypackages__",
    }
)
_SENSITIVE_DIRS = frozenset(
    {"secrets", "secret", "credentials", "credential", "private", "certificates"}
)
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
_MAX_TOTAL_BYTES = 16 * 1024 * 1024


class SourceEvidence(FrozenModel):
    path: str  # relative to the inspected root, POSIX separators
    line: int | None = None
    kind: str  # "dependency" | "import"
    detail: str  # the dependency or module name, never file contents
    # manifest | code | test_code | type_checking | guarded | commented_out
    context: str = "code"


class InspectionBudget(FrozenModel):
    """Hard limits for deterministic repository inspection.

    Upper bounds prevent a caller from turning inspection into an effectively unbounded
    filesystem walk. Dataset candidate files are inventoried by name only and are not read.
    """

    max_files: int = Field(default=_MAX_FILES, gt=0, le=20_000)
    max_file_bytes: int = Field(default=_MAX_BYTES, gt=0, le=1_048_576)
    max_total_bytes: int = Field(default=_MAX_TOTAL_BYTES, gt=0, le=64 * 1024 * 1024)
    max_directories: int = Field(default=2_000, gt=0, le=10_000)
    max_entries_per_directory: int = Field(default=2_000, gt=0, le=10_000)
    max_depth: int = Field(default=32, gt=0, le=128)
    max_discoveries: int = Field(default=2_000, gt=0, le=10_000)


class DiscoveryResult(FrozenModel):
    """A bounded repository clue, not proof that a component works at runtime."""

    kind: Literal[
        "manifest",
        "source_file",
        "test_candidate",
        "dataset_candidate",
        "evaluator_candidate",
        "invocation_candidate",
        "unsupported_source",
    ]
    subject: str
    provenance: ObservationState
    confidence: Literal["high", "medium", "low"]
    summary: str
    evidence: tuple[SourceEvidence, ...]
    limitations: tuple[str, ...] = ()


class SupportedFormat(FrozenModel):
    pattern: str
    purpose: str
    analysis: str


SUPPORTED_FORMATS = (
    SupportedFormat(pattern="pyproject.toml", purpose="Python project manifest", analysis="declared dependencies and entrypoint names"),
    SupportedFormat(pattern="requirements*.txt", purpose="Python dependency manifest", analysis="dependency names"),
    SupportedFormat(pattern="package.json", purpose="Node project manifest", analysis="dependency names and script names"),
    SupportedFormat(pattern="Dockerfile / *.dockerfile", purpose="container manifest", analysis="dependency install hints and entrypoint directives"),
    SupportedFormat(pattern="*.py", purpose="Python source", analysis="imports and __main__ guards via AST; never imported"),
    SupportedFormat(pattern="*.js, *.mjs, *.cjs, *.ts, *.tsx, *.jsx", purpose="JavaScript/TypeScript source", analysis="bounded static import patterns; no full parser"),
    SupportedFormat(pattern="*.jsonl, *.json, *.csv, *.parquet", purpose="possible dataset files", analysis="path/name candidate only; file contents are not read"),
)  # fmt: skip


class SourceFinding(FrozenModel):
    capability: str
    state: ObservationState = ObservationState.INFERRED
    confidence: Literal["high", "medium", "low"] = "medium"
    library: str
    summary: str
    evidence: tuple[SourceEvidence, ...]
    caveats: tuple[str, ...] = ()


class SourceInspection(FrozenModel):
    root: str
    scope: str = SOURCE_SCOPE
    findings: tuple[SourceFinding, ...] = ()
    discoveries: tuple[DiscoveryResult, ...] = ()
    supported_formats: tuple[SupportedFormat, ...] = SUPPORTED_FORMATS
    budget: InspectionBudget = Field(default_factory=InspectionBudget)
    files_read: int = 0
    files_seen: int = 0
    directories_seen: int = 0
    bytes_read: int = 0
    skipped: dict[str, int] = Field(default_factory=dict)
    limitations: tuple[str, ...] = (
        "static: a dependency or import does not show use at runtime",
        "only Python and JavaScript/TypeScript imports and common manifests are read",
        "dynamic imports, plugins loaded by name and non-Python/JS code are not seen",
        "repository text is untrusted data and cannot authorize execution, access, or policy changes",
        "dataset and evaluation files are candidates only; their contents and correctness are not validated",
    )

    def capability(self, name: str) -> list[SourceFinding]:
        return [f for f in self.findings if f.capability == name]


@dataclass
class _Collector:
    evidence: dict[str, list[SourceEvidence]] = field(default_factory=dict)
    discoveries: list[DiscoveryResult] = field(default_factory=list)

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


def inspect_source(
    root: Path,
    *,
    policy: ExecutionPolicy,
    budget: InspectionBudget | None = None,
) -> SourceInspection:
    """Inspect an approved root with a bounded, static pass; never run project code."""
    budget = budget or InspectionBudget()
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
    bytes_read = 0
    usage = {"directories_seen": 0, "files_seen": 0}
    for path in _walk(root, skipped, budget=budget, usage=usage):
        relative = path.relative_to(root).as_posix()
        if _SECRET_NAME.search(path.name):
            skipped["secret_like"] = skipped.get("secret_like", 0) + 1
            continue
        _discover_path(relative, collector, budget, skipped)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        reader = _reader(path)
        if reader is None:
            continue
        if size > budget.max_file_bytes:
            skipped["too_large"] = skipped.get("too_large", 0) + 1
            continue
        remaining_bytes = budget.max_total_bytes - bytes_read
        if remaining_bytes <= 0:
            skipped["total_bytes_limit"] = skipped.get("total_bytes_limit", 0) + 1
            continue
        try:
            with path.open("rb") as stream:
                raw = stream.read(min(budget.max_file_bytes, remaining_bytes) + 1)
        except OSError:
            continue
        if len(raw) > budget.max_file_bytes:
            skipped["too_large"] = skipped.get("too_large", 0) + 1
            continue
        if len(raw) > remaining_bytes:
            skipped["total_bytes_limit"] = skipped.get("total_bytes_limit", 0) + 1
            continue
        if b"\x00" in raw[:4096]:
            skipped["binary"] = skipped.get("binary", 0) + 1
            continue
        files_read += 1
        bytes_read += len(raw)
        text = raw.decode("utf-8", errors="replace")
        reader(relative, text, collector)
        _discover_manifest_invocations(
            path.name.lower(), relative, text, collector, budget, skipped
        )
        if path.suffix.lower() == ".py":
            _discover_python_invocations(relative, text, collector, budget, skipped)
    return SourceInspection(
        root=str(root),
        findings=tuple(_findings(collector)),
        discoveries=tuple(
            sorted(collector.discoveries, key=lambda item: (item.kind, item.subject))
        ),
        budget=budget,
        files_read=files_read,
        files_seen=usage["files_seen"],
        directories_seen=usage["directories_seen"],
        bytes_read=bytes_read,
        skipped=dict(sorted(skipped.items())),
    )


def _add_discovery(
    collector: _Collector,
    result: DiscoveryResult,
    budget: InspectionBudget,
    skipped: dict[str, int],
) -> None:
    if len(collector.discoveries) >= budget.max_discoveries:
        skipped["discovery_limit"] = skipped.get("discovery_limit", 0) + 1
        return
    collector.discoveries.append(result)


def _discovery(
    kind: Literal[
        "manifest",
        "source_file",
        "test_candidate",
        "dataset_candidate",
        "evaluator_candidate",
        "invocation_candidate",
        "unsupported_source",
    ],
    relative: str,
    *,
    summary: str,
    provenance: ObservationState = ObservationState.OBSERVED,
    confidence: Literal["high", "medium", "low"] = "high",
    line: int | None = None,
    detail: str = "file presence",
    limitations: tuple[str, ...] = (),
) -> DiscoveryResult:
    return DiscoveryResult(
        kind=kind,
        subject=relative,
        provenance=provenance,
        confidence=confidence,
        summary=summary,
        evidence=(
            SourceEvidence(
                path=relative,
                line=line,
                kind=kind,
                detail=detail,
                context=provenance.value,
            ),
        ),
        limitations=limitations,
    )


_SUPPORTED_SOURCE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
_UNSUPPORTED_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".go",
    ".java",
    ".php",
    ".rb",
    ".rs",
    ".swift",
}
_DATASET_SUFFIXES = {".csv", ".json", ".jsonl", ".parquet"}
_DATA_DIRS = {"data", "dataset", "datasets", "benchmark", "benchmarks", "golden", "goldens"}
_EVAL_DIRS = {"eval", "evals", "evaluation", "evaluations", "evaluator", "evaluators", "metrics"}


def _discover_path(
    relative: str,
    collector: _Collector,
    budget: InspectionBudget,
    skipped: dict[str, int],
) -> None:
    path = Path(relative)
    name = path.name.lower()
    suffix = path.suffix.lower()
    parts = {part.lower() for part in path.parts[:-1]}
    if (
        name == "pyproject.toml"
        or name == "package.json"
        or name == "dockerfile"
        or name.endswith(".dockerfile")
        or (name.startswith("requirements") and name.endswith(".txt"))
    ):
        _add_discovery(
            collector,
            _discovery(
                "manifest",
                relative,
                summary="Project manifest found; supported fields may be inspected if budgets allow.",
            ),
            budget,
            skipped,
        )
    if suffix in _SUPPORTED_SOURCE_SUFFIXES:
        _add_discovery(
            collector,
            _discovery(
                "source_file",
                relative,
                summary="Supported source file found; supported patterns may be parsed if budgets allow.",
                limitations=(
                    "unsupported patterns remain unknown; budget limits may prevent parsing",
                ),
            ),
            budget,
            skipped,
        )
    if _TEST_PATH.search(relative) or ".test." in name or ".spec." in name:
        _add_discovery(
            collector,
            _discovery(
                "test_candidate",
                relative,
                summary="Test/spec file candidate found; it is not treated as a benchmark dataset or golden.",
                confidence="medium",
                limitations=("test intent and oracle quality are not validated",),
            ),
            budget,
            skipped,
        )
    if parts & _EVAL_DIRS or any(token in name for token in ("eval", "metric")):
        _add_discovery(
            collector,
            _discovery(
                "evaluator_candidate",
                relative,
                summary="Evaluation-related path candidate found; compatibility is unknown.",
                confidence="low",
                limitations=(
                    "the file is not imported or executed; evaluator semantics are not validated",
                ),
            ),
            budget,
            skipped,
        )
    if suffix in _DATASET_SUFFIXES and (
        parts & _DATA_DIRS or any(token in name for token in ("dataset", "golden", "benchmark"))
    ):
        _add_discovery(
            collector,
            _discovery(
                "dataset_candidate",
                relative,
                summary="Dataset-shaped path candidate found; contents and reference quality were not inspected.",
                confidence="medium",
                limitations=("candidate only; not loaded, validated, or promoted",),
            ),
            budget,
            skipped,
        )
    if suffix in _UNSUPPORTED_SOURCE_SUFFIXES:
        _add_discovery(
            collector,
            _discovery(
                "unsupported_source",
                relative,
                summary=f"Source file type {suffix} is outside the tested parser support; code understanding is unknown.",
                provenance=ObservationState.UNKNOWN,
                confidence="low",
                limitations=("file contents were not read or parsed",),
                detail="unsupported source suffix",
            ),
            budget,
            skipped,
        )


def _discover_manifest_invocations(
    name: str,
    relative: str,
    text: str,
    collector: _Collector,
    budget: InspectionBudget,
    skipped: dict[str, int],
) -> None:
    declarations: list[tuple[int | None, str]] = []
    if name == "pyproject.toml":
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return
        project = data.get("project", {})
        poetry = data.get("tool", {}).get("poetry", {})
        scripts = project.get("scripts", {}) if isinstance(project, dict) else {}
        if not scripts and isinstance(poetry, dict):
            scripts = poetry.get("scripts", {})
        if isinstance(scripts, dict):
            for script_name in scripts:
                safe_name = str(script_name)
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", safe_name):
                    line = next(
                        (
                            i
                            for i, row in enumerate(text.splitlines(), 1)
                            if re.match(rf"\s*{re.escape(safe_name)}\s*=", row)
                        ),
                        None,
                    )
                    declarations.append((line, "project script name declared; command omitted"))
    elif name == "package.json":
        try:
            data = json.loads(text)
        except ValueError:
            return
        scripts = data.get("scripts") if isinstance(data, dict) else None
        if isinstance(scripts, dict) and scripts:
            for script_name in scripts:
                if re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", str(script_name)):
                    lines = text.splitlines()
                    scripts_line = next(
                        (i for i, row in enumerate(lines) if '"scripts"' in row), None
                    )
                    line = next(
                        (
                            i + 1
                            for i, row in enumerate(lines)
                            if scripts_line is not None
                            and i > scripts_line
                            and f'"{script_name}"' in row
                        ),
                        None,
                    )
                    declarations.append((line, "package script name declared; command omitted"))
    elif name == "dockerfile" or name.endswith(".dockerfile"):
        for number, row in enumerate(text.splitlines(), 1):
            if re.match(r"\s*(?:CMD|ENTRYPOINT)\b", row, re.IGNORECASE):
                declarations.append((number, "container entrypoint directive; command omitted"))
    for line, detail in declarations:
        _add_discovery(
            collector,
            _discovery(
                "invocation_candidate",
                relative,
                summary="Manifest declares a plausible invocation path; it is unvalidated and never executed during inspection.",
                provenance=ObservationState.DECLARED,
                confidence="medium",
                line=line,
                detail=detail,
                limitations=(
                    "candidate only; command, arguments, effects, and runtime success are not validated",
                ),
            ),
            budget,
            skipped,
        )


def _discover_python_invocations(
    relative: str,
    text: str,
    collector: _Collector,
    budget: InspectionBudget,
    skipped: dict[str, int],
) -> None:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        compact = ast.unparse(node.test).replace(" ", "")
        if re.fullmatch(r"__name__==(['\"])__main__\1", compact):
            _add_discovery(
                collector,
                _discovery(
                    "invocation_candidate",
                    relative,
                    summary="Python __main__ guard found; candidate invocation is not executed or validated.",
                    provenance=ObservationState.INFERRED,
                    confidence="medium",
                    line=node.lineno,
                    detail="__main__ guard",
                    limitations=("candidate only; callable behavior and side effects are unknown",),
                ),
                budget,
                skipped,
            )


class CodebaseInspector:
    """Policy-bound facade for bounded static repository inspection."""

    def __init__(
        self,
        policy: ExecutionPolicy,
        *,
        budget: InspectionBudget | None = None,
    ) -> None:
        self.policy = policy
        self.budget = budget or InspectionBudget()

    def inspect(self, root: Path) -> SourceInspection:
        return inspect_source(root, policy=self.policy, budget=self.budget)


def _is_link(entry: Path) -> bool:
    """A symlink, or a Windows directory junction (which `is_symlink` does not report)."""
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    return entry.is_symlink() or bool(is_junction and is_junction())


def _walk(
    root: Path,
    skipped: dict[str, int],
    *,
    budget: InspectionBudget,
    usage: dict[str, int],
):
    """Yield a deterministic bounded subset of files and never follow links."""
    from itertools import islice

    stack = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if usage["directories_seen"] >= budget.max_directories:
            skipped["directory_limit"] = skipped.get("directory_limit", 0) + 1
            continue
        usage["directories_seen"] += 1
        try:
            iterator = directory.iterdir()
            entries = list(islice(iterator, budget.max_entries_per_directory + 1))
            iterator.close()
        except OSError:
            continue
        if len(entries) > budget.max_entries_per_directory:
            skipped["directory_entry_limit"] = skipped.get("directory_entry_limit", 0) + 1
            entries = entries[: budget.max_entries_per_directory]
        for entry in sorted(entries):
            try:
                outside = _is_link(entry) or not entry.resolve().is_relative_to(root)
            except OSError:
                outside = True
            if outside:
                skipped["symlink"] = skipped.get("symlink", 0) + 1  # never leave the root
                continue
            if entry.is_dir():
                if entry.name.lower() in _SENSITIVE_DIRS:
                    skipped["sensitive_directory"] = skipped.get("sensitive_directory", 0) + 1
                    continue
                if entry.name in _SKIP_DIRS or entry.name.startswith("."):
                    skipped["directory"] = skipped.get("directory", 0) + 1
                    continue
                if depth >= budget.max_depth:
                    skipped["depth_limit"] = skipped.get("depth_limit", 0) + 1
                    continue
                if usage["directories_seen"] + len(stack) >= budget.max_directories:
                    skipped["directory_limit"] = skipped.get("directory_limit", 0) + 1
                    continue
                stack.append((entry, depth + 1))
            elif entry.is_file():
                if usage["files_seen"] >= budget.max_files:
                    skipped["file_limit"] = skipped.get("file_limit", 0) + 1
                    return
                usage["files_seen"] += 1
                yield entry


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
                confidence="medium" if live else "low",
                library=library,
                summary=summary,
                evidence=tuple(evidence[:20]),
                caveats=tuple(caveats),
            )
        )
    return findings
