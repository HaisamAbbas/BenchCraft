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
from rich.markup import escape

from aibench.cli.errors import error_exit
from aibench.cli.output import Console
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
from aibench.planning.opportunities import OpportunityReport, discover_opportunities
from aibench.planning.planner import PlannerLimits, plan_with_model, plan_with_template
from aibench.planning.service import gather_inputs, write_draft
from aibench.security.secrets import SUPPORTED_SOURCES

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


def _fail(
    message: str,
    code: int,
    *,
    json_output: bool = False,
    details: list[str] | None = None,
) -> typer.Exit:
    return error_exit(
        message,
        exit_code=code,
        json_output=json_output,
        console=console,
        err_console=err_console,
        details=details,
    )


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


def _selection(
    sample: int | None, seed: int | None, limit: int | None, *, json_output: bool = False
) -> CaseSelection | None:
    if sample is None and limit is None:
        if seed is not None:
            raise _fail("--seed requires --sample", EXIT_INVALID, json_output=json_output)
        return None
    try:
        return CaseSelection(sample_size=sample, seed=seed, limit=limit)
    except PydanticValidationError as exc:
        raise _fail(
            f"invalid selection: {exc.errors()[0]['msg']}", EXIT_INVALID, json_output=json_output
        ) from exc


def _plugin_environments(
    python: Path | None,
    secrets: list[str],
    paths: list[Path],
    *,
    json_output: bool = False,
) -> tuple[PluginEnvironmentRef, ...]:
    if python is None:
        if secrets or paths:
            raise _fail(
                "--plugin-secret and --plugin-path require --plugin-env",
                EXIT_INVALID,
                json_output=json_output,
            )
        return ()
    secret_env: dict[str, str] = {}
    for item in secrets:
        name, sep, ref = item.partition("=")
        if not sep or not name or ":" not in ref:
            raise _fail(
                f"--plugin-secret must look like NAME=source:name, got {item!r}",
                EXIT_INVALID,
                json_output=json_output,
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
        raise _fail(
            f"invalid plugin environment: {exc.errors()[0]['msg']}",
            EXIT_INVALID,
            json_output=json_output,
        ) from exc


def _keyed_json(items: list[str], option: str, *, json_output: bool = False) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key:
            raise _fail(
                f"{option} must look like EVALUATOR_ID=JSON, got {item!r}",
                EXIT_INVALID,
                json_output=json_output,
            )
        try:
            parsed[key] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise _fail(
                f"{option} {key}: invalid JSON ({exc.msg})",
                EXIT_INVALID,
                json_output=json_output,
            ) from exc
    return parsed


def _rules(items: list[str], *, json_output: bool = False) -> dict[str, DecisionRule]:
    rules = {}
    for key, value in _keyed_json(items, "--rule", json_output=json_output).items():
        try:
            rules[key] = DecisionRule.model_validate(value)
        except PydanticValidationError as exc:
            raise _fail(
                f"--rule {key}: {exc.errors()[0]['msg']}", EXIT_INVALID, json_output=json_output
            ) from exc
    return rules


def _provider(
    config_source: Path | Any, policy_path: Path | None, *, json_output: bool = False
) -> tuple[Any, list[str]]:
    """The provider, or (None, denials) when the policy does not permit contacting it."""
    from aibench.planning.openai_provider import (
        OpenAICompatibleConfig,
        OpenAICompatibleProvider,
        provider_denials,
    )

    if isinstance(config_source, OpenAICompatibleConfig):
        config = config_source
    else:
        try:
            config = OpenAICompatibleConfig.model_validate(load_mapping_file(config_source))
        except PydanticValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: "
                f"{'must be a secret reference' if 'api_key' in error['loc'] else error['msg']}"
                for error in exc.errors(
                    include_url=False, include_context=False, include_input=False
                )
            )
            raise _fail(
                f"invalid provider config {config_source}: {problems}",
                EXIT_INVALID,
                json_output=json_output,
            ) from exc
        except (AibenchError, OSError) as exc:
            raise _fail(
                f"invalid provider config {config_source}: {type(exc).__name__}",
                EXIT_INVALID,
                json_output=json_output,
            ) from exc
    if config.api_key is not None and config.api_key.partition(":")[0] not in SUPPORTED_SOURCES:
        raise _fail(
            "invalid provider config: api_key must use a supported secret source (env)",
            EXIT_INVALID,
            json_output=json_output,
        )
    try:
        denials = provider_denials(config, load_policy(policy_path))
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    if denials:
        return None, denials
    try:
        return OpenAICompatibleProvider(config), []
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc


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


