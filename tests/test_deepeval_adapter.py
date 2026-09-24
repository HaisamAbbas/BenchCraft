"""DeepEval adapter against the REAL installed DeepEval package (05-T2..T4; gates 05-G1..G4).

Check labels, per the prompt:
- real-package: every test here runs the pinned DeepEval (plugins/deepeval/.venv) in a real
  worker process, with a deterministic judge that implements DeepEval's DeepEvalBaseLLM
  contract (tests/fixtures/deepeval_judges). No DeepEval code is mocked.
- live-provider: `test_live_provider_smoke` only, skipped unless explicitly authorized.
If the plugin environment is not installed, this module is skipped with that reason; the
worker protocol itself is still covered by tests/test_worker_evaluator.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import Decision, ExecutionStatus, ReferenceAnswer
from aibench.registry import EvaluatorRegistry
from tests.runner_support import REPO_ROOT
from tests.scoring_support import Seeded, case, execution

PLUGIN_ENV = Path(
    os.environ.get("AIBENCH_DEEPEVAL_PYTHON")
    or REPO_ROOT
    / "plugins"
    / "deepeval"
    / ".venv"
    / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
)
JUDGES = REPO_ROOT / "tests" / "fixtures" / "deepeval_judges"
pytestmark = pytest.mark.skipif(
    not PLUGIN_ENV.is_file(),
    reason=f"DeepEval plugin environment not installed at {PLUGIN_ENV} (see plugins/deepeval/README.md)",
)

CONTEXT = ("FACT-A is documented.", "FACT-B is documented.")


def _judge(name: str) -> dict[str, Any]:
    return {
        "metric": "deepeval.faithfulness",
        "params": {"judge": {"kind": "python_factory", "factory": f"aibench_test_judges:{name}"}},
    }


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(PLUGIN_ENV, extra_paths=[JUDGES])
    assert [load.error for load in loads] == [None]
    return registry


def _plugin_python(code: str) -> Any:
    """Run a snippet in the plugin environment and return its JSON output."""
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    env.update(PYTHONPATH=str(JUDGES), DEEPEVAL_TELEMETRY_OPT_OUT="1", DEEPEVAL_DISABLE_DOTENV="1")
    done = subprocess.run(
        [str(PLUGIN_ENV), "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        cwd=JUDGES.parent,
        check=False,  # the returncode is asserted below with stderr for context
    )
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


# --------------------------------------------------------------------------- 05-G1


def test_discovery_finds_the_pinned_adapter_without_importing_deepeval_here(
    registry: EvaluatorRegistry,
) -> None:
    manifest, _ = registry.resolve("deepeval.faithfulness@1")
    assert manifest.requires_worker and manifest.plugin_id == "aibench-deepeval"
    assert manifest.internal_retries == 0 and manifest.internal_concurrency == 2
    assert "4.2.5" in manifest.description
    assert "deepeval" not in sys.modules


def test_test_case_conversion_matches_the_pinned_deepeval_api() -> None:
    result = _plugin_python(
        """
import json
from deepeval.test_case import LLMTestCase
from aibench.core.models import BenchmarkCase, ExecutionResult, ReferenceAnswer
from aibench.evaluators.protocol import EvaluationView
from aibench_deepeval.faithfulness import build_test_case
case = BenchmarkCase(case_id="c", input={"question": "q?"}, reference=ReferenceAnswer(answer="gold", context=("REFERENCE ONLY",)))
execution = ExecutionResult(execution_id="e", run_id="r", case_id="c", status="ok", output="answer", retrieved_context=("doc one", "doc two"))
tc = build_test_case(EvaluationView(case=case, execution=execution))
print(json.dumps({"type": type(tc).__module__ + "." + type(tc).__name__, "input": tc.input,
  "actual_output": tc.actual_output, "retrieval_context": tc.retrieval_context,
  "expected_output": tc.expected_output, "context": tc.context}))
