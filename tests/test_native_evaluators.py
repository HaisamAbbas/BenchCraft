"""Conformance fixtures for the native checks and the domain-authored example (04-T3,
04-G1): each fixture states the expected canonical status, value and decision, so a
legitimate low score, a not-applicable case and an evaluator error stay distinct."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aibench.core.models import Decision, EvaluationResult, ExecutionStatus
from aibench.registry import EvaluatorRegistry
from tests.runner_support import REPO_ROOT
from tests.scoring_support import Seeded, case, execution

OK, NA, ERR = ExecutionStatus.OK, ExecutionStatus.NOT_APPLICABLE, ExecutionStatus.ERROR
PASS, FAIL, NOT_EVAL = Decision.PASS, Decision.FAIL, Decision.NOT_EVALUATED


def _score_one(
    tmp_path: Path, binding: dict[str, Any], c: Any, e: Any, **kw: Any
) -> EvaluationResult:
    seeded = Seeded(tmp_path)
    seeded.seed([c], [e])
    [result] = seeded.score([binding], **kw).results
    assert result.schema_version == "1.0.0"
    EvaluationResult.model_validate_json(result.model_dump_json())  # canonical round trip
    return result


EXACT = [
    ("match", {}, "Refunds within 30 days.", " Refunds within 30 days.\n", OK, True, PASS),
    ("low score", {}, "Refunds within 30 days.", "Refunds within 14 days.", OK, False, FAIL),
    ("case sensitive by default", {}, "Yes", "yes", OK, False, FAIL),
    ("case folded", {"case_sensitive": False}, "Yes", "YES", OK, True, PASS),
    ("empty output is a real mismatch", {}, "Yes", "", OK, False, FAIL),
    ("non-text output is a failed answer", {}, "Yes", {"answer": "Yes"}, OK, False, FAIL),
    ("null output is a failed answer", {}, "Yes", None, OK, False, FAIL),
]


@pytest.mark.parametrize(
    ("label", "params", "reference", "output", "status", "value", "decision"),
    EXACT,
    ids=[e[0] for e in EXACT],
)
def test_exact_match_conformance(
    tmp_path: Path,
    label: str,
    params: dict[str, Any],
    reference: str,
    output: Any,
    status: ExecutionStatus,
    value: Any,
    decision: Decision,
) -> None:
    result = _score_one(
        tmp_path,
        {"metric": "native.exact_match", "params": params},
        case("c1", reference),
        execution("c1", output),
    )
    assert (result.status, result.decision) == (status, decision)
    assert (result.value.value if result.value else None) == value
    assert result.resources["accounting"] == "complete" and result.resources["cost"] == 0.0


def test_exact_match_without_a_reference_is_not_applicable(tmp_path: Path) -> None:
    result = _score_one(
        tmp_path, {"metric": "native.exact_match"}, case("c1"), execution("c1", "hi")
    )
    assert (result.status, result.reason) == (NA, "missing:case.reference.answer")


SCHEMA = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
JSON_SCHEMA = [
    ("valid object", {"name": "Ada"}, OK, True, PASS),
    ("valid JSON text", '{"name": "Ada"}', OK, True, PASS),
    ("schema violation", {"name": 7}, OK, False, FAIL),
    ("text that is not JSON", "Ada", OK, False, FAIL),
    ("hostile deep JSON text", "[" * 5000, OK, False, FAIL),
]


@pytest.mark.parametrize(
    ("label", "output", "status", "value", "decision"), JSON_SCHEMA, ids=[j[0] for j in JSON_SCHEMA]
)
def test_json_schema_conformance(
    tmp_path: Path, label: str, output: Any, status: ExecutionStatus, value: Any, decision: Decision
) -> None:
    result = _score_one(
        tmp_path,
        {"metric": "native.json_schema", "params": {"schema": SCHEMA}},
        case("c1"),
        execution("c1", output),
    )
    assert (result.status, result.value.value if result.value else None, result.decision) == (
        status,
        value,
        decision,
    )
    assert result.raw_artifact_ref is not None  # violations are kept as raw evidence


def test_json_schema_violations_are_stored_as_a_verified_raw_artifact(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed([case("c1")], [execution("c1", {"name": 7})])
    [result] = seeded.score(
        [{"metric": "native.json_schema", "params": {"schema": SCHEMA}}]
    ).results
    ref = seeded.storage.get_artifact(result.raw_artifact_ref)
    assert ref is not None
    assert b"is not of type 'string'" in seeded.artifacts.read_bytes(ref)


def test_json_schema_from_a_case_field_and_invalid_case_schema_is_an_error(tmp_path: Path) -> None:
    seeded = Seeded(tmp_path)
    seeded.seed(
        [
            case("good", expectations={"schema": SCHEMA}),
            case("bad", expectations={"schema": {"type": "not-a-type"}}),
            case("none"),
        ],
        [execution(c, {"name": "Ada"}) for c in ("good", "bad", "none")],
    )
    report = seeded.score(
        [{"metric": "native.json_schema", "params": {"schema_field": "case.expectations.schema"}}]
    )
    by_case = {r.case_id: r for r in report.results}
    assert (by_case["good"].status, by_case["good"].decision) == (OK, PASS)
    assert (
        by_case["bad"].status is ERR and by_case["bad"].decision is NOT_EVAL
    )  # the schema's fault
    assert by_case["none"].status is NA  # no schema for this case


def test_json_schema_never_fetches_a_remote_ref(tmp_path: Path) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from tests.runner_support import serving

    hits: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"type": "object"}')

        def log_message(self, *args: Any) -> None:
            return

    with serving(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as base:
        result = _score_one(
            tmp_path,
            {"metric": "native.json_schema", "params": {"schema": {"$ref": f"{base}/schema.json"}}},
            case("c1"),
            execution("c1", {"a": 1}),
        )
    assert result.status is ERR and "schema could not be applied" in (result.reason or "")
    assert hits == []


def _custom_registry() -> EvaluatorRegistry:
    registry = EvaluatorRegistry.with_native()
    registry.load_local_file(
        REPO_ROOT / "examples" / "evaluators" / "refund_window.py", trusted=True
    )
    return registry


REFUND = [
    ("correct", "Refunds are available within 30 days.", 30, OK, "correct", PASS),
    ("wrong window", "You have 14 days to request a refund.", 30, OK, "wrong_window", FAIL),
    (
        "two windows, one right",
        "Within 30 days, or 60 days for members.",
        30,
        OK,
        "wrong_window",
        FAIL,
    ),
    ("no window", "Please contact support.", 30, OK, "no_window_stated", FAIL),
    ("bad expectation", "Within 30 days.", "thirty", ERR, None, NOT_EVAL),
]


@pytest.mark.parametrize(
    ("label", "output", "days", "status", "category", "decision"),
    REFUND,
    ids=[r[0] for r in REFUND],
)
def test_domain_refund_oracle_conformance(
    tmp_path: Path,
    label: str,
    output: str,
    days: Any,
    status: ExecutionStatus,
    category: Any,
    decision: Decision,
) -> None:
    result = _score_one(
        tmp_path,
        {"metric": "acme.refund_window@1"},
        case("c1", expectations={"refund_days": days}),
        execution("c1", output),
        registry=_custom_registry(),
    )
    assert (result.status, result.value.value if result.value else None, result.decision) == (
        status,
        category,
        decision,
    )
    assert result.metric_id == "acme.refund_window"
    assert result.provenance["plugin_id"] == "acme.support_evaluators"