def _provider_source(
    provider_config: Path | None, provider_profile: str | None, *, json_output: bool
) -> Path | Any | None:
    if provider_config is not None and provider_profile is not None:
        raise _fail(
            "use only one of --provider-config and --provider-profile",
            EXIT_INVALID,
            json_output=json_output,
        )
    if provider_config is not None:
        return provider_config
    from aibench import userconfig

    if provider_profile is not None:
        config = userconfig.saved_provider(provider_profile)
        if config is None:
            raise _fail(
                f"provider profile {provider_profile!r} is unavailable",
                EXIT_INVALID,
                json_output=json_output,
            )
        return config
    return userconfig.saved_provider()


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
    provider_profile: str | None = typer.Option(
        None, "--provider-profile", help="Named provider profile for --planner model."
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
            json_output=json_output,
        )
    if planner not in ("template", "model"):
        raise _fail("--planner must be template or model", EXIT_INVALID, json_output=json_output)
    provider_source = _provider_source(provider_config, provider_profile, json_output=json_output)
    if planner == "model" and provider_source is None:
        raise _fail(
            "--planner model needs --provider-config or a saved provider profile",
            EXIT_INVALID,
            json_output=json_output,
        )
    try:
        gathered = gather_inputs(
            application=app,
            dataset=dataset,
            objectives=list(objectives),
            out=out,
            policy=load_policy(policy),
            trusted_local=trust_local_app,
            selection=_selection(sample, seed, limit, json_output=json_output),
            budgets=BudgetLimits(
                max_application_calls=max_app_calls, max_evaluator_calls=max_evaluator_calls
            ),
            plugin_environments=_plugin_environments(
                plugin_env, plugin_secret, plugin_path, json_output=json_output
            ),
            params=_keyed_json(params, "--params", json_output=json_output),
            rules=_rules(rule, json_output=json_output),
            source_root=Path.cwd(),
        )
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    for note in gathered.notes:
        err_console.print(f"[yellow]note:[/yellow] {escape(note)}")
    inputs = gathered.inputs
    provider_denied: list[str] = []
    if planner == "model":
        assert provider_source is not None
        provider, provider_denied = _provider(provider_source, policy, json_output=json_output)
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
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    findings = outcome.validation.findings
    code = EXIT_DENIED if provider_denied else _exit_for(findings)
    for denial in provider_denied:
        err_console.print(f"[red]denied:[/red] {escape(denial)}")
    if json_output:
        console.print_json(data=json.loads(document.model_dump_json()), cli_exit_code=code)
    else:
        _summary(document)
        _print_findings(findings)
        console.print(f"wrote {escape(str(out))} and its draft document")
    if provider_denied:
        err_console.print("the model planner was not contacted; the draft uses the template")
        raise typer.Exit(code=code)
    raise typer.Exit(code=code)


