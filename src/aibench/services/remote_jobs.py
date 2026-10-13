"""Remote evaluation jobs: the hosted OpenAI Evals API bridge (§9, §11B, 17-T2).

A job grades a run's recorded outputs remotely. Its life:

    prepared --create eval--> eval_created --create run--> submitted --poll--> running
        |                         |                                              |
        +-> eval_unknown          +-> run_unknown                 completed / failed / canceled
            (reconcile)               (reconcile)                         --fetch--> imported

- **Before sending.** The requests are built and checked by the plugin (no network), then
  stored with their fingerprint as a restricted artifact, and the job row is committed.
- **Ambiguous submissions.** A timeout, dropped connection or 5xx on a create call leaves
  the job `*_unknown`: the service may have processed it. Reconciliation lists the remote
  evals or runs and adopts the one carrying this job's ID in its metadata. If none is
  found, nothing is resent: the service documents no idempotency guarantee, so a
  duplicate is possible, and resending needs an explicit `--resend`, recorded in the job's
  history.
- **Fetching.** Output items are read page by page (cursor `after`). Each maps back to a
  case through the `aibench_case_id` field of its uploaded item. A case is recorded once:
  - an item for an unknown case is reported, never attached;
  - a repeated item ID is ignored and counted;
  - a second item for the same case makes that case's result an error;
  - a case with no item (a failed or cancelled run) is recorded as skipped, so the lost
    coverage stays in the denominator.
- **Scoring.** The results enter the run as an ordinary scoring pass of the
  `openai_evals_api.criterion` metric, one binding per testing criterion, with the remote
  IDs in each result's raw record.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import uuid
from collections import Counter
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self

from aibench.core.errors import AibenchError
from aibench.core.hashes import bytes_hash, content_hash
from aibench.core.models import (
    BenchmarkCase,
    ExecutionResult,
    ExecutionStatus,
    MetricBinding,
    RedactionClass,
    deep_unfreeze,
)
from aibench.core.plans import PluginEnvironmentRef
from aibench.evaluators.protocol import EvaluationOutcome
from aibench.evaluators.worker_client import WORKER_ENV_ALLOWLIST
from aibench.registry import EvaluatorRegistry
from aibench.runners.process_tree import ProcessTree, spawn_kwargs
from aibench.security.policy import (
    ExecutionPolicy,
    egress_denials,
    evaluator_denials,
    plugin_denials,
)
from aibench.security.secrets import Redactor, resolve_secret
from aibench.services.scoring import (
    BindingScorer,
    MissingExecution,
    metric_profiles,
    select_final_executions,
)
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import Storage

API_PLUGIN_ID = "aibench-openai-evals-api"
API_WORKER_MODULE = "aibench_openai_evals_api.worker"
API_WORKER_PROTOCOL = "aibench-openai-evals-api-worker/1"
API_METRIC = "openai_evals_api.criterion"
DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_API_KEY = "env:OPENAI_API_KEY"
EGRESS = "case inputs, recorded application outputs and reference answers"
TERMINAL_RUN_STATES = ("completed", "failed", "canceled")
# Local run states whose final executions can no longer change.
_FINISHED_RUNS = ("completed", "cancelled", "budget_exhausted")
_RECONCILE_PAGES = 20
FETCH_PAGE_SIZE = 100
_LINE_LIMIT = 64 * 1024 * 1024


class RemoteJobError(AibenchError):
    """A remote job could not proceed; the message says why and what is safe to do."""


class RemoteJobRefused(RemoteJobError):
    def __init__(self, denials: Sequence[str]) -> None:
        super().__init__("; ".join(denials))
        self.denials = list(denials)


@dataclass(frozen=True)
class RemoteConfig:
    plugin_python: Path
    base_url: str = DEFAULT_BASE_URL
    api_key: str = DEFAULT_API_KEY  # a secret reference, never the key

    def as_dict(self) -> dict[str, str]:
        return {
            "plugin_python": str(self.plugin_python),
            "base_url": self.base_url,
            "api_key": self.api_key,
        }


class _Worker:
    """The plugin's API worker for one sequence of operations."""

    def __init__(self, config: RemoteConfig, environ: Mapping[str, str]) -> None:
        self.config = config
        self.environ = environ
        self.workdir = Path(tempfile.mkdtemp(prefix="aibench-oaievals-"))
        self.proc: asyncio.subprocess.Process | None = None
        self.redactor = Redactor([])

    async def __aenter__(self) -> Self:
        try:
            env = {k: self.environ[k] for k in WORKER_ENV_ALLOWLIST if k in self.environ}
            key = resolve_secret(self.config.api_key, self.environ)
            self.redactor = Redactor([(self.config.api_key, key)])
            env.update(
                HOME=str(self.workdir),
                USERPROFILE=str(self.workdir),
                PYTHONNOUSERSITE="1",
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONIOENCODING="utf-8",
                OPENAI_API_KEY=key,
                AIBENCH_OPENAI_BASE_URL=self.config.base_url,
            )
            self.proc = await asyncio.create_subprocess_exec(
                str(self.config.plugin_python),
                "-m",
                API_WORKER_MODULE,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=self.workdir,
                env=env,
                limit=_LINE_LIMIT,
                **spawn_kwargs(),
            )
            self.tree = ProcessTree(self.proc.pid)
            hello = await self.call({"op": "hello"})
            if hello.get("protocol") != API_WORKER_PROTOCOL:
                raise RemoteJobError(f"unexpected worker protocol {hello.get('protocol')!r}")
            self.hello = hello
            return self
        except BaseException:
            await self._cleanup()
            raise

    async def __aexit__(self, *exc: object) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with suppress(Exception):
                await self.call({"op": "close"})
                await asyncio.wait_for(self.proc.wait(), 10)
        await self._cleanup()

    async def _cleanup(self) -> None:
        """Stop the worker tree and release its job handle and private directory."""
        proc = self.proc
        tree = getattr(self, "tree", None)
        try:
            with suppress(Exception):
                if tree is not None:
                    tree.kill()
                if proc is not None and proc.returncode is None:
                    # ProcessTree cannot contain the child if Windows job assignment failed.
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
            shutil.rmtree(self.workdir, ignore_errors=True)

    async def call(self, message: Mapping[str, Any], timeout: float = 300) -> dict[str, Any]:
        assert self.proc is not None and self.proc.stdin and self.proc.stdout
        self.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.proc.stdin.drain()
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        if not line:
            raise RemoteJobError("the Evals API worker exited unexpectedly")
        reply: dict[str, Any] = json.loads(line)
        if reply.get("ok") is False:
            reply["error"] = self.redactor.text(str(reply.get("error", "")))
        return reply


