"""Terminal rendering for the conversation (§13): compact cards, never one message per
case. Output is plain text with Rich markup, ASCII only, so it works on any Windows code
page; everything interpolated from data is escaped."""

from __future__ import annotations

from collections import Counter
from typing import Any

from rich.console import Console
from rich.markup import escape

from aibench.security.redaction import sanitize


def out(console: Console, *objects: object) -> None:
    """Print with Rich's emoji codes off: data such as `exec:b:r0` must not render as
    `exec🅱r0` (a `:name:` in a case ID, output or reason is text, not an emoji)."""
    console.print(*objects, emoji=False)


def safe(value: object) -> str:
    """Untrusted text made printable: secrets redacted, terminal control sequences removed,
    Rich markup escaped (10-T3)."""
    return escape(sanitize(str(value)))


def _counts(counts: dict[str, dict[str, int]], kind: str) -> str:
    states = counts.get(kind, {})
    total = sum(states.values())
    done = sum(n for s, n in states.items() if s not in ("pending", "running"))
    return f"{done}/{total}"


def _failures(counts: dict[str, dict[str, int]]) -> int:
    return sum(
        states.get(s, 0)
        for states in counts.values()
        for s in ("failed", "blocked", "unknown_effect")
    )


def _spend_label(status: dict[str, Any]) -> str:
    if status.get("provisional"):
        return "spend provisional (in-flight calls excluded)"
    budget = status.get("budget")
    if not isinstance(budget, dict):
        return "spend not recorded"
    unknown = sum(
        int(role.get("calls_with_unknown_cost", 0))
        for role in (budget.get("application", {}), budget.get("evaluator", {}))
        if isinstance(role, dict)
    )
    if unknown or budget.get("unenforced"):
        return f"spend partial ({unknown} call(s) with unknown cost)"
    return "spend accounted"


def status_line(status: dict[str, Any]) -> str:
    """One line for a run snapshot: state, progress, failures, provisional label."""
    counts = status.get("counts", {})
    label = (
        " (provisional)"
        if status.get("provisional")
        else " (partial)"
        if status.get("partial")
        else ""
    )
    return (
        f"run {status['run_id']} {status['status']}{label}: "
        f"executions {_counts(counts, 'execution')}, evaluations {_counts(counts, 'evaluation')}, "
        f"needs attention {_failures(counts)}; {_spend_label(status)}"
    )


def draft(console: Console, summary: dict[str, Any]) -> None:
    state = "ready to run" if summary["executable"] else "not executable yet"
    out(console, f"[bold]Plan, revision {summary['revision']}[/bold] ({state})")
    for objective in summary.get("objectives", []):
        out(console, f"  objective: {safe(objective)}")
    if summary.get("test_world"):
        out(console, f"  test world: {safe(summary['test_world'])} (loaded before each case)")
    for metric in summary.get("metrics", []):
        out(console, f"  metric {safe(metric['metric'])}: {safe(metric['rationale'])}")
    for gap in summary.get("gaps", []):
        out(console, f"  [yellow]gap[/yellow] {safe(gap['subject'])}: {safe(gap['reason'])}")
    for label, key in (
        ("needs information", "missing_information"),
        ("needs permission", "missing_permission"),
        ("invalid", "invalid"),
    ):
        for message in summary.get(key, []):
            out(console, f"  [red]{label}:[/red] {safe(message)}")
    estimate = summary.get("estimate")
    if estimate:
        cost = estimate["estimated_cost_usd"]
        cost_text = "unknown" if cost is None else f"${cost:g} (estimate)"
        out(
            console,
            f"  {estimate['selected_cases']} case(s) x {estimate['repetitions']} repetition(s): "
            f"up to {estimate['application_calls_upper_bound']} application call(s), "
            f"{estimate['evaluations']} evaluation(s), {estimate['model_evaluations']} by a "
            f"model judge; cost {cost_text}",
        )


def application(console: Console, data: dict[str, Any]) -> None:
    out(console, f"[bold]{safe(data['application_id'])}[/bold] ({safe(data['kind'])})")
    out(console, f"  isolation: {safe(data['isolation'])}")
    out(console, f"  reset: {safe(data['reset']['summary'])}")
    seen = [c for c, s in data["observable"].items() if s != "unknown"]
    out(console, f"  observable: {safe(', '.join(seen))}")
    for gap in data["missing_evidence"]:
        out(
            console,
            f"  [yellow]missing {safe(gap['capability'])}:[/yellow] {safe(gap['consequence'])}",
        )
    selected = data.get("selected_test_world")
    for world in data["test_worlds"]:
        mark = "*" if world["world_id"] == selected else " "
        approval = "approved" if world["approved"] else "not approved by the policy"
        out(
            console,
            f"  {mark} world {safe(world['world_id'])}: {safe(world['description'])} ({approval})",
        )


