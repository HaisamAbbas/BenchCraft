"""Real-package contract tests for the isolated Ragas 0.4.3 adapter.

Every test in this module that needs Ragas runs in ``plugins/ragas/.venv`` or
in an aibench worker using that interpreter.  The test process imports no Ragas
module: discovery and evaluation are deliberately isolated.  The deterministic
judges in ``tests/fixtures/ragas_judges`` are real InstructorBaseRagasLLM
subclasses and return Ragas' exact pinned response model classes; no Ragas
metric is mocked.

The module skips only when the optional plugin interpreter is absent.  If the
interpreter exists but the package is not installed or the real contract is
broken, the tests fail rather than silently substituting a fake package.
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
    os.environ.get("AIBENCH_RAGAS_PYTHON")
    or REPO_ROOT
    / "plugins"
    / "ragas"
    / ".venv"
    / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
)
JUDGES = REPO_ROOT / "tests" / "fixtures" / "ragas_judges"
pytestmark = pytest.mark.skipif(
    not PLUGIN_ENV.is_file(),
    reason=f"Ragas plugin environment not installed at {PLUGIN_ENV} (see plugins/ragas/README.md)",
)

CONTEXT = ("FACT-A is documented.", "FACT-B is documented.")


def _judge(name: str) -> dict[str, Any]:
    return {
        "metric": "ragas.faithfulness",
        "params": {
            "judge": {"kind": "python_factory", "factory": f"aibench_test_ragas_judges:{name}"}
        },
    }


@pytest.fixture(scope="module")
def registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        PLUGIN_ENV,
        extra_paths=[JUDGES],
        startup_timeout_seconds=600,
    )
    assert [load.error for load in loads] == [None]
    return registry


@pytest.fixture(scope="module")
def real_scores(
    registry: EvaluatorRegistry, tmp_path_factory: pytest.TempPathFactory
) -> tuple[Seeded, Any]:
    """One real worker pass shared by the finite and NaN raw-contract tests."""

    seeded = Seeded(tmp_path_factory.mktemp("ragas-real-scores"))
    seeded.seed(
        [case("one"), case("half"), case("zero"), case("empty-statements")],
        [
            execution("one", "CLAIM:FACT-A and CLAIM:FACT-B", retrieved_context=CONTEXT),
            execution("half", "CLAIM:FACT-A and CLAIM:FACT-Z", retrieved_context=CONTEXT),
            execution("zero", "CLAIM:FACT-Z", retrieved_context=CONTEXT),
            execution("empty-statements", "NO_STATEMENTS", retrieved_context=CONTEXT),
        ],
    )
    report = seeded.score(
        [_judge("token_judge") | {"rule": {"comparator": ">=", "threshold": 0.8}}],
        registry=registry,
        timeout_seconds=600,
    )
    return seeded, report


def _plugin_python(code: str) -> Any:
    """Run a contract snippet in the isolated plugin interpreter."""

    env = {k: v for k, v in os.environ.items() if k in ("PATH", "SYSTEMROOT", "TEMP", "TMP")}
    env.update(
        PYTHONPATH=str(JUDGES),
        RAGAS_DO_NOT_TRACK="true",
        PYTHONNOUSERSITE="1",
    )
    done = subprocess.run(
        [str(PLUGIN_ENV), "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        cwd=JUDGES.parent,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------- discovery/contract


def test_discovery_finds_pinned_adapter_without_importing_ragas_here(
    registry: EvaluatorRegistry,
) -> None:
    manifest, _ = registry.resolve("ragas.faithfulness@1")
    assert manifest.evaluator_id == "ragas.faithfulness"
    assert manifest.version == "1.0.0"
    assert manifest.plugin_id == "aibench-ragas"
    assert manifest.package_name == "ragas" and manifest.package_version == "0.4.3"
    assert manifest.requires_worker
    assert manifest.consumes == "recorded_outputs"
    assert manifest.uses_models
    assert manifest.internal_retries == 0
    assert manifest.internal_concurrency == 1
    assert "0.4.3" in manifest.description
    assert any("text-only" in limitation.lower() for limitation in manifest.limitations)
    assert any("deepeval" in limitation.lower() for limitation in manifest.limitations)
    # Discovery reads metadata and loads manifests in another process.
    assert "ragas" not in sys.modules
    assert "aibench_ragas" not in sys.modules


def test_exact_field_mapping_never_uses_reference_context() -> None:
    result = _plugin_python(
        """