def _text(value: Any) -> str:
    value = deep_unfreeze(value)
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True)


def _items(
    storage: Storage, run_id: str
) -> tuple[list[dict[str, str]], dict[str, ExecutionResult], dict[str, str]]:
    """The items to upload: one per case with a successful, text final execution."""
    record = storage.get_run(run_id)
    if record is None:
        raise RemoteJobError(f"no run committed with run_id={run_id!r}")
    cases: dict[str, list[BenchmarkCase]] = {}
    for case in storage.list_cases(record.manifest.dataset_hash):
        cases.setdefault(case.case_id, []).append(case)
    items, executions, skipped = [], {}, {}
    for execution in select_final_executions(storage.list_execution_attempts(run_id)):
        if execution.warmup:
            skipped[f"{execution.case_id}:r{execution.repetition_id}"] = "warmup_not_uploaded"
            continue
        if execution.repetition_id != 0:
            skipped[f"{execution.case_id}:r{execution.repetition_id}"] = "repetition_not_uploaded"
            continue
        candidates = cases.get(execution.case_id, [])
        if len(candidates) != 1:
            skipped[execution.case_id] = "case_not_recorded_once"
        elif execution.status is not ExecutionStatus.OK:
            skipped[execution.case_id] = f"execution_{execution.status.value}"
        elif not isinstance(execution.output, str):
            skipped[execution.case_id] = "non_text_output"
        else:
            case = candidates[0]
            item = {
                "aibench_case_id": case.case_id,
                "input": _text(case.input),
                "output": execution.output,
            }
            reference = deep_unfreeze(case.reference.answer) if case.reference else None
            if isinstance(reference, str):
                item["reference"] = reference
            items.append(item)
            executions[case.case_id] = execution
    return items, executions, skipped


