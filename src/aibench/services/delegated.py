"""Delegated suites: an external eval framework that owns an execution stage (§9, §11A).

The openai/evals live bridge (17-T1) runs the upstream eval in its plugin environment
(`aibench_openai_evals_oss.bridge`). Its completion function asks this service for each
answer; the service invokes the application through the harness runner and records the
execution like any other attempt. The run is labelled `delegated_suite` with the plugin's
identity, so it is never mistaken for a planned benchmark, and the application is called
exactly once per sample: the recorded outputs are then scored by the same plugin's
replay evaluators in an ordinary stored-output scoring pass.

Before anything runs, the worker reports the exact request each sample will make; each
sample becomes a case with that request as its input. During the run, a request that
differs from its case's input or a second request for one sample (a follow-up) is
refused with a stable reason, never answered with another sample's output.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import sys
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Self

from aibench.core.errors import AibenchError, ConfigError
from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    ApplicationSpec,
    BenchmarkCase,
    ExecutionStatus,
    MetricBinding,
    RunManifest,
    deep_unfreeze,
)
from aibench.core.plans import PluginEnvironmentRef
from aibench.datasets.ingest import ingest_dataset
from aibench.evaluators.worker_client import WORKER_ENV_ALLOWLIST
from aibench.registry import EvaluatorRegistry
from aibench.runners import LoadedApplication, create_runner
from aibench.runners.bindings import InputBinding
from aibench.runners.process_tree import ProcessTree, spawn_kwargs
from aibench.security.policy import (
    ExecutionPolicy,
    application_denials,
    evaluator_denials,
    plugin_denials,
)
from aibench.services.execution import invoke_and_record
from aibench.services.scoring import score_recorded_run
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.repositories import Storage

OSS_PLUGIN_ID = "aibench-openai-evals-oss"
OSS_BRIDGE_MODULE = "aibench_openai_evals_oss.bridge"
OSS_BRIDGE_PROTOCOL = "aibench-openai-evals-oss-bridge/1"
_LINE_LIMIT = 16 * 1024 * 1024
_STARTUP_SECONDS = 180.0
_REPLY_SECONDS = 600.0


class DelegatedSuiteError(AibenchError):
    """The delegated suite could not run (bad samples, refused policy, broken bridge)."""


class PolicyRefused(DelegatedSuiteError):
    def __init__(self, denials: Sequence[str]) -> None:
        super().__init__("; ".join(denials))
        self.denials = list(denials)


@dataclass
class DelegatedReport:
    run_id: str
    eval_type: str
    samples: int
    executed: int = 0
    refused: Counter[str] = field(default_factory=Counter)
    unprepared: dict[str, str] = field(default_factory=dict)  # sample -> why no case
    live_correct: dict[str, bool | None] = field(default_factory=dict)
    scoring_id: str | None = None
    decisions: dict[str, str] = field(default_factory=dict)  # case -> pass/fail/...
    mismatches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": "delegated_suite",
            "plugin": OSS_PLUGIN_ID,
            "eval_type": self.eval_type,
            "samples": self.samples,
            "executed": self.executed,
            "refused": dict(sorted(self.refused.items())),
            "unprepared": self.unprepared,
            "scoring_id": self.scoring_id,
            "decisions": self.decisions,
            "live_and_replay_disagree": self.mismatches,
        }


def canonical(value: Any) -> str:
    """The exact-match form of a request, tagged by type so a string can never equal a
    list of messages."""
    value = deep_unfreeze(value)
    tag = "text" if isinstance(value, str) else "json"
    return json.dumps({tag: value}, sort_keys=True)


def input_problem(spec: ApplicationSpec) -> str | None:
    """Why the application would not receive the eval's request unchanged, or None: an
    exact answer needs the whole input delivered as it is."""
    transport = spec.transport
    if transport is not None and transport.kind == "openai_compatible":
        return "its OpenAI-compatible transport builds its own chat payload from the input"
    try:
        binding = InputBinding.from_spec(spec.input_binding)
    except AibenchError as exc:
        return str(exc)
    sources = list(binding.fields.values())
    if not sources:
        return None  # the whole envelope, input included, is sent unchanged
    if "/input" not in sources or any(s.startswith("/input/") for s in sources):
        return (
            "its input_binding does not deliver the whole input unchanged (it selects or "
            "drops parts of it), so the application would not answer the eval's request"
        )
    return None


def read_samples(path: Path) -> list[dict[str, Any]]:
    """openai/evals samples: one JSON object per line with `input` and `ideal`."""
    samples: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DelegatedSuiteError(f"cannot read samples {path}: {exc}") from exc
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            sample = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DelegatedSuiteError(f"{path.name}:{number}: not JSON: {exc}") from exc
        if not isinstance(sample, dict) or "input" not in sample or "ideal" not in sample:
            raise DelegatedSuiteError(f"{path.name}:{number}: a sample needs `input` and `ideal`")
        sample_id = str(sample.get("id") or f"s{number:05d}")
        samples.append({"sample_id": sample_id, "line": number, "sample": sample})
    if not samples:
        raise DelegatedSuiteError(f"{path} has no samples")
    ids: Counter[str] = Counter(s["sample_id"] for s in samples)
    duplicates = sorted(i for i, n in ids.items() if n > 1)
    if duplicates:
        raise DelegatedSuiteError(f"duplicate sample ids: {', '.join(duplicates[:5])}")
    return samples


class _Bridge:
    """The plugin's bridge worker: a contained subprocess speaking JSON lines."""

    def __init__(self, python: Path) -> None:
        self.python = python
        self.proc: asyncio.subprocess.Process | None = None
        self.tree: ProcessTree | None = None
        self.workdir = Path(tempfile.mkdtemp(prefix="aibench-bridge-"))
        self.stderr = bytearray()
        self._drain: asyncio.Task[None] | None = None

    async def __aenter__(self) -> Self:
        try:
            env = {k: os.environ[k] for k in WORKER_ENV_ALLOWLIST if k in os.environ}
            env.update(
                HOME=str(self.workdir),
                USERPROFILE=str(self.workdir),
                PYTHONNOUSERSITE="1",
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONIOENCODING="utf-8",
            )
            self.proc = await asyncio.create_subprocess_exec(
                str(self.python),
                "-m",
                OSS_BRIDGE_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
                env=env,
                limit=_LINE_LIMIT,
                **spawn_kwargs(),
            )
            self.tree = ProcessTree(self.proc.pid)
            self._drain = asyncio.ensure_future(self._drain_stderr())
            hello = await self.call({"op": "hello"}, _STARTUP_SECONDS)
            if hello.get("protocol") != OSS_BRIDGE_PROTOCOL:
                raise DelegatedSuiteError(f"unexpected bridge protocol: {hello.get('protocol')!r}")
            self.hello = hello
            return self
        except BaseException:
            await self._cleanup()
            raise

    async def __aexit__(self, *exc: object) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with suppress(Exception):
                await self.call({"op": "close"}, 10)
                await asyncio.wait_for(self.proc.wait(), 10)
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Stop the bridge tree, reap its reader, and release its OS resources."""
        proc, tree = self.proc, self.tree
        try:
            with suppress(Exception):
                if tree is not None:
                    tree.kill()
                if proc is not None and proc.returncode is None:
                    if tree is None or not tree.contained:
                        with suppress(ProcessLookupError):
                            proc.kill()
                    try:
                        await asyncio.wait_for(proc.wait(), 10)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            proc.kill()
                        await proc.wait()
        finally:
            if tree is not None:
                with suppress(Exception):
                    tree.close()
            if self._drain is not None:
                self._drain.cancel()
                with suppress(asyncio.CancelledError):
                    await self._drain
                self._drain = None
            shutil.rmtree(self.workdir, ignore_errors=True)

    async def _drain_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while chunk := await self.proc.stderr.read(4096):
            self.stderr = (self.stderr + chunk)[-8192:]

    async def send(self, message: Mapping[str, Any]) -> None:
        assert self.proc is not None and self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()

    async def read(self, timeout: float) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdout is not None
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        if not line:
            tail = self.stderr.decode("utf-8", "replace")[-2000:]
            raise DelegatedSuiteError(f"the bridge worker exited: {tail}")
        message: dict[str, Any] = json.loads(line)
        if message.get("ok") is False:
            raise DelegatedSuiteError(f"bridge: {message.get('error')}")
        return message

    async def call(self, message: Mapping[str, Any], timeout: float) -> dict[str, Any]:
        await self.send(message)
        return await self.read(timeout)


def _dataset_rows(
    samples: Sequence[dict[str, Any]], prompts: Mapping[str, Any], eval_type: str, source: str
) -> list[dict[str, Any]]:
    rows = []
    for item in samples:
        sid = item["sample_id"]
        if sid not in prompts:
            continue
        rows.append(
            {
                "case_id": sid,
                "input": prompts[sid],
                "reference": {"answer": item["sample"]["ideal"]},
                "extensions": {
                    "openai_evals.sample_id": sid,
                    "openai_evals.eval_type": eval_type,
                },
                "provenance": {"source_refs": [f"{source}#L{item['line']}"]},
            }
        )
    return rows


async def run_oss_suite(
    *,
    loaded: LoadedApplication,
    samples_path: Path,
    eval_type: str,
    params: Mapping[str, Any],
    plugin_python: Path,
    policy: ExecutionPolicy,
    storage: Storage,
    artifacts: ArtifactStore,
    scratch_dir: Path,
    environ: Mapping[str, str] | None = None,
) -> DelegatedReport:
    """Run an allowlisted openai/evals eval live against the application, then score the
    recorded outputs with the plugin's replay evaluator."""
    python = Path(os.path.abspath(plugin_python))
    problem = input_problem(loaded.spec)
    if problem is not None:
        raise DelegatedSuiteError(f"application {loaded.spec.application_id!r}: {problem}")
    denials = application_denials(policy, loaded.spec) + plugin_denials(
        policy, [PluginEnvironmentRef(python=str(python))], Path.cwd()
    )
    if denials:
        raise PolicyRefused(denials)
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(python)
    metric_id = f"openai_evals_oss.{eval_type}"
    manifests = [m for m in registry.manifests() if m.evaluator_id == metric_id]
    if not manifests:
        raise DelegatedSuiteError(
            f"{metric_id} is not provided by {python} (supported eval types come from the "
            "plugin's allowlist)"
        )
    denials = evaluator_denials(policy, manifests)
    if denials:
        raise PolicyRefused(denials)
    registry.validate(
        [MetricBinding(metric=metric_id, params=dict(params))], application=loaded.spec
    )  # parameters, before any call

    samples = read_samples(samples_path)
    source = f"{samples_path.name}@{bytes_hash(samples_path.read_bytes())[7:23]}"
    runner = create_runner(
        loaded,
        trusted_local=policy.allow_trusted_local,
        environ=dict(environ if environ is not None else os.environ),
    )
    async with _Bridge(python) as bridge:
        request: dict[str, Any] = {
            "eval_type": eval_type,
            "params": dict(params),
            "samples": [{"sample_id": s["sample_id"], "sample": s["sample"]} for s in samples],
        }
        found = await bridge.call({"op": "prompts", **request}, _REPLY_SECONDS)
        prompts = {p["sample_id"]: p["prompt"] for p in found["prompts"] if "prompt" in p}
        unprepared = {p["sample_id"]: p["error"] for p in found["prompts"] if "error" in p}
        rows = _dataset_rows(samples, prompts, eval_type, source)
        if not rows:
            raise DelegatedSuiteError(f"no sample could be prepared: {unprepared}")
        scratch_dir.mkdir(parents=True, exist_ok=True)
        dataset_file = (
            scratch_dir / f"openai-evals-{bytes_hash(json.dumps(rows).encode())[7:19]}.jsonl"
        )
        dataset_file.write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
        )
        ingested = ingest_dataset(dataset_file, dataset_id=f"openai-evals:{samples_path.stem}")
        if not ingested.is_valid or ingested.manifest is None:
            raise DelegatedSuiteError(
                f"samples did not form a valid dataset: {ingested.errors[:3]}"
            )
        dataset, cases = ingested.manifest, list(ingested.cases)
        by_id: dict[str, BenchmarkCase] = {c.case_id: c for c in cases}
        if storage.get_dataset(dataset.content_hash) is None:
            storage.commit_dataset(dataset)
        storage.commit_cases(dataset.content_hash, cases)
        storage.commit_application(loaded.spec)
        run_id = f"delegated-{bytes_hash(os.urandom(16))[7:19]}"
        report = DelegatedReport(run_id, eval_type, len(samples), unprepared=unprepared)
        suite = {
            "plugin": OSS_PLUGIN_ID,
            "plugin_version": bridge.hello.get("plugin_version"),
            "upstream": f"evals=={bridge.hello.get('evals')}",
            "eval_type": eval_type,
            "params": dict(params),
            "samples": source,
            "execution_owner": "delegated: the eval's completion-function bridge",
        }
        storage.commit_run(
            RunManifest(
                run_id=run_id,
                dataset_hash=dataset.content_hash,
                application_hash=content_hash(loaded.spec.model_dump(mode="json")),
                plan_hash=content_hash({"kind": "delegated_suite", **suite}),
                parameters={"mode": "delegated_suite", "suite": suite, "retries": 0},
                environment={"python": platform.python_version(), "platform": sys.platform},
                application_id=loaded.spec.application_id,
            ),
            status="running",
        )
        answered: set[str] = set()
        try:
            async with runner:
                await bridge.send(
                    {
                        "op": "run",
                        **request,
                        "samples": [r for r in request["samples"] if r["sample_id"] in by_id],
                    }
                )
                while True:
                    message = await bridge.read(_REPLY_SECONDS)
                    if message.get("op") == "done":
                        break
                    if message.get("op") == "sample":
                        report.live_correct[message["sample_id"]] = message.get("correct")
                        storage.append_run_event(
                            run_id,
                            "delegated_sample",
                            {
                                "sample_id": message["sample_id"],
                                "requests": message.get("requests"),
                                "correct": message.get("correct"),
                                "reason": message.get("reason"),
                                "events": message.get("events"),
                            },
                        )
                        continue
                    if message.get("op") != "complete":
                        raise DelegatedSuiteError(f"unexpected bridge message: {message}")
                    reply = await _complete(
                        message, by_id, answered, runner, storage, artifacts, run_id, report
                    )
                    await bridge.send(reply)
        except BaseException:
            storage.update_run_status(run_id, "interrupted")
            raise
        storage.update_run_status(run_id, "completed")
        # Each case's input is the request the eval actually sent, prompt-shaping
        # parameters (few-shot) already applied: the replay must not apply them twice.
        shaping = set(bridge.hello.get("prompt_params") or ())
        binding = MetricBinding(
            metric=metric_id, params={k: v for k, v in params.items() if k not in shaping}
        )

    if report.executed:
        scoring = await score_recorded_run(
            storage=storage,
            artifacts=artifacts,
            registry=registry,
            run_id=run_id,
            bindings=[binding],
            application=loaded.spec,
        )
        report.scoring_id = scoring.scoring_id
        for result in scoring.results:
            report.decisions[result.case_id] = result.decision.value
            live = report.live_correct.get(result.case_id)
            replayed = result.value.value if result.value is not None else None
            if live is not None and replayed is not None and bool(replayed) != live:
                report.mismatches.append(result.case_id)
    storage.append_run_event(run_id, "delegated_suite_completed", report.as_dict())
    return report