import json
from aibench.core.models import BenchmarkCase, ExecutionResult, ReferenceAnswer
from aibench.evaluators.protocol import EvaluationView
from aibench_ragas.faithfulness import build_inputs
case = BenchmarkCase(
    case_id="c",
    input={"question": "q?"},
    reference=ReferenceAnswer(context=("REFERENCE ONLY",)),
)
execution = ExecutionResult(
    execution_id="e", run_id="r", case_id="c", status="ok", output="answer",
    retrieved_context=("doc one", "   ", "doc two"),
)
print(json.dumps(build_inputs(EvaluationView(case=case, execution=execution))))
"""
    )
    assert result == {
        "user_input": '{"question": "q?"}',
        "response": "answer",
        "retrieved_contexts": ["doc one", "doc two"],
    }
    assert "REFERENCE ONLY" not in json.dumps(result)


def test_llm_factory_binding_is_structurally_supported_without_credentials_in_params(
    registry: EvaluatorRegistry,
) -> None:
    from aibench.core.models import MetricBinding

    resolved = registry.resolve_binding(
        MetricBinding.model_validate(
            {
                "metric": "ragas.faithfulness",
                "params": {"judge": {"kind": "llm_factory", "model": "gpt-4o-mini"}},
            }
        )
    )
    assert resolved.manifest.evaluator_id == "ragas.faithfulness"
    assert resolved.manifest.credentials
    assert "worker environment" in " ".join(resolved.manifest.credentials)


def test_llm_factory_constructs_with_the_advisory_fixed_openai_sdk() -> None:
    result = _plugin_python(
        """
import asyncio, importlib.metadata, json, os
os.environ["OPENAI_API_KEY"] = "sk-test-only"
from ragas.llms.base import InstructorBaseRagasLLM
from aibench_ragas.faithfulness import Faithfulness

async def main():
    evaluator = Faithfulness()
    await evaluator.prepare({"judge": {"kind": "llm_factory", "model": "gpt-4o-mini"}})
    judge = evaluator._new_judge()
    return {
        "is_ragas_judge": isinstance(judge, InstructorBaseRagasLLM),
        "instructor": importlib.metadata.version("instructor"),
        "openai": importlib.metadata.version("openai"),
    }

print(json.dumps(asyncio.run(main())))
"""
    )
    from packaging.version import Version

    assert result["is_ragas_judge"] is True
    assert Version("1.17.0") <= Version(result["instructor"]) < Version("1.18")
    assert Version("2.26") <= Version(result["openai"]) < Version("3")


# ---------------------------------------------------------------- real worker scores


def test_real_worker_scores_one_half_and_zero_with_raw_ragas_semantics(
    real_scores: tuple[Seeded, Any],
) -> None:
    seeded, report = real_scores
    by_case = {result.case_id: result for result in report.results}
    assert {
        case_id: (result.value.value if result.value else None, result.decision)
        for case_id, result in by_case.items()
        if case_id != "empty-statements"
    } == {
        "one": (1.0, Decision.PASS),
        "half": (0.5, Decision.FAIL),
        "zero": (0.0, Decision.FAIL),
    }
    raw = json.loads(
        seeded.artifacts.read_bytes(seeded.storage.get_artifact(by_case["half"].raw_artifact_ref))
    )
    assert raw["ragas_version"] == "0.4.3"
    assert raw["score"] == 0.5
    assert raw["upstream"]["value"] == 0.5
    assert raw["reason"] is None
    assert raw["traces"] is None
    assert "aibench_test_ragas_judges:token_judge" in raw["judge"]
    # Ragas 0.4.3 does not expose a dependable cost/usage contract here.
    assert by_case["half"].resources["cost"] is None
    assert by_case["half"].resources["accounting"] == "unknown"


def test_no_statements_nan_is_not_applicable_not_zero_or_one(
    real_scores: tuple[Seeded, Any],
) -> None:
    seeded, report = real_scores
    result = next(result for result in report.results if result.case_id == "empty-statements")
    assert result.status is ExecutionStatus.NOT_APPLICABLE
    assert result.reason == "no_statements"
    assert result.value is None
    assert result.raw_artifact_ref is not None
    raw = json.loads(
        seeded.artifacts.read_bytes(seeded.storage.get_artifact(result.raw_artifact_ref))
    )
    assert raw["score"] is None
    assert raw["upstream_value"] == "nan"
    assert raw["ragas_version"] == "0.4.3"


# ---------------------------------------------------------------- evidence policies


def test_missing_empty_blank_and_non_text_policies_never_invent_evidence(
    tmp_path: Path, registry: EvaluatorRegistry
) -> None:
    seeded = Seeded(tmp_path)
    golden = case("missing").model_copy(
        update={"reference": ReferenceAnswer(context=("GOLDEN-ONLY-CONTEXT",))}
    )
    seeded.seed(
        [
            golden,
            case("empty"),
            case("blank-context"),
            case("dict-output"),
            case("blank-output"),
        ],
        [
            execution("missing", "CLAIM:FACT-A"),
            execution("empty", "CLAIM:FACT-A", retrieved_context=()),
            execution("blank-context", "CLAIM:FACT-A", retrieved_context=("", "   ")),
            execution("dict-output", {"answer": "CLAIM:FACT-A"}, retrieved_context=CONTEXT),
            execution("blank-output", "   ", retrieved_context=CONTEXT),
        ],
    )
    report = seeded.score([_judge("token_judge")], registry=registry, timeout_seconds=600)
    assert {result.case_id: (result.status, result.reason) for result in report.results} == {
        "missing": (ExecutionStatus.NOT_APPLICABLE, "missing:execution.retrieved_context"),
        "empty": (ExecutionStatus.NOT_APPLICABLE, "empty:execution.retrieved_context"),
        "blank-context": (ExecutionStatus.NOT_APPLICABLE, "empty:execution.retrieved_context"),
        "dict-output": (ExecutionStatus.NOT_APPLICABLE, "unscorable_output:dict"),
        "blank-output": (ExecutionStatus.NOT_APPLICABLE, "unscorable_output:blank"),
    }
    assert all(
        result.value is None and result.decision is Decision.NOT_EVALUATED
        for result in report.results
    )
    assert all("GOLDEN-ONLY-CONTEXT" not in (result.reason or "") for result in report.results)


def test_non_text_context_is_rejected_before_the_real_metric() -> None:
    result = _plugin_python(
        """
