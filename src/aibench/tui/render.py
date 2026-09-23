"""Terminal rendering for the conversation (§13): compact cards, never one message per
case. Output is plain text with Rich markup, ASCII only, so it works on any Windows code
page; everything interpolated from data is escaped."""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.markup import escape



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
    console.print(f"[bold]Plan, revision {summary['revision']}[/bold] ({state})")
    for objective in summary.get("objectives", []):
        console.print(f"  objective: {escape(objective)}")
    for metric in summary.get("metrics", []):
        console.print(f"  metric {escape(metric['metric'])}: {escape(metric['rationale'])}")
    for gap in summary.get("gaps", []):
        console.print(f"  [yellow]gap[/yellow] {escape(gap['subject'])}: {escape(gap['reason'])}")
    for label, key in (
        ("needs information", "missing_information"),
        ("needs permission", "missing_permission"),
        ("invalid", "invalid"),
    ):
        for message in summary.get(key, []):
            console.print(f"  [red]{label}:[/red] {escape(message)}")
    estimate = summary.get("estimate")
    if estimate:
        cost = estimate["estimated_cost_usd"]
        cost_text = "unknown" if cost is None else f"${cost:g} (estimate)"
        console.print(
            f"  {estimate['selected_cases']} case(s) x {estimate['repetitions']} repetition(s): "
            f"up to {estimate['application_calls_upper_bound']} application call(s), "
            f"{estimate['evaluations']} evaluation(s), {estimate['model_evaluations']} by a "
            f"model judge; cost {cost_text}"
        )


def status(console: Console, snapshot: dict[str, Any]) -> None:
    console.print(escape(status_line(snapshot)) + f" [dim]as of {escape(snapshot['as_of'])}[/dim]")
    for item in snapshot.get("needs_attention", [])[:10]:
        console.print(
            f"  [yellow]{escape(item['state'])}[/yellow] {escape(item['task_key'])}: "
            f"{escape(str(item['reason']))}"
        )
    if snapshot.get("session_error"):
        console.print(f"  [red]could not run:[/red] {escape(snapshot['session_error'])}")


def failures(console: Console, data: dict[str, Any]) -> None:
    label = (
        " (provisional snapshot)"
        if data["provisional"]
        else " (partial snapshot)"
        if data.get("partial")
        else ""
    )
    console.print(
        f"run {escape(data['run_id'])}: {data['total_metric_failures']} metric failure(s), "
        f"{data['total_application_failures']} application failure(s){label}"
    )
    for item in data["metric_failures"]:
        console.print(
            f"  {escape(item['case_id'])} r{item['repetition']} {escape(item['metric'])}: "
            f"{item['decision']} value={escape(str(item['value']))} {escape(str(item['reason'] or ''))}"
        )
    for item in data["application_failures"]:
        console.print(
            f"  {escape(item['case_id'])} r{item['repetition']}: application {item['status']} "
            f"({escape(str(item['error_kind']))}) {escape(str(item['error'] or ''))}"
        )


def case(console: Console, data: dict[str, Any]) -> None:
    console.print(f"[bold]case {escape(data['case_id'])}[/bold] in run {escape(data['run_id'])}")
    golden = data.get("golden") or {}
    if golden:
        console.print(f"  input: {escape(str(golden.get('input')))}")
        reference = golden.get("reference") or {}
        if reference.get("answer") is not None:
            console.print(f"  reference: {escape(str(reference['answer']))}")
    for execution in data["executions"]:
        console.print(
            f"  r{execution['repetition']} attempt {execution['attempt']}: {execution['status']}"
            f" output={escape(str(execution.get('output')))}"
        )
        if execution.get("error"):
            console.print(f"    error: {escape(execution['error'])}")
    for result in data["results"]:
        console.print(
            f"  {escape(result['metric'])} r{result['repetition']}: {result['decision']} "
            f"value={escape(str(result['value']))} {escape(str(result['reason'] or ''))}"
        )


def budget(console: Console, data: dict[str, Any]) -> None:
    hard = data["limits"]["hard"]
    soft = data["limits"]["soft"]
    console.print(f"[bold]budget[/bold] run {escape(data['run_id'])} ({escape(data['basis'])})")
    for role in ("application", "evaluator"):
        spend = data[role]
        limit = hard[f"max_{role}_calls"]
        unknown = spend["calls_with_unknown_cost"]
        cost = f"known ${spend['known_cost_usd']:g}" + (
            f" + {unknown} call(s) of unknown cost" if unknown else ""
        )
        console.print(f"  {role}: {spend['calls']} call(s) of {limit or 'no limit'}; {cost}")
    console.print(
        f"  judge tokens limit {hard['max_judge_tokens'] or 'none'}; wall time "
        f"{data['elapsed_seconds']}s of {hard['max_wall_seconds'] or 'no limit'}; "
        f"cost limit {soft['max_cost_usd'] or 'none'} (soft)"
    )
    for note in data.get("unenforced", []):
        console.print(f"  [yellow]not enforced:[/yellow] {escape(note)}")
    usage = data.get("conversation") or {}
    if usage:
        console.print(
            f"  conversation model: {usage.get('model_calls', 0)} call(s), "
            f"{usage.get('prompt_tokens', 0) + usage.get('completion_tokens', 0)} token(s) "
            "(tracked separately from the run)"
        )


def tool_call(name: str, args: dict[str, Any]) -> str:
    shown = ", ".join(f"{k}={v}" for k, v in args.items() if k not in ("patch", "user_quote"))
    if "patch" in args:
        shown = ", ".join(f"{k}={v}" for k, v in args["patch"].items())
    return escape(f"  > {name}({shown})"[:160])


def tool_result(name: str, data: dict[str, Any]) -> str | None:
    if "error" in data:
        return escape(f"    error: {data['error']}"[:200])
    if name == "propose_plan_patch":
        if data.get("status") == "applied":
            return f"    draft revision {data['revision']}"
        return escape(f"    {data.get('status')}: {'; '.join(data.get('problems', []))}"[:200])
    if name == "request_action":
        reason = f" ({data['reason']})" if data.get("reason") else ""
        run = f" {data['run_id']}" if data.get("run_id") else ""
        return escape(f"    {data.get('kind')}: {data.get('state')}{run}{reason}"[:200])
    if data.get("status") == "rejected":
        return escape(f"    rejected: {'; '.join(data.get('problems', []))}"[:200])
    return None
