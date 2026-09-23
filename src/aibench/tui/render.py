"""Terminal rendering for the conversation (§13): compact cards, never one message per
case. Output is plain text with Rich markup, ASCII only, so it works on any Windows code
page; everything interpolated from data is escaped."""

from __future__ import annotations

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


def status(console: Console, snapshot: dict[str, Any]) -> None:
    out(console, safe(status_line(snapshot)) + f" [dim]as of {safe(snapshot['as_of'])}[/dim]")
    for item in snapshot.get("needs_attention", [])[:10]:
        out(
            console,
            f"  [yellow]{safe(item['state'])}[/yellow] {safe(item['task_key'])}: "
            f"{safe(str(item['reason']))}",
        )
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
            f"{m['unavailable']}, cancelled {m.get('cancelled', 0)}, pending {m['pending']}",
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
