"""Offline report aggregation and resource-cap audit probes."""
from __future__ import annotations

import json

import httpx

import audit_probes as audit


def main() -> None:
    from aibench.core.models import EvaluationResult, EvaluatorManifest, MetricValue
    from aibench.reporting.aggregation import summarize
    from aibench.connectors import langfuse

    audit.RESULTS = json.loads((audit.OUT / "probes.json").read_text(encoding="utf-8"))
    manifest = EvaluatorManifest(evaluator_id="audit.scalar", version="1.0.0", plugin_id="audit", plugin_version="1.0.0", description="Macro averaging probe", value_kind="scalar", direction="higher", aggregation="mean")
    rows = [EvaluationResult(result_id=f"r-{case}-{repeat}", run_id="audit", case_id=case, repetition_id=repeat, metric_id="audit.scalar", metric_version="1.0.0", status="ok", value=MetricValue(kind="scalar", value=value), decision="pass") for case, repeat, value in [("a", 0, 0.0), ("a", 1, 0.0), ("b", 0, 1.0)]]
    summary = summarize(rows, manifest=manifest, binding_hash="audit", planned=4)
    audit.probe("report_macro_averaging", {"values": [{"case": row.case_id, "repetition": row.repetition_id, "value": row.value.value} for row in rows], "reported_mean": summary.value_summary["mean"], "case_macro_mean": 0.5, "pair_weighted_mean": 1/3, "pending": summary.pending})

    # A tiny cap with a benign larger response shows whether reading stops at the cap.
    read = []
    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(10):
                read.append(100)
                yield b"x" * 100
    transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream()))
    original = langfuse.MAX_RESPONSE_BYTES
    langfuse.MAX_RESPONSE_BYTES = 100
    client = object.__new__(langfuse.LangfuseClient)
    client.client = httpx.Client(base_url="http://127.0.0.1/", transport=transport)
    try:
        error = None
        try:
            client._request("GET", "audit")
        except langfuse.ConnectorError as exc:
            error = str(exc)
        audit.probe("langfuse_response_cap", {"declared_cap": 100, "bytes_read_before_rejection": sum(read), "error": error})
    finally:
        client.client.close()
        langfuse.MAX_RESPONSE_BYTES = original


if __name__ == "__main__":
    main()