"""
    )
    assert result == {
        "type": "deepeval.test_case.llm_test_case.LLMTestCase",
        "input": '{"question": "q?"}',
        "actual_output": "answer",
        "retrieval_context": ["doc one", "doc two"],
        "expected_output": None,  # faithfulness does not use the reference answer
        "context": None,  # the Golden's reference context is never passed
    }


def test_faithfulness_scores_with_the_real_metric_and_harness_rule(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("faithful"), case("half"), case("none")],
        [
            execution("faithful", "CLAIM:FACT-A and CLAIM:FACT-B", retrieved_context=CONTEXT),
            execution("half", "CLAIM:FACT-A but also CLAIM:FACT-Z", retrieved_context=CONTEXT),
            execution("none", "CLAIM:FACT-Y", retrieved_context=CONTEXT),
        ],
    )
    binding = _judge("token_judge") | {"rule": {"comparator": ">=", "threshold": 0.8}}
    report = seeded.score([binding], registry=registry, timeout_seconds=120)
    by_case = {r.case_id: r for r in report.results}
    assert {c: (r.value.value if r.value else None, r.decision) for c, r in by_case.items()} == {
        "faithful": (1.0, Decision.PASS),
        "half": (0.5, Decision.FAIL),
        "none": (0.0, Decision.FAIL),
    }
    raw = json.loads(
        seeded.artifacts.read_bytes(seeded.storage.get_artifact(by_case["half"].raw_artifact_ref))
    )
    assert raw["claims"] == ["FACT-A", "FACT-Z"] and raw["truths"] == ["FACT-A", "FACT-B"]
    assert [v["verdict"] for v in raw["verdicts"]] == ["yes", "no"]
    assert raw["deepeval_version"] == "4.2.5" and raw["upstream_threshold"] == 0.5
    assert raw["upstream_success"] is True  # upstream says pass at 0.5 ...
    assert by_case["half"].decision is Decision.FAIL  # ... the frozen harness rule decides
    # A custom judge reports no cost: accounting is unknown, never zero.
    assert (
        by_case["half"].resources["cost"] is None
        and by_case["half"].resources["accounting"] == "unknown"
    )


# --------------------------------------------------------------------------- 05-G2


def test_missing_retrieval_is_never_filled_from_reference_context(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    seeded = Seeded(tmp_path)
    golden = case("c1").model_copy(update={"reference": ReferenceAnswer(context=CONTEXT)})
    seeded.seed([golden], [execution("c1", "CLAIM:FACT-A")])  # retrieval not observed
    [result] = seeded.score([_judge("token_judge")], registry=registry, timeout_seconds=120).results
    assert (result.status, result.reason) == (
        ExecutionStatus.NOT_APPLICABLE,
        "missing:execution.retrieved_context",
    )
    assert result.value is None and result.decision is Decision.NOT_EVALUATED


@pytest.mark.parametrize(
    ("output", "context", "reason"),
    [
        ("CLAIM:FACT-A", (), "empty:execution.retrieved_context"),
        ({"answer": "CLAIM:FACT-A"}, CONTEXT, "unscorable_output:dict"),
        ("   ", CONTEXT, "unscorable_output:blank"),
    ],
)
def test_empty_context_and_unscorable_output_follow_the_documented_policy(
    tmp_path: Path, registry: EvaluatorRegistry, output: Any, context: tuple[str, ...], reason: str
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", output, retrieved_context=context)])
    [result] = seeded.score([_judge("token_judge")], registry=registry, timeout_seconds=120).results
    assert (result.status, result.reason) == (ExecutionStatus.NOT_APPLICABLE, reason)
    assert result.value is None  # no vacuous perfect score


# --------------------------------------------------------------------------- 05-G3


def test_concurrent_cases_never_share_metric_or_judge_state() -> None:
    """Six cases evaluated concurrently on ONE adapter instance, with a judge that yields
    between calls so the cases interleave. Each case must get its own score, which holds
    only if every case gets its own metric *and* judge instance (the adapter creates both
    per case). Sensitivity control: the same concurrent cases on one shared upstream
    metric instance (which necessarily shares its judge) produce wrong scores, so the
    assertion above would catch sharing. The control does not isolate metric-state sharing
    from judge-state sharing; the adapter avoids both. This runs in-process in the plugin
    environment; through the harness, each binding's worker handles one case at a time."""
    result = _plugin_python(
        """
import asyncio, json
from deepeval.metrics import FaithfulnessMetric
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluationView, EvaluatorContext
from aibench_deepeval.faithfulness import Faithfulness, build_test_case
from aibench_test_judges import SlowTokenJudge

outputs = {f"c{i}": " ".join(f"CLAIM:FACT-{l}" for l in ("A", "Z")[: 1 + i % 2]) for i in range(6)}
views = {c: EvaluationView(case=BenchmarkCase(case_id=c, input="q"), execution=ExecutionResult(
    execution_id=c, run_id="r", case_id=c, status="ok", output=o, retrieved_context=("FACT-A.",)))
    for c, o in outputs.items()}

async def adapter_run():
    ev = Faithfulness()
    await ev.prepare({"judge": {"kind": "python_factory", "factory": "aibench_test_judges:slow_token_judge"}})
    outcomes = await asyncio.gather(*(ev.evaluate(v, EvaluatorContext(run_id="r", scoring_id="s")) for v in views.values()))
    return {c: o.value.value for c, o in zip(views, outcomes)}

async def shared_metric_run():
    metric = FaithfulnessMetric(model=SlowTokenJudge(), async_mode=True)
    scores = await asyncio.gather(*(metric.a_measure(build_test_case(v), _show_indicator=False) for v in views.values()))
    return dict(zip(views, scores))

print(json.dumps({"adapter": asyncio.run(adapter_run()), "shared": asyncio.run(shared_metric_run())}))
"""
    )
    expected = {f"c{i}": (1.0 if i % 2 == 0 else 0.5) for i in range(6)}
    assert result["adapter"] == expected
    assert result["shared"] != expected  # control: a shared metric instance really corrupts


