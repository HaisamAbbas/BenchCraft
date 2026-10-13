"""Langfuse connector: dataset import, trace import and score export (§9, §18, 17-T3).

Selected as the default platform connector (no stated preference; recorded as a
reversible choice in ADR 0017). It moves data in and out; it never computes a metric.
Metric evaluation stays with the harness's evaluators, and an export carries results the
harness already recorded.

The HTTP contract is Langfuse's public API, checked against the generated client of
`langfuse==4.15.6`. It uses only endpoints that stay in Langfuse v4; the v3 trace,
dataset-run and score-list endpoints are deprecated on Langfuse Cloud:

- `GET /api/public/v2/datasets/{name}`: the dataset;
- `GET /api/public/dataset-items?datasetName=&page=&limit=`: items, paged by page number
  (`meta.totalPages`);
- `GET /api/public/v2/observations?traceId=&fields=&cursor=`: observations, paged by cursor
  (`meta.cursor`);
- `POST /api/public/scores`: create a score (with our own `id`);
- `GET /api/public/v3/scores?id=`: read a score back.

Authentication is HTTP Basic with the project's public and secret keys, given as secret
references. Every request goes to one policy-approved origin (`allowed_egress_origins`).
Redirects are never followed and proxies from the environment are ignored.

Round trip (17-G4):
- an imported item becomes a case whose `extensions["langfuse.dataset_item"]` and
  provenance keep the item's ID, dataset and version timestamp;
- executions are matched to Langfuse traces by trace ID = the execution's correlation ID
  (the `X-Request-ID` the HTTP runner sends);
- an exported score names the trace and carries the harness run, result, case, metric and
  dataset item IDs in its metadata;
- a score is created only after a read-back shows it does not exist, and verified by a
  second read-back afterwards: creating a score with the same ID twice is not documented
  as idempotent, so it is never relied on.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from aibench.core.errors import AibenchError
from aibench.core.models import (
    BenchmarkCase,
    EvaluationResult,
    ExecutionStatus,
    RedactionClass,
    deep_unfreeze,
)
from aibench.datasets.ingest import ingest_dataset
from aibench.observations.otel import Span, Trace, normalize
from aibench.security.endpoints import origin_of
from aibench.security.http import (
    BOUNDED_ACCEPT_ENCODING,
    ResponseEncodingError,
    ResponseTooLarge,
    read_limited_response,
)
from aibench.security.policy import ExecutionPolicy, egress_denials
from aibench.security.secrets import Redactor, resolve_secret
from aibench.services.scoring import select_final_executions
from aibench.storage.artifacts import ArtifactStore, commit_verified_artifact
from aibench.storage.repositories import Storage

CONTRACT = "langfuse public API, v4 endpoints (shapes from langfuse==4.15.6)"
NORMALIZATION = "langfuse-observations/1"
ITEM_EXTENSION = "langfuse.dataset_item"
HOST_EXTENSION = "langfuse.host"
SCORE_NAMESPACE = uuid.UUID("0b8f6a57-1a3e-4d6c-9f1e-6a2d6c1b7e21")
PAGE_LIMIT = 50
MAX_PAGES = 1_000
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
_OBSERVATION_FIELDS = "core,basic,time,usage,model,metadata"


class ConnectorError(AibenchError):
    """The connector could not complete; the message says what happened remotely."""


class ConnectorRefused(ConnectorError):
    def __init__(self, denials: Sequence[str]) -> None:
        super().__init__("; ".join(denials))
        self.denials = list(denials)


@dataclass(frozen=True)
class LangfuseConfig:
    host: str
    public_key: str = "env:LANGFUSE_PUBLIC_KEY"  # secret references, never keys
    secret_key: str = "env:LANGFUSE_SECRET_KEY"
    timeout_seconds: float = 30.0


def denials(policy: ExecutionPolicy, config: LangfuseConfig, sends: str) -> list[str]:
    found = egress_denials(policy, config.host, sends=sends, secret_ref=config.public_key)
    if config.secret_key not in policy.allowed_secret_refs:
        found.append(f"secret {config.secret_key} is not allowed by the policy")
    return found


class LangfuseClient:
    def __init__(
        self,
        config: LangfuseConfig,
        environ: Mapping[str, str],
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        public = resolve_secret(config.public_key, environ)
        secret = resolve_secret(config.secret_key, environ)
        self.redactor = Redactor([(config.public_key, public), (config.secret_key, secret)])
        self.origin = origin_of(config.host)
        self.client = httpx.Client(
            base_url=config.host.rstrip("/") + "/",
            auth=(public, secret),
            follow_redirects=False,
            trust_env=False,
            timeout=config.timeout_seconds,
            headers={"Accept-Encoding": BOUNDED_ACCEPT_ENCODING},
            transport=transport,
        )

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            with self.client.stream(method, path, **kwargs) as response:
                raw = read_limited_response(response, MAX_RESPONSE_BYTES)
                status = response.status_code
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"Langfuse {method} {path}: timed out (outcome unknown)") from exc
        except httpx.HTTPError as exc:
            raise ConnectorError(self.redactor.text(f"Langfuse {method} {path}: {exc}")) from exc
        except ResponseTooLarge as exc:
            raise ConnectorError(
                f"Langfuse {method} {path}: response over {MAX_RESPONSE_BYTES} bytes"
            ) from exc
        except ResponseEncodingError as exc:
            raise ConnectorError(
                f"Langfuse {method} {path}: response could not be decoded"
            ) from exc
        if status >= 300:
            detail = self.redactor.text(raw.decode("utf-8", errors="replace")[:300])
            raise ConnectorError(f"Langfuse {method} {path}: HTTP {status}: {detail}")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise ConnectorError(f"Langfuse {method} {path}: the response is not JSON") from exc

    def dataset(self, name: str) -> dict[str, Any]:
        return dict(self._request("GET", f"api/public/v2/datasets/{quote(name, safe='')}"))

    def dataset_items(self, name: str) -> Iterator[dict[str, Any]]:
        page = 1
        while page <= MAX_PAGES:
            body = self._request(
                "GET",
                "api/public/dataset-items",
                params={"datasetName": name, "page": page, "limit": PAGE_LIMIT},
            )
            yield from body.get("data") or []
            if page >= int((body.get("meta") or {}).get("totalPages") or 0):
                return
            page += 1
        raise ConnectorError(f"dataset {name!r} has more than {MAX_PAGES} pages")

    def observations(self, trace_id: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        cursor = None
        for _ in range(MAX_PAGES):
            params = {"traceId": trace_id, "fields": _OBSERVATION_FIELDS, "limit": PAGE_LIMIT}
            if cursor:
                params["cursor"] = cursor
            body = self._request("GET", "api/public/v2/observations", params=params)
            found.extend(body.get("data") or [])
            cursor = (body.get("meta") or {}).get("cursor")
            if not cursor:
                return found
        raise ConnectorError(f"trace {trace_id} has more than {MAX_PAGES} pages of observations")

    def score(self, score_id: str) -> dict[str, Any] | None:
        body = self._request("GET", "api/public/v3/scores", params={"id": score_id, "limit": 1})
        matches = [s for s in body.get("data") or [] if s.get("id") == score_id]
        return matches[0] if matches else None

    def create_score(self, body: Mapping[str, Any]) -> dict[str, Any]:
        return dict(self._request("POST", "api/public/scores", json=dict(body)))


# ---------------------------------------------------------------- dataset import


def import_dataset(
    config: LangfuseConfig,
    dataset_name: str,
    out: Path,
    *,
    policy: ExecutionPolicy,
    environ: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Write a Langfuse dataset's active items as an aibench JSONL dataset, each case
    keeping the item's identity and version (nothing is sent but the credentials)."""
    refused = denials(policy, config, "your Langfuse credentials")
    if refused:
        raise ConnectorRefused(refused)
    client = LangfuseClient(config, environ, transport=transport)
    try:
        dataset = client.dataset(dataset_name)
        items = list(client.dataset_items(dataset_name))
    finally:
        client.close()
    rows, skipped = [], Counter[str]()
    for item in items:
        if item.get("status") != "ACTIVE":
            skipped["archived"] += 1
            continue
        if item.get("input") is None:
            skipped["no_input"] += 1
            continue
        rows.append(_case_row(item, dataset, client.origin))
    if not rows:
        raise ConnectorError(f"dataset {dataset_name!r} has no active items with an input")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )
    report = ingest_dataset(out, dataset_id=f"langfuse:{dataset_name}")
    if not report.is_valid or report.manifest is None:
        raise ConnectorError(f"the imported items are not a valid dataset: {report.errors[:3]}")
    return {
        "dataset": dataset_name,
        "dataset_id": dataset.get("id"),
        "host": client.origin,
        "file": str(out),
        "items": len(items),
        "imported": len(rows),
        "skipped": dict(sorted(skipped.items())),
        "content_hash": report.manifest.content_hash,
        "contract": CONTRACT,
    }


