"""Worker-executed evaluators (05-T3): a third-party evaluator runs only in a worker process
started with its environment's Python. These tests use a real installed-style plugin
distribution and real worker processes (the plugin environment here is this venv, so no
extra dependencies are needed); DeepEval-specific behaviour is in test_deepeval_adapter.py."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import Decision, ExecutionStatus
from aibench.registry import EvaluatorRegistry
from tests.scoring_support import Seeded, case, execution

MODULE = "aibench_worker_test_plugin"
PLUGIN = """
import os, sys, time
from aibench.core.models import EvaluatorManifest, FieldRequirement, MetricDirection
from aibench.evaluators.protocol import Evaluator, EvaluationOutcome

INSTANCES = 0

class Probe(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="vendor.probe", version="1.0.0", plugin_id="vendor-probe",
        plugin_version="0.1.0", description="worker protocol probe", value_kind="scalar",
        direction=MetricDirection.NONE, aggregation="mean",
        requires=(FieldRequirement(path="execution.output", non_empty=False),),
        default_rule={"comparator": ">=", "threshold": 0.5},
        parameters_schema={"type": "object", "properties": {"mode": {"type": "string"}}},
        requires_worker=True,
    )

    async def prepare(self, params):
        global INSTANCES
        INSTANCES += 1
        self.params = dict(params)
        self.calls = 0

    async def evaluate(self, view, ctx):
        self.calls += 1
        mode = view.get("execution.output")
        if mode == "noise":
            print("garbage on stdout that must not break the protocol")
            os.write(1, b"raw fd1 garbage\\n")
        if mode == "hang":
            time.sleep(60)
        if mode == "crash":
            os._exit(3)
        if mode == "raise":
            raise ValueError("judge exploded")
        if mode == "usage":
            ctx.report_usage(provider="judge", calls=2, tokens={"input": 5}, cost=None)
        if mode == "usage_secret":
            secret = os.environ["JUDGE_KEY"]
            ctx.report_usage(provider=secret, calls=1, tokens={secret: 3}, cost=None)
        if mode == "usage_malformed_secret":
            ctx.report_usage(provider="judge", calls=os.environ["JUDGE_KEY"], cost=None)
        if mode == "usage_negative":
            ctx.report_usage(provider="judge", calls=1, tokens={"input": -5}, cost=None)
        if mode == "secret":
            return EvaluationOutcome.ok("scalar", 1.0, raw={"echo": os.environ.get("JUDGE_KEY")})
        return EvaluationOutcome.ok(
            "scalar", 0.75,
            raw={"pid": os.getpid(), "calls": self.calls, "instances": INSTANCES,
                  "home": os.environ.get("HOME"), "cwd": os.getcwd(),
                  "leak": os.environ.get("HARNESS_ONLY_SECRET")},
        )

