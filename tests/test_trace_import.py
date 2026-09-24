"""OpenTelemetry trace import (16-T2, 16-G2), end to end: `aibench run` against an
application that exports spans, then `aibench traces import` and the report.

The fixture (`examples/apps/traced_app.py`) repeats its model calls' usage on a parent
span, exports one trace unsampled and one without its root span."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from aibench.cli.main import app
from aibench.observations.otel import TraceFormatError, normalize, parse_otlp

cli = CliRunner()
# The fixture's two model calls: 12+5 and 20+9 tokens; its agent span repeats the total.
CALL_TOKENS = (12 + 5) + (20 + 9)


@pytest.fixture
def traced(tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    from tests.runner_support import load_example

    trace_file = tmp_path / "traces.jsonl"
    server = load_example("traced_app").make_server(trace_file, port=0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    (tmp_path / "app.json").write_text(
        json.dumps(
            {
                "application_id": "traced-app",
                "runner": "http",
                "target": f"{base}/answer",
                "transport": {"kind": "http", "url": f"{base}/answer"},
                "input_binding": {"fields": {"/input": "/input"}},
                "output_binding": {"output": "/answer"},
            }
        ),
        encoding="utf-8",
    )
    questions = ["refund policy", "shipping", "sampled out please", "lost root please"]
    (tmp_path / "data.jsonl").write_text(
        "".join(
            json.dumps({"case_id": f"c{i}", "input": q}) + "\n" for i, q in enumerate(questions)
        ),
        encoding="utf-8",
    )
    (tmp_path / "plan.json").write_text(
        json.dumps({"plan_id": "traced", "dataset": "data.jsonl", "application": "app.json",
                    "metrics": [{"metric": "native.json_schema",
                                 "params": {"schema": {"type": "string"}, "parse_text": False}}]}),
        encoding="utf-8",
    )  # fmt: skip
    try:
        yield tmp_path, trace_file
    finally:
        server.shutdown()
        server.server_close()


def _cli(*args: str, code: int = 0) -> Any:
    result = cli.invoke(app, list(args))
    assert result.exit_code == code, result.output
    return json.loads(result.stdout) if "--json" in args and code == 0 else result


def test_traces_attach_to_executions_and_partial_traces_stay_partial(
    traced: tuple[Path, Path],
) -> None:
    root, trace_file = traced
    ws = ["--workspace", str(root)]
    run = _cli("run", "--plan", str(root / "plan.json"), "--json", *ws)
    run_id = run["run_id"]

    summary = _cli("traces", "import", run_id, str(trace_file), "--json", *ws)
    assert (summary["traces"], summary["matched"], summary["unmatched"]) == (4, 4, 0)
    assert summary["partial"] == 2
    assert summary["partial_reasons"] == {"missing_parent": 1, "no_root_span": 1, "not_sampled": 1}

    shown = _cli("traces", "show", run_id, "--json", *ws)
    usage = shown["usage"]
    # Every trace counts its two model calls once; the agent span's repeat is excluded.
    assert usage["total_tokens"] == 4 * CALL_TOKENS
    assert usage["aggregate_spans_excluded"] == 4
    assert usage["bound"] == "lower_bound"  # two traces are partial
    assert (shown["complete"], shown["partial"], shown["tool_spans"]) == (2, 2, 4)

    again = _cli("traces", "import", run_id, str(trace_file), "--json", *ws)
    assert again["added"] == 0  # the same file adds nothing

    report = _cli("report", run_id, "--format", "markdown", "--out", "-", *ws)
    assert "4 trace(s), 4 matched to executions; 2 complete, 2 partial" in report.stdout
    assert (
        f"{4 * CALL_TOKENS} tokens from traces (lower bound, 4 aggregate span(s)" in report.stdout
    )


def test_an_unmatched_trace_is_kept_but_attached_to_nothing(
    traced: tuple[Path, Path], tmp_path: Path
) -> None:
    root, trace_file = traced
    ws = ["--workspace", str(root)]
    run_id = _cli("run", "--plan", str(root / "plan.json"), "--json", *ws)["run_id"]
    foreign = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[0])
    for span in foreign["resourceSpans"][0]["scopeSpans"][0]["spans"]:
        span["traceId"] = "f" * 32
        span["attributes"] = [
            a for a in span["attributes"] if a["key"] != "http.request.header.x-request-id"
        ]
    other = tmp_path / "foreign.json"
    other.write_text(json.dumps(foreign), encoding="utf-8")
    summary = _cli("traces", "import", run_id, str(other), "--json", *ws)
    assert (summary["traces"], summary["matched"], summary["unmatched"]) == (1, 0, 1)


def test_a_file_that_is_not_a_trace_export_is_refused(traced: tuple[Path, Path]) -> None:
    root, _ = traced
    ws = ["--workspace", str(root)]
    run_id = _cli("run", "--plan", str(root / "plan.json"), "--json", *ws)["run_id"]
    bad = root / "bad.json"
    bad.write_text('{"spans": []}', encoding="utf-8")
    result = _cli("traces", "import", run_id, str(bad), *ws, code=2)
    assert "resourceSpans" in result.output


def _document(spans: list[dict[str, Any]]) -> bytes:
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}).encode()


def _span(sid: str, parent: str | None = None, *, inp: int = 0, out: int = 0) -> dict[str, Any]:
    attributes = []
    if inp:
        attributes.append({"key": "gen_ai.usage.input_tokens", "value": {"intValue": str(inp)}})
    if out:
        attributes.append({"key": "gen_ai.usage.output_tokens", "value": {"intValue": str(out)}})
    span: dict[str, Any] = {
        "traceId": "a" * 32,
        "spanId": sid,
        "name": sid,
        "attributes": attributes,
    }
    if parent:
        span["parentSpanId"] = parent
    return span


def test_nested_aggregates_count_only_the_lowest_usage() -> None:
    """Three levels all reporting usage: only the leaves count."""
    [trace] = parse_otlp(
        _document(
            [
                _span("root", None, inp=100),
                _span("mid", "root", inp=60),
                _span("leaf1", "mid", inp=25),
                _span("leaf2", "mid", inp=35),
                _span("side", "root", inp=40),
            ]
        )
    )
    usage = normalize(trace)["usage"]
    assert usage["input_tokens"] == 25 + 35 + 40
    assert sorted(usage["aggregate_spans_excluded"]) == ["mid", "root"]
    assert usage["bound"] == "complete"


def test_a_trace_without_its_parent_is_partial_and_its_usage_a_lower_bound() -> None:
    [trace] = parse_otlp(_document([_span("child", "gone", out=7)]))
    observation = normalize(trace)
    assert observation["partial_reasons"] == ["missing_parent:1", "no_root_span"]
    assert observation["usage"]["bound"] == "lower_bound"


def test_json_lines_exports_and_malformed_files() -> None:
    lines = b"\n".join([_document([_span("a")]), _document([_span("b", "a")])])
    [trace] = parse_otlp(lines)
    assert {s.span_id for s in trace.spans} == {"a", "b"}
    with pytest.raises(TraceFormatError):
        parse_otlp(b"not json at all")


def test_an_appended_export_adds_only_the_new_traces_and_never_recounts(
    traced: tuple[Path, Path], tmp_path: Path
) -> None:
    """File exporters append: importing the grown file must not count the first traces
    twice (review finding)."""
    root, trace_file = traced
    ws = ["--workspace", str(root)]
    run_id = _cli("run", "--plan", str(root / "plan.json"), "--json", *ws)["run_id"]
    lines = trace_file.read_text(encoding="utf-8").splitlines()
    early = tmp_path / "early.jsonl"
    early.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
    assert _cli("traces", "import", run_id, str(early), "--json", *ws)["added"] == 2
    grown = _cli("traces", "import", run_id, str(trace_file), "--json", *ws)
    assert grown["added"] == 2 and grown["merged_with_earlier_imports"] == 0
    shown = _cli("traces", "show", run_id, "--json", *ws)
    assert shown["traces"] == 4 and shown["imports"] == 2
    assert shown["usage"]["total_tokens"] == 4 * CALL_TOKENS


def test_a_trace_split_across_files_is_merged_not_added_up(
    traced: tuple[Path, Path], tmp_path: Path
) -> None:
    """A trace exported in two batches: the halves are merged and normalized as one trace,
    so the parent's repeat of its children's usage is still excluded, and the trace is
    complete only once all of it has arrived."""
    root, trace_file = traced
    ws = ["--workspace", str(root)]
    run_id = _cli("run", "--plan", str(root / "plan.json"), "--json", *ws)["run_id"]
    document = json.loads(trace_file.read_text(encoding="utf-8").splitlines()[0])
    spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
    [whole] = parse_otlp(json.dumps(document).encode())
    expected = normalize(whole)
    assert not expected["partial_reasons"]
    ids = {s["spanId"] for s in spans}
    children = [s for s in spans if s.get("parentSpanId") in ids]
    parents = [s for s in spans if s not in children]
    for name, part in (("children.json", children), ("parents.json", parents)):
        piece = json.loads(json.dumps(document))
        piece["resourceSpans"][0]["scopeSpans"][0]["spans"] = part
        (tmp_path / name).write_text(json.dumps(piece), encoding="utf-8")

    first = _cli("traces", "import", run_id, str(tmp_path / "children.json"), "--json", *ws)
    assert first["partial"] == 1  # the parents have not arrived yet
    second = _cli("traces", "import", run_id, str(tmp_path / "parents.json"), "--json", *ws)
    assert second["merged_with_earlier_imports"] == 1 and second["partial"] == 0
    shown = _cli("traces", "show", run_id, "--json", *ws)
    assert (shown["traces"], shown["complete"]) == (1, 1)
    assert shown["usage"]["total_tokens"] == expected["usage"]["total_tokens"] == CALL_TOKENS
    assert shown["usage"]["bound"] == "complete"
    again = _cli("traces", "import", run_id, str(tmp_path / "parents.json"), "--json", *ws)
    assert again["added"] == 0


def test_duplicate_conflicting_and_cyclic_spans() -> None:
    # An exporter retry repeats a span exactly: counted once, the trace stays complete.
    [trace] = parse_otlp(_document([_span("r"), _span("a", "r", inp=7), _span("a", "r", inp=7)]))
    observation = normalize(trace)
    assert observation["usage"]["input_tokens"] == 7 and not observation["partial_reasons"]
    assert observation["duplicate_spans_ignored"] == 1
    # Two different spans with one ID: the trace is partial, the first one kept.
    [trace] = parse_otlp(_document([_span("r"), _span("a", "r", inp=7), _span("a", "r", inp=9)]))
    observation = normalize(trace)
    assert observation["partial_reasons"] == ["conflicting_span_id:1"]
    assert observation["usage"] == {**observation["usage"], "input_tokens": 7,
                                    "bound": "lower_bound"}  # fmt: skip
    # Parent links in a loop used to hang normalization: now partial, loop usage left out.
    [trace] = parse_otlp(
        _document(
            [_span("r"), _span("s", "r", inp=5), _span("d", "e", inp=3), _span("e", "d", inp=4)]
        )
    )
    observation = normalize(trace)
    assert observation["partial_reasons"] == ["parent_cycle:2"]
    assert observation["usage"]["input_tokens"] == 5
    assert sorted(observation["usage"]["unplaced_spans_excluded"]) == ["d", "e"]
    # A span that is its own parent is not silently dropped: the trace says it is partial.
    [trace] = parse_otlp(_document([_span("r"), _span("x", "x", inp=11)]))
    observation = normalize(trace)
    assert observation["partial_reasons"] == ["parent_cycle:1"]
    assert observation["usage"] is None