import asyncio, json
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluatorContext, EvaluationView
from aibench_ragas.faithfulness import Faithfulness
case = BenchmarkCase(case_id="c", input="q")
execution = ExecutionResult.model_construct(
    execution_id="e", run_id="r", case_id="c", status="ok", output="answer",
    retrieved_context=(object(),),
)
async def main():
    evaluator = Faithfulness()
    await evaluator.prepare({"judge": {"kind": "python_factory", "factory": "aibench_test_ragas_judges:token_judge"}})
    return await evaluator.evaluate(EvaluationView(case=case, execution=execution), EvaluatorContext(run_id="r", scoring_id="s"))
outcome = asyncio.run(main())
print(json.dumps({"status": outcome.status.value, "reason": outcome.reason, "value": outcome.value}))
"""
    )
    assert result == {
        "status": "not_applicable",
        "reason": "unscorable_context:object",
        "value": None,
    }


def test_text_metric_does_not_enter_unpatched_multimodal_or_diskcache_paths() -> None:
    result = _plugin_python(
        """
import asyncio, json
from ragas.cache import DiskCacheBackend
from ragas.metrics.collections.multi_modal_faithfulness import util
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluatorContext, EvaluationView
from aibench_ragas.faithfulness import Faithfulness

def forbidden(*args, **kwargs):
    raise AssertionError("out-of-scope Ragas path was called")

async def main():
    evaluator = Faithfulness()
    await evaluator.prepare({"judge": {"kind": "python_factory", "factory": "aibench_test_ragas_judges:token_judge"}})
    util.process_image_to_base64 = forbidden
    DiskCacheBackend.__init__ = forbidden
    view = EvaluationView(
        case=BenchmarkCase(case_id="c", input="q"),
        execution=ExecutionResult(
            execution_id="e", run_id="r", case_id="c", status="ok",
            output="CLAIM:FACT-A", retrieved_context=("FACT-A is documented.",),
        ),
    )
    return await evaluator.evaluate(view, EvaluatorContext(run_id="r", scoring_id="s"))

outcome = asyncio.run(main())
print(json.dumps({"status": outcome.status.value, "value": outcome.value.value}))
"""
    )
    assert result["status"] == "ok"
    assert result["value"] == 1.0


# ---------------------------------------------------------------- isolation and drift


def test_fresh_metric_and_judge_instances_handle_interleaved_cases() -> None:
    result = _plugin_python(
        """
import asyncio, json
from aibench.core.models import BenchmarkCase, ExecutionResult
from aibench.evaluators.protocol import EvaluatorContext, EvaluationView
from aibench_ragas.faithfulness import Faithfulness

outputs = {
    f"c{i}": ("CLAIM:FACT-A" if i % 2 == 0 else "CLAIM:FACT-A and CLAIM:FACT-Z")
    for i in range(6)
}
views = {
    case_id: EvaluationView(
        case=BenchmarkCase(case_id=case_id, input="q"),
        execution=ExecutionResult(
            execution_id=case_id, run_id="r", case_id=case_id, status="ok",
            output=output, retrieved_context=("FACT-A is documented.",),
        ),
    )
    for case_id, output in outputs.items()
}
async def main():
    evaluator = Faithfulness()
    await evaluator.prepare({"judge": {"kind": "python_factory", "factory": "aibench_test_ragas_judges:slow_token_judge"}})
    outcomes = await asyncio.gather(*(
        evaluator.evaluate(view, EvaluatorContext(run_id="r", scoring_id="s"))
        for view in views.values()
    ))
    return {case_id: outcome.value.value for case_id, outcome in zip(views, outcomes)}
print(json.dumps(asyncio.run(main())))
"""
    )
    assert result == {"c0": 1.0, "c1": 0.5, "c2": 1.0, "c3": 0.5, "c4": 1.0, "c5": 0.5}


def test_version_drift_is_refused_before_ragas_is_imported() -> None:
    result = _plugin_python(
        """
import importlib.metadata, json
import aibench_ragas.faithfulness as adapter
real_version = importlib.metadata.version
importlib.metadata.version = lambda name: "0.4.2" if name == "ragas" else real_version(name)
try:
    adapter._require_pinned_ragas()
except RuntimeError as exc:
    print(json.dumps(str(exc)))
else:
    print(json.dumps("accepted"))
"""
    )
    assert "pinned to 0.4.3" in result
