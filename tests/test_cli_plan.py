"""`aibench plan` and `aibench plan validate` through the Typer app (07-T4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from aibench.cli.main import app
from tests.planning_support import write_app, write_dataset
from tests.test_openai_provider import chat_server, completion, tool_call

cli = CliRunner()
ROWS = [
    {"case_id": "a", "input": "q1", "expected_output": "x"},
    {"case_id": "b", "input": "q2", "expected_output": "y"},
    {"case_id": "c", "input": "q3"},
]


def _setup(tmp_path: Path, **app_kwargs: Any) -> tuple[Path, Path]:
    return write_app(tmp_path, **app_kwargs), write_dataset(tmp_path, ROWS)


def _plan(tmp_path: Path, *args: str) -> Any:
    app_file, dataset = tmp_path / "app.json", tmp_path / "data.jsonl"
    return cli.invoke(
        app,
        [
            "plan",
            "--app",
            str(app_file),
            "--dataset",
            str(dataset),
            "--out",
            str(tmp_path / "plan.json"),
            *args,
        ],
    )


def test_plan_writes_an_executable_plan_and_its_draft_document(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = _plan(
        tmp_path, "--objective", "catch wrong answers", "--objective", "keep latency low", "--json"
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["executable"] is True and document["revision"] == 1
    assert [m["metric"] for m in document["rationale"]] == ["native.exact_match@1.0.0"]
    assert document["coverage"][0]["eligible_cases"] == 2
    assert document["estimate"]["executions"] == 3
    assert document["estimate"]["estimated_cost_usd"] is None  # unknown, never $0
    assert "no hidden labels were used" in document["scope"]
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan["dataset"] == "data.jsonl" and plan["application"] == "app.json"
    validated = cli.invoke(app, ["plan", "validate", str(tmp_path / "plan.json"), "--json"])
    assert validated.exit_code == 0, validated.output
    summary = json.loads(validated.output)
    assert summary["valid"] is True
    expected = (
        "native.exact_match@1.0.0: requires case.reference.answer, present in 2 of 3 selected "
        "case(s); the rest will be not_applicable"
    )
    assert [f["message"] for f in summary["findings"]] == [expected]


def test_missing_objectives_exit_2_with_a_pending_question(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = _plan(tmp_path, "--json")
    assert result.exit_code == 2
    document = json.loads(result.stdout)
    [question] = document["pending_questions"]
    assert question["prompt"] == "What should this benchmark check?"
    assert question["draft_revision"] == 1 and "correctness" in question["choices"]
    assert any(f["kind"] == "missing_information" for f in document["findings"])


def test_seed_without_sample_is_rejected_instead_of_silently_ignored(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = _plan(tmp_path, "--objective", "wrong answers", "--seed", "7")
    assert result.exit_code == 2 and "--seed requires --sample" in result.output
    assert not (tmp_path / "plan.json").exists()


def test_plan_inherits_policy_cost_ceiling_estimates(tmp_path: Path) -> None:
    _setup(tmp_path)
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "ceilings": {
                    "max_cost_usd": 5.0,
                    "estimated_cost_per_application_call_usd": 0.25,
                    "estimated_cost_per_evaluation_usd": 0.05,
                }
            }
        ),
        encoding="utf-8",
    )

    result = _plan(tmp_path, "--objective", "wrong answers", "--policy", str(policy), "--json")

    assert result.exit_code == 0, result.output
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan["budgets"]["max_cost_usd"] == 5.0
    assert plan["budgets"]["estimated_cost_per_application_call_usd"] == 0.25
    assert plan["budgets"]["estimated_cost_per_evaluation_usd"] == 0.05


def test_missing_permission_exits_4_and_is_distinguished(tmp_path: Path) -> None:
    _setup(tmp_path, runner="cli")
    result = _plan(tmp_path, "--objective", "wrong answers", "--json")
    assert result.exit_code == 4
    kinds = {f["kind"] for f in json.loads(result.stdout)["findings"] if f["blocking"]}
    assert kinds == {"missing_permission"}
    assert (
        _plan(tmp_path, "--objective", "wrong answers", "--trust-local-app", "--revise").exit_code
        == 0
    )


def test_revisions_never_silently_overwrite_a_different_plan(tmp_path: Path) -> None:
    _setup(tmp_path)
    assert _plan(tmp_path, "--objective", "wrong answers").exit_code == 0
    first = json.loads((tmp_path / "plan.draft.json").read_text(encoding="utf-8"))
    assert _plan(tmp_path, "--objective", "wrong answers").exit_code == 0  # identical: fine
    refused = _plan(tmp_path, "--objective", "wrong answers", "--sample", "2", "--seed", "3")
    assert refused.exit_code == 2 and "pass --revise" in refused.output
    revised = _plan(
        tmp_path,
        "--objective",
        "wrong answers",
        "--sample",
        "2",
        "--seed",
        "3",
        "--revise",
        "--json",
    )
    assert revised.exit_code == 0, revised.output
    document = json.loads(revised.stdout)
    assert document["revision"] == 2 and document["supersedes"] == first["plan_hash"]
    assert (tmp_path / "plan.rev1.json").is_file()
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert plan["selection"]["sample_size"] == 2 and plan["selection"]["seed"] == 3


def test_model_planner_through_the_cli_and_a_denied_endpoint(tmp_path: Path) -> None:
    _setup(tmp_path)
    proposal = {
        "objectives": [
            {"objective_id": "o1", "text": "wrong answers", "concepts": ["correctness"]}
        ],
        "metrics": [
            {
                "metric": "native.exact_match@1.0.0",
                "objective_ids": ["o1"],
                "rationale": "references",
            }
        ],
    }
    with chat_server([(200, completion([tool_call("write_plan_draft", proposal)]))]) as server:
        host, port = server.server_address[:2]
        config = tmp_path / "provider.json"
        config.write_text(
            json.dumps({"base_url": f"http://{host}:{port}/v1", "model": "m"}), encoding="utf-8"
        )
        result = _plan(
            tmp_path,
            "--objective",
            "wrong answers",
            "--planner",
            "model",
            "--provider-config",
            str(config),
            "--json",
        )
    assert result.exit_code == 0, result.output
    planner = json.loads(result.stdout)["planner"]
    assert (planner["kind"], planner["model_calls"], planner["fallback_reason"]) == (
        "model",
        1,
        None,
    )

    remote = tmp_path / "remote.json"
    remote.write_text(
        json.dumps({"base_url": "https://llm.example.com/v1", "model": "m"}), encoding="utf-8"
    )
    denied = _plan(
        tmp_path,
        "--objective",
        "wrong answers",
        "--planner",
        "model",
        "--provider-config",
        str(remote),
        "--revise",
    )
    assert denied.exit_code == 4
    assert "the model planner was not contacted" in denied.output


def test_plan_validate_reports_invalid_and_denied_plans(tmp_path: Path) -> None:
    _setup(tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            {
                "plan_id": "p",
                "dataset": "data.jsonl",
                "application": "app.json",
                "metrics": [{"metric": "made.up"}],
            }
        ),
        encoding="utf-8",
    )
    result = cli.invoke(app, ["plan", "validate", str(bad), "--json"])
    assert result.exit_code == 2
    [finding] = json.loads(result.output)["findings"]
    assert finding["kind"] == "invalid" and "unknown evaluator 'made.up'" in finding["message"]


def test_real_deepeval_manifest_needs_a_judge_the_planner_will_not_invent(tmp_path: Path) -> None:
    """Real-package check (skipped without plugins/deepeval/.venv): the pinned DeepEval
    faithfulness manifest is eligible for a RAG app that declares retrieval, but its judge
    is required input — the template asks for it instead of inventing one."""
    import pytest

    from tests.test_deepeval_adapter import PLUGIN_ENV

    if not PLUGIN_ENV.is_file():
        pytest.skip(f"DeepEval plugin environment not installed at {PLUGIN_ENV}")
    write_app(tmp_path, output_binding={"output": "/answer", "retrieved_context": "/ctx"})
    write_dataset(tmp_path, ROWS)
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps(
            {
                "allowed_plugin_environments": [str(PLUGIN_ENV)],
                "allowed_evaluators": ["native.*", "deepeval.*"],
                "allow_model_evaluators": True,
            }
        ),
        encoding="utf-8",
    )
    result = _plan(
        tmp_path,
        "--objective",
        "unsupported claims",
        "--plugin-env",
        str(PLUGIN_ENV),
        "--policy",
        str(policy),
        "--json",
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["rationale"] == []
    [question] = document["pending_questions"]
    assert "deepeval.faithfulness@1.0.0 needs judge" in question["prompt"]
    assert question["required_fields"] == ["params.deepeval.faithfulness.judge"]