def integrations(console: Console, data: dict[str, Any]) -> None:
    for entry in data["integrations"]:
        state = (
            "[green]available[/green]"
            if entry["status"]["available"]
            else "[yellow]unavailable[/yellow]"
        )
        out(console, f"[bold]{safe(entry['name'])}[/bold]: {state}")
        for reason in entry["status"]["reasons"]:
            out(console, f"  [yellow]-[/yellow] {safe(reason)}")
        for mode in entry["modes"]:
            support = "" if mode["supported"] else " [dim](not supported)[/dim]"
            out(console, f"  mode {safe(mode['mode'])}{support}")
        destinations = entry["data_destinations"]
        if not destinations:
            out(console, "  sends data: nowhere (local only)")
        for destination in destinations:
            out(
                console,
                f"  sends {safe(destination['sends'])} to {safe(str(destination['url']))}",
            )
        out(console, f"  [dim]{safe(entry['live_verification'])}[/dim]")


def plugins(console: Console, data: dict[str, Any]) -> None:
    """`/plugins`: optional metric plugins, their state here and how to enable them."""
    labels = {
        "installed": "[green]installed[/green]",
        "not_allowed": "[yellow]installed, not allowed by the policy[/yellow]",
        "broken": "[red]environment missing[/red]",
        "not_installed": "[dim]not installed[/dim]",
    }
    for row in data["plugins"]:
        out(
            console,
            f"[bold]{safe(row['name'])}[/bold] {labels[row['state']]}: {safe(row['summary'])}",
        )
        out(console, f"  metrics: {safe(', '.join(row['metrics']))}")
        out(console, f"  [dim]not included: {safe(row['not_included'])}[/dim]")
        for missing in row["missing"]:
            out(console, f"  [yellow]needs:[/yellow] {safe(missing)}")
        if row["state"] != "installed":
            out(console, f"  enable: {safe(row['enable'])}")


def plugin_preview(console: Console, data: dict[str, Any]) -> None:
    """What `/plugins install NAME` would change; nothing has changed yet."""
    where = "create" if data["creates_environment"] else "use"
    out(console, f"[bold]install {safe(data['plugin'])}[/bold] ({safe(data['package'])})")
    out(console, f"  {where} environment: {safe(data['environment'])}")
    if data["creates_environment"]:
        out(console, f"  installs from: {safe(data['installs_from'])}")
    out(console, f"  metrics: {safe(', '.join(data['metrics']))}")
    if data["judge"]:
        kept = " - the judge this project already uses, kept" if data.get("judge_kept") else ""
        out(console, f"  judge: {safe(data['judge'])} (paid calls to that provider){kept}")
    for name, ref in data["secret_env"].items():
        out(console, f"  judge key: {safe(ref)}, passed to its workers as {safe(name)}")
    out(console, f"  project config: {safe(data['config'])} (plugin_environments)")
    if data["policy_changes"]:
        out(console, f"  policy {safe(data['policy'])} (kept as .bak):")
        for change in data["policy_changes"]:
            out(console, f"    {safe(change)}")
    out(console, f"[bold]Nothing has changed yet.[/bold] To go ahead: {safe(data['confirm'])}")


def plugin_installed(console: Console, data: dict[str, Any]) -> None:
    out(
        console,
        f"[green]{safe(data['plugin'])} installed[/green]: {len(data['evaluators'])} metric(s) "
        f"now in this session (draft revision {data['revision']})",
    )
    for problem in data.get("problems") or []:
        out(console, f"  [yellow]{safe(problem)}[/yellow]")
    out(console, '  ask for what to measure, e.g. "check relevancy and bias", or /plan')


