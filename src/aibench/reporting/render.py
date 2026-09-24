"""Render a report document (services.reports.build_report) as JSON, Markdown or HTML.

Pure functions of the document: rendering never reads storage and never computes a new
statistic. Percentages are shown next to the counts they come from ("8/10 = 80.0%"), so a
reader can check every number against its denominator.

Untrusted text (case IDs, outputs, reasons, plan names) is sanitized again here and then
escaped for the target format: HTML entities in HTML, which also carries a Content
Security Policy that forbids scripts and remote loads; Markdown metacharacters and inline
HTML in Markdown. The HTML report is a static file: no scripts, no external resources.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from aibench.security.redaction import sanitize

EVIDENCE_LIMIT = 50  # items shown in Markdown/HTML; the JSON report has all of them


def render(report: dict[str, Any], fmt: str) -> str:
    if fmt == "json":
        return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if fmt == "markdown":
        return _markdown(report)
    if fmt == "html":
        return _html(report)
    raise ValueError(f"unknown report format {fmt!r}")


# --------------------------------------------------------------------------- numbers


def fraction(numerator: int | None, denominator: int | None) -> str:
    if numerator is None or denominator is None:
        return "unknown"
    if denominator == 0:
        return f"{numerator}/0"
    return f"{numerator}/{denominator} = {100 * numerator / denominator:.1f}%"


def _state_text(state: dict[str, Any]) -> str:
    mode = state.get("reset_mode", "none")
    text = {
        "per_case": f"reset before every case ({state.get('reset_hook')})",
        "per_episode": f"reset before each episode ({state.get('reset_hook')})",
    }.get(mode, "shared" if state.get("reset_policy") == "shared" else "not reset")
    world = state.get("test_world")
    if world:
        text += f"; test world {world['world_id']} (seed {world['seed_hash'][:19]})"
    if state.get("resets"):
        text += "; resets: " + ", ".join(f"{k} {v}" for k, v in state["resets"].items())
    return text


def _number(value: Any, unit: str = "") -> str:
    if value is None:
        return "unknown"
    if isinstance(value, float):
        text = f"{value:.6g}"
    else:
        text = str(value)
    return f"{text}{unit}"


def _usd(block: dict[str, Any]) -> str:
    accounting = block.get("accounting")
    if accounting == "no_calls":
        return "no calls"
    if accounting == "not_attributed":
        return "not attributed to runs"
    known = block["known_cost_usd"]
    counted = fraction(block["calls_with_known_cost"], block["calls"])
    if block.get("total_cost_usd") is not None:
        return f"USD {known:.6g} observed; accounting complete ({counted} calls)"
    if accounting == "unknown":
        return f"unknown: no call reported its cost ({block['calls']} calls)"
    return (
        f"at least USD {known:.6g} observed; accounting partial ({counted} calls with known cost)"
    )


def _value_summary(summary: dict[str, Any]) -> str:
    values = summary["value_summary"]
    if "rate" in values:
        return f"true {fraction(values['true'], values['true'] + values['false'])} of completed"
    if "mean" in values:
        if values["n"] == 0:
            return "no completed values"
        return (
            f"mean {_number(values['mean'])} (min {_number(values['min'])}, "
            f"max {_number(values['max'])}) across {values['n']} completed"
        )
    if "counts" in values:
        return ", ".join(f"{k}: {v}" for k, v in values["counts"].items()) or "none"
    return "reported per case (not aggregated)"


def _rule(rule: dict[str, Any] | None) -> str:
    if not rule:
        return "none (decisions indeterminate)"
    if rule["comparator"] == "is_true":
        return "pass if true"
    if rule["comparator"] == "in":
        return f"pass if in {', '.join(rule.get('categories') or [])}"
    return f"pass if value {rule['comparator']} {rule['threshold']}"


def _rows(report: dict[str, Any]) -> dict[str, Any]:
    """Everything the text formats show, as plain strings (not yet escaped)."""
    run = report["run"]
    provenance = [
        ("Run", run["run_id"]),
        ("Status", run["status"] + ("" if run["finished"] else " (not finished)")),
        (
            "Plan",
            f"{run['plan_id']} ({run['plan_hash']})"
            if run["plan_id"]
            else f"not a plan run (mode {run['mode'] or 'unknown'})",
        ),
        ("Dataset", run["dataset_hash"]),
        (
            "Application",
            (
                f"{run['application_id']} ({run['application_hash']}), runner {run['runner']}, "
                f"effects {run['effects']}, "
                f"revision {run['application_revision'] or 'not declared'}"
            ),
        ),
        ("Policy", run["policy_hash"] or "unknown"),
        ("Evaluator plugins", ", ".join(f"{k}: {v}" for k, v in run["plugins"].items()) or "none"),
        ("Repetitions", _number(run["repetitions"])),
        ("Seed", _number(run["seed"])),
        ("Environment", ", ".join(f"{k} {v}" for k, v in run["environment"].items())),
        ("Approved by", run["approved_by"] or "unknown"),
        ("Created", run["created_at"]),
        ("Report basis", report["basis"]),
        ("As of event", str(report["as_of_event_sequence"])),
    ]
    gates = [
        (
            g["gate_id"],
            f"metrics[{g['binding']}] {g['metric']}",
            "; ".join(
                part
                for part in (
                    f"pass rate >= {g['min_pass_rate']}" if g["min_pass_rate"] is not None else "",
                    (
                        f"completed coverage >= {g['min_completed_coverage']}"
                        if g["min_completed_coverage"] is not None
                        else ""
                    ),
                )
                if part
            ),
            (
                f"passes {fraction(g.get('passes'), g.get('selected'))}; completed "
                f"{fraction(g.get('completed'), g.get('selected'))}"
                if "selected" in g
                else "no results"
            ),
            g["status"].upper() + (f": {g['reason']}" if g.get("reason") else ""),
        )
        for g in report["gates"]
    ]
    passes = []
    for scoring in report["scoring_passes"]:
        metrics = []
        for m in scoring["metrics"]:
            s, p = m["summary"], m["profile"]
            decisions = s["decisions"]
            metrics.append(
                {
                    "metric": m["label"],
                    "profile": (
                        f"{p['value_kind']}, {p['direction']} is better, "
                        f"aggregation {p['aggregation']}; {_rule(p['rule'])}"
                        + (
                            f"; params {json.dumps(p['params'], sort_keys=True)}"
                            if p["params"]
                            else ""
                        )
                        + (
                            "; profile derived from results"
                            if p["source"] != "frozen_with_run"
                            else ""
                        )
                    ),
                    "selected": str(s["selected"]),
                    "completed": fraction(s["completed"], s["selected"]),
                    "decisions": (
                        f"pass {decisions.get('pass', 0)}, fail {decisions.get('fail', 0)}, "
                        f"indeterminate {decisions.get('indeterminate', 0)}, "
                        f"not evaluated {decisions.get('not_evaluated', 0)}"
                    ),
                    "pass_of_selected": fraction(decisions.get("pass", 0), s["selected"]),
                    "values": _value_summary(s),
                    "evaluator_errors": str(s["errors"])
                    + (
                        " ("
                        + ", ".join(f"{k}: {v}" for k, v in m["evaluator_failures"].items())
                        + ")"
                        if m["evaluator_failures"]
                        else ""
                    ),
                    "not_applicable": str(s["not_applicable"]),
                    "unavailable": str(s["unavailable"]),
                    "cancelled": str(s["cancelled"]),
                    "pending": str(s["pending"]),
                    "limitations": "; ".join(p["limitations"]),
                }
            )
        passes.append(
            {
                "title": f"{scoring['kind']} scoring pass {scoring['scoring_id']}",
                "basis": scoring["basis"],
                "cost": _usd(scoring["evaluator_cost"]),
                "metrics": metrics,
            }
        )
    app = report["application"]
    latency = app["latency"]
    application = [
        ("Planned executions", _number(app["planned"])),
        ("Recorded", _number(app["recorded"])),
        ("Completed", fraction(app["completed"], app["planned"] or app["recorded"])),
        (
            "Application failures",
            f"{app['failed']}"
            + (
                " (" + ", ".join(f"{k}: {v}" for k, v in app["error_kinds"].items()) + ")"
                if app["error_kinds"]
                else ""
            ),
        ),
        (
            "Attempts (incl. retries)",
            str(app["attempts"])
            + (
                f", plus {app['uncommitted_dispatches']} call(s) that may have reached the "
                "application while a session was lost (no recorded attempt; cost unknown)"
                if app.get("uncommitted_dispatches")
                else ""
            ),
        ),
        (
            "Successful-request latency",
            (
                f"p50 {_number(latency['p50_ms'], ' ms')}, "
                f"p95 {_number(latency['p95_ms'], ' ms')} "
                f"over {latency['successful_requests']} successful requests; excluded: "
                f"{latency['excluded_failures']} failed, "
                f"{latency['excluded_timeouts']} timed out; "
                f"concurrency {_number(latency['concurrency'])}"
            ),
        ),
        ("Latency definition", latency["definition"]),
        ("State between cases", _state_text(app.get("state") or {})),
    ]
    cost = report["cost"]
    costs = [
        ("Application", _usd(cost["application"])),
        (f"Evaluator (scoring pass {cost['evaluator_scoring_id']})", _usd(cost["evaluator"])),
        ("Planner", _usd(cost["planner"])),
    ]
    evidence = []
    for item in report["evidence"]["items"][:EVIDENCE_LIMIT]:
        execution = item["execution"]
        lines = []
        if execution is None:
            lines.append("application: no recorded execution")
        else:
            state = execution["status"] + (
                f" ({execution['error_kind']})" if execution["error_kind"] else ""
            )
            lines.append(
                f"application: {state}, attempt {execution['attempt']}, "
                f"wall {_number(execution['wall_ms'], ' ms')}, execution {execution['execution_id']}"
            )
            if execution["output_excerpt"] is not None:
                lines.append(f"output: {execution['output_excerpt']}")
            if execution["error_excerpt"] is not None:
                lines.append(f"error: {execution['error_excerpt']}")
            if execution.get("retrieved_context_excerpts") is not None:
                shown = execution["retrieved_context_excerpts"]
                lines.append(
                    f"retrieved ({len(shown)} of {execution['retrieved_context_items']}): "
                    + (" | ".join(shown) if shown else "nothing")
                )
        for r in item["results"]:
            value = "none" if r["value"] is None else json.dumps(r["value"])
            lines.append(
                f"{r['metric']}: {r['decision']} (status {r['status']}, value {value})"
                + (f", reason: {r['reason_excerpt']}" if r["reason_excerpt"] else "")
                + (f", raw artifact {r['raw_artifact']}" if r["raw_artifact"] else "")
                + (f", evidence {', '.join(r['evidence_refs'])}" if r["evidence_refs"] else "")
            )
        evidence.append((f"{item['case_id']} (repetition {item['repetition']})", lines))
    total = len(report["evidence"]["items"])
    return {
        "title": f"Benchmark report: {run['run_id']}",
        "banner": (
            None
            if not run["partial"]
            else "Partial snapshot."
            if run["provisional"]
            else "Partial results."
        ),
        "provenance": provenance,
        "gates": gates,
        "passes": passes,
        "application": application,
        "costs": costs,
        "cost_note": cost["note"],
        "evidence": evidence,
        "evidence_total": total,
        "evidence_note": report["evidence"]["note"]
        + (
            f"; showing {min(total, EVIDENCE_LIMIT)} of {total} items (all are in report.json)"
            if total > EVIDENCE_LIMIT
            else ""
        )
        + ("; case content withheld" if report["evidence"]["content"] == "withheld" else ""),
        "notes": report["notes"],
        "work": ", ".join(
            f"{kind}: " + ", ".join(f"{s} {n}" for s, n in sorted(states.items()))
            for kind, states in sorted(report["work"]["counts"].items())
        ),
    }


_METRIC_COLUMNS = (
    ("metric", "Metric"),
    ("selected", "Selected"),
    ("completed", "Completed"),
    ("decisions", "Decisions"),
    ("pass_of_selected", "Passes / selected"),
    ("values", "Values"),
    ("evaluator_errors", "Evaluator errors"),
    ("not_applicable", "Not applicable"),
    ("unavailable", "Unavailable (no app output)"),
    ("cancelled", "Cancelled"),
    ("pending", "Pending"),
)


# --------------------------------------------------------------------------- markdown

# With brackets escaped, no link or image can form, so parentheses need no escape; `&` is
# escaped so a literal entity in the data ("&amp;") is shown as written, not decoded.
_MD_SPECIAL = re.compile(r"([\\`*_\[\]<>|~&])")


def md(value: Any) -> str:
    """Sanitized Markdown text: metacharacters (including inline HTML) escaped, newlines
    folded, so data cannot inject markup, links or table cells."""
    text = sanitize(str(value)).replace("\r", " ").replace("\n", " ")
    return _MD_SPECIAL.sub(r"\\\1", text)


def _md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    out = ["| " + " | ".join(md(h) for h in headers) + " |", "|" + "---|" * len(headers)]
    out += ["| " + " | ".join(md(c) for c in row) + " |" for row in rows]
    return out


def _markdown(report: dict[str, Any]) -> str:
    r = _rows(report)
    lines = [f"# {md(r['title'])}", ""]
    if r["banner"]:
        lines += [f"> **{r['banner']}** " + md(r["notes"][0]), ""]
    lines += ["## Provenance", ""] + _md_table(
        ["Item", "Value"], [list(p) for p in r["provenance"]]
    )
    lines += ["", f"Work items: {md(r['work'])}", ""]
    lines += ["## Release gates", ""]
    if r["gates"]:
        lines += _md_table(
            ["Gate", "Binding", "Threshold", "Observed", "Status"], [list(g) for g in r["gates"]]
        )
    else:
        lines.append("No release gates were declared in the plan.")
    for scoring in r["passes"]:
        lines += ["", f"## Metrics: {md(scoring['title'])}", "", f"Basis: {md(scoring['basis'])}."]
        lines += [f"Evaluator cost: {md(scoring['cost'])}.", ""]
        lines += _md_table(
            [h for _, h in _METRIC_COLUMNS],
            [[m[k] for k, _ in _METRIC_COLUMNS] for m in scoring["metrics"]],
        )
        lines.append("")
        for m in scoring["metrics"]:
            lines.append(f"- **{md(m['metric'])}**: {md(m['profile'])}")
            if m["limitations"]:
                lines.append(f"  Limitations: {md(m['limitations'])}")
    lines += ["", "## Application", ""] + _md_table(
        ["Item", "Value"], [list(a) for a in r["application"]]
    )
    lines += ["", "## Cost", ""] + _md_table(["Role", "Observed"], [list(c) for c in r["costs"]])
    lines += ["", md(r["cost_note"]), "", "## Case evidence", "", md(r["evidence_note"]), ""]
    if not r["evidence"]:
        lines.append("No failed, errored or indeterminate cases.")
    for title, detail in r["evidence"]:
        lines.append(f"### {md(title)}")
        lines += [f"- {md(line)}" for line in detail]
        lines.append("")
    if r["notes"]:
        lines += ["## Notes", ""] + [f"- {md(n)}" for n in r["notes"]]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- html


def h(value: Any) -> str:
    """Sanitized, HTML-escaped text (quotes included, so it is safe in attributes too)."""
    return html.escape(sanitize(str(value)), quote=True)


def _html_table(headers: list[str], rows: list[list[str]]) -> str:
    head = "".join(f"<th>{h(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{h(c)}</td>" for c in row) + "</tr>" for row in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


_CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)
_CSS = """
:root{--bg:#fff;--fg:#1d1d1f;--muted:#5f6368;--line:#d9dce1;--warn:#fff4d6;--fail:#b3261e;--pass:#1e7a3c}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8eaed;--muted:#a0a4ab;--line:#3a3d43;--warn:#3b3220;--fail:#f28b82;--pass:#81c995}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0 auto;max-width:1100px;padding:16px}
h1{font-size:1.5rem}h2{font-size:1.2rem;margin-top:2rem;border-bottom:1px solid var(--line)}
table{border-collapse:collapse;width:100%;margin:.5rem 0;display:block;overflow-x:auto}
th,td{border:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top}
th{background:rgba(127,127,127,.08)}.muted{color:var(--muted)}
.banner{background:var(--warn);padding:8px 12px;border-radius:6px}
.FAIL{color:var(--fail);font-weight:600}.PASS{color:var(--pass);font-weight:600}
code,pre{font-family:ui-monospace,monospace;white-space:pre-wrap;word-break:break-word}
"""


def _html(report: dict[str, Any]) -> str:
    r = _rows(report)
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        # Static evidence: no scripts, no remote resources, no forms, no framing.
        f'<meta http-equiv="Content-Security-Policy" content="{_CSP}">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{h(r['title'])}</title><style>{_CSS}</style></head><body>",
        f"<h1>{h(r['title'])}</h1>",
    ]
    if r["banner"]:
        parts.append(f'<p class="banner"><strong>{h(r["banner"])}</strong> {h(r["notes"][0])}</p>')
    parts.append("<h2>Provenance</h2>")
    parts.append(_html_table(["Item", "Value"], [list(p) for p in r["provenance"]]))
    parts.append(f'<p class="muted">Work items: {h(r["work"])}</p>')
    parts.append("<h2>Release gates</h2>")
    if r["gates"]:
        head = "".join(
            f"<th>{h(x)}</th>" for x in ("Gate", "Binding", "Threshold", "Observed", "Status")
        )
        body = "".join(
            "<tr>"
            + "".join(f"<td>{h(c)}</td>" for c in g[:4])
            + f'<td class="{h(g[4].split(":")[0])}">{h(g[4])}</td></tr>'
            for g in r["gates"]
        )
        parts.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
    else:
        parts.append("<p>No release gates were declared in the plan.</p>")
    for scoring in r["passes"]:
        parts.append(f"<h2>Metrics: {h(scoring['title'])}</h2>")
        parts.append(
            f'<p class="muted">Basis: {h(scoring["basis"])}. Evaluator cost: {h(scoring["cost"])}.</p>'
        )
        parts.append(
            _html_table(
                [x for _, x in _METRIC_COLUMNS],
                [[m[k] for k, _ in _METRIC_COLUMNS] for m in scoring["metrics"]],
            )
        )
        parts.append("<ul>")
        for m in scoring["metrics"]:
            limits = (
                f'<br><span class="muted">Limitations: {h(m["limitations"])}</span>'
                if m["limitations"]
                else ""
            )
            parts.append(f"<li><strong>{h(m['metric'])}</strong>: {h(m['profile'])}{limits}</li>")
        parts.append("</ul>")
    parts.append("<h2>Application</h2>")
    parts.append(_html_table(["Item", "Value"], [list(a) for a in r["application"]]))
    parts.append("<h2>Cost</h2>")
    parts.append(_html_table(["Role", "Observed"], [list(c) for c in r["costs"]]))
    parts.append(f'<p class="muted">{h(r["cost_note"])}</p>')
    parts.append("<h2>Case evidence</h2>")
    parts.append(f'<p class="muted">{h(r["evidence_note"])}</p>')
    if not r["evidence"]:
        parts.append("<p>No failed, errored or indeterminate cases.</p>")
    for title, detail in r["evidence"]:
        items = "".join(f"<li><code>{h(line)}</code></li>" for line in detail)
        parts.append(f"<h3>{h(title)}</h3><ul>{items}</ul>")
    if r["notes"]:
        parts.append(
            "<h2>Notes</h2><ul>" + "".join(f"<li>{h(n)}</li>" for n in r["notes"]) + "</ul>"
        )
    parts.append("</body></html>")
    return "\n".join(parts) + "\n"
