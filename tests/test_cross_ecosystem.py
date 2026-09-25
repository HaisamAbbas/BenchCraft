"""Real DeepEval-versus-Ragas evidence over one set of stored executions.

This test is intentionally separate from the synthetic comparison tests. It loads
both pinned plugin interpreters, scores the same committed ``ExecutionResult``
objects, and then asks the shared comparison service for an exploratory
cross-framework diagnostic. The two deterministic judges deliberately express
different policies for one unsupported claim; the report must preserve that
 disagreement without inventing a cross-vendor average.

The test skips only when one of the optional plugin environments is absent. If
an environment exists but its real package contract is broken, the test fails.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import MetricBinding
from aibench.registry import EvaluatorRegistry
from aibench.services.comparison import compare_runs
from aibench.services.scoring import score_recorded_run
from tests.engine_support import Harness
from tests.runner_support import REPO_ROOT, run
from tests.scoring_support import Seeded, case, execution

DEEPEVAL_ENV = Path(
    os.environ.get("AIBENCH_DEEPEVAL_PYTHON")
    or REPO_ROOT
    / "plugins"
    / "deepeval"
    / ".venv"
    / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
)
RAGAS_ENV = Path(
    os.environ.get("AIBENCH_RAGAS_PYTHON")
    or REPO_ROOT
    / "plugins"
    / "ragas"
    / ".venv"
    / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
)
DEEPEVAL_JUDGES = REPO_ROOT / "tests" / "fixtures" / "deepeval_judges"
RAGAS_JUDGES = REPO_ROOT / "tests" / "fixtures" / "ragas_judges"

pytestmark = pytest.mark.skipif(
    not DEEPEVAL_ENV.is_file() or not RAGAS_ENV.is_file(),
    reason=(
        "cross-ecosystem evidence needs both plugins/ragas/.venv and "
        "plugins/deepeval/.venv (see their READMEs)"
    ),
)

_CONTEXT = ("FACT-A is documented.", "FACT-B is documented.")


def _registry(python: Path, judges: Path) -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    loads = registry.load_plugin_environment(
        python,
        extra_paths=[judges],
        # Ragas' cold catalogue import is measured separately from the case
        # timeout; both are finite and no application code is loaded here.
        startup_timeout_seconds=600,
    )
    assert [load.error for load in loads] == [None]
    return registry


def _binding(metric: str, factory: str) -> dict[str, Any]:
    return {
        "metric": metric,
        "params": {"judge": {"kind": "python_factory", "factory": factory}},
        "rule": {"comparator": ">=", "threshold": 0.5},
    }


def test_real_ecosystems_score_the_same_stored_outputs_without_application_calls(
    tmp_path: Path,
) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [case("one"), case("half"), case("zero")],
        [
            execution("one", "CLAIM:FACT-A and CLAIM:FACT-B", retrieved_context=_CONTEXT),
            execution("half", "CLAIM:FACT-A and CLAIM:FACT-Z", retrieved_context=_CONTEXT),
            execution("zero", "CLAIM:FACT-Z", retrieved_context=_CONTEXT),
        ],
    )
    before_execution_ids = [
        item.execution_id for item in seeded.storage.list_execution_attempts("run-1")
    ]
    before_evaluator_attempts = len(seeded.storage.list_evaluation_attempts("run-1"))

    deepeval = seeded.score(
        [_binding("deepeval.faithfulness", "aibench_test_judges:token_judge")],
        registry=_registry(DEEPEVAL_ENV, DEEPEVAL_JUDGES),
        timeout_seconds=600,
    )
    ragas = seeded.score(
        [_binding("ragas.faithfulness", "aibench_test_ragas_judges:lenient_token_judge")],
        registry=_registry(RAGAS_ENV, RAGAS_JUDGES),
        timeout_seconds=600,
    )

    # Stored-output scoring is the only operation in this test. The execution
    # attempt IDs/count are the application-invocation counter.
    assert before_execution_ids == [
        item.execution_id for item in seeded.storage.list_execution_attempts("run-1")
    ]
    assert len(seeded.storage.list_execution_attempts("run-1")) == len(before_execution_ids)
    assert len(seeded.storage.list_evaluation_attempts("run-1")) == before_evaluator_attempts + 6

    report = compare_runs(
        seeded.storage,
        seeded.artifacts,
        "run-1",
        "run-1",
        baseline_scoring_id=deepeval.scoring_id,
        current_scoring_id=ragas.scoring_id,
        mode="exploratory",
        bootstrap_replicates=100,
    )
    assert report["status"] == "exploratory"
    assert report["qualified"] is False
    assert report["invocation_basis"]["application_invocations"] == 0
    [diagnostic] = report["cross_framework"]
    assert diagnostic["paired_count"] == 3
    assert diagnostic["execution_identity_mismatch_count"] == 0
    assert diagnostic["execution_identity_unknown_count"] == 0
    # The independent lenient judge disagrees on the unsupported zero case.
    assert diagnostic["matrix"]["fail_pass"] >= 1
    assert diagnostic["cross_framework_difference_calculated"] is False
    assert diagnostic["combined_score_calculated"] is False
    assert diagnostic["scale_equivalence"] == "not_claimed"


def test_real_engine_stored_deepeval_and_ragas_scoring_does_not_invoke_the_application(
    tmp_path: Path,
) -> None:
    """Use a real RunEngine execution, then prove both real adapters add no calls."""
    harness = Harness(tmp_path)
    app_script = harness.root / "real_rag_app.py"
    app_script.write_text(
        "import json, os, pathlib, sys, time\n"
        "request = json.load(sys.stdin)\n"
        "text = request['input']\n"
        "log = pathlib.Path(os.environ['APP_LOG'])\n"
        "start = time.time()\n"
        "with log.open('a') as handle:\n"
        "    handle.write(json.dumps({'case': text, 'start': start, 'end': time.time()}) + '\\n')\n"
        "claims = {'one': 'CLAIM:FACT-A and CLAIM:FACT-B',"
        " 'half': 'CLAIM:FACT-A and CLAIM:FACT-Z', 'zero': 'CLAIM:FACT-Z'}\n"
        "print(json.dumps({'output': claims.get(text, 'CLAIM:FACT-A'),"
        " 'retrieved_context': list(" + repr(list(_CONTEXT)) + ")}))\n",
        encoding="utf-8",
    )
    app_config = {
        "application_id": "real-rag",
        "runner": "cli",
        "target": app_script.name,
        "output_binding": {"output": "/output", "retrieved_context": "/retrieved_context"},
        "transport": {
            "kind": "cli",
            "argv": [sys.executable, app_script.name],
            "env": {"APP_LOG": str(harness.log)},
        },
    }
    (harness.root / "real_rag.app.json").write_text(json.dumps(app_config), encoding="utf-8")
    plan = harness.plan(
        dataset=harness.dataset({"one": "one", "half": "half", "zero": "zero"}),
        application="real_rag.app.json",
    )
    run_id = harness.create(plan)
    harness.execute(run_id)
    assert harness.count() == 3

    storage, artifacts = harness.storage()
    before_ids = [item.execution_id for item in storage.list_execution_attempts(run_id)]
    before_calls = harness.count()
    try:
        report = run(
            score_recorded_run(
                storage=storage,
                artifacts=artifacts,
                registry=_registry(RAGAS_ENV, RAGAS_JUDGES),
                run_id=run_id,
                bindings=[
                    MetricBinding.model_validate(
                        _binding(
                            "ragas.faithfulness",
                            "aibench_test_ragas_judges:lenient_token_judge",
                        )
                    )
                ],
                timeout_seconds=600,
            )
        )
        assert len(report.results) == 3
        deepeval = run(
            score_recorded_run(
                storage=storage,
                artifacts=artifacts,
                registry=_registry(DEEPEVAL_ENV, DEEPEVAL_JUDGES),
                run_id=run_id,
                bindings=[
                    MetricBinding.model_validate(
                        _binding("deepeval.faithfulness", "aibench_test_judges:token_judge")
                    )
                ],
                timeout_seconds=600,
            )
        )
        assert len(deepeval.results) == 3
        diagnostic_report = compare_runs(
            storage,
            artifacts,
            run_id,
            run_id,
            baseline_scoring_id=deepeval.scoring_id,
            current_scoring_id=report.scoring_id,
            mode="exploratory",
            bootstrap_replicates=100,
        )
        assert diagnostic_report["status"] == "exploratory"
        assert diagnostic_report["cross_framework"][0]["paired_count"] == 3
        assert diagnostic_report["cross_framework"][0]["execution_identity_mismatch_count"] == 0
        assert harness.count() == before_calls
        assert [item.execution_id for item in storage.list_execution_attempts(run_id)] == before_ids
    finally:
        storage.db.close()