# --------------------------------------------------------------------------- errors, timeouts, drift


def test_judge_errors_and_bad_judges_are_evaluator_errors(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "CLAIM:FACT-A", retrieved_context=CONTEXT)])
    [failing] = seeded.score(
        [_judge("failing_judge")], registry=registry, timeout_seconds=120
    ).results
    assert failing.status is ExecutionStatus.ERROR
    assert "judge provider unavailable" in (failing.reason or "")
    [bad] = seeded.score([_judge("not_a_judge")], registry=registry, timeout_seconds=120).results
    assert bad.status is ExecutionStatus.ERROR
    assert "did not return a DeepEvalBaseLLM" in (bad.reason or "")  # caught in prepare


def test_blocking_judge_is_killed_and_the_next_case_runs_in_a_fresh_worker(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    import time

    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("a"), case("b")],
        [execution(c, "CLAIM:FACT-A", retrieved_context=CONTEXT) for c in ("a", "b")],
    )
    started = time.monotonic()
    report = seeded.score([_judge("blocking_judge")], registry=registry, timeout_seconds=20)
    # Unkilled, the judge would block 2 x 180s. The 300s bound still proves the worker
    # was killed while allowing two cold DeepEval worker starts on a loaded Windows host.
    assert time.monotonic() - started < 300
    assert all(
        r.status is ExecutionStatus.ERROR and (r.reason or "").startswith("timeout:")
        for r in report.results
    )


def test_version_drift_is_refused() -> None:
    result = _plugin_python(
        """
import json, importlib.metadata
import aibench_deepeval.faithfulness as f
real = importlib.metadata.version
importlib.metadata.version = lambda name: "4.2.4" if name == "deepeval" else real(name)
try:
    f._require_pinned_deepeval()
    print(json.dumps("accepted"))
except RuntimeError as exc:
    print(json.dumps(str(exc)))
"""
    )
    assert "pinned to 4.2.5" in result


def test_no_deepeval_files_or_types_leak_into_the_harness(
    tmp_path: Path, registry: EvaluatorRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "CLAIM:FACT-A", retrieved_context=CONTEXT)])
    [result] = seeded.score([_judge("token_judge")], registry=registry, timeout_seconds=120).results
    assert result.status is ExecutionStatus.OK
    assert not (tmp_path / ".deepeval").exists()  # DeepEval wrote only in the worker's temp dir
    assert "deepeval" not in sys.modules
    json.loads(result.model_dump_json())  # plain canonical JSON, no framework objects


# --------------------------------------------------------------------------- live provider (opt-in)


@pytest.mark.skipif(
    os.environ.get("AIBENCH_LIVE_DEEPEVAL") != "1" or not os.environ.get("OPENAI_API_KEY"),
    reason="live-provider smoke needs AIBENCH_LIVE_DEEPEVAL=1 and OPENAI_API_KEY (one small judge call budget)",
)
def test_live_provider_smoke(tmp_path: Path) -> None:
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(
        PLUGIN_ENV, secret_env={"OPENAI_API_KEY": "env:OPENAI_API_KEY"}
    )
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("c1")],
        [
            execution(
                "c1",
                "Refunds are available within 30 days.",
                retrieved_context=("Refunds may be requested within 30 days of purchase.",),
            )
        ],
    )
    binding = {
        "metric": "deepeval.faithfulness",
        "params": {
            "judge": {
                "kind": "deepeval_model",
                "model": os.environ.get("AIBENCH_LIVE_JUDGE_MODEL", "gpt-4.1-mini"),
            }
        },
    }
    [result] = seeded.score([binding], registry=registry, timeout_seconds=180).results
    assert result.status is ExecutionStatus.OK and result.value is not None
    assert result.resources["accounting"] in ("reported", "partial")


# --------------------------------------------------------------------------- product path