async def _complete(
    message: Mapping[str, Any],
    cases: Mapping[str, BenchmarkCase],
    answered: set[str],
    runner: Any,
    storage: Storage,
    artifacts: ArtifactStore,
    run_id: str,
    report: DelegatedReport,
) -> dict[str, Any]:
    def refuse(reason: str, detail: str) -> dict[str, Any]:
        report.refused[reason] += 1
        storage.append_run_event(
            run_id,
            "delegated_request_refused",
            {"sample_id": message.get("sample_id"), "reason": reason, "detail": detail},
        )
        return {"op": "completion", "refused": reason, "detail": detail}

    sample_id = str(message.get("sample_id"))
    case = cases.get(sample_id)
    if case is None:
        return refuse("unknown_sample", f"no case was prepared for sample {sample_id!r}")
    if message.get("index", 0) > 0 or sample_id in answered:
        return refuse(
            "unsupported_follow_up",
            "the eval made a second request for one sample; only single-request evals are bridged",
        )
    if canonical(message.get("prompt")) != canonical(case.input):
        return refuse(
            "unsupported_dynamic_request",
            "the eval's request differs from the one it reported before the run",
        )
    answered.add(sample_id)
    result = await invoke_and_record(
        runner, case, storage=storage, artifacts=artifacts, run_id=run_id
    )
    report.executed += 1
    if result.status is not ExecutionStatus.OK:
        return refuse("application_failed", f"the application call ended {result.status.value}")
    if not isinstance(result.output, str):
        return refuse("unsupported_output", "the application's output is not text")
    return {"op": "completion", "text": result.output}


def default_scratch(workspace_root: Path) -> Path:
    return workspace_root / "delegated"


def require_file(path: Path, what: str) -> Path:
    if not path.is_file():
        raise ConfigError(f"{what} not found: {path}")
    return path
