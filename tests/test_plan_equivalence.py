"""07-G2: equivalent manual and generated plans produce identical deterministic
measurements (§17: "A hand-authored plan must produce the same deterministic metrics as an
LLM-authored plan with identical content").

Three plans with the same content — written by hand, by the template planner, and by the
model loop (fake provider) — each run end to end through the real engine against a real
subprocess application; per-case metric values and decisions must match exactly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aibench.core.plans import ExecutablePlan
from aibench.engine.engine import RunState
from aibench.planning.planner import plan_with_model, plan_with_template
from aibench.planning.service import write_draft
from tests.engine_support import Harness
from tests.planning_support import FakeProvider, call, draft, metric, objective, planning_inputs


def _measurements(h: Harness, plan_file: Path) -> list[tuple[Any, ...]]:
    run_id = h.create(plan_file)
    assert h.execute(run_id).state is RunState.COMPLETED
    storage, _ = h.storage()
    try:
        results = storage.list_metric_results(run_id, scoring_id=f"engine-{run_id}")
    finally:
        storage.db.close()
    return sorted(
        (
            r.case_id,
            r.metric_id,
            r.metric_version,
            json.dumps(r.model_dump(mode="json")["value"], sort_keys=True),
            r.decision.value,
            r.status.value,
        )
        for r in results
    )


def _content(plan_file: Path) -> dict[str, Any]:
    data = ExecutablePlan.model_validate_json(plan_file.read_text(encoding="utf-8")).model_dump(
        mode="json"
    )
    data.pop("plan_id")
    return data


def test_manual_template_and_model_plans_measure_identically(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    dataset = tmp_path / h.dataset({"a": "hi", "b": "hi", "c": "crash"})  # expected "yes"
    app = tmp_path / h.cli_app()
    inputs = planning_inputs(tmp_path, app, dataset, ["catch wrong answers"])

    template_file = tmp_path / "template.json"
    write_draft(plan_with_template(inputs), inputs, template_file)

    model_draft = draft(
        [metric("native.exact_match@1.0.0", "o1")],
        objectives=[objective("o1", "catch wrong answers", "correctness")],
    )
    model = plan_with_model(inputs, FakeProvider([call("write_plan_draft", model_draft)]))
    assert model.provenance.fallback_reason is None
    model_file = tmp_path / "model.json"
    write_draft(model, inputs, model_file)

    manual_file = tmp_path / "manual.json"
    manual_file.write_text(
        json.dumps(
            {
                "plan_id": "hand-written",
                "dataset": "data.jsonl",
                "application": "app.json",
                "metrics": [{"metric": "native.exact_match@1.0.0"}],
            }
        ),
        encoding="utf-8",
    )

    assert _content(template_file) == _content(model_file) == _content(manual_file)
    manual = _measurements(h, manual_file)
    assert manual == _measurements(h, template_file) == _measurements(h, model_file)
    decisions = {row[0]: row[4] for row in manual}
    assert decisions["a"] == decisions["b"] == "pass"
    assert decisions["c"] == "not_evaluated"  # the crashing case: an app failure, not a score
