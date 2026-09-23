"""`aibench plan` (draft a plan) and `aibench plan validate FILE` (07-T4, 06-T4).

`aibench plan --app APP --dataset DATA [--objective TEXT ...] --out plan.json` writes the
executable plan and `plan.draft.json`: objectives, per-metric rationale, explicit gaps,
pending clarification questions, classified validation findings, per-metric coverage and a
spend estimate. `--planner model --provider-config FILE` uses a model within strict bounds
and falls back to the deterministic template on any failure — or without contacting the
model at all when the policy does not permit the provider. Metric parameters and pass/fail
rules come only from the user (`--params ID=JSON`, `--rule ID=JSON`).

Exit codes: 0 the draft is executable; 2 it needs objective information or is invalid;
4 it needs a permission the policy does not grant. A draft is written in every case, so the
missing pieces are visible and actionable (§3: "produces an actionable draft identifying
the missing fields").
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError as PydanticValidationError
from rich.console import Console
from rich.markup import escape

from aibench.cli.score import _PLUGIN_ENV_OPTION, _PLUGIN_PATH_OPTION, _PLUGIN_SECRET_OPTION
from aibench.config.resolve import load_mapping_file
from aibench.core.errors import AibenchError
from aibench.core.models import DecisionRule
from aibench.core.plans import BudgetLimits, CaseSelection, PluginEnvironmentRef
from aibench.engine.compile import (
    PlanFinding,
    PlanInvalid,
    analyze_plan,
    load_plan,
    load_policy,
)
from aibench.planning.drafts import PlanDraft, estimate_spend
from aibench.planning.planner import PlannerLimits, plan_with_model, plan_with_template
from aibench.planning.service import gather_inputs, write_draft

plan_app = typer.Typer(
    help="Draft (`aibench plan --app ... --dataset ...`) or validate executable plans.",
    invoke_without_command=True,
)
console = Console()
err_console = Console(stderr=True)

EXIT_OK, EXIT_INVALID, EXIT_DENIED = 0, 2, 4
_POLICY = typer.Option(
    None, "--policy", help="Execution policy file. Default: the conservative built-in policy."
)
_JSON = typer.Option(False, "--json", help="Machine-readable output on stdout.")


def _fail(message: str, code: int) -> typer.Exit:
    err_console.print(f"[red]{escape(message)}[/red]")
    return typer.Exit(code=code)


def _exit_for(findings: list[PlanFinding]) -> int:
    blocking = [f for f in findings if f.blocking]
    if any(f.kind == "missing_permission" for f in blocking):
        return EXIT_DENIED
    return EXIT_INVALID if blocking else EXIT_OK


def _print_findings(findings: list[PlanFinding]) -> None:
    labels = {
        "missing_permission": "[red]needs permission[/red]",
        "missing_information": "[yellow]needs information[/yellow]",
        "invalid": "[red]invalid[/red]",
    }
    for f in sorted(findings, key=lambda f: (not f.blocking, f.kind, f.subject)):
        prefix = labels[f.kind] if f.blocking else "[dim]warning[/dim]"
        err_console.print(f"  {prefix} {escape(f.subject)}: {escape(f.message)}")


# --------------------------------------------------------------------------- draft


def _selection(sample: int | None, seed: int | None, limit: int | None) -> CaseSelection | None:
    if sample is None and limit is None:
        if seed is not None:
            raise _fail("--seed requires --sample", EXIT_INVALID)
        return None
    try:
        return CaseSelection(sample_size=sample, seed=seed, limit=limit)
    except PydanticValidationError as exc:
        raise _fail(f"invalid selection: {exc.errors()[0]['msg']}", EXIT_INVALID) from exc


def _plugin_environments(
    python: Path | None, secrets: list[str], paths: list[Path]
) -> tuple[PluginEnvironmentRef, ...]:
    if python is None:
        if secrets or paths:
            raise _fail("--plugin-secret and --plugin-path require --plugin-env", EXIT_INVALID)
        return ()
    secret_env: dict[str, str] = {}
    for item in secrets:
        name, sep, ref = item.partition("=")
        if not sep or not name or ":" not in ref:
            raise _fail(
                f"--plugin-secret must look like NAME=source:name, got {item!r}", EXIT_INVALID
            )
        secret_env[name] = ref
    try:
        return (
            PluginEnvironmentRef(
                python=str(python.resolve()),
                paths=tuple(str(p.resolve()) for p in paths),
                secret_env=secret_env,
            ),
        )
    except PydanticValidationError as exc:
        raise _fail(f"invalid plugin environment: {exc.errors()[0]['msg']}", EXIT_INVALID) from exc


def _keyed_json(items: list[str], option: str) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise _fail(f"{option} must look like EVALUATOR_ID=JSON, got {item!r}", EXIT_INVALID)
        try:
            parsed[key] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _fail(f"{option} {key}: invalid JSON ({exc.msg})", EXIT_INVALID) from exc
    return parsed


def _rules(items: list[str]) -> dict[str, DecisionRule]:
    rules = {}
    for key, value in _keyed_json(items, "--rule").items():
        try:
            rules[key] = DecisionRule.model_validate(value)
        except PydanticValidationError as exc:
            raise _fail(f"--rule {key}: {exc.errors()[0]['msg']}", EXIT_INVALID) from exc
    return rules


def _provider(config_path: Path, policy_path: Path | None) -> tuple[Any, list[str]]:
    """The provider, or (None, denials) when the policy does not permit contacting it."""
    from aibench.planning.openai_provider import (
        OpenAICompatibleConfig,
        OpenAICompatibleProvider,
        provider_denials,
    )

    try:
        config = OpenAICompatibleConfig.model_validate(load_mapping_file(config_path))
    except (PydanticValidationError, AibenchError, OSError) as exc:
        raise _fail(f"invalid provider config {config_path}: {exc}", EXIT_INVALID) from exc
    denials = provider_denials(config, load_policy(policy_path))
    if denials:
        return None, denials
    try:
        return OpenAICompatibleProvider(config), []
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID) from exc


def _summary(document: PlanDraft) -> None:
    status = "executable" if document.executable else "not executable yet"
    console.print(
        f"draft revision {document.revision} of {escape(document.plan_id)}: {status} "
        f"(planner: {document.planner.kind}"
        + (
            f", fell back: {escape(document.planner.fallback_reason)}"
            if document.planner.fallback_reason
            else ""
        )
        + ")"
    )
    for choice in document.rationale:
        console.print(f"  metric {escape(choice.metric)}: {escape(choice.rationale)}")
    for gap in document.gaps:
        console.print(f"  [yellow]gap[/yellow] {escape(gap.subject)}: {escape(gap.reason)}")
    for question in document.pending_questions:
        choices = f" [{', '.join(question.choices)}]" if question.choices else ""
        console.print(f"  [cyan]question[/cyan] {escape(question.prompt)}{escape(choices)}")
    if document.estimate:
        e = document.estimate
        cost = f"${e.estimated_cost_usd}" if e.estimated_cost_usd is not None else "unknown"
        console.print(
            f"  estimate: {e.executions} execution(s) (up to {e.application_calls_upper_bound} "
            f"calls with retries), {e.evaluations} evaluation(s), {e.model_evaluations} sent "
            f"to a model judge; cost {cost} ({escape(e.cost_basis)})"
        )


@plan_app.callback()
def plan(
    ctx: typer.Context,
    app: Path | None = typer.Option(None, "--app", help="Application config file."),  # noqa: B008
    dataset: Path | None = typer.Option(None, "--dataset", help="JSONL dataset."),  # noqa: B008
    objectives: list[str] = typer.Option(  # noqa: B008
        [], "--objective", help="What the benchmark should check (repeatable)."
    ),
    out: Path = typer.Option(Path("plan.json"), "--out", help="Plan file to write."),  # noqa: B008
    planner: str = typer.Option("template", "--planner", help="template | model"),
    provider_config: Path | None = typer.Option(  # noqa: B008
        None, "--provider-config", help="Model provider config (JSON/YAML) for --planner model."
    ),
    policy: Path | None = _POLICY,
    trust_local_app: bool = typer.Option(
        False, "--trust-local-app", help="Grant trusted-local mode for a CLI application."
    ),
    sample: int | None = typer.Option(None, "--sample", min=1, help="Seeded random sample size."),
    seed: int | None = typer.Option(None, "--seed", min=0, help="Seed for --sample."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="First N cases."),
    max_app_calls: int | None = typer.Option(None, "--max-app-calls", min=1),
    max_evaluator_calls: int | None = typer.Option(None, "--max-evaluator-calls", min=1),
    plugin_env: Path | None = _PLUGIN_ENV_OPTION,
    plugin_secret: list[str] = _PLUGIN_SECRET_OPTION,
    plugin_path: list[Path] = _PLUGIN_PATH_OPTION,
    params: list[str] = typer.Option(  # noqa: B008
        [], "--params", help="Metric parameters you supply: EVALUATOR_ID=JSON (repeatable)."
    ),
    rule: list[str] = typer.Option(  # noqa: B008
        [], "--rule", help="Pass/fail rule you supply: EVALUATOR_ID=JSON (repeatable)."
    ),
    revise: bool = typer.Option(
        False, "--revise", help="Replace a different existing plan as a new revision."
    ),
    json_output: bool = _JSON,
) -> None:
    """Draft a plan from the declared app config, dataset field coverage and installed
    evaluators. Nothing is executed."""
    if ctx.invoked_subcommand is not None:
        return
    if app is None or dataset is None:
        raise _fail(
            "aibench plan needs --app and --dataset (or use `aibench plan validate FILE`)",
            EXIT_INVALID,
        )
    if planner not in ("template", "model"):
        raise _fail("--planner must be template or model", EXIT_INVALID)
    if planner == "model" and provider_config is None:
        raise _fail("--planner model needs --provider-config", EXIT_INVALID)
    try:
        gathered = gather_inputs(
            application=app,
            dataset=dataset,
            objectives=list(objectives),
            out=out,
            policy=load_policy(policy),
            trusted_local=trust_local_app,
            selection=_selection(sample, seed, limit),
            budgets=BudgetLimits(
                max_application_calls=max_app_calls, max_evaluator_calls=max_evaluator_calls
            ),
            plugin_environments=_plugin_environments(plugin_env, plugin_secret, plugin_path),
            params=_keyed_json(params, "--params"),
            rules=_rules(rule),
        )
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID) from exc
    for note in gathered.notes:
        err_console.print(f"[yellow]note:[/yellow] {escape(note)}")
    inputs = gathered.inputs
    provider_denied: list[str] = []
    if planner == "model":
        assert provider_config is not None
        provider, provider_denied = _provider(provider_config, policy)
        if provider is None:
            outcome = plan_with_template(inputs)
            outcome.provenance = outcome.provenance.model_copy(
                update={
                    "kind": "model",
                    "fallback_reason": "model not contacted: the policy does not permit the "
                    "planner provider",
                }
            )
        else:
            try:
                outcome = plan_with_model(inputs, provider, PlannerLimits())
            finally:
                provider.close()
    else:
        outcome = plan_with_template(inputs)
    try:
        document = write_draft(outcome, inputs, out, revise=revise)
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID) from exc
    findings = outcome.validation.findings
    for denial in provider_denied:
        err_console.print(f"[red]denied:[/red] {escape(denial)}")
    if json_output:
        console.print_json(data=json.loads(document.model_dump_json()))
    else:
        _summary(document)
        _print_findings(findings)
        console.print(f"wrote {escape(str(out))} and its draft document")
    if provider_denied:
        err_console.print("the model planner was not contacted; the draft uses the template")
        raise typer.Exit(code=EXIT_DENIED)
    raise typer.Exit(code=_exit_for(findings))


# --------------------------------------------------------------------------- validate


@plan_app.command("validate")
def validate_plan(
    plan_file: Path = typer.Argument(..., help="Plan file (JSON/YAML)."),  # noqa: B008
    policy: Path | None = _POLICY,
    trust_local_app: bool = typer.Option(
        False, "--trust-local-app", help="Grant trusted-local mode."
    ),
    json_output: bool = _JSON,
) -> None:
    """Structural, evidence and policy validation only; nothing is dispatched."""
    try:
        loaded = load_plan(plan_file)
        analysis = analyze_plan(
            loaded,
            plan_file.resolve().parent,
            policy=load_policy(policy),
            trusted_local=trust_local_app,
        )
    except PlanInvalid as exc:
        for problem in exc.problems:
            err_console.print(f"[red]invalid:[/red] {escape(problem)}")
        raise _fail("nothing was dispatched: the plan is invalid", EXIT_INVALID) from exc
    code = _exit_for(analysis.findings)
    estimate = estimate_spend(loaded, analysis) if analysis.dataset is not None else None
    summary: dict[str, Any] = {
        "valid": code == EXIT_OK,
        "findings": [f.as_dict() for f in analysis.findings],
        "cases": len(analysis.cases),
        "repetitions": loaded.repetitions,
        "execution_items": len(analysis.cases) * loaded.repetitions,
        "evaluation_items": len(analysis.cases) * loaded.repetitions * len(analysis.metrics),
        "metrics": [f"{m.manifest.evaluator_id}@{m.manifest.version}" for m in analysis.metrics],
        "coverage": [
            {
                "metric": c.metric,
                "eligible_cases": c.eligible_cases,
                "selected_cases": c.selected_cases,
                "fields": c.fields,
            }
            for c in analysis.coverage
        ],
        "estimate": estimate.model_dump(mode="json") if estimate else None,
    }
    if json_output:
        console.print_json(data=summary)
    else:
        _print_findings(analysis.findings)
        if code == EXIT_OK:
            console.print(
                f"plan valid: {summary['cases']} case(s) x {summary['repetitions']} "
                f"repetition(s), {len(summary['metrics'])} metric(s); nothing was dispatched"
            )
        elif code == EXIT_DENIED:
            err_console.print("nothing was dispatched: the policy denies this plan")
        else:
            err_console.print("nothing was dispatched: the plan is invalid")
    raise typer.Exit(code=code)