def cases(console: Console, data: dict[str, Any]) -> None:
    """`/cases`: candidate test cases, each beside the source quote it cites, for review."""
    rows = data["rows"]
    verb = "generated" if data["generated"] else "in"
    out(console, f"[bold]{len(rows)} candidate case(s)[/bold] {verb} {safe(data['pool_id'])}")
    for row in rows:
        tag = {"candidate": "to review", "reviewed": "accepted", "rejected": "rejected"}.get(
            row["status"], row["status"]
        )
        out(console, f"\n[bold]{row['number']}.[/bold] ({tag})  {safe(row['question'])}")
        out(console, f"   answer: {safe(row['answer'])}")
        if row["quote"] is None:
            out(console, "   [yellow]source changed or missing: this case cannot be used[/yellow]")
            continue
        quote = row["quote"] if len(row["quote"]) <= 240 else row["quote"][:237] + "..."
        out(console, f"   [dim]{safe(row['source'])}:[/dim] \"{safe(quote)}\"")
        if not row["verbatim"]:
            out(console, "   [yellow]the answer is not word for word in that quote: check it[/yellow]")
    if data.get("dropped"):
        out(
            console,
            f"[yellow]left out {len(data['dropped'])} case(s) whose quoted source text is not in the "
            "document (nothing to check them against)[/yellow]",
        )
    for item in data.get("duplicate_sources", []):
        out(console, f"[yellow]same document twice: {safe(item['source_ref'])}[/yellow]")
    out(
        console,
        "\nRead each case against its quote. Then /cases accept 1 2 3 (or /cases accept all), "
        "/cases reject N, and /cases save. [dim]Nothing is a test case until it is accepted "
        "and saved.[/dim]",
    )


def cases_decided(console: Console, data: dict[str, Any]) -> None:
    word = "accepted" if data["accepted"] else "rejected"
    numbers = ", ".join(str(item["number"]) for item in data["done"]) or "none"
    out(console, f"{word}: {numbers}; {data['undecided']} still to review")
    if data["accepted"] and data["done"]:
        out(console, "  /cases save writes the accepted cases to a new dataset file")


def cases_saved(console: Console, data: dict[str, Any]) -> None:
    out(console, f"[green]saved {data['count']} case(s)[/green] to {safe(data['path'])}")
    out(
        console,
        f'  to benchmark with them, tell me: "use the dataset {safe(data["path"])}"; '
        "the answers in it were written by a model and accepted by you",
    )


def traces(console: Console, data: dict[str, Any]) -> None:
    """`/traces`: what a run's imported traces add."""
    if data["run_id"] is None or not data["available"]:
        out(console, safe(data["reason"]))
        if data["run_id"] is not None:
            out(console, "  attach an OpenTelemetry export: /traces import FILE")
        return
    summary = data["trace_summary"]
    usage = summary["usage"]
    out(
        console,
        f"run {safe(data['run_id'])}: {summary['traces']} trace(s), "
        f"{summary['matched_to_executions']} matched to executions, "
        f"{summary['complete']} complete, {summary['partial']} partial",
    )
    if summary["partial_reasons"]:
        reasons = ", ".join(f"{k} {v}" for k, v in summary["partial_reasons"].items())
        out(console, f"  [yellow]partial:[/yellow] {safe(reasons)}")
    bound = usage["bound"].replace("_", " ")
    out(console, f"  usage from traces: {usage['total_tokens']} tokens ({safe(bound)})")
    out(console, f"  tool spans: {summary['tool_spans']} ({summary['tool_errors']} failed)")


def traces_imported(console: Console, data: dict[str, Any]) -> None:
    """`/traces import FILE`: what the import attached."""
    out(
        console,
        f"imported {data['traces']} trace(s) from {safe(data['file'])} into run "
        f"{safe(data['run_id'])}: {data['matched']} matched to executions, "
        f"{data['unmatched']} unmatched, {data['partial']} partial",
    )
    if data["partial_reasons"]:
        reasons = ", ".join(f"{k} {v}" for k, v in data["partial_reasons"].items())
        out(console, f"  [yellow]partial:[/yellow] {safe(reasons)} (not scored by trace metrics)")
    if not data["added"]:
        out(console, "  already imported; nothing added")
    else:
        out(console, "  score the draft's trace metrics on it: /rescore")