@plan_app.command("opportunities")
def show_opportunities(
    app: Path = typer.Option(..., "--app", help="Application config file."),  # noqa: B008
    dataset: Path = typer.Option(..., "--dataset", help="JSONL dataset."),  # noqa: B008
    objectives: list[str] = typer.Option(  # noqa: B008
        [], "--objective", help="What the benchmark should check (repeatable)."
    ),
    policy: Path | None = _POLICY,
    trust_local_app: bool = typer.Option(
        False, "--trust-local-app", help="Grant trusted-local mode for a CLI application."
    ),
    plugin_env: Path | None = _PLUGIN_ENV_OPTION,
    plugin_secret: list[str] = _PLUGIN_SECRET_OPTION,
    plugin_path: list[Path] = _PLUGIN_PATH_OPTION,
    params: list[str] = typer.Option(  # noqa: B008
        [], "--params", help="Metric parameters you supply: EVALUATOR_ID=JSON (repeatable)."
    ),
    rule: list[str] = typer.Option(  # noqa: B008
        [], "--rule", help="Pass/fail rules you supply: EVALUATOR_ID=JSON (repeatable)."
    ),
    json_output: bool = _JSON,
) -> None:
    """Show evidence- and policy-aware metric opportunities; write no plan and run nothing."""
    supplied_params = _keyed_json(params, "--params", json_output=json_output)
    if any(not isinstance(value, dict) for value in supplied_params.values()):
        raise _fail("--params values must be JSON objects", EXIT_INVALID, json_output=json_output)
    try:
        gathered = gather_inputs(
            application=app,
            dataset=dataset,
            objectives=list(objectives),
            out=None,
            policy=load_policy(policy),
            trusted_local=trust_local_app,
            plugin_environments=_plugin_environments(
                plugin_env, plugin_secret, plugin_path, json_output=json_output
            ),
            params=supplied_params,
            rules=_rules(rule, json_output=json_output),
            source_root=Path.cwd(),
        )
    except AibenchError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    for note in gathered.notes:
        err_console.print(f"[yellow]note:[/yellow] {escape(note)}")
    report = discover_opportunities(gathered.inputs)
    if json_output:
        console.print_json(data=json.loads(report.model_dump_json()))
        return
    _print_opportunities(report)


def _print_opportunities(report: OpportunityReport) -> None:
    console.print(f"dataset {report.dataset_hash}: {report.dataset_cases} case(s)")
    for objective in report.objectives:
        console.print(
            f"objective {escape(objective.objective_id)} ({objective.state}): "
            f"{escape(objective.objective)}"
        )
        for concept in objective.concepts:
            console.print(f"  {escape(concept.concept)}: {concept.state}")
            if concept.recommended_metric:
                console.print(f"    recommended: {escape(concept.recommended_metric)}")
            if concept.note:
                console.print(f"    {escape(concept.note)}")
            if concept.gap:
                console.print(f"    gap: {escape(concept.gap)}")
            for metric in concept.metrics:
                label = "recommended" if metric.recommended else metric.state
                console.print(f"    {escape(metric.metric)} ({label})")
                for requirement in metric.requirements:
                    console.print(
                        f"      {escape(requirement.path)}: {requirement.state} — "
                        f"{escape(requirement.detail)}"
                    )
                for reason in metric.reasons:
                    console.print(f"      reason: {escape(reason)}")
        for question in objective.questions:
            console.print(f"  question: {escape(question)}")
    for question in report.questions:
        console.print(f"question: {escape(question)}")
    for limitation in report.limitations:
        console.print(f"  limit: {escape(limitation)}")


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
        if not json_output:
            for problem in exc.problems:
                err_console.print(f"[red]invalid:[/red] {escape(problem)}")
        raise _fail(
            "nothing was dispatched: the plan is invalid",
            EXIT_INVALID,
            json_output=json_output,
            details=list(exc.problems),
        ) from exc
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
        console.print_json(data=summary, cli_exit_code=code)
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


# --------------------------------------------------------------------------- benchmark