def _case_row(item: Mapping[str, Any], dataset: Mapping[str, Any], origin: str) -> dict[str, Any]:
    expected = item.get("expectedOutput")
    identity = {
        key: item.get(key)
        for key in ("id", "datasetId", "datasetName", "sourceTraceId", "sourceObservationId",
                    "createdAt", "updatedAt", "status")
    }  # fmt: skip
    row: dict[str, Any] = {
        "case_id": str(item["id"]),
        "input": item["input"],
        "extensions": {ITEM_EXTENSION: identity, HOST_EXTENSION: origin},
        "provenance": {
            "source_refs": [
                (
                    f"langfuse:{origin}datasets/{dataset.get('name')}/items/{item['id']}"
                    f"@{item.get('updatedAt')}"
                )
            ]
        },
    }
    if isinstance(expected, str):
        row["reference"] = {"answer": expected}
    elif expected is not None:
        # A structured expected output is kept as it is, not flattened into an answer.
        row["extensions"]["langfuse.expected_output"] = expected
    if isinstance(item.get("metadata"), dict):
        row["metadata"] = item["metadata"]
    return row


# ---------------------------------------------------------------- trace import


def import_traces(
    storage: Storage,
    artifacts: ArtifactStore,
    run_id: str,
    config: LangfuseConfig,
    *,
    policy: ExecutionPolicy,
    environ: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Attach the Langfuse trace of each of the run's executions (trace ID = the
    execution's correlation ID), normalized like OpenTelemetry traces: usage from the
    lowest observations only, partial traces kept partial."""
    refused = denials(policy, config, "your Langfuse credentials and the run's trace IDs")
    if refused:
        raise ConnectorRefused(refused)
    if storage.get_run(run_id) is None:
        raise ConnectorError(f"no run committed with run_id={run_id!r}")
    executions = [
        e for e in select_final_executions(storage.list_execution_attempts(run_id))
        if e.correlation_id
    ]  # fmt: skip
    client = LangfuseClient(config, environ, transport=transport)
    fetched: dict[str, list[dict[str, Any]]] = {}
    try:
        for execution in executions:
            assert execution.correlation_id is not None
            fetched[execution.execution_id] = client.observations(execution.correlation_id)
    finally:
        client.close()
    import_id = f"langfuse-{uuid.uuid4().hex[:16]}"
    raw = artifacts.write_bytes(
        json.dumps(fetched, sort_keys=True).encode("utf-8"),
        mime_type="application/json",
        run_id=run_id,
        redaction=RedactionClass.RESTRICTED,
        artifact_id=f"{import_id}:raw",
    )
    commit_verified_artifact(artifacts, storage, raw)
    rows: list[tuple[str, str | None, bool, str]] = []
    reasons: Counter[str] = Counter()
    matched = 0
    for execution in executions:
        observations = fetched[execution.execution_id]
        if not observations:
            continue
        matched += 1
        observation = normalize(_trace(execution.correlation_id or "", observations))
        observation.update(
            normalization=NORMALIZATION,
            source="langfuse",
            host=client.origin,
            raw_artifact_ids=[raw.artifact_id],
            correlation_id=execution.correlation_id,
        )
        for reason in observation["partial_reasons"]:
            reasons[reason.split(":", 1)[0]] += 1
        rows.append(
            (
                f"langfuse:{execution.correlation_id}",
                execution.execution_id,
                not observation["partial_reasons"],
                json.dumps(observation),
            )
        )
    if rows:
        storage.commit_trace_observations(import_id, run_id, rows)
    summary = {
        "import_id": import_id,
        "source": "langfuse",
        "host": client.origin,
        "executions": len(executions),
        "traces": matched,
        "without_trace": len(executions) - matched,
        "partial": sum(1 for r in rows if not r[2]),
        "partial_reasons": dict(sorted(reasons.items())),
        "raw_artifact_id": raw.artifact_id,
    }
    storage.append_run_event(run_id, "langfuse_traces_imported", summary)
    return summary


def _trace(trace_id: str, observations: Sequence[Mapping[str, Any]]) -> Trace:
    """Langfuse observations as spans, so the OpenTelemetry rules apply unchanged:
    `usageDetails` input/output become gen_ai usage attributes."""
    trace = Trace(trace_id)
    for o in observations:
        usage = o.get("usageDetails") or {}
        attributes: dict[str, Any] = {}
        if "input" in usage:
            attributes["gen_ai.usage.input_tokens"] = usage["input"]
        if "output" in usage:
            attributes["gen_ai.usage.output_tokens"] = usage["output"]
        if o.get("model"):
            attributes["gen_ai.response.model"] = o["model"]
        if o.get("type") == "TOOL":
            attributes["gen_ai.tool.name"] = o.get("name") or "tool"
        trace.add(
            Span(
                trace_id=trace_id,
                span_id=str(o.get("id")),
                parent_span_id=o.get("parentObservationId") or None,
                name=str(o.get("name") or ""),
                attributes=attributes,
                start_ns=None,
                end_ns=None,
                sampled=None,
                dropped=0,
                error=o.get("level") == "ERROR",
            )
        )
    return trace


# ---------------------------------------------------------------- score export


def score_id(run_id: str, result_id: str) -> str:
    return str(uuid.uuid5(SCORE_NAMESPACE, f"{run_id}/{result_id}"))


def export_scores(
    storage: Storage,
    run_id: str,
    config: LangfuseConfig,
    *,
    policy: ExecutionPolicy,
    environ: Mapping[str, str],
    include_reasons: bool = False,
    scoring_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Export the run's recorded results as Langfuse scores on the matching traces: for each
    case, metric and binding, the result of the latest scoring pass (or of `scoring_id`),
    so a rescored run never puts two same-named scores on one trace."""
    sends = "metric results (values, metric identity, case and dataset item IDs)"
    if include_reasons:
        sends += " and evaluator reasons"
    refused = denials(policy, config, sends)
    if refused:
        raise ConnectorRefused(refused)
    record = storage.get_run(run_id)
    if record is None:
        raise ConnectorError(f"no run committed with run_id={run_id!r}")
    cases = {c.case_id: c for c in storage.list_cases(record.manifest.dataset_hash)}
    executions = {e.execution_id: e for e in storage.list_execution_attempts(run_id)}
    host = origin_of(config.host)
    traced = {
        o["execution_id"]
        for o in storage.list_trace_observations(run_id)
        if o.get("source") == "langfuse" and o.get("execution_id") and o.get("host") == host
    }
    planned, skipped = [], Counter[str]()
    for result in _selected_results(storage, run_id, scoring_id):
        reason = _unexportable(result, cases, executions, traced, host)
        if reason:
            skipped[reason] += 1
            continue
        planned.append(
            _score_body(run_id, result, cases[result.case_id], executions, include_reasons)
        )
    storage.append_run_event(
        run_id,
        "langfuse_export_prepared",
        {"host": origin_of(config.host), "scores": [b["id"] for b in planned],
         "skipped": dict(skipped)},
    )  # fmt: skip
    client = LangfuseClient(config, environ, transport=transport)
    created: list[str] = []
    existing: list[str] = []
    conflicts: list[str] = []
    failed: dict[str, str] = {}
    try:
        for body in planned:
            try:
                found = client.score(body["id"])
                if found is not None:
                    (existing if _same(found, body) else conflicts).append(body["id"])
                    continue
                try:
                    client.create_score(body)
                except ConnectorError:
                    # The outcome may be unknown (a timeout, a 5xx): look before deciding.
                    if client.score(body["id"]) is None:
                        raise
                back = client.score(body["id"])
                if back is None or not _same(back, body):
                    failed[body["id"]] = "created, but the read-back does not match"
                    continue
                created.append(body["id"])
            except ConnectorError as exc:
                failed[body["id"]] = str(exc)[:300]
    finally:
        client.close()
    summary = {
        "host": origin_of(config.host),
        "planned": len(planned),
        "created": len(created),
        "already_present": len(existing),
        "conflicts": conflicts,
        "failed": failed,
        "skipped": dict(sorted(skipped.items())),
        "score_ids": [b["id"] for b in planned],
    }
    storage.append_run_event(run_id, "langfuse_scores_exported", summary)
    return summary


def _selected_results(
    storage: Storage, run_id: str, scoring_id: str | None
) -> list[EvaluationResult]:
    """One result per (case, repetition, metric, binding): from `scoring_id` when given,
    otherwise from the most recent scoring pass that produced one."""
    results = storage.list_metric_results(run_id)
    if scoring_id is not None:
        chosen = [r for r in results if r.scoring_id == scoring_id]
        if not chosen:
            raise ConnectorError(f"run {run_id!r} has no results from scoring pass {scoring_id!r}")
        return chosen
    order = {
        e["payload"].get("scoring_id"): index
        for index, e in enumerate(storage.list_run_events(run_id))
        if e["event_type"] == "scoring_pass"
    }
    latest: dict[str, EvaluationResult] = {}
    for result in results:
        binding = json.dumps(
            (deep_unfreeze(result.provenance) or {}).get("binding"), sort_keys=True
        )
        key = f"{result.case_id}	{result.repetition_id}	{result.metric_id}	{binding}"
        rank = order.get(result.scoring_id, -1)  # the run's own pass comes first
        current = latest.get(key)
        if current is None or rank >= order.get(current.scoring_id, -1):
            latest[key] = result
    return list(latest.values())


def _unexportable(
    result: EvaluationResult,
    cases: Mapping[str, BenchmarkCase],
    executions: Mapping[str, Any],
    traced: set[str],
    host: str,
) -> str | None:
    if result.status is not ExecutionStatus.OK or result.value is None:
        return f"not_ok_{result.status.value}"  # a missing result never becomes a zero
    case = cases.get(result.case_id)
    extensions = deep_unfreeze(case.extensions) if case is not None else None
    if case is None or ITEM_EXTENSION not in (extensions or {}):
        return "case_not_from_langfuse"
    if (extensions or {}).get(HOST_EXTENSION) != host:
        return "case_from_another_langfuse_host"
    if result.execution_id not in executions or result.execution_id not in traced:
        return "no_langfuse_trace"
    if result.value.kind not in ("boolean", "scalar", "category"):
        return f"unsupported_value_{result.value.kind}"
    return None


def _score_body(
    run_id: str,
    result: EvaluationResult,
    case: BenchmarkCase,
    executions: Mapping[str, Any],
    include_reasons: bool,
) -> dict[str, Any]:
    assert result.value is not None and result.execution_id is not None
    value: Any = result.value.value
    data_type = {"boolean": "BOOLEAN", "scalar": "NUMERIC", "category": "CATEGORICAL"}[
        result.value.kind
    ]
    if data_type == "BOOLEAN":
        value = 1 if value else 0
    item = (deep_unfreeze(case.extensions) or {})[ITEM_EXTENSION]
    provenance = deep_unfreeze(result.provenance) or {}
    body: dict[str, Any] = {
        "id": score_id(run_id, result.result_id),
        "traceId": executions[result.execution_id].correlation_id,
        "name": result.metric_id,
        "value": value,
        "dataType": data_type,
        "metadata": {
            "aibench_run_id": run_id,
            "aibench_result_id": result.result_id,
            "aibench_scoring_id": result.scoring_id,
            "aibench_case_id": result.case_id,
            "aibench_metric": f"{result.metric_id}@{result.metric_version}",
            "aibench_plugin": provenance.get("plugin_id"),
            "aibench_decision": result.decision.value,
            "langfuse_dataset_item_id": item.get("id"),
            "langfuse_dataset_item_version": item.get("updatedAt"),
        },
    }
    if include_reasons and result.reason:
        body["comment"] = result.reason[:500]
    return body


def _same(found: Mapping[str, Any], body: Mapping[str, Any]) -> bool:
    """Whether a stored score is the one we would create (value, trace and provenance)."""
    value = found.get("value")
    if body["dataType"] == "BOOLEAN" and isinstance(value, bool):
        value = 1 if value else 0
    subject = found.get("subject") or {}
    trace = found.get("traceId") or subject.get("id") or subject.get("traceId")
    meta = found.get("metadata") or {}
    return (
        found.get("name") == body["name"]
        and value == body["value"]
        and trace == body["traceId"]
        and all(meta.get(k) == v for k, v in body["metadata"].items())
    )
