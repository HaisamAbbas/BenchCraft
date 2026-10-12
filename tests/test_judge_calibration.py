"""Judge calibration and scoring invariants (§23 "Judge and outcome validation",
"Engine and contract validation"; 12-T3).

Calibration numbers are pinned as observations of the native evaluators against the v1
labels (written by the implementing agent; unreviewed). They quantify known limitations
rather than hide them: surface matching rejects correct paraphrases.

The invariant test is a seeded randomized (property-style) check through the real scorer,
with an evaluator that misbehaves on purpose: evaluation never mutates the case, anything
but an ok result carries no score, and a decision follows the frozen rule. It asserts that
every branch (pass, fail, error, not applicable, skipped) actually occurred."""

from __future__ import annotations

import asyncio
import json
import random
import string
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli import score as score_cli
from aibench.cli.main import app
from aibench.core.models import (
    BenchmarkCase,
    Decision,
    DecisionRule,
    EvaluatorManifest,
    ExecutionResult,
    ExecutionStatus,
    FieldRequirement,
    MetricBinding,
    MetricDirection,
    ReferenceAnswer,
    RunManifest,
)
from aibench.core.plans import BudgetLimits, PluginEnvironmentRef
from aibench.evaluators.protocol import (
    EvaluationOutcome,
    EvaluationView,
    Evaluator,
    EvaluatorContext,
)
from aibench.registry import EvaluatorRegistry
from aibench.security.policy import ExecutionPolicy, plugin_denials
from aibench.services.calibration import CalibrationError, load_calibration_set, run_calibration
from aibench.services.scoring import BindingScorer
from aibench.storage.artifacts import ArtifactStore
from aibench.storage.db import Database, Workspace
from aibench.storage.repositories import Storage

SET = Path(__file__).resolve().parents[1] / "benchmarks" / "judges" / "v1"


def _frac(measure: dict[str, Any]) -> tuple[int, int]:
    return measure["numerator"], measure["denominator"]


def test_native_evaluators_against_the_labelled_calibration_set() -> None:
    report = run_calibration(SET)
    assert report["label_review"] == "unreviewed"
    strict = report["bindings"]["native.exact_match {}"]["overall"]
    normalized = report["bindings"][
        'native.exact_match {"case_sensitive": false, "collapse_whitespace": true}'
    ]["overall"]
    [schema_key] = [k for k in report["bindings"] if k.startswith("native.json_schema")]
    schema = report["bindings"][schema_key]["overall"]
    # no false acceptance anywhere: injected instructions and contradictions are rejected
    for measured in (strict, normalized, schema):
        assert measured["false_acceptance"]["numerator"] == 0
        assert measured["undecided"] == 0
        assert measured["repeat_stability"]["value"] == 1.0
    # the known limitation, quantified: correct but differently worded answers fail
    assert strict["observations"] == 26
    assert _frac(strict["false_rejection"]) == (10, 14)
    assert _frac(normalized["false_rejection"]) == (6, 14)
    assert _frac(schema["agreement"]) == (14, 14)
    rejected = {d["category"] for d in report["bindings"]["native.exact_match {}"]["disagreements"]}
    assert rejected == {"formatting", "paraphrase", "verbose", "non_text"}
    assert next(n.get("evaluator") for n in report["not_measured"]) == "deepeval.faithfulness@1"