EVALUATORS = (Probe,)
"""


def _plugin_env(tmp_path: Path) -> Path:
    root = tmp_path / "site"
    dist = root / "vendor_probe-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: vendor-probe\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (dist / "entry_points.txt").write_text(
        f"[aibench.evaluators]\nprobe = {MODULE}:EVALUATORS\n", encoding="utf-8"
    )
    (root / f"{MODULE}.py").write_text(PLUGIN, encoding="utf-8")
    return root


def _registry(tmp_path: Path, **kwargs: Any) -> EvaluatorRegistry:
    """Load the probe plugin as a plugin environment. Discovery scans the interpreter's
    own site-packages, so the plugin's directory is passed as an extra path; the manifest
    is then read and the evaluator executed by workers only."""
    from aibench.evaluators.worker_client import WorkerSpec
    from aibench.registry.discovery import discover_plugins, load_manifests

    root = _plugin_env(tmp_path)
    registry = EvaluatorRegistry.with_native()
    [plugin] = discover_plugins(paths=[root])
    loaded = load_manifests(plugin, extra_paths=[root])
    assert loaded.error is None, loaded.error
    spec = WorkerSpec(
        python=Path(sys.executable), target=plugin.target, extra_paths=(root,), **kwargs
    )
    for manifest in loaded.manifests:
        registry.register_external(manifest, worker=spec)
    return registry


def _score(tmp_path: Path, outputs: dict[str, str], registry: EvaluatorRegistry, **kw: Any):  # type: ignore[no-untyped-def]
    seeded = Seeded(tmp_path)
    seeded.seed([case(c) for c in outputs], [execution(c, o) for c, o in outputs.items()])
    return seeded, seeded.score([{"metric": "vendor.probe"}], registry=registry, **kw)


def test_worker_results_are_canonical_and_the_plugin_is_never_imported_here(
    tmp_path: Path,
) -> None:
    _, report = _score(tmp_path, {"a": "x", "b": "y"}, _registry(tmp_path))
    results = sorted(report.results, key=lambda r: r.case_id)
    assert [(r.status, r.decision) for r in results] == [(ExecutionStatus.OK, Decision.PASS)] * 2
    assert all(r.value is not None and r.value.value == 0.75 for r in results)
    assert results[0].provenance["plugin_id"] == "vendor-probe"
    assert MODULE not in sys.modules  # executed only in the worker


def test_worker_raw_payload_shows_isolation_and_one_worker_per_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import os

    monkeypatch.setenv("HARNESS_ONLY_SECRET", "must-not-reach-the-worker")
    seeded, report = _score(tmp_path, {"a": "x", "b": "y"}, _registry(tmp_path))
    raws = []
    for r in sorted(report.results, key=lambda r: r.case_id):
        ref = seeded.storage.get_artifact(r.raw_artifact_ref)
        raws.append(json.loads(seeded.artifacts.read_bytes(ref)))
    assert raws[0]["pid"] == raws[1]["pid"] != os.getpid()  # same worker for the binding
    assert [r["calls"] for r in raws] == [1, 2] and raws[0]["instances"] == 1
    assert raws[0]["leak"] is None  # harness environment is not inherited
    assert raws[0]["home"] == raws[0]["cwd"] != os.getcwd()  # private HOME and workdir


def test_stdout_noise_from_plugin_code_cannot_corrupt_the_protocol(tmp_path: Path) -> None:
    _, report = _score(tmp_path, {"a": "noise", "b": "x"}, _registry(tmp_path))
    assert all(r.status is ExecutionStatus.OK for r in report.results)


@pytest.mark.parametrize(
    ("mode", "reason"),
    [
        ("raise", "worker_error:ValueError: judge exploded"),
        ("crash", "worker_failed:worker exited"),
    ],
)
def test_worker_failures_are_errors_and_the_next_case_gets_a_fresh_worker(
    tmp_path: Path, mode: str, reason: str
) -> None:
    _, report = _score(tmp_path, {"a": mode, "b": "x"}, _registry(tmp_path))
    by_case = {r.case_id: r for r in report.results}
    assert by_case["a"].status is ExecutionStatus.ERROR
    assert (by_case["a"].reason or "").startswith(reason)
    assert by_case["b"].status is ExecutionStatus.OK


def test_timeout_kills_the_worker_and_state_restarts_fresh(tmp_path: Path) -> None:
    import json
    import time

    started = time.monotonic()
    seeded, report = _score(
        tmp_path, {"a": "hang", "b": "x"}, _registry(tmp_path), timeout_seconds=3
    )
    assert time.monotonic() - started < 40  # not the 60s the judge would have slept
    by_case = {r.case_id: r for r in report.results}
    assert by_case["a"].status is ExecutionStatus.ERROR and (by_case["a"].reason or "").startswith(
        "timeout:"
    )
    raw = json.loads(
        seeded.artifacts.read_bytes(seeded.storage.get_artifact(by_case["b"].raw_artifact_ref))
    )
    assert raw["calls"] == 1  # a new worker: nothing carried over from the killed one


def test_usage_is_passed_through_and_unknown_cost_stays_unknown(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    manifest = registry.resolve("vendor.probe")[0]
    registry._external[(manifest.evaluator_id, manifest.version)] = manifest.model_copy(
        update={"uses_models": True}
    )
    seeded, report = _score(tmp_path, {"a": "usage"}, registry)
    [result] = report.results
    assert result.resources["model_calls"] == 2
    assert result.resources["cost"] is None and result.resources["accounting"] == "partial"
    [event] = seeded.storage.list_usage_events("run-1")
    assert (event.calls, event.cost) == (2, None)


def test_configured_secrets_reach_the_worker_but_are_redacted_from_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    monkeypatch.setenv("MY_JUDGE_KEY", "sk-test-abcdef123456")
    registry = _registry(tmp_path, secret_env={"JUDGE_KEY": "env:MY_JUDGE_KEY"})
    seeded, report = _score(tmp_path, {"a": "secret"}, registry)
    [result] = report.results
    raw = seeded.artifacts.read_bytes(seeded.storage.get_artifact(result.raw_artifact_ref))
    assert json.loads(raw) == {"echo": "<redacted:env:MY_JUDGE_KEY>"}  # delivered, then redacted
    assert b"sk-test-abcdef123456" not in raw


def test_worker_usage_strings_are_redacted_before_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    secret = "sk-test-usage-secret-abcdef"
    marker = "<redacted:env:MY_JUDGE_KEY>"
    monkeypatch.setenv("MY_JUDGE_KEY", secret)
    registry = _registry(tmp_path, secret_env={"JUDGE_KEY": "env:MY_JUDGE_KEY"})
    seeded, report = _score(
        tmp_path, {"valid": "usage_secret", "invalid": "usage_malformed_secret"}, registry
    )
    by_case = {result.case_id: result for result in report.results}
    assert by_case["invalid"].status is ExecutionStatus.ERROR
    assert secret not in (by_case["invalid"].reason or "")
    assert marker in (by_case["invalid"].reason or "")
    [event] = seeded.storage.list_usage_events("run-1")
    serialized = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    assert secret not in serialized
    assert marker in serialized


def test_negative_worker_token_counts_are_rejected(tmp_path: Path) -> None:
    _, report = _score(tmp_path, {"a": "usage_negative"}, _registry(tmp_path))
    [result] = report.results
    assert result.status is ExecutionStatus.ERROR
    assert "tokens" in (result.reason or "")
    assert "non-negative" in (result.reason or "")


def test_worker_usage_and_version_are_validated(tmp_path: Path) -> None:
    """Review: malformed usage from a worker is an evaluator error (never a crash of the
    pass), and a worker built against a different core schema is refused at start."""
    from aibench.evaluators.worker_client import WorkerEvaluator

    bad = {"provider": "p", "calls": "many", "tokens": {"input": "x"}, "cost": "free"}
    problems = WorkerEvaluator._usage_problems(bad)
    assert problems and "calls" in problems[0]
    assert (
        WorkerEvaluator._usage_problems(
            {"provider": None, "calls": None, "tokens": {}, "cost": None}
        )
        == []
    )
    assert WorkerEvaluator._version_problem({"schema_version": "9.9.9"}) is not None
    assert WorkerEvaluator._version_problem({}) is not None  # an old worker that sends none
