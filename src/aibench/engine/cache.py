"""Explicit cross-run caches (§14 "Cache boundaries", 16-T3). Opt-in per plan
(`cache.executions`, `cache.evaluations`); resume state is not a cache and never depends
on this.

Keys are version-complete content hashes, so any change to what a record depends on is a
different key (a miss), never a stale hit (16-G3):

- **execution:** the app-visible input, the frozen application identity (config hash:
  transport, bindings, revision, environment digest), the application's code (the source
  files beside a CLI or Python entry point), the values of the environment variables it
  inherits (hashed, never stored), the aibench version, the selected test world's seed
  hash, the policy hash, and the repetition index. An application whose code can't be read
  (an HTTP endpoint, or an entry point that is not a local source file) must declare a
  `revision` or `environment_digest` for its executions to be cached (compile checks it);
- **evaluation:** the execution content the evaluator can see (status, output, retrieved
  context, tool events, world state, usage, cost), the whole Golden (input, every
  reference, expectations and fixtures), the metric binding (evaluator ID and semantic
  version, parameters including any judge settings and rubric, decision rule), the plugin
  version, the policy hash and the repetition index (a judge's repeats stay independent).

A cache hit is a copy that says where it came from (`cache` on the execution,
`provenance.cache` on the result). It is not dispatched, costs nothing now, is excluded
from fresh latency, and is not an independent repetition. Only successful records are
stored. Execution caching is refused for applications whose output depends on state the
key cannot capture: declared effects without a snapshotted test world, episodes, and
shared state (compile checks it).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from aibench import __version__
from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    ApplicationSpec,
    BenchmarkCase,
    CliTransport,
    ContainerTransport,
    EffectState,
    EvaluationResult,
    ExecutionResult,
    PythonTransport,
    deep_unfreeze,
)

KEY_VERSION = 2
# Source files that make up an application's code, found beside its entry point.
CODE_SUFFIXES = frozenset(
    {".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".rb", ".php", ".pl", ".lua",
     ".r", ".sh", ".bash", ".ps1", ".bat", ".cmd", ".jl"}
)  # fmt: skip
_SKIP_DIRS = frozenset(
    {"__pycache__", "venv", "env", "node_modules", "site-packages"}
)  # fmt: skip
MAX_CODE_FILES = 2_000
MAX_CODE_BYTES = 32 * 1024 * 1024


class CodeUnreadable(Exception):
    """The application's code can't be fingerprinted (none found, or too much)."""


def _entry_points(spec: ApplicationSpec, base_dir: Path) -> list[Path]:
    """Local source files that start the application: argv entries of a CLI app, the file
    or module of a Python callable. Interpreters and other executables are not code here."""
    transport = spec.transport
    if not isinstance(transport, CliTransport | PythonTransport):
        return []
    cwd = base_dir / transport.cwd if transport.cwd else base_dir
    candidates: list[Path] = []
    if isinstance(transport, CliTransport):
        candidates = [Path(arg) for arg in transport.argv]
    else:
        target = transport.callable.rsplit(":", 1)[0]
        if target.endswith(".py"):
            candidates = [Path(target)]
        else:
            module = Path(*target.split("."))
            for root in (cwd, base_dir, *(base_dir / p for p in transport.paths)):
                candidates += [root / module.with_suffix(".py"), root / module / "__init__.py"]
    found = []
    for candidate in candidates:
        options = (
            (candidate,) if candidate.is_absolute() else (cwd / candidate, base_dir / candidate)
        )
        for path in options:
            if path.suffix.lower() in CODE_SUFFIXES and path.is_file():
                found.append(path.resolve())
                break
    return found


def code_files(spec: ApplicationSpec, base_dir: Path) -> list[Path]:
    """The local source roots an application's configured runner can execute or import.

    Besides entry-point directories, this includes the runner's working directory and the
    explicit Python import paths. Hidden, virtual-environment and dependency directories are
    excluded. Raises `CodeUnreadable` when there is no local entry point or the tree is too
    large to fingerprint."""
    entries = _entry_points(spec, base_dir)
    if not entries:
        raise CodeUnreadable("no local source file among the application's entry points")
    roots = {entry.parent for entry in entries}
    transport = spec.transport
    if isinstance(transport, CliTransport | PythonTransport):
        cwd = Path(transport.cwd) if transport.cwd else base_dir
        roots.add((cwd if cwd.is_absolute() else base_dir / cwd).resolve())
    if isinstance(transport, PythonTransport):
        for extra in transport.paths:
            path = Path(extra)
            roots.add((path if path.is_absolute() else base_dir / path).resolve())
    files: set[Path] = set()
    total = 0
    for directory in sorted(roots):
        if not directory.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(directory):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            for name in filenames:
                path = Path(dirpath) / name
                if path.suffix.lower() not in CODE_SUFFIXES or path.is_symlink():
                    continue
                files.add(path)
                total += path.stat().st_size
                if len(files) > MAX_CODE_FILES or total > MAX_CODE_BYTES:
                    raise CodeUnreadable(
                        f"more than {MAX_CODE_FILES} source files or {MAX_CODE_BYTES} bytes "
                        f"beside {directory}"
                    )
    return sorted(files)


def application_code_identity(
    spec: ApplicationSpec, base_dir: Path, environ: Mapping[str, str]
) -> dict[str, Any]:
    """What an execution depends on beyond the config: the code's content, the inherited
    environment's values (hashed, never stored), and the harness version. A declared
    `revision` or `environment_digest` is already in the config hash."""
    try:
        files = code_files(spec, base_dir)
        code: str | None = content_hash([(p.as_posix(), bytes_hash(p.read_bytes())) for p in files])
    except CodeUnreadable:
        code = None  # compile refused caching unless revision/environment_digest is declared
    inherited: tuple[str, ...] = getattr(spec.transport, "inherit_env", ())
    transport = spec.transport
    secret_refs = set(getattr(transport, "secret_env", {}).values())
    secret_refs.update(
        header.ref for header in getattr(transport, "secret_headers", {}).values()
    )
    api_key = getattr(transport, "api_key", None)
    if api_key:
        secret_refs.add(api_key)
    secret_values = {
        ref: environ.get(name)
        for ref in sorted(secret_refs)
        for source, separator, name in (ref.partition(":"),)
        if separator and source == "env" and name
    }
    return {
        "code": code,
        "inherited_env": content_hash({name: environ.get(name) for name in sorted(inherited)}),
        # Secret values can change which tenant/account an app addresses. Hash the resolved
        # values into the identity; never persist them in the cache key's source record.
        "explicit_secret_env": content_hash(secret_values),
        "aibench": __version__,
    }


def code_identity_problem(spec: ApplicationSpec, base_dir: Path) -> str | None:
    """Why the execution cache key can't capture this application's code, or None."""
    if spec.revision or spec.environment_digest:
        return None
    if isinstance(spec.transport, ContainerTransport):
        if spec.transport.mounts:
            return (
                "the container has host bind mounts whose contents are not covered by the image "
                "digest; declare `revision` or `environment_digest` and change it when a mount "
                "changes"
            )
        return None  # the image is pinned by digest in the config
    try:
        code_files(spec, base_dir)
    except CodeUnreadable as exc:
        return (
            f"the cache key can't see its code ({exc}); declare `revision` or "
            "`environment_digest` and change it whenever the application changes"
        )
    return None


CACHE_NOTE = "reused a stored observation: not a fresh measurement or an independent repetition"


def execution_key(
    case: BenchmarkCase,
    repetition: int,
    *,
    application_hash: str,
    code_identity: dict[str, Any],
    world_seed_hash: str | None,
    policy_hash: str,
) -> str:
    return content_hash(
        {
            "kind": "execution",
            "v": KEY_VERSION,
            "input": deep_unfreeze(case.application_input_projection()),
            "application": application_hash,
            "code": code_identity,
            "world_seed": world_seed_hash,
            "policy": policy_hash,
            "repetition": repetition,
        }
    )


def evaluation_key(
    execution: ExecutionResult,
    case: BenchmarkCase,
    *,
    binding_hash: str,
    evaluator: str,
    plugin: str,
    compatibility_hash: str,
    policy_hash: str,
) -> str:
    return content_hash(
        {
            "kind": "evaluation",
            "v": KEY_VERSION,
            "execution": {
                "status": execution.status.value,
                "output": deep_unfreeze(execution.output),
                "retrieved_context": deep_unfreeze(execution.retrieved_context),
                "tool_events": deep_unfreeze(execution.tool_events),
                "world_state": deep_unfreeze(execution.world_state),
                "usage": deep_unfreeze(execution.usage),
                "cost": execution.cost,
                "observed": deep_unfreeze(execution.observation_completeness),
            },
            "case": case.model_dump(mode="json", exclude={"source_line", "duplicate_of_line"}),
            "repetition": execution.repetition_id,
            "binding": binding_hash,
            "evaluator": evaluator,
            "plugin": plugin,
            "compatibility": compatibility_hash,
            "policy": policy_hash,
        }
    )


def execution_from_cache(
    source: ExecutionResult, *, run_id: str, repetition: int, attempt: int, key: str
) -> ExecutionResult:
    """This run's copy of a cached execution. Not dispatched: no application call, no
    effect, no fresh latency."""
    return source.model_copy(
        update={
            "execution_id": ExecutionResult.build_id(run_id, source.case_id, repetition, attempt),
            "run_id": run_id,
            "repetition_id": repetition,
            "attempt_id": attempt,
            "timing": {"cached": True, "source_timing": deep_unfreeze(source.timing)},
            "effect_state": EffectState.NOT_DISPATCHED,
            "correlation_id": None,
            "cache": {
                "hit": True,
                "key": key,
                "source_execution_id": source.execution_id,
                "source_run_id": source.run_id,
                "note": CACHE_NOTE,
            },
        }
    )


def evaluation_from_cache(
    source: EvaluationResult, fresh: EvaluationResult, *, key: str
) -> EvaluationResult:
    """`fresh` carries this run's identities (IDs, attempt number); `source` the reused
    value, decision and evidence."""
    evidence = tuple(
        ref.replace(source.execution_id or "\0", fresh.execution_id or "", 1)
        for ref in source.evidence_refs
    )
    provenance: dict[str, Any] = {
        **deep_unfreeze(fresh.provenance),
        "cache": {
            "hit": True,
            "key": key,
            "source_result_id": source.result_id,
            "source_run_id": source.run_id,
            "note": CACHE_NOTE,
        },
    }
    return fresh.model_copy(
        update={
            "value": source.value,
            "status": source.status,
            "decision": source.decision,
            "evidence_refs": evidence,
            "reason": source.reason,
            "raw_artifact_ref": source.raw_artifact_ref,
            "uncertainty": source.uncertainty,
            "provenance": provenance,
            "resources": {
                "latency_ms": None,
                "model_calls": 0,
                "cost": 0.0,
                "accounting": "complete",
                "cache_hit": True,
            },
        }
    )


def is_cache_hit(record: ExecutionResult | EvaluationResult) -> bool:
    if isinstance(record, ExecutionResult):
        return bool(record.cache)
    return bool((deep_unfreeze(record.provenance) or {}).get("cache"))