def _registry(config: RemoteConfig) -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(config.plugin_python)
    return registry


def remote_denials(policy: ExecutionPolicy, config: RemoteConfig) -> list[str]:
    """Checked before any plugin code starts: the plugin environment, the destination and
    the credential."""
    denials = plugin_denials(
        policy, [PluginEnvironmentRef(python=str(config.plugin_python))], Path.cwd()
    )
    return denials + egress_denials(
        policy, config.base_url, sends=EGRESS, secret_ref=config.api_key
    )


def check_remote_policy(
    policy: ExecutionPolicy, config: RemoteConfig, *, uses_models: bool
) -> list[str]:
    """Every permission a remote job needs, checked before any data leaves."""
    denials = remote_denials(policy, config)
    if denials:
        return denials
    manifests = [m for m in _registry(config).manifests() if m.evaluator_id == API_METRIC]
    if not manifests:
        return [f"{config.plugin_python} does not provide {API_METRIC}"]
    if uses_models:
        manifests = [m.model_copy(update={"uses_models": True}) for m in manifests]
    return evaluator_denials(policy, manifests)


def _history(job: dict[str, Any], event: str, **detail: Any) -> None:
    job.setdefault("history", []).append({"event": event, **detail})


def _save(storage: Storage, job: dict[str, Any]) -> None:
    data = {k: v for k, v in job.items() if k not in _COLUMNS}
    storage.update_remote_job(job["job_id"], job["state"], data)


_COLUMNS = ("job_id", "run_id", "plugin_id", "state", "fingerprint", "created_at", "updated_at")


def _config(job: Mapping[str, Any]) -> RemoteConfig:
    c = job["config"]
    return RemoteConfig(Path(c["plugin_python"]), c["base_url"], c["api_key"])