def test_cli_scores_a_recorded_rag_run_with_deepeval(tmp_path: Path) -> None:
    """End to end: smoke-run an app that reports its retrieval, then
    `aibench score --plugin-env ...` with deepeval.faithfulness through the real CLI."""
    from typer.testing import CliRunner

    from aibench.cli.main import app

    script = tmp_path / "rag_app.py"
    script.write_text(
        "import json, sys\n"
        "q = json.load(sys.stdin)['input']\n"
        "print(json.dumps({'answer': 'CLAIM:FACT-A' if 'good' in q else 'CLAIM:FACT-Q',"
        " 'docs': ['FACT-A is documented.']}))\n",
        encoding="utf-8",
    )
    (tmp_path / "rag.app.json").write_text(
        json.dumps(
            {
                "application_id": "rag-probe",
                "runner": "cli",
                "target": "rag_app.py",
                "transport": {"kind": "cli", "argv": [sys.executable, str(script)]},
                "output_binding": {"output": "/answer", "retrieved_context": "/docs"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "data.jsonl").write_text(
        '{"case_id":"good","input":"good question"}\n{"case_id":"bad","input":"bad question"}\n',
        encoding="utf-8",
    )
    (tmp_path / "metrics.json").write_text(
        json.dumps({"metrics": [_judge("token_judge")]}), encoding="utf-8"
    )
    cli = CliRunner()
    smoke = cli.invoke(
        app,
        [
            "app",
            "smoke",
            str(tmp_path / "rag.app.json"),
            "--dataset",
            str(tmp_path / "data.jsonl"),
            "--workspace",
            str(tmp_path),
            "--trust-local-app",
            "--json",
        ],
    )
    assert smoke.exit_code == 0, smoke.output
    run_id = json.loads(smoke.output)["run_id"]
    scored = cli.invoke(
        app,
        [
            "score",
            run_id,
            "--metrics",
            str(tmp_path / "metrics.json"),
            "--workspace",
            str(tmp_path),
            "--plugin-env",
            str(PLUGIN_ENV),
            "--plugin-path",
            str(JUDGES),
            "--json",
        ],
    )
    assert scored.exit_code == 0, scored.output
    [summary] = json.loads(scored.output)["summaries"]
    assert summary["metric_id"] == "deepeval.faithfulness"
    assert (summary["completed"], summary["decisions"]["pass"], summary["decisions"]["fail"]) == (
        2,
        1,
        1,
    )
    assert summary["value_summary"]["mean"] == 0.5


# --------------------------------------------------------------------------- review regressions


@pytest.mark.parametrize(
    ("output", "context", "reason"),
    [
        ("I don't know.", CONTEXT, "no_claims"),  # upstream would score a vacuous 1.0
        ("CLAIM:FACT-A", ("   ", ""), "empty:execution.retrieved_context"),  # blank chunks
    ],
)
def test_vacuous_inputs_never_become_perfect_scores(
    tmp_path: Path, registry: EvaluatorRegistry, output: str, context: tuple[str, ...], reason: str
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", output, retrieved_context=context)])
    [result] = seeded.score([_judge("token_judge")], registry=registry, timeout_seconds=120).results
    assert (result.status, result.reason) == (ExecutionStatus.NOT_APPLICABLE, reason)
    assert result.value is None


def test_worker_startup_is_not_charged_to_the_per_case_timeout(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    """A cold DeepEval worker takes many seconds to start. With a per-case timeout far
    shorter than that, cases must still be evaluated: startup has its own bound."""
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "CLAIM:FACT-A", retrieved_context=CONTEXT)])
    [result] = seeded.score([_judge("token_judge")], registry=registry, timeout_seconds=3).results
    assert result.status is ExecutionStatus.OK, result.reason


def test_relative_plugin_env_paths_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The README's relative --plugin-env must work on every platform: the worker runs in a
    private directory, so the interpreter path has to be made absolute first."""
    monkeypatch.chdir(REPO_ROOT)
    relative = (
        PLUGIN_ENV.relative_to(REPO_ROOT) if PLUGIN_ENV.is_relative_to(REPO_ROOT) else PLUGIN_ENV
    )
    registry = EvaluatorRegistry.with_native()
    registry.load_plugin_environment(relative, extra_paths=[JUDGES])
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", "CLAIM:FACT-A", retrieved_context=CONTEXT)])
    [result] = seeded.score([_judge("token_judge")], registry=registry, timeout_seconds=120).results
    assert result.status is ExecutionStatus.OK, result.reason


def test_missing_plugin_secret_fails_before_scoring() -> None:
    from aibench.registry import RegistryError

    registry = EvaluatorRegistry.with_native()
    with pytest.raises(RegistryError, match="NOT_SET_AIBENCH_JUDGE_KEY"):
        registry.load_plugin_environment(
            PLUGIN_ENV, secret_env={"OPENAI_API_KEY": "env:NOT_SET_AIBENCH_JUDGE_KEY"}
        )