def rescored(console: Console, data: dict[str, Any]) -> None:
    """`/rescore`: one line per metric of the new scoring pass."""
    out(
        console,
        f"rescored run {safe(data['run_id'])} as {safe(data['scoring_id'])} "
        "[dim](stored outputs; the application was not called)[/dim]",
    )
    carried = data.get("carried_forward", 0)
    if carried:
        evaluated = data.get("evaluated_now", 0)
        out(
            console,
            f"  carried forward {carried} finished result(s); evaluated {evaluated} now"
            + (" [dim](/rescore all evaluates everything again)[/dim]" if evaluated == 0 else ""),
        )
    for summary in data["summaries"]:
        mean = summary["value_summary"].get("mean")
        value = f" mean {_number(mean)}" if mean is not None else ""
        line = (
            f"  {safe(summary['metric_id'])}:{value} completed {summary['completed']}/"
            f"{summary['selected']}, not applicable {summary['not_applicable']}, "
            f"errors {summary['errors']}"
        )
        out(console, line)
        if summary["reasons"]:
            top = sorted(summary["reasons"].items(), key=lambda item: -item[1])[:3]
            out(console, f"    [dim]{safe(', '.join(f'{k} {v}' for k, v in top))}[/dim]")
    for warning in data["warnings"]:
        out(console, f"  [yellow]warning:[/yellow] {safe(warning)}")


def status(console: Console, snapshot: dict[str, Any]) -> None:
    out(console, safe(status_line(snapshot)) + f" [dim]as of {safe(snapshot['as_of'])}[/dim]")
    for item in snapshot.get("needs_attention", [])[:10]:
        out(
            console,
            f"  [yellow]{safe(item['state'])}[/yellow] {safe(item['task_key'])}: "
            f"{safe(str(item['reason']))}",
        )
    for warning in snapshot.get("warnings", []):
        out(console, f"  [yellow]warning:[/yellow] {safe(warning)}")
    if snapshot.get("session_error"):
        out(console, f"  [red]could not run:[/red] {safe(snapshot['session_error'])}")


def failures(console: Console, data: dict[str, Any]) -> None:
    label = (
        " (provisional snapshot)"
        if data["provisional"]
        else " (partial snapshot)"
        if data.get("partial")
        else ""
    )
    out(
        console,
        f"run {safe(data['run_id'])}: {data['total_metric_failures']} metric failure(s), "
        f"{data['total_application_failures']} application failure(s){label}",
    )
    for item in data["metric_failures"]:
        out(
            console,
            f"  {safe(item['case_id'])} r{item['repetition']} {safe(item['metric'])}: "
            f"{item['decision']} value={safe(str(item['value']))} {safe(str(item['reason'] or ''))}",
        )
    for item in data["application_failures"]:
        out(
            console,
            f"  {safe(item['case_id'])} r{item['repetition']}: application {item['status']} "
            f"({safe(str(item['error_kind']))}) {safe(str(item['error'] or ''))}",
        )


def case(console: Console, data: dict[str, Any]) -> None:
    out(console, f"[bold]case {safe(data['case_id'])}[/bold] in run {safe(data['run_id'])}")
    golden = data.get("golden") or {}
    if golden:
        out(console, f"  input: {safe(str(golden.get('input')))}")
        reference = golden.get("reference") or {}
        if reference.get("answer") is not None:
            out(console, f"  reference: {safe(str(reference['answer']))}")
    for execution in data["executions"]:
        out(
            console,
            f"  r{execution['repetition']} attempt {execution['attempt']}: {execution['status']}"
            f" output={safe(str(execution.get('output')))}",
        )
        if execution.get("error"):
            out(console, f"    error: {safe(execution['error'])}")
    for result in data["results"]:
        out(
            console,
            f"  {safe(result['metric'])} r{result['repetition']}: {result['decision']} "
            f"value={safe(str(result['value']))} {safe(str(result['reason'] or ''))}",
        )


def budget(console: Console, data: dict[str, Any]) -> None:
    hard = data["limits"]["hard"]
    soft = data["limits"]["soft"]
    out(console, f"[bold]budget[/bold] run {safe(data['run_id'])} ({safe(data['basis'])})")
    for role in ("application", "evaluator"):
        spend = data[role]
        limit = hard[f"max_{role}_calls"]
        unknown = spend["calls_with_unknown_cost"]
        cost = f"known ${spend['known_cost_usd']:g}" + (
            f" + {unknown} call(s) of unknown cost" if unknown else ""
        )
        out(console, f"  {role}: {spend['calls']} call(s) of {limit or 'no limit'}; {cost}")
    out(
        console,
        f"  judge tokens limit {hard['max_judge_tokens'] or 'none'}; wall time "
        f"{data['elapsed_seconds']}s of {hard['max_wall_seconds'] or 'no limit'}; "
        f"cost limit {soft['max_cost_usd'] or 'none'} (soft)",
    )
    for note in data.get("unenforced", []):
        out(console, f"  [yellow]not enforced:[/yellow] {safe(note)}")
    usage = data.get("conversation") or {}
    if usage:
        out(
            console,
            f"  conversation model: {usage.get('model_calls', 0)} call(s), "
            f"{usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)} token(s) "
            "(tracked separately from the run)",
        )