def test_calibrate_command_reports_unmeasured_judges(tmp_path: Path) -> None:
    out = tmp_path / "calibration.json"
    result = CliRunner().invoke(
        app, ["evaluators", "calibrate", "--set", str(SET), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    text = " ".join(result.output.split())
    assert "false rejection 10/14" in text and "labels: unreviewed" in text
    assert "not measured deepeval.faithfulness@1" in text
    stored = json.loads(out.read_text(encoding="utf-8"))
    assert stored["set"] == "judge-calibration-v1"
    assert stored["schema"] == "aibench.judge-calibration-report/3"


def test_calibration_repetitions_are_configurable_and_count_observations() -> None:
    report = run_calibration(SET, repetitions=3)
    strict = report["bindings"]["native.exact_match {}"]["overall"]
    assert report["repetitions"] == 3
    assert strict["observations"] == 39
    assert _frac(strict["false_rejection"]) == (15, 21)
    assert _frac(strict["repeat_stability"]) == (13, 13)


class CalibrationFields(Evaluator):
    manifest = EvaluatorManifest(
        evaluator_id="test.calibration_fields",
        version="1.0.0",
        plugin_id="test",
        plugin_version="0",
        description="checks calibration case input and retrieved evidence",
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(
            FieldRequirement(path="case.input"),
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="execution.retrieved_context"),
            FieldRequirement(path="case.reference.context"),
        ),
        default_rule=DecisionRule(comparator="is_true"),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        del ctx
        valid = (
            view.get("case.input") == "What is the return period?"
            and view.get("execution.output") == "30 days"
            and view.get("execution.retrieved_context") == ["Return within 30 days."]
            and view.get("case.reference.context")
            == ["Policy: returns are accepted within 30 days."]
        )
        return EvaluationOutcome.ok("boolean", valid)


def test_calibration_carries_full_case_evidence_into_the_evaluator(tmp_path: Path) -> None:
    (tmp_path / "set.json").write_text(
        json.dumps({"schema": "aibench.judge-calibration/1", "version": "fields-v1"}),
        encoding="utf-8",
    )
    (tmp_path / "cases.jsonl").write_text(
        json.dumps(
            {
                "id": "field-case",
                "category": "correct",
                "metric": "test.calibration_fields",
                "input": "What is the return period?",
                "reference": {
                    "answer": "30 days",
                    "context": ["Policy: returns are accepted within 30 days."],
                },
                "output": "30 days",
                "retrieved_context": ["Return within 30 days."],
                "label": "acceptable",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = EvaluatorRegistry.with_native()
    registry.register(CalibrationFields)
    report = run_calibration(tmp_path, registry, repetitions=1)
    result = report["cases"][0]
    assert result["decision"] == "pass"
    assert report["bindings"][result["binding"]]["overall"]["agreement"]["value"] == 1.0
    assert report["bindings"][result["binding"]]["overall"]["repeat_stability"] == {
        "value": None,
        "numerator": 0,
        "denominator": 0,
    }


class CalibrationModelJudge(Evaluator):
    calls = 0
    manifest = EvaluatorManifest(
        evaluator_id="test.calibration_model",
        version="1.0.0",
        plugin_id="test",
        plugin_version="0",
        description="fake model judge for calibration safety and accounting tests",
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(FieldRequirement(path="case.input"),),
        default_rule=DecisionRule(comparator="is_true"),
        uses_models=True,
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        type(self).calls += 1
        ctx.report_usage(provider="fixture", calls=1, cost=None)
        return EvaluationOutcome.ok("boolean", view.get("case.input") == "question")


def _write_live_calibration_set(
    root: Path, *, reviewed: bool = True, metric: str = "test.calibration_model"
) -> Path:
    root.mkdir()
    (root / "set.json").write_text(
        json.dumps(
            {
                "schema": "aibench.judge-calibration/1",
                "version": "cli-model-v1",
                "review": (
                    {"status": "reviewed", "reviewer": "test reviewer"}
                    if reviewed
                    else {"status": "unreviewed"}
                ),
                "not_measured": [
                    {"evaluator": "test.calibration_model@1", "reason": "not measured yet"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "cases.jsonl").write_text(
        json.dumps(
            {
                "id": "cli-model-case",
                "category": "correct",
                "metric": metric,
                "input": "question",
                "label": "acceptable",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return root


def test_model_calibration_requires_review_opt_in_and_a_bounded_repeat_count(
    tmp_path: Path,
) -> None:
    (tmp_path / "set.json").write_text(
        json.dumps(
            {
                "schema": "aibench.judge-calibration/1",
                "version": "model-v1",
                "review": {"status": "reviewed", "reviewer": "test reviewer"},
                "not_measured": [
                    {"evaluator": "test.calibration_model@1", "reason": "not run yet"},
                    {"measure": "style sensitivity", "reason": "no paired cases"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "cases.jsonl").write_text(
        json.dumps(
            {
                "id": "model-case",
                "category": "correct",
                "metric": "test.calibration_model",
                "input": "question",
                "output": "answer",
                "label": "acceptable",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = EvaluatorRegistry.with_native()
    registry.register(CalibrationModelJudge)
    CalibrationModelJudge.calls = 0

    with pytest.raises(CalibrationError, match="explicit live authorization"):
        run_calibration(tmp_path, registry, repetitions=2)
    with pytest.raises(CalibrationError, match="--max-model-evaluations"):
        run_calibration(
            tmp_path, registry, repetitions=2, allow_models=True, max_model_evaluations=1
        )
    assert CalibrationModelJudge.calls == 0

    report = run_calibration(tmp_path, registry, repetitions=2, allow_models=True)
    assert CalibrationModelJudge.calls == 2
    assert report["not_measured"] == [{"measure": "style sensitivity", "reason": "no paired cases"}]
    assert report["usage"]["model_evaluations"] == 2
    assert report["usage"]["reported_calls"] == 2
    assert report["usage"]["reported_cost"] is None
    assert report["usage"]["unknown_cost_evaluations"] == 2


def test_live_calibration_requires_review_and_plugin_environment_policy_before_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration", reviewed=False)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(ExecutionPolicy().model_dump_json(), encoding="utf-8")
    loads = 0

    def fake_registry(*args: Any) -> EvaluatorRegistry:
        nonlocal loads
        loads += 1
        return EvaluatorRegistry.with_native()

    monkeypatch.setattr(score_cli, "_registry", fake_registry)
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            sys.executable,
        ],
    )
    assert result.exit_code == 2
    assert "review.status='reviewed'" in result.output
    assert loads == 0


def test_live_calibration_rejects_an_unapproved_plugin_environment_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(ExecutionPolicy().model_dump_json(), encoding="utf-8")
    loads = 0

    def fake_registry(*args: Any) -> EvaluatorRegistry:
        nonlocal loads
        loads += 1
        return EvaluatorRegistry.with_native()

    monkeypatch.setattr(score_cli, "_registry", fake_registry)
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            sys.executable,
            "--json",
        ],
    )
    assert result.exit_code == 4
    error = json.loads(result.output)
    assert error["schema"] == "aibench.cli-error/1"
    assert error["exit_code"] == 4
    assert "plugin environment" in error["details"][0] and "not allowed" in error["details"][0]
    assert loads == 0


def test_live_calibration_applies_the_policy_evaluator_call_ceiling_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration")
    plugin_python = str(Path(sys.executable).absolute())
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        ExecutionPolicy(
            allowed_plugin_environments=(plugin_python,),
            ceilings=BudgetLimits(max_evaluator_calls=1),
        ).model_dump_json(),
        encoding="utf-8",
    )
    loads = 0

    def fake_registry(*args: Any) -> EvaluatorRegistry:
        nonlocal loads
        loads += 1
        return EvaluatorRegistry.with_native()

    monkeypatch.setattr(score_cli, "_registry", fake_registry)
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            sys.executable,
            "--repetitions",
            "2",
        ],
    )
    assert result.exit_code == 4
    assert "policy ceiling max_evaluator_calls=1" in result.output
    assert loads == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("max_judge_tokens", 10), ("max_cost_usd", 0.15), ("max_wall_seconds", 5)],
)
def test_live_calibration_refuses_policy_ceilings_it_cannot_enforce_before_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: float,
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration")
    plugin_python = str(Path(sys.executable).absolute())
    limits: dict[str, Any] = {field: value}
    if field == "max_cost_usd":
        limits["estimated_cost_per_application_call_usd"] = 0.1
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        ExecutionPolicy(
            allowed_plugin_environments=(plugin_python,),
            ceilings=BudgetLimits.model_validate(limits),
        ).model_dump_json(),
        encoding="utf-8",
    )
    loads = 0

    def fake_registry(*args: Any) -> EvaluatorRegistry:
        nonlocal loads
        loads += 1
        return EvaluatorRegistry.with_native()

    monkeypatch.setattr(score_cli, "_registry", fake_registry)
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            sys.executable,
        ],
    )
    assert result.exit_code == 4
    assert f"cannot enforce policy ceiling {field}=" in result.output
    assert loads == 0


def test_live_calibration_applies_policy_data_roots_before_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration")
    plugin_python = str(Path(sys.executable).absolute())
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        ExecutionPolicy(
            allowed_plugin_environments=(plugin_python,),
            data_roots=(str(tmp_path / "approved-data"),),
        ).model_dump_json(),
        encoding="utf-8",
    )
    loads = 0

    def fake_registry(*args: Any) -> EvaluatorRegistry:
        nonlocal loads
        loads += 1
        return EvaluatorRegistry.with_native()

    monkeypatch.setattr(score_cli, "_registry", fake_registry)
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            sys.executable,
        ],
    )
    assert result.exit_code == 4
    assert "outside the policy's data_roots" in result.output
    assert loads == 0


@pytest.mark.parametrize("filename", ["set.json", "cases.jsonl"])
def test_live_calibration_rejects_a_file_resolving_outside_data_roots_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    data_root = tmp_path / "approved-data"
    data_root.mkdir()
    calibration_set = _write_live_calibration_set(data_root / "calibration")
    plugin_python = str(Path(sys.executable).absolute())
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        ExecutionPolicy(
            allowed_plugin_environments=(plugin_python,),
            data_roots=(str(data_root),),
        ).model_dump_json(),
        encoding="utf-8",
    )
    original_resolve = Path.resolve
    outside_file = tmp_path / "outside" / filename

    def resolve_with_external_case(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path == calibration_set / filename:
            return outside_file
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve_with_external_case)
    loads = 0

    def fake_registry(*args: Any) -> EvaluatorRegistry:
        nonlocal loads
        loads += 1
        return EvaluatorRegistry.with_native()

    monkeypatch.setattr(score_cli, "_registry", fake_registry)
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            sys.executable,
        ],
    )
    assert result.exit_code == 4
    message = " ".join(result.output.split())
    assert f"{filename} resolves outside the policy's data_roots" in message
    assert loads == 0


def test_plugin_policy_preserves_a_virtualenv_interpreter_symlink(tmp_path: Path) -> None:
    link = tmp_path / "venv-python"
    try:
        link.symlink_to(Path(sys.executable))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create interpreter symlink on this host: {exc}")

    policy = ExecutionPolicy(allowed_plugin_environments=(str(link),)).resolved_against(tmp_path)
    assert policy.allowed_plugin_environments == (str(link),)
    assert plugin_denials(policy, [PluginEnvironmentRef(python=str(link))], tmp_path) == []


def test_live_calibration_checks_evaluator_policy_and_runs_fake_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration")
    plugin_python = str(Path(sys.executable).resolve())
    registry = EvaluatorRegistry.with_native()
    registry.register(CalibrationModelJudge)
    CalibrationModelJudge.calls = 0
    monkeypatch.setattr(score_cli, "_registry", lambda *args: registry)
    policy_path = tmp_path / "policy.json"
    out = tmp_path / "report.json"
    policy_path.write_text(
        ExecutionPolicy(
            allowed_evaluators=("test.calibration_model",),
            allow_model_evaluators=True,
            allowed_plugin_environments=(plugin_python,),
        ).model_dump_json(),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            plugin_python,
            "--repetitions",
            "2",
            "--max-model-evaluations",
            "2",
            "--timeout",
            "5",
            "--out",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "live"
    assert report["usage"]["model_evaluations"] == 2
    assert report["usage"]["reported_cost"] is None
    assert report["not_measured"] == []
    assert CalibrationModelJudge.calls == 2


def test_live_calibration_runs_a_model_judge_in_the_isolated_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_worker_evaluator import _registry as worker_registry

    plugin_root = tmp_path / "site"
    registry = worker_registry(tmp_path)
    manifest = registry.resolve("vendor.probe")[0]
    registry._external[(manifest.evaluator_id, manifest.version)] = manifest.model_copy(
        update={"uses_models": True}
    )
    calibration_set = _write_live_calibration_set(tmp_path / "calibration", metric="vendor.probe")
    plugin_python = str(Path(sys.executable).absolute())
    monkeypatch.setattr(score_cli, "_registry", lambda *args: registry)
    policy_path = tmp_path / "policy.json"
    out = tmp_path / "worker-report.json"
    policy_path.write_text(
        ExecutionPolicy(
            allowed_evaluators=("vendor.probe",),
            allow_model_evaluators=True,
            allowed_plugin_environments=(plugin_python,),
            allowed_plugin_paths=(str(plugin_root),),
        ).model_dump_json(),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            plugin_python,
            "--plugin-path",
            str(plugin_root),
            "--repetitions",
            "1",
            "--max-model-evaluations",
            "1",
            "--timeout",
            "15",
            "--out",
            str(out),
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "live"
    assert report["cases"][0]["decision"] == "pass"
    assert report["usage"]["model_evaluations"] == 1
    assert report["usage"]["unknown_cost_evaluations"] == 1


class DeniedCalibrationModelJudge(CalibrationModelJudge):
    calls = 0
    manifest = CalibrationModelJudge.manifest.model_copy(
        update={"evaluator_id": "test.calibration_denied"}
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        del view, ctx
        type(self).calls += 1
        return EvaluationOutcome.ok("boolean", True)


def test_live_calibration_evaluates_the_same_case_snapshot_checked_by_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration")
    plugin_python = str(Path(sys.executable).absolute())
    registry = EvaluatorRegistry.with_native()
    registry.register(CalibrationModelJudge)
    registry.register(DeniedCalibrationModelJudge)
    CalibrationModelJudge.calls = 0
    DeniedCalibrationModelJudge.calls = 0

    def fake_registry(*args: Any) -> EvaluatorRegistry:
        # Simulate an approved plugin worker changing the set on disk during manifest load.
        (calibration_set / "cases.jsonl").write_text(
            json.dumps(
                {
                    "id": "changed-case",
                    "category": "correct",
                    "metric": "test.calibration_denied",
                    "input": "question",
                    "label": "acceptable",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        return registry

    monkeypatch.setattr(score_cli, "_registry", fake_registry)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        ExecutionPolicy(
            allowed_evaluators=("test.calibration_model",),
            allow_model_evaluators=True,
            allowed_plugin_environments=(plugin_python,),
        ).model_dump_json(),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            plugin_python,
        ],
    )
    assert result.exit_code == 0, result.output
    assert CalibrationModelJudge.calls == 2
    assert DeniedCalibrationModelJudge.calls == 0


def test_live_calibration_denied_model_policy_makes_no_evaluation_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calibration_set = _write_live_calibration_set(tmp_path / "calibration")
    plugin_python = str(Path(sys.executable).resolve())
    registry = EvaluatorRegistry.with_native()
    registry.register(CalibrationModelJudge)
    CalibrationModelJudge.calls = 0
    monkeypatch.setattr(score_cli, "_registry", lambda *args: registry)
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(
        ExecutionPolicy(allowed_plugin_environments=(plugin_python,)).model_dump_json(),
        encoding="utf-8",
    )
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(calibration_set),
            "--live",
            "--policy",
            str(policy_path),
            "--plugin-env",
            plugin_python,
        ],
    )
    assert result.exit_code == 4
    assert "model-backed evaluators" in result.output
    assert CalibrationModelJudge.calls == 0


def test_offline_calibration_passes_the_requested_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: dict[str, Any] = {}

    def fake_calibrate(
        set_dir: Path,
        registry: EvaluatorRegistry | None = None,
        *,
        repetitions: int = 2,
        timeout_seconds: float = 600.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        del set_dir, registry, repetitions, kwargs
        captured["timeout_seconds"] = timeout_seconds
        return {"timeout_seconds": timeout_seconds}

    monkeypatch.setattr(score_cli, "run_calibration", fake_calibrate)
    result = CliRunner().invoke(
        app,
        [
            "evaluators",
            "calibrate",
            "--set",
            str(tmp_path / "unused"),
            "--timeout",
            "7",
            "--json",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["timeout_seconds"] == 7
    payload = json.loads(result.output)
    assert payload["timeout_seconds"] == 7
    assert payload["_cli"]["exit_code"] == 0


def test_model_calibration_missing_required_evidence_never_calls_the_judge(tmp_path: Path) -> None:
    (tmp_path / "set.json").write_text(
        json.dumps(
            {
                "schema": "aibench.judge-calibration/1",
                "version": "missing-evidence-v1",
                "review": {"status": "reviewed", "reviewer": "test reviewer"},
                "not_measured": [
                    {
                        "evaluator": "test.calibration_model@1",
                        "reason": "no evidence-ready cases",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "cases.jsonl").write_text(
        json.dumps(
            {
                "id": "no-input",
                "category": "missing_evidence",
                "metric": "test.calibration_model",
                "output": "answer",
                "label": "acceptable",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = EvaluatorRegistry.with_native()
    registry.register(CalibrationModelJudge)
    CalibrationModelJudge.calls = 0
    report = run_calibration(
        tmp_path, registry, repetitions=2, allow_models=True, max_model_evaluations=2
    )
    assert CalibrationModelJudge.calls == 0
    assert report["usage"]["model_evaluations"] == 0
    assert report["usage"]["model_evaluations_planned"] == 2
    assert report["cases"][0]["repetitions"][0]["reason"] == "missing:case.input"
    assert report["not_measured"] == [
        {
            "evaluator": "test.calibration_model@1",
            "reason": "no evidence-ready cases",
        }
    ]


def test_calibration_loader_rejects_duplicate_and_oversized_sets(tmp_path: Path) -> None:
    (tmp_path / "set.json").write_text(
        json.dumps({"schema": "aibench.judge-calibration/1", "version": "invalid-v1"}),
        encoding="utf-8",
    )
    case = {
        "id": "same",
        "category": "correct",
        "metric": "native.exact_match",
        "reference": "yes",
        "output": "yes",
        "label": "acceptable",
    }
    path = tmp_path / "cases.jsonl"
    path.write_text("\n".join(json.dumps(case) for _ in range(2)) + "\n", encoding="utf-8")
    with pytest.raises(CalibrationError, match="duplicate case id"):
        load_calibration_set(tmp_path)

    path.write_text(
        "\n".join(json.dumps({**case, "id": f"case-{number}"}) for number in range(1_001)) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(CalibrationError, match="1000-case limit"):
        load_calibration_set(tmp_path)


class FailsFirstCalibration(Evaluator):
    calls = 0
    closes = 0
    manifest = EvaluatorManifest(
        evaluator_id="test.calibration_fails_first",
        version="1.0.0",
        plugin_id="test",
        plugin_version="0",
        description="fails one calibration repetition and then recovers",
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        default_rule=DecisionRule(comparator="is_true"),
    )

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        del view, ctx
        type(self).calls += 1
        if type(self).calls == 1:
            raise RuntimeError("fixture failure")
        return EvaluationOutcome.ok("boolean", True)

    async def close(self) -> None:
        type(self).closes += 1


def test_calibration_keeps_later_repeats_after_an_evaluator_failure(tmp_path: Path) -> None:
    (tmp_path / "set.json").write_text(
        json.dumps({"schema": "aibench.judge-calibration/1", "version": "failure-v1"}),
        encoding="utf-8",
    )
    (tmp_path / "cases.jsonl").write_text(
        json.dumps(
            {
                "id": "unstable-case",
                "category": "correct",
                "metric": "test.calibration_fails_first",
                "label": "acceptable",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    registry = EvaluatorRegistry.with_native()
    registry.register(FailsFirstCalibration)
    FailsFirstCalibration.calls = 0
    FailsFirstCalibration.closes = 0

    report = run_calibration(tmp_path, registry, repetitions=2)
    measured = report["bindings"][report["cases"][0]["binding"]]["overall"]
    assert [item["decision"] for item in report["cases"][0]["repetitions"]] == [
        "not_evaluated",
        "pass",
    ]
    assert report["cases"][0]["repetitions"][0]["error_type"] == "RuntimeError"
    assert measured["undecided"] == 1
    assert measured["repeat_stability"] == {"value": 0.0, "numerator": 0, "denominator": 1}
    assert FailsFirstCalibration.closes == 2


# --------------------------------------------------------------------------- invariants

ALPHABET = string.ascii_letters + string.digits + ' \n\t.,!?{}[]":'


class Erratic(Evaluator):
    """Misbehaves on purpose: raises, returns an error, the wrong value kind, a
    non-boolean, or a boolean. The scorer must turn every misbehaviour into a recorded
    error, never a score."""

    manifest = EvaluatorManifest(
        evaluator_id="test.erratic",
        version="1.0.0",
        plugin_id="test",
        plugin_version="0",
        description="an erratic evaluator for invariant checks",
        value_kind="boolean",
        direction=MetricDirection.HIGHER,
        aggregation="rate",
        requires=(
            FieldRequirement(path="execution.output", non_empty=False),
            FieldRequirement(path="case.reference.answer"),
        ),
        default_rule=DecisionRule(comparator="is_true"),
    )
    rng = random.Random(7)

    async def evaluate(self, view: EvaluationView, ctx: EvaluatorContext) -> EvaluationOutcome:
        choice = self.rng.randrange(6)
        if choice == 0:
            raise RuntimeError("evaluator bug")
        if choice == 1:
            return EvaluationOutcome.error("judge unavailable")
        if choice == 2:
            return EvaluationOutcome.ok("scalar", 0.7)  # not the declared kind
        if choice == 3:
            return EvaluationOutcome.ok("boolean", "yes")  # not a boolean
        return EvaluationOutcome.ok("boolean", choice == 4)


def _random_output(rng: random.Random, reference: str | None) -> Any:
    kind = rng.randrange(6)
    if kind == 0 and reference:
        return reference  # a correct answer, so passes happen
    if kind == 1:
        return None
    if kind == 2:
        return {"answer": "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(12)))}
    if kind == 3:
        return rng.randrange(-5, 50)
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randrange(40)))


def test_scoring_invariants_hold_for_generated_cases(tmp_path: Path) -> None:
    """Through the real scorer (`BindingScorer`), over 600 generated cases: the Golden is
    never mutated, anything but `ok` carries no score and is not evaluated, and an `ok`
    decision follows the frozen rule. Every branch must actually occur."""
    registry = EvaluatorRegistry.with_native()
    registry.register(Erratic)
    workspace = Workspace.at(tmp_path)
    workspace.ensure_directories()
    storage = Storage(Database.open_workspace(workspace))
    artifacts = ArtifactStore(workspace.artifacts_dir)
    # raw evaluator outputs are artifacts of a run, so the run must exist
    storage.commit_run(
        RunManifest(run_id="prop", dataset_hash="d", application_hash="a", plan_hash="p")
    )
    rng = random.Random(20260924)
    seen: Counter[str] = Counter()

    async def run_all() -> None:
        scorers = []
        for binding in (
            MetricBinding(metric="test.erratic"),
            MetricBinding(metric="native.exact_match"),
            MetricBinding(
                metric="native.exact_match",
                params={"case_sensitive": False, "collapse_whitespace": True},
            ),
        ):
            scorer = BindingScorer(
                storage, artifacts, "prop", registry.resolve_binding(binding), 5.0, None
            )
            await scorer.open()
            scorers.append(scorer)
        for number in range(600):
            scorer = scorers[number % len(scorers)]
            reference = rng.choice([None, "", "".join(rng.choice(ALPHABET) for _ in range(20))])
            case = BenchmarkCase(
                case_id=f"p{number}",
                input={"question": "q", "nested": {"k": [1, 2]}},
                reference=None if reference is None else ReferenceAnswer(answer=reference),
            )
            before = case.model_dump_json()
            execution = ExecutionResult(
                execution_id=f"e{number}",
                run_id="prop",
                case_id=case.case_id,
                status=rng.choice([ExecutionStatus.OK] * 5 + [ExecutionStatus.ERROR]),
                output=_random_output(rng, reference),
            )
            result = await scorer._score_one(scorer._evaluator, execution, [case])
            assert case.model_dump_json() == before  # the Golden is never mutated
            if result.status is ExecutionStatus.OK:
                assert result.value is not None and isinstance(result.value.value, bool)
                expected = Decision.PASS if result.value.value else Decision.FAIL
                assert result.decision is expected, result  # from the frozen rule
                seen[expected.value] += 1
            else:
                assert result.value is None, result  # never a score
                assert result.decision is Decision.NOT_EVALUATED, result
                seen[result.status.value] += 1
        for scorer in scorers:
            await scorer.close([])

    try:
        asyncio.run(run_all())
    finally:
        storage.db.close()
    # every branch was exercised, so the assertions above were not vacuous
    for branch in ("pass", "fail", "error", "not_applicable", "skipped"):
        assert seen[branch] > 0, seen
