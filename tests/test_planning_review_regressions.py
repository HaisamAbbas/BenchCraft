"""Regression tests for the Prompt 07 independent review (see reports/07.md §4).

Each finding was reproduced first against the pre-fix code; these tests assert the fixed
behavior. Planner-side tests use the scripted fake provider (harness behavior, not model
quality)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import (
    DecisionRule,
    ExecutionResult,
    ExecutionStatus,
    ObservationState,
)
from aibench.core.plans import BudgetLimits, CaseSelection, ExecutablePlan
from aibench.engine.compile import analyze_plan, sample_cases
from aibench.inspection.profile import inspect_application
from aibench.planning.benchmark import PlannerFixture, assess
from aibench.planning.drafts import (
    DraftProposal,
    Gap,
    MetricChoice,
    Objective,
    QuestionProposal,
)
from aibench.planning.drafts import pending_questions as questions_for
from aibench.planning.openai_provider import (
    OpenAICompatibleConfig,
    parse_reply,
    provider_denials,
)
from aibench.planning.planner import ModelReply, PlannerError, plan_with_model, plan_with_template
from aibench.security.policy import ExecutionPolicy
from tests.planning_support import (
    GROUNDED,
    TRUSTED,
    FakeProvider,
    call,
    draft,
    metric,
    objective,
    planning_inputs,
    registry_with,
    write_app,
    write_dataset,
)

ROWS = [
    {"case_id": "a", "input": "q1", "expected_output": "Paris"},
    {"case_id": "b", "input": "q2", "expected_output": "Rome"},
]
JUDGES = ExecutionPolicy(
    allow_trusted_local=True,
    allowed_evaluators=("native.*", "fixture.*"),
    allow_model_evaluators=True,
)
CORRECT = objective("o1", "catch wrong answers", "correctness")


def _inputs(tmp_path: Path, objectives: list[str], **kwargs: Any) -> Any:
    app = write_app(tmp_path, **kwargs.pop("app", {}))
    dataset = write_dataset(tmp_path, kwargs.pop("rows", ROWS))
    return planning_inputs(tmp_path, app, dataset, objectives, **kwargs)


def _user(o: dict[str, Any]) -> dict[str, Any]:
    return {**o, "source": "user"}


# --------------------------------------------------------------------------- major


def test_the_model_cannot_probe_label_values_through_selection(tmp_path: Path) -> None:
    """Finding 1: `where` predicates on case.reference.answer + estimate_cost counts leaked
    reference answers. Model-authored selection no longer exists."""
    assert "selection" not in DraftProposal.model_fields
    probe = {
        **draft(objectives=[_user(CORRECT)]),
        "selection": {
            "where": [{"path": "case.reference.answer", "op": "equals", "value": "Paris"}]
        },
    }
    fake = FakeProvider(
        [
            call("estimate_cost", probe),
            call("validate_plan", probe, "c2"),
            call("write_plan_draft", probe, "c3"),
        ]
    )
    plan_with_model(_inputs(tmp_path, ["catch wrong answers"]), fake)
    for reply in (fake.requests[1][-1], fake.requests[2][-1]):
        body = json.loads(reply["content"])
        assert "schema_errors" in body and "selected_cases" not in body


def test_stated_objectives_cannot_be_dropped_reworded_or_half_served(tmp_path: Path) -> None:
    """Finding 2."""
    stated = ["catch wrong answers", "catch hallucinated unsupported claims"]
    inputs = _inputs(tmp_path, stated, registry=registry_with(GROUNDED), policy=JUDGES)
    dropped = draft([metric("native.exact_match@1.0.0", "o1")], objectives=[_user(CORRECT)])
    fake = FakeProvider([call("write_plan_draft", dropped)] * 3)
    outcome = plan_with_model(inputs, fake)
    feedback = json.loads(fake.requests[1][-1]["content"])["blocking_findings"]
    assert any(
        "'catch hallucinated unsupported claims' is missing from the draft" in f for f in feedback
    )
    assert outcome.provenance.fallback_reason == "repair limit reached (2)"

    # Relabelled as engine-recorded latency: visible as a mapping warning, not silent.
    relabel = DraftProposal(
        objectives=(
            Objective(objective_id="o1", text="catch hallucinations", concepts=("latency",)),
        )
    )
    from aibench.planning.drafts import validate_draft

    other = tmp_path / "other"
    other.mkdir()
    inputs2 = _inputs(other, ["catch hallucinations"])
    findings = validate_draft(relabel, inputs2.context).findings
    assert any("its wording suggests ['groundedness']" in f.message for f in findings)
    # ...and blocking: the concept its wording names is neither measured nor gapped.
    assert any(f.blocking and "nothing measures groundedness" in f.message for f in findings)
    explained = relabel.model_copy(
        update={"gaps": (Gap(subject="groundedness", reason="no retrieval is exposed"),)}
    )
    assert not any(f.blocking for f in validate_draft(explained, inputs2.context).findings)

    # One objective, two concepts, a metric for only one of them: blocking.
    both = DraftProposal(
        objectives=(
            Objective(
                objective_id="o1",
                text="catch hallucinations",
                concepts=("groundedness", "correctness"),
            ),
        ),
        metrics=(
            MetricChoice(metric="native.exact_match@1.0.0", objective_ids=("o1",), rationale="r"),
        ),
    )
    findings = validate_draft(both, inputs2.context).findings
    assert any(f.blocking and "nothing measures groundedness" in f.message for f in findings)


def test_planner_invented_parameters_and_thresholds_block_until_the_user_supplies_them(
    tmp_path: Path,
) -> None:
    """Finding 3."""
    from aibench.planning.drafts import validate_draft

    inputs = _inputs(tmp_path, ["valid JSON format"])
    fmt = Objective(objective_id="o1", text="valid JSON format", concepts=("format",))
    invented = DraftProposal(
        objectives=(fmt,),
        metrics=(
            MetricChoice(
                metric="native.json_schema@1.0.0",
                params={"schema": {"type": "object"}},
                rule=DecisionRule(comparator="is_true"),
                objective_ids=("o1",),
                rationale="r",
            ),
        ),
    )
    messages = [f.message for f in validate_draft(invented, inputs.context).findings if f.blocking]
    assert any("parameters were set by the planner" in m for m in messages)
    assert any("pass/fail rule was set by the planner" in m for m in messages)

    inputs.context.user_params = {"native.json_schema": {"schema": {"type": "object"}}}
    inputs.context.user_rules = {"native.json_schema": DecisionRule(comparator="is_true")}
    assert validate_draft(invented, inputs.context).executable
    template = plan_with_template(inputs)  # the template uses what the user supplied
    assert template.validation.executable
    assert template.proposal.metrics[0].params == {"schema": {"type": "object"}}


def test_planner_cannot_silently_omit_user_supplied_metric_settings(tmp_path: Path) -> None:
    """Explicit CLI settings must not be replaced by evaluator defaults."""
    from aibench.planning.drafts import validate_draft

    inputs = _inputs(tmp_path, ["catch wrong answers"])
    supplied_rule = DecisionRule(comparator="is_true", rule_id="user-approved")
    inputs.context.user_params = {"native.exact_match": {"strip": False}}
    inputs.context.user_rules = {"native.exact_match": supplied_rule}
    proposal = DraftProposal(
        objectives=(
            Objective(
                objective_id="o1",
                text="catch wrong answers",
                concepts=("correctness",),
            ),
        ),
        metrics=(
            MetricChoice(
                metric="native.exact_match@1.0.0",
                objective_ids=("o1",),
                rationale="measures correctness",
            ),
        ),
    )

    findings = [f for f in validate_draft(proposal, inputs.context).findings if f.blocking]
    assert any("omitted or changed the user-supplied parameters" in f.message for f in findings)
    assert any("omitted or changed the user-supplied pass/fail rule" in f.message for f in findings)


def test_policy_cost_ceiling_carries_its_required_estimate_into_the_plan() -> None:
    from aibench.planning.service import effective_budgets

    policy = ExecutionPolicy(
        ceilings=BudgetLimits(
            max_cost_usd=5.0,
            estimated_cost_per_application_call_usd=0.25,
            estimated_cost_per_evaluation_usd=0.05,
        )
    )

    effective = effective_budgets(policy, BudgetLimits())

    assert effective.max_cost_usd == 5.0
    assert effective.estimated_cost_per_application_call_usd == 0.25
    assert effective.estimated_cost_per_evaluation_usd == 0.05


@pytest.mark.parametrize(
    "body",
    [
        {"choices": [{"message": "hi"}]},
        {"choices": [{"message": {"tool_calls": [{"function": "x"}]}}]},
        {"choices": [{"message": {"tool_calls": "x"}}]},
        {"choices": [{"message": {"content": "hi"}}], "usage": [1]},
        [],
    ],
)
def test_malformed_provider_responses_are_planner_errors(body: Any) -> None:
    """Finding 4 (parsing)."""
    text = json.dumps(body)
    if isinstance(body, dict) and body.get("usage") == [1]:
        assert parse_reply(text).prompt_tokens is None  # tolerated, usage unknown
        return
    with pytest.raises(PlannerError):
        parse_reply(text)


def test_any_provider_exception_falls_back_instead_of_crashing(tmp_path: Path) -> None:
    """Finding 4 (loop)."""
    outcome = plan_with_model(
        _inputs(tmp_path, ["catch wrong answers"]), FakeProvider([AttributeError("boom")])
    )
    assert outcome.provenance.fallback_reason == "provider failed: AttributeError: boom"
    assert outcome.validation.executable


# --------------------------------------------------------------------------- minor


def test_revision_numbering_survives_a_lost_draft_document_and_never_overwrites_history(
    tmp_path: Path,
) -> None:
    """Finding 5."""
    from typer.testing import CliRunner

    from aibench.cli.main import app

    write_app(tmp_path)
    write_dataset(tmp_path, ROWS)
    base = ["plan", "--app", str(tmp_path / "app.json"), "--dataset", str(tmp_path / "data.jsonl")]
    base += ["--out", str(tmp_path / "plan.json"), "--objective", "catch wrong answers"]
    runner = CliRunner()
    assert runner.invoke(app, base).exit_code == 0
    assert runner.invoke(app, [*base, "--limit", "1", "--revise"]).exit_code == 0  # rev 2
    (tmp_path / "plan.draft.json").write_text("corrupt", encoding="utf-8")
    third = runner.invoke(app, [*base, "--sample", "1", "--seed", "4", "--revise", "--json"])
    assert third.exit_code == 0, third.output
    assert json.loads(third.output)["revision"] == 3
    assert sorted(p.name for p in tmp_path.glob("plan.rev*.json")) == [
        "plan.rev1.json",
        "plan.rev2.json",
    ]
    rev1 = json.loads((tmp_path / "plan.rev1.json").read_text(encoding="utf-8"))
    assert rev1["selection"]["limit"] is None  # the original, not overwritten


def test_duplicate_questions_get_one_id() -> None:
    """Finding 6."""
    q = QuestionProposal(prompt="Provide the schema", required_fields=("params.x.schema",))
    other_scope = QuestionProposal(
        prompt="Provide the schema",
        required_fields=("params.x.schema",),
        blocking_scope="objective:o2",
    )
    proposal = DraftProposal(questions=(q, q, other_scope))
    ids = [p.question_id for p in questions_for(proposal, 1)]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_the_benchmark_counts_unjustified_and_uncatalogued_selections(tmp_path: Path) -> None:
    """Finding 7."""
    inputs = _inputs(
        tmp_path, ["catch wrong answers"], registry=registry_with(GROUNDED), policy=JUDGES
    )
    fixture = PlannerFixture(
        "f", "chatbot", ("catch wrong answers",), frozenset({"correctness"}), frozenset()
    )
    proposal = DraftProposal(
        objectives=(
            Objective(objective_id="o1", text="catch wrong answers", concepts=("correctness",)),
        ),
        metrics=(
            MetricChoice(metric="native.exact_match@1.0.0", objective_ids=("o1",), rationale="r"),
            MetricChoice(metric="fixture.grounded@1.0.0", objective_ids=("o1",), rationale="r"),
            MetricChoice(metric="made.up", objective_ids=("o1",), rationale="r"),
        ),
    )
    result = assess(fixture, proposal, inputs.catalog, executable=True)
    assert result.unnecessary == 1 and "groundedness" in result.selected
    assert result.forbidden_selected == {"made.up"}


@pytest.mark.parametrize(
    ("text", "concepts"),
    [
        ("don't care about latency", ()),
        ("answers cite the reference passages", ("groundedness",)),
        ("no unsupported languages", ()),
        ("toolbar labels", ()),
        ("well-structured prose", ()),
        ("keep latency low and catch wrong answers", ("correctness", "latency")),
    ],
)
def test_keyword_matching_respects_negation_and_word_boundaries(
    text: str, concepts: tuple[str, ...]
) -> None:
    """Finding 8."""
    from aibench.planning.catalog import concepts_in

    assert concepts_in(text) == concepts


def test_plugin_load_problems_make_the_draft_not_executable(tmp_path: Path) -> None:
    """Finding 9: the draft and the execution gate agree."""
    inputs = _inputs(tmp_path, ["catch wrong answers"])
    inputs.context.plugin_problems = ("plugin broken_plugin: import failed",)
    outcome = plan_with_template(inputs)
    assert not outcome.validation.executable
    assert any(f.subject == "plugins" for f in outcome.validation.findings)


def test_a_typo_is_invalid_even_when_unrelated_permissions_are_missing(tmp_path: Path) -> None:
    """Finding 10."""
    write_app(tmp_path, runner="cli")
    write_dataset(tmp_path, ROWS)
    plan = ExecutablePlan.model_validate(
        {
            "plan_id": "p",
            "dataset": "data.jsonl",
            "application": "app.json",
            "metrics": [{"metric": "native.exact_matc"}],
            "plugin_environments": [{"python": "somewhere/python"}],
        }
    )
    analysis = analyze_plan(plan, tmp_path, policy=ExecutionPolicy())
    [typo] = [f for f in analysis.findings if "exact_matc" in f.message]
    assert typo.kind == "invalid"


def test_no_briefing_leaves_when_the_plan_needs_permissions(tmp_path: Path) -> None:
    """Finding 11: a CLI app without trusted-local mode — the model is never contacted."""
    inputs = _inputs(
        tmp_path, ["catch wrong answers"], app={"runner": "cli"}, policy=ExecutionPolicy()
    )
    fake = FakeProvider([call("write_plan_draft", draft())])
    outcome = plan_with_model(inputs, fake)
    assert fake.requests == []
    assert outcome.provenance.fallback_reason == (
        "model not contacted: the plan needs permissions the policy does not grant"
    )


def test_planner_endpoint_needs_https_off_loopback_and_a_valid_url() -> None:
    """Finding 12."""
    policy = ExecutionPolicy(allowed_planner_origins=("http://llm.internal:8000",))
    plain = OpenAICompatibleConfig(base_url="http://llm.internal:8000/v1", model="m")
    assert any("uses plain http" in d for d in provider_denials(plain, policy))
    broken = OpenAICompatibleConfig(base_url="http://[::1/v1", model="m")
    assert "is not a valid URL" in provider_denials(broken, ExecutionPolicy())[0]
    # Application origins do not authorize planner egress.
    app_only = ExecutionPolicy(allowed_http_origins=("https://llm.example.com",))
    remote = OpenAICompatibleConfig(base_url="https://llm.example.com/v1", model="m")
    assert any("allowed_planner_origins" in d for d in provider_denials(remote, app_only))


def test_replies_without_usage_are_counted(tmp_path: Path) -> None:
    """Finding 13: the token cap cannot be enforced without usage; say so."""
    reply = call(
        "write_plan_draft",
        draft([metric("native.exact_match@1.0.0", "o1")], objectives=[_user(CORRECT)]),
    )
    silent = ModelReply(text=None, tool_calls=reply.tool_calls)
    outcome = plan_with_model(_inputs(tmp_path, ["catch wrong answers"]), FakeProvider([silent]))
    assert outcome.provenance.calls_without_usage == 1
    assert outcome.provenance.prompt_tokens is None


def test_inspect_ignores_failed_attempts_as_evidence(tmp_path: Path) -> None:
    """Finding 14."""
    app = write_app(tmp_path, output_binding={"output": "/answer", "retrieved_context": "/ctx"})
    failed = ExecutionResult(
        execution_id="r:a:r0:a1",
        run_id="r",
        case_id="a",
        status=ExecutionStatus.ERROR,
        observation_completeness={"retrieved_context": {"state": "unknown", "detail": "missing"}},
    )
    claim = inspect_application(app, executions=[failed]).claim("retrieved_context")
    assert claim is not None and claim.state is ObservationState.DECLARED
    assert claim.limitations is None  # no contradiction drawn from a failed call


def test_a_denied_provider_still_writes_a_template_draft(tmp_path: Path) -> None:
    """Finding 15."""
    from typer.testing import CliRunner

    from aibench.cli.main import app

    write_app(tmp_path)
    write_dataset(tmp_path, ROWS)
    config = tmp_path / "remote.json"
    config.write_text(
        json.dumps({"base_url": "https://llm.example.com/v1", "model": "m"}), encoding="utf-8"
    )
    result = CliRunner().invoke(
        app,
        [
            "plan",
            "--app",
            str(tmp_path / "app.json"),
            "--dataset",
            str(tmp_path / "data.jsonl"),
            "--out",
            str(tmp_path / "plan.json"),
            "--objective",
            "catch wrong answers",
            "--planner",
            "model",
            "--provider-config",
            str(config),
        ],
    )
    assert result.exit_code == 4
    document = json.loads((tmp_path / "plan.draft.json").read_text(encoding="utf-8"))
    assert document["planner"]["fallback_reason"].startswith("model not contacted")


def test_sampling_is_pinned_across_platforms_and_versions(tmp_path: Path) -> None:
    """Finding 16: hash-based ranking, independent of `random`'s algorithm."""
    write_app(tmp_path)
    rows = [{"case_id": f"c{i}", "input": "q", "expected_output": "x"} for i in range(10)]
    write_dataset(tmp_path, rows)
    plan = ExecutablePlan(
        plan_id="p",
        dataset="data.jsonl",
        application="app.json",
        selection=CaseSelection(sample_size=3, seed=7),
    )
    picked = [c.case_id for c in analyze_plan(plan, tmp_path, policy=TRUSTED).cases]
    # Pinned: SHA-256 ranking gives the same cases on every platform and Python version.
    assert picked == ["c5", "c6", "c7"]
    everything = analyze_plan(
        plan.model_copy(update={"selection": CaseSelection()}), tmp_path, policy=TRUSTED
    ).cases
    assert [c.case_id for c in sample_cases(everything, 3, 7)] == picked