def _of(numerator: int | None, denominator: int | None) -> str:
    if numerator is None or denominator is None:
        return "unknown"
    if denominator == 0:
        return f"{numerator}/0"
    return f"{numerator}/{denominator} ({100 * numerator / denominator:.1f}%)"


def report(console: Console, data: dict[str, Any]) -> None:
    """`/report`: the report's aggregates, each count with its denominator."""
    label = (
        " [yellow](partial snapshot: not finished)[/yellow]"
        if data.get("provisional")
        else " [yellow](partial results)[/yellow]"
        if data.get("partial")
        else ""
    )
    out(console, f"[bold]report for run {safe(data['run_id'])}[/bold]: {data['status']}{label}")
    out(console, f"  {safe(data['basis'])}")
    for gate in data["gates"]:
        colour = {"pass": "green", "fail": "red"}.get(gate["status"], "yellow")
        reason = f": {gate['reason']}" if gate.get("reason") else ""
        out(
            console,
            f"  gate {safe(gate['gate_id'])}: [{colour}]{gate['status']}[/{colour}]{safe(reason)}",
        )
    for m in data["metrics"]:
        d = m["decisions"]
        out(
            console,
            f"  {safe(m['metric'])}: pass {_of(d.get('pass', 0), m['selected'])} of selected, "
            f"completed {_of(m['completed'], m['selected'])}; evaluator errors "
            f"{m['evaluator_errors']}, not applicable {m['not_applicable']}, unavailable "
            f"{m['unavailable']}, cancelled {m.get('cancelled', 0)}, pending {m['pending']}"
            + (
                f"; [yellow]{m['unstable_results']} unstable score(s)[/yellow] "
                "(repeated judge scores disagreed; see /case)"
                if m.get("unstable_results")
                else ""
            ),
        )
    app = data["application"]
    latency = data["latency_ms"]
    out(
        console,
        f"  application: completed {_of(app['completed'], app['planned'] or app['recorded'])}, "
        f"failed {app['failed']}; successful-request latency "
        + (
            f"p50 {latency['p50_ms']} ms, p95 {latency['p95_ms']} ms over "
            f"{latency['successful_requests']}"
            if latency["successful_requests"]
            else "not measured (no successful request)"
        ),
    )
    suspect = app.get("error_like_answers") or {"count": 0}
    if suspect["count"]:
        out(
            console,
            f"  [yellow]warning: {suspect['count']} of {app['completed']} answers look like "
            "errors, not answers[/yellow] (e.g. "
            f"{safe(', '.join(suspect['case_ids'][:3]))}): the application may be failing "
            "while reporting success, and metrics would score the error text. Check with "
            "/case CASE_ID before trusting these scores.",
        )
    for role, cost in data["cost"].items():
        if cost.get("accounting") in (None, "not_attributed"):
            continue
        if cost["accounting"] == "no_calls":
            spent = "no calls"
        elif cost["total_cost_usd"] is not None:
            spent = f"USD {cost['total_cost_usd']:g} (complete)"
        elif cost["accounting"] == "unknown":
            spent = "unknown (no call reported its cost)"
        else:
            spent = f"at least USD {cost['known_cost_usd']:g} ({cost['accounting']})"
        out(console, f"  {safe(role)} cost: {safe(spent)}")
    cases = data["non_passing_cases"]
    if cases["total"]:
        more = cases["total"] - len(cases["first"])
        tail = f" and {more} more" if more else ""
        out(
            console,
            f"  non-passing cases: {safe(', '.join(cases['first']))}{tail} (/case CASE_ID)",
        )
    for fmt, path in data.get("exported", {}).items():
        out(console, f"  wrote {safe(fmt)}: {safe(path)}")