async def submit_job(
    storage: Storage,
    artifacts: ArtifactStore,
    run_id: str,
    criteria: Sequence[Mapping[str, Any]],
    *,
    config: RemoteConfig,
    policy: ExecutionPolicy,
    name: str | None = None,
    allow_duplicate: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Prepare, persist and send a job; returns the job as stored."""
    env = dict(environ if environ is not None else os.environ)
    denials = remote_denials(policy, config)
    if denials:
        raise RemoteJobRefused(denials)
    record = storage.get_run(run_id)
    if record is not None and record.status not in _FINISHED_RUNS:
        raise RemoteJobError(
            f"run {run_id!r} is {record.status}: its final outputs can still change; "
            "finish or cancel it before grading them remotely"
        )
    items, executions, skipped = _items(storage, run_id)
    if not items:
        raise RemoteJobError(f"run {run_id!r} has no recorded text outputs to grade: {skipped}")
    job_id = f"oaievals-{uuid.uuid4().hex[:16]}"
    metadata = {"aibench_job": job_id, "aibench_run": run_id[:512]}
    async with _Worker(config, env) as worker:
        prepared = await worker.call(
            {
                "op": "prepare",
                "name": name or f"aibench {run_id}",
                "criteria": [dict(c) for c in criteria],
                "items": items,
                "metadata": metadata,
                "models_allowed": policy.allow_model_evaluators,
            }
        )
        if not prepared.get("ok"):
            raise RemoteJobRefused([prepared["error"]])
        denials = check_remote_policy(policy, config, uses_models=bool(prepared["uses_models"]))
        if denials:
            raise RemoteJobRefused(denials)
        requests = {"eval": prepared["eval_request"], "run": prepared["run_request"]}
        # The fingerprint ignores this job's own ID, so resubmitting the same grading of
        # the same outputs is recognised as a possible duplicate.
        fingerprint = content_hash(
            {
                "base_url": config.base_url,
                "criteria": [dict(c) for c in criteria],
                "items": items,
            }
        )
        existing = [
            j for j in storage.list_remote_jobs(run_id)
            if j["fingerprint"] == fingerprint and j["state"] not in ("rejected",)
        ]  # fmt: skip
        if existing and not allow_duplicate:
            raise RemoteJobError(
                f"job {existing[0]['job_id']} already submitted the same grading of these "
                "outputs (it may have been processed); pass --allow-duplicate to send again"
            )
        ref = artifacts.write_bytes(
            json.dumps(requests, sort_keys=True).encode("utf-8"),
            mime_type="application/json",
            run_id=run_id,
            redaction=RedactionClass.RESTRICTED,
            artifact_id=f"{job_id}:request",
        )
        commit_verified_artifact(artifacts, storage, ref)
        job: dict[str, Any] = {
            "job_id": job_id,
            "run_id": run_id,
            "plugin_id": API_PLUGIN_ID,
            "state": "prepared",
            "fingerprint": fingerprint,
            "config": config.as_dict(),
            "request_artifact": ref.artifact_id,
            "criteria": [c["name"] for c in criteria],
            "criteria_config": [dict(c) for c in criteria],
            "cases": [i["aibench_case_id"] for i in items],
            # Exactly what was uploaded: results attach to these executions, whatever
            # the run records later.
            "uploads": {
                i["aibench_case_id"]: {
                    "execution_id": executions[i["aibench_case_id"]].execution_id,
                    "output_digest": bytes_hash(i["output"].encode("utf-8")),
                }
                for i in items
            },
            "skipped": skipped,
            "egress": {"destination": config.base_url, "sends": EGRESS, "items": len(items)},
            "remote": {},
        }
        storage.commit_remote_job(
            job_id, run_id, API_PLUGIN_ID, "prepared", fingerprint,
            {k: v for k, v in job.items() if k not in _COLUMNS},
        )  # fmt: skip
        storage.append_run_event(
            run_id, "remote_job_prepared", {"job_id": job_id, "items": len(items)}
        )
        await _advance(storage, worker, job, requests)
    return job


async def resume_job(
    storage: Storage,
    artifacts: ArtifactStore,
    job_id: str,
    *,
    policy: ExecutionPolicy,
    resend: bool = False,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Continue a job from its stored state: reconcile an unknown submission, or send the
    next request. `resend` explicitly accepts the risk of a duplicate for an unknown one."""
    job = _approved_job(storage, job_id, policy)
    ref = storage.get_artifact(job["request_artifact"])
    if ref is None:
        raise RemoteJobError(f"job {job_id}: its stored request is missing")
    requests = json.loads(artifacts.read_bytes(ref))
    async with _Worker(_config(job), dict(environ if environ is not None else os.environ)) as w:
        if resend and job["state"] in ("eval_unknown", "run_unknown"):
            back = "prepared" if job["state"] == "eval_unknown" else "eval_created"
            _history(job, "resend_accepted", from_state=job["state"])
            job["state"] = back
            _save(storage, job)
        await _advance(storage, w, job, requests)
    return job


async def _advance(
    storage: Storage, worker: _Worker, job: dict[str, Any], requests: Mapping[str, Any]
) -> None:
    remote = job["remote"]
    if job["state"] == "eval_unknown":
        found = await _find(worker, "list_evals", {}, job["job_id"])
        if found is None:
            _history(job, "reconcile_not_found", kind="eval")
            _save(storage, job)
            return
        remote["eval_id"] = found["id"]
        job["state"] = "eval_created"
        _history(job, "reconciled", kind="eval", eval_id=found["id"])
        _save(storage, job)
    if job["state"] == "prepared":
        created = await _send_create(
            storage, worker, job, "eval", {"op": "create_eval", "request": requests["eval"]}
        )
        if created is None:
            return
        remote["eval_id"] = created["id"]
        job["state"] = "eval_created"
        _history(job, "eval_created", eval_id=remote["eval_id"])
        _save(storage, job)
    if job["state"] == "run_unknown":
        found = await _find(worker, "list_runs", {"eval_id": remote["eval_id"]}, job["job_id"])
        if found is None:
            _history(job, "reconcile_not_found", kind="run")
            _save(storage, job)
            return
        _adopt_run(job, found)
        _history(job, "reconciled", kind="run", run_id=found["id"])
        _save(storage, job)
    if job["state"] == "eval_created":
        created = await _send_create(
            storage,
            worker,
            job,
            "run",
            {"op": "create_run", "eval_id": remote["eval_id"], "request": requests["run"]},
        )
        if created is None:
            return
        _adopt_run(job, created)
        _history(job, "run_created", run_id=remote["run_id"])
        _save(storage, job)
        storage.append_run_event(
            job["run_id"],
            "remote_job_submitted",
            {
                "job_id": job["job_id"],
                "eval_id": remote["eval_id"],
                "remote_run_id": remote["run_id"],
            },
        )


def _adopt_run(job: dict[str, Any], run: Mapping[str, Any]) -> None:
    job["remote"]["run_id"] = run["id"]
    job["remote"]["run_status"] = run.get("status")
    job["remote"]["report_url"] = run.get("report_url")
    job["state"] = "submitted"


async def _send_create(
    storage: Storage,
    worker: _Worker,
    job: dict[str, Any],
    kind: str,
    message: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Send a create, having first recorded that its outcome is unknown: if anything
    interrupts from here on (a crash, a kill, a lost worker), resuming reconciles by the
    job's ID instead of sending it again. Returns the created object, or None."""
    before = job["state"]
    job["state"] = f"{kind}_unknown"
    _history(job, "sending", kind=kind)
    _save(storage, job)
    reply = await worker.call(message)
    result = reply.get("result") if reply.get("ok") else None
    if isinstance(result, Mapping) and isinstance(result.get("id"), str):
        return dict(result)
    if reply.get("ok"):
        reply = {"error_kind": "ambiguous", "error": "a success reply without an ID"}
    error = {k: reply.get(k) for k in ("error_kind", "status", "error", "retry_after")}
    if reply.get("error_kind") == "ambiguous":
        _history(job, "ambiguous", kind=kind, **error)  # may exist remotely: reconcile
    elif reply.get("error_kind") == "rate_limited":
        job["state"] = before  # not processed: resume later
        _history(job, "rate_limited", kind=kind, **error)
    else:
        job["state"] = "rejected"  # an explicit client error: not processed
        _history(job, "rejected", kind=kind, **error)
    _save(storage, job)
    return None


async def _find(
    worker: _Worker, op: str, args: Mapping[str, Any], job_id: str
) -> dict[str, Any] | None:
    after = None
    for _ in range(_RECONCILE_PAGES):
        reply = await worker.call({"op": op, **args, "after": after})
        if not reply.get("ok"):
            raise RemoteJobError(f"reconciliation failed: {reply.get('error')}")
        page = reply["result"]
        for entry in page["data"]:
            if (entry.get("metadata") or {}).get("aibench_job") == job_id:
                return dict(entry)
        if not page.get("has_more") or not page["data"]:
            return None
        after = page["data"][-1]["id"]
    return None


def _job(storage: Storage, job_id: str) -> dict[str, Any]:
    job = storage.get_remote_job(job_id)
    if job is None:
        raise RemoteJobError(f"no remote job {job_id!r}")
    return job


def _approved_job(storage: Storage, job_id: str, policy: ExecutionPolicy) -> dict[str, Any]:
    """A stored job whose plugin, destination and credential the current policy still
    approves: the policy may have changed since it was submitted."""
    job = _job(storage, job_id)
    denials = remote_denials(policy, _config(job))
    if denials:
        raise RemoteJobRefused(denials)
    return job


async def poll_job(
    storage: Storage,
    job_id: str,
    *,
    policy: ExecutionPolicy,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    job = _approved_job(storage, job_id, policy)
    if "run_id" not in job["remote"]:
        raise RemoteJobError(f"job {job_id} has no remote run yet (state {job['state']})")
    async with _Worker(_config(job), dict(environ if environ is not None else os.environ)) as w:
        reply = await w.call(
            {
                "op": "retrieve_run",
                "eval_id": job["remote"]["eval_id"],
                "run_id": job["remote"]["run_id"],
            }
        )
    if not reply.get("ok"):
        raise RemoteJobError(f"poll failed ({reply.get('error_kind')}): {reply.get('error')}")
    _update_from_run(job, reply["result"])
    _save(storage, job)
    return job


def _update_from_run(job: dict[str, Any], run: Mapping[str, Any]) -> None:
    status = run.get("status")
    job["remote"]["run_status"] = status
    job["remote"]["result_counts"] = run.get("result_counts")
    if job["state"] != "imported":
        job["state"] = status if status in TERMINAL_RUN_STATES else "running"


async def cancel_job(
    storage: Storage,
    job_id: str,
    *,
    policy: ExecutionPolicy,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Best-effort cancel. Items graded before the service stopped stay graded; fetching a
    cancelled run imports them and records the rest as not evaluated."""
    job = _approved_job(storage, job_id, policy)
    if "run_id" not in job["remote"]:
        raise RemoteJobError(f"job {job_id} has no remote run to cancel (state {job['state']})")
    async with _Worker(_config(job), dict(environ if environ is not None else os.environ)) as w:
        reply = await w.call(
            {
                "op": "cancel_run",
                "eval_id": job["remote"]["eval_id"],
                "run_id": job["remote"]["run_id"],
            }
        )
    if not reply.get("ok"):
        raise RemoteJobError(f"cancel failed ({reply.get('error_kind')}): {reply.get('error')}")
    _history(job, "cancel_requested", status=reply["result"].get("status"))
    _update_from_run(job, reply["result"])
    _save(storage, job)
    return job


async def fetch_job(
    storage: Storage,
    artifacts: ArtifactStore,
    job_id: str,
    *,
    policy: ExecutionPolicy,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Import a finished job's results into its run (once)."""
    job = _approved_job(storage, job_id, policy)
    if job["state"] == "imported":
        return job
    job = await poll_job(storage, job_id, policy=policy, environ=environ)
    if job["state"] not in TERMINAL_RUN_STATES:
        raise RemoteJobError(f"job {job_id} is still {job['remote'].get('run_status')}; poll later")
    config = _config(job)
    remote = job["remote"]
    items: list[dict[str, Any]] = []
    async with _Worker(config, dict(environ if environ is not None else os.environ)) as w:
        after = None
        while True:
            reply = await w.call(
                {"op": "list_output_items", "eval_id": remote["eval_id"],
                 "run_id": remote["run_id"], "after": after, "limit": FETCH_PAGE_SIZE}
            )  # fmt: skip
            if not reply.get("ok"):
                raise RemoteJobError(
                    f"fetch failed ({reply.get('error_kind')}): {reply.get('error')}"
                )
            page = reply["result"]
            items.extend(page["data"])
            if not page.get("has_more") or not page["data"]:
                break
            after = page["data"][-1]["id"]
    raw = artifacts.write_bytes(
        json.dumps(items, sort_keys=True).encode("utf-8"),
        mime_type="application/json",
        run_id=job["run_id"],
        redaction=RedactionClass.RESTRICTED,
        artifact_id=f"{job_id}:output_items",
    )
    commit_verified_artifact(artifacts, storage, raw)
    mapping = map_output_items(items, job["cases"])
    await _record(storage, artifacts, job, mapping, config)
    job["mapping"] = mapping.summary()
    job["output_items_artifact"] = raw.artifact_id
    job["state"] = "imported"
    _history(job, "imported", **job["mapping"])
    _save(storage, job)
    storage.append_run_event(
        job["run_id"], "remote_job_imported", {"job_id": job_id, **job["mapping"]}
    )
    return job


@dataclass
class Mapping_:
    by_case: dict[str, dict[str, Any]]
    missing: list[str]
    unknown: list[str]
    duplicate_items: int
    conflicting_cases: list[str]
    pages_items: int

    def summary(self) -> dict[str, Any]:
        return {
            "output_items": self.pages_items,
            "mapped": len(self.by_case),
            "missing": sorted(self.missing),
            "unknown_case_ids": sorted(self.unknown),
            "duplicate_items_ignored": self.duplicate_items,
            "conflicting_cases": sorted(self.conflicting_cases),
        }


def map_output_items(items: Sequence[Mapping[str, Any]], cases: Sequence[str]) -> Mapping_:
    """Remote output items to canonical cases, one-to-one (17-G3)."""
    expected = set(cases)
    seen_ids: set[str] = set()
    by_case: dict[str, dict[str, Any]] = {}
    unknown, conflicting = [], set()
    duplicates = 0
    for item in items:
        item_id = str(item.get("id"))
        if item_id in seen_ids:
            duplicates += 1  # the same item on two pages: counted once
            continue
        seen_ids.add(item_id)
        case_id = (item.get("datasource_item") or {}).get("aibench_case_id")
        if case_id not in expected:
            unknown.append(str(case_id))
            continue
        if case_id in by_case:
            conflicting.add(case_id)  # two remote results for one case: neither is trusted
            continue
        by_case[case_id] = dict(item)
    for case_id in conflicting:
        by_case.pop(case_id, None)
    missing = sorted(expected - set(by_case) - conflicting)
    return Mapping_(by_case, missing, unknown, duplicates, sorted(conflicting), len(seen_ids))


async def _record(
    storage: Storage,
    artifacts: ArtifactStore,
    job: dict[str, Any],
    mapping: Mapping_,
    config: RemoteConfig,
) -> None:
    run_id = job["run_id"]
    executions: dict[str, ExecutionResult] = {}
    for case_id, upload in job["uploads"].items():
        stored = storage.get_execution_attempt(upload["execution_id"])
        if stored is not None:
            executions[case_id] = stored
    registry = _registry(config)
    record = storage.get_run(run_id)
    assert record is not None
    application = (
        storage.get_application(record.manifest.application_id)
        if record.manifest.application_id
        else None
    )
    metrics = [
        registry.resolve_binding(
            MetricBinding(
                metric=API_METRIC, params={"criterion": criterion, "job_id": job["job_id"]}
            )
        )
        for criterion in job["criteria_config"]
    ]
    scoring_id = f"remote-{job['job_id']}"
    storage.append_run_event(
        run_id,
        "scoring_pass",
        {
            "scoring_id": scoring_id,
            "metric_profiles": metric_profiles(
                metrics,
                application=application,
                dependency_lock_hash=record.manifest.dependency_lock_hash,
            ),
            "repeat_reason": "remote_job",
            "remote_job": job["job_id"],
            "independent_judge_repeat": "not_proven",
        },
    )
    status = job["remote"].get("run_status")
    count = 0
    for criterion, metric in zip(job["criteria_config"], metrics, strict=True):
        scorer = BindingScorer(
            storage, artifacts, scoring_id, metric, 60.0, None, application=application
        )
        for case_id in job["cases"]:
            execution = executions.get(case_id)
            target: ExecutionResult | MissingExecution = execution or MissingExecution(
                run_id, case_id, 0
            )
            item = mapping.by_case.get(case_id)
            if case_id in mapping.conflicting_cases:
                outcome = EvaluationOutcome.error(
                    "remote_conflict: the service returned more than one result for this case"
                )
            elif item is None:
                outcome = EvaluationOutcome(
                    ExecutionStatus.SKIPPED, reason=f"remote_missing: run {status}"
                )
            else:
                outcome = _outcome(item, criterion["name"], job)
            scorer.record_outcome(target, outcome)
            count += 1
    storage.append_run_event(
        run_id,
        "scoring_pass_completed",
        {"scoring_id": scoring_id, "result_count": count, "status": "completed"},
    )


def _outcome(item: Mapping[str, Any], criterion: str, job: Mapping[str, Any]) -> EvaluationOutcome:
    results = [r for r in item.get("results") or [] if r.get("name") == criterion]
    raw = {
        "remote": {
            "eval_id": job["remote"]["eval_id"],
            "run_id": job["remote"]["run_id"],
            "output_item_id": item.get("id"),
            "datasource_item_id": item.get("datasource_item_id"),
            "status": item.get("status"),
        },
        "result": results[0] if len(results) == 1 else None,
    }
    if len(results) != 1:
        return EvaluationOutcome.error(
            f"remote_result: {len(results)} results named {criterion!r} in the output item",
            raw=raw,
        )
    passed = results[0].get("passed")
    if not isinstance(passed, bool):
        return EvaluationOutcome.error("remote_result: no pass/fail verdict", raw=raw)
    return EvaluationOutcome.ok("boolean", passed, evidence=("execution.output",), raw=raw)


def job_counts(job: Mapping[str, Any]) -> dict[str, Any]:
    return dict(Counter(h["event"] for h in job.get("history", [])))