@plan_app.command("benchmark")
def benchmark_planner(
    fixtures: Path = typer.Option(  # noqa: B008
        Path("benchmarks/planner/v1"), "--fixtures", help="Fixture set directory."
    ),
    planner: str = typer.Option("template", "--planner", help="template | model"),
    provider_config: Path | None = typer.Option(  # noqa: B008
        None, "--provider-config", help="Model provider config for --planner model."
    ),
    provider_profile: str | None = typer.Option(
        None, "--provider-profile", help="Named provider profile for --planner model."
    ),
    policy: Path | None = _POLICY,
    out: Path | None = typer.Option(None, "--out", help="Write the full report (JSON) here."),  # noqa: B008
    require_targets: bool = typer.Option(
        False, "--require-targets", help="Exit 1 unless every §23 target is met."
    ),
    json_output: bool = _JSON,
) -> None:
    """Measure a planner against an annotated fixture set (§23). Nothing is executed:
    applications are never contacted and catalog evaluators are manifests only."""
    from aibench.planning.benchmark import FixtureSetError, load_fixture_set, run_fixture_set
    from aibench.services.reports import write_text_atomic

    if planner not in ("template", "model"):
        raise _fail("--planner must be template or model", EXIT_INVALID, json_output=json_output)
    provider_source = _provider_source(provider_config, provider_profile, json_output=json_output)
    try:
        fixture_set = load_fixture_set(fixtures)
    except FixtureSetError as exc:
        raise _fail(str(exc), EXIT_INVALID, json_output=json_output) from exc
    provider = None
    if planner == "model":
        if provider_source is None:
            raise _fail(
                "--planner model needs --provider-config or a saved provider profile",
                EXIT_INVALID,
                json_output=json_output,
            )
        provider, denials = _provider(provider_source, policy, json_output=json_output)
        if provider is None:
            if not json_output:
                for denial in denials:
                    err_console.print(f"[red]denied:[/red] {escape(denial)}")
            raise _fail(
                "the model planner was not contacted; nothing was measured",
                EXIT_DENIED,
                json_output=json_output,
                details=denials,
            )

    def plan_one(inputs: Any) -> Any:
        if provider is None:
            return plan_with_template(inputs)
        return plan_with_model(inputs, provider, PlannerLimits())

    try:
        name = "template" if provider is None else f"model:{provider.name}:{provider.model}"
        report = run_fixture_set(fixture_set, plan_one, planner_name=name)
    finally:
        if provider is not None:
            provider.close()
    if out is not None:
        write_text_atomic(out, json.dumps(report, indent=2) + "\n")
    missed = [t for t in report["targets"] if t["status"] != "met"]
    if json_output:
        console.print_json(
            data=report,
            cli_exit_code=1 if require_targets and missed else EXIT_OK,
        )
    else:
        o = report["overall"]
        console.print(
            f"planner {escape(report['planner'])} on {escape(report['fixture_set'])}: "
            f"{o['fixtures']} fixture(s); review: {escape(report['review']['status'])}"
        )
        for key in (
            "selection_precision",
            "selection_recall",
            "gap_precision",
            "gap_recall",
            "unnecessary_evaluator_rate",
            "first_pass_valid",
            "invalid_rejection",
        ):
            m = o[key]
            interval = f" (95% CI {m['wilson95'][0]}-{m['wilson95'][1]})" if m["wilson95"] else ""
            console.print(f"  {key}: {m['value']} = {m['numerator']}/{m['denominator']}{interval}")
        console.print(f"  unsupported_selections: {o['unsupported_selections']}")
        for part in ("development_families", "holdout_families"):
            p = report[part]
            console.print(
                f"  {part}: {p['fixtures']} fixture(s), precision "
                f"{p['selection_precision']['value']}, recall {p['selection_recall']['value']}, "
                f"gap precision {p['gap_precision']['value']}"
            )
        for t in report["targets"]:
            colour = "green" if t["status"] == "met" else "red"
            console.print(
                f"  target {t['measure']} {t['target']}: [{colour}]{t['status']}[/{colour}] "
                f"(observed {t['observed']})"
            )
        wrong = [f["id"] for f in report["fixtures"] if not f["correct"]]
        if wrong:
            console.print(f"  fixtures not planned as annotated: {escape(', '.join(wrong))}")
    raise typer.Exit(code=1 if require_targets and missed else EXIT_OK)