def comparison(console: Console, data: dict[str, Any]) -> None:
    """Compact paired-comparison card; no per-case ledger or raw judge content."""
    status = str(data.get("status", "unknown"))
    colour = {"qualified": "green", "comparable": "green", "blocked": "red", "exploratory": "yellow"}.get(
        status, "yellow"
    )
    out(
        console,
        f"[bold]comparison[/bold] [{colour}]{safe(status)}[/colour]"
        f" (identity_qualified={str(bool(data.get('identity_qualified', data.get('qualified')))).lower()},"
        f" claim_qualified={str(bool(data.get('claim_qualified', data.get('qualified')))).lower()})",
    )
    baseline = (data.get("runs") or {}).get("baseline") or {}
    current = (data.get("runs") or {}).get("current") or {}
    out(
        console,
        f"  {safe(baseline.get('run_id', '?'))} -> {safe(current.get('run_id', '?'))}; "
        f"{safe(data.get('basis', 'stored facts only'))}",
    )
    gate: dict[str, Any] = data.get("overall_coverage_gate") or data.get("gate") or {}
    if gate:
        gate_status = str(gate.get("status", "unknown"))
        gate_colour = {"pass": "green", "fail": "red"}.get(gate_status, "yellow")
        out(
            console,
            f"  coverage gate: [{gate_colour}]{safe(gate_status)}[/{gate_colour}]"
            f" {safe(str(gate.get('reason') or ''))}",
        )
    for metric in data.get("metrics", []):
        detail = metric.get("comparison") or metric.get("diagnostic_comparison")
        if not isinstance(detail, dict):
            out(console, f"  {safe(metric.get('label', 'metric'))}: no numeric comparison")
            continue
        denominators = detail.get("denominators") or {}
        complete = denominators.get("complete_numeric_pairs")
        selected = denominators.get("paired_selected")
        macro = detail.get("case_macro") or {}
        mean = macro.get("mean_current_minus_baseline")
        interval = detail.get("uncertainty") or {}
        estimate = f", mean delta {_number(mean)}" if mean is not None else ""
        if interval.get("lower") is not None and interval.get("upper") is not None:
            estimate += f" (95% CI {_number(interval['lower'])}..{_number(interval['upper'])})"
        out(
            console,
            f"  {safe(metric.get('label', 'metric'))}: {complete}/{selected} complete pairs"
            f"{estimate}",
        )
    for warning in data.get("warnings", [])[:10]:
        out(console, f"  [yellow]warning:[/yellow] {safe(warning)}")
    disagreement = data.get("ecosystem_disagreement") or data.get("cross_framework") or []
    if isinstance(disagreement, list):
        matrix_counts: Counter[str] = Counter()
        for item in disagreement:
            if isinstance(item, dict) and isinstance(item.get("decision_matrix"), dict):
                matrix_counts.update(item["decision_matrix"])
        disagreement = {
            "decision_matrix": dict(matrix_counts),
            "paired_count": sum(
                int(item.get("paired_count", 0))
                for item in disagreement
                if isinstance(item, dict)
            ),
        }
    if disagreement.get("paired_count") or disagreement.get("total"):
        matrix: dict[str, Any] = disagreement.get("decision_matrix") or {}
        out(
            console,
            "  ecosystem decisions (diagnostic, scales not equivalent): "
            f"both pass {matrix.get('pass_pass', 0)}, "
            f"baseline only {matrix.get('pass_fail', 0)}, "
            f"current only {matrix.get('fail_pass', 0)}, "
            f"both fail {matrix.get('fail_fail', 0)}",
        )


def _number(value: object) -> str:
    return f"{float(value):.6g}" if isinstance(value, (int, float)) else "unknown"


def tool_call(name: str, args: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if k not in ("patch", "user_quote"))
    if "patch" in args:
        shown = ", ".join(f"{k}={v}" for k, v in args["patch"].items())
    return safe(f"  > {name}({shown})"[:160])


def tool_result(name: str, data: dict[str, Any]) -> str | None:
    if "error" in data:
        return safe(f"    error: {data['error']}"[:200])
    if name == "propose_plan_patch":
        if data.get("status") == "applied":
            return f"    draft revision {data['revision']}"
        return safe(f"    {data.get('status')}: {'; '.join(data.get('problems', []))}"[:200])
    if name == "request_action":
        reason = f" ({data['reason']})" if data.get("reason") else ""
        run = f" {data['run_id']}" if data.get("run_id") else ""
        return safe(f"    {data.get('kind')}: {data.get('state')}{run}{reason}"[:200])
    if data.get("status") == "rejected":
        return safe(f"    rejected: {'; '.join(data.get('problems', []))}"[:200])
    if name == "export_report" and data.get("paths"):
        return safe("    wrote " + ", ".join(data["paths"].values()))
    return None
