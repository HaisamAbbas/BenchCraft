"""02-T1/02-G2: repository transactions, idempotent commits, and conflict detection."""

from __future__ import annotations

import pytest

from aibench.core.errors import ConflictError
from aibench.core.models import (
    ApplicationSpec,
    Approval,
    ArtifactRef,
    BenchmarkCase,
    DatasetManifest,
    Decision,
    EvaluationPlan,
    EvaluationResult,
    ExecutionResult,
    ExecutionStatus,
    MetricValue,
    ObservationClaim,
    ObservationState,
    RedactionClass,
    RunManifest,
    RunnerKind,
    UsageEvent,
    UsageRole,
    WorkItem,
)
from aibench.storage.db import Database
from aibench.storage.repositories import Storage


@pytest.fixture
def storage():
    """In-memory database: this file tests pure repository/transaction logic, not
    filesystem persistence (that's `test_storage_recovery.py`), so it needs no temp
    directory at all — see `Database.open_in_memory`."""
    db = Database.open_in_memory()
    s = Storage(db)
    yield s
    db.close()


def _manifest(run_id: str = "r1", **overrides) -> RunManifest:
    fields = {
        "run_id": run_id,
        "dataset_hash": "sha256:d",
        "application_hash": "sha256:a",
        "plan_hash": "sha256:p",
    }
    fields.update(overrides)
    return RunManifest(**fields)


# --------------------------------------------------------------------------- datasets/cases


def test_commit_dataset_is_idempotent(storage) -> None:
    manifest = DatasetManifest(
        dataset_id="ds1", content_hash="sha256:abc", case_count=2, source_refs=("f.jsonl",)
    )
    assert storage.commit_dataset(manifest) is True
    assert storage.commit_dataset(manifest) is False  # identical content, no-op
    fetched = storage.get_dataset("sha256:abc")
    assert fetched.dataset_id == "ds1"
    assert fetched.case_count == 2
    assert fetched.source_refs == ("f.jsonl",)


def test_commit_dataset_conflict_on_mismatched_manifest_under_same_hash(storage) -> None:
    """content_hash is a caller-supplied field, not verified against the other fields by
    the model itself — a buggy caller could pass a second, inconsistent manifest under an
    already-used content_hash. That must raise, not silently keep the first version."""
    manifest1 = DatasetManifest(dataset_id="ds1", content_hash="sha256:abc", case_count=2)
    manifest2 = DatasetManifest(dataset_id="ds1", content_hash="sha256:abc", case_count=999)
    storage.commit_dataset(manifest1)
    with pytest.raises(ConflictError):
        storage.commit_dataset(manifest2)


def test_dataset_suite_versions_are_idempotent_and_immutable(storage) -> None:
    fields = {
        "suite_name": "support",
        "suite_version": "1.0.0",
        "dataset_content_hash": "sha256:abc",
        "dataset_path": "dataset-suites/support/1.0.0.jsonl",
        "case_count": 3,
        "description": "support benchmark",
    }
    assert storage.register_dataset_suite(**fields) is True
    assert storage.register_dataset_suite(**fields) is False
    record = storage.get_dataset_suite("support", "1.0.0")
    assert record is not None
    assert record.dataset_content_hash == "sha256:abc"
    assert record.case_count == 3
    assert storage.list_dataset_suites("support") == [record]
    assert storage.list_dataset_suites("missing") == []

    with pytest.raises(ConflictError):
        storage.register_dataset_suite(**{**fields, "description": "changed metadata"})
    assert storage.get_dataset_suite("support", "1.0.0") == record


def test_commit_cases_is_idempotent_and_preserves_duplicates(storage) -> None:
    manifest = DatasetManifest(dataset_id="ds1", content_hash="sha256:abc", case_count=2)
    storage.commit_dataset(manifest)
    case1 = BenchmarkCase(case_id="c1", input="hi", source_line=1)
    case2 = BenchmarkCase(case_id="c1", input="hi again", source_line=2, duplicate_of_line=1)
    inserted = storage.commit_cases("sha256:abc", [case1, case2])
    assert inserted == 2
    inserted_again = storage.commit_cases("sha256:abc", [case1, case2])
    assert inserted_again == 0  # identical rows, no-op


def test_commit_cases_conflict_on_mismatched_content_at_same_key(storage) -> None:
    manifest = DatasetManifest(dataset_id="ds1", content_hash="sha256:abc", case_count=1)
    storage.commit_dataset(manifest)
    case1 = BenchmarkCase(case_id="c1", input="hi", source_line=1)
    case2 = BenchmarkCase(case_id="c1", input="DIFFERENT", source_line=1)
    storage.commit_cases("sha256:abc", [case1])
    with pytest.raises(ConflictError):
        storage.commit_cases("sha256:abc", [case2])
    cases = storage.list_cases("sha256:abc")
    assert len(cases) == 1
    assert cases[0].input == "hi"  # the original committed case is untouched


def test_commit_cases_requires_existing_dataset_fk(storage) -> None:
    import sqlite3

    case = BenchmarkCase(case_id="c1", input="hi", source_line=1)
    with pytest.raises(sqlite3.IntegrityError):
        storage.commit_cases("sha256:does-not-exist", [case])


# --------------------------------------------------------------------------- applications / profiles


def test_commit_application_conflict_on_mismatched_content(storage) -> None:
    spec1 = ApplicationSpec(application_id="app1", runner=RunnerKind.CLI, target="./run.sh")
    spec2 = ApplicationSpec(application_id="app1", runner=RunnerKind.HTTP, target="http://x")
    assert storage.commit_application(spec1) is True
    assert storage.commit_application(spec1) is False
    with pytest.raises(ConflictError):
        storage.commit_application(spec2)
    assert storage.get_application("app1").target == "./run.sh"


def test_commit_observation_is_idempotent(storage) -> None:
    spec = ApplicationSpec(application_id="app1", runner=RunnerKind.CLI, target="./run.sh")
    storage.commit_application(spec)
    claim = ObservationClaim(
        observation_id="obs1", capability="retrieval", state=ObservationState.OBSERVED
    )
    assert storage.commit_observation(claim, application_id="app1") is True
    assert storage.commit_observation(claim, application_id="app1") is False
    claims = storage.list_observations("app1")
    assert len(claims) == 1
    assert claims[0].capability == "retrieval"


def test_commit_observation_conflict_on_mismatched_content(storage) -> None:
    spec = ApplicationSpec(application_id="app1", runner=RunnerKind.CLI, target="./run.sh")
    storage.commit_application(spec)
    claim1 = ObservationClaim(
        observation_id="obs1", capability="retrieval", state=ObservationState.OBSERVED
    )
    claim2 = ObservationClaim(
        observation_id="obs1", capability="tool_use", state=ObservationState.UNKNOWN
    )
    storage.commit_observation(claim1, application_id="app1")
    with pytest.raises(ConflictError):
        storage.commit_observation(claim2, application_id="app1")


# --------------------------------------------------------------------------- plans


def test_commit_plan_conflict_on_mismatched_content(storage) -> None:
    plan1 = EvaluationPlan(plan_id="p1", objectives=("correctness",))
    plan2 = EvaluationPlan(plan_id="p1", objectives=("faithfulness",))
    assert storage.commit_plan(plan1) is True
    assert storage.commit_plan(plan1) is False
    with pytest.raises(ConflictError):
        storage.commit_plan(plan2)


# --------------------------------------------------------------------------- runs


def test_commit_run_idempotent_and_conflicting(storage) -> None:
    m1 = _manifest()
    m2 = _manifest(dataset_hash="sha256:DIFFERENT")
    assert storage.commit_run(m1) is True
    assert storage.commit_run(m1) is False
    with pytest.raises(ConflictError):
        storage.commit_run(m2)


def test_get_run_returns_none_for_unknown_id(storage) -> None:
    assert storage.get_run("nope") is None


def test_update_run_status_requires_existing_run(storage) -> None:
    with pytest.raises(KeyError):
        storage.update_run_status("nope", "running")
    storage.commit_run(_manifest())
    storage.update_run_status("r1", "running")
    assert storage.get_run("r1").status == "running"


def test_list_runs_filters_by_status_and_orders_recent_first(storage) -> None:
    storage.commit_run(_manifest(run_id="r1"))
    storage.commit_run(_manifest(run_id="r2"))
    storage.update_run_status("r2", "running")
    running = storage.list_runs(status="running")
    assert [r.manifest.run_id for r in running] == ["r2"]
    all_runs = storage.list_runs()
    assert {r.manifest.run_id for r in all_runs} == {"r1", "r2"}


def test_duplicate_run_commit_does_not_reset_status(storage) -> None:
    """02-G2: a retried duplicate commit must not silently reset engine-managed state."""
    manifest = _manifest()
    storage.commit_run(manifest)
    storage.update_run_status("r1", "succeeded")
    storage.commit_run(manifest)  # retry of the identical original commit
    assert storage.get_run("r1").status == "succeeded"


# --------------------------------------------------------------------------- work items


def test_commit_work_item_unique_task_key_per_run(storage) -> None:
    storage.commit_run(_manifest())
    item = WorkItem(work_item_id="w1", run_id="r1", task_key="case:c1:exec", kind="execution")
    assert storage.commit_work_item(item) is True
    assert storage.commit_work_item(item) is False  # identical retry, no-op


def test_work_item_task_key_collision_across_different_items_is_rejected(storage) -> None:
    import sqlite3

    storage.commit_run(_manifest())
    item1 = WorkItem(work_item_id="w1", run_id="r1", task_key="case:c1:exec", kind="execution")
    item2 = WorkItem(work_item_id="w2", run_id="r1", task_key="case:c1:exec", kind="execution")
    storage.commit_work_item(item1)
    with pytest.raises(sqlite3.IntegrityError):
        storage.commit_work_item(item2)  # same (run_id, task_key), different work_item_id


# --------------------------------------------------------------------------- execution attempts


def test_commit_execution_attempt_idempotent_and_conflicting(storage) -> None:
    storage.commit_run(_manifest())
    result1 = ExecutionResult(
        execution_id="e1", run_id="r1", case_id="c1", status=ExecutionStatus.OK, output="ok"
    )
    result2 = ExecutionResult(
        execution_id="e1", run_id="r1", case_id="c1", status=ExecutionStatus.ERROR, error="boom"
    )
    assert storage.commit_execution_attempt(result1) is True
    assert storage.commit_execution_attempt(result1) is False
    with pytest.raises(ConflictError):
        storage.commit_execution_attempt(result2)


def test_execution_attempt_preserves_null_usage_as_unknown_not_zero(storage) -> None:
    storage.commit_run(_manifest())
    result = ExecutionResult(
        execution_id="e1", run_id="r1", case_id="c1", status=ExecutionStatus.OK, usage=None
    )
    storage.commit_execution_attempt(result)
    fetched = storage.get_execution_attempt("e1")
    assert fetched.usage is None  # not 0, not {}


def test_list_execution_attempts_filters_by_case(storage) -> None:
    storage.commit_run(_manifest())
    storage.commit_execution_attempt(
        ExecutionResult(execution_id="e1", run_id="r1", case_id="c1", status=ExecutionStatus.OK)
    )
    storage.commit_execution_attempt(
        ExecutionResult(execution_id="e2", run_id="r1", case_id="c2", status=ExecutionStatus.OK)
    )
    assert len(storage.list_execution_attempts("r1")) == 2
    assert len(storage.list_execution_attempts("r1", case_id="c1")) == 1


# --------------------------------------------------------------------------- evaluation attempts / results


def test_evaluation_attempt_idempotent_and_conflicting(storage) -> None:
    storage.commit_run(_manifest())
    result1 = EvaluationResult(
        result_id="res1",
        run_id="r1",
        case_id="c1",
        metric_id="faithfulness",
        metric_version="1.0.0",
        status=ExecutionStatus.OK,
        decision=Decision.PASS,
        value=MetricValue(kind="scalar", value=0.9),
    )
    result2 = result1.model_copy(update={"decision": Decision.FAIL})
    assert storage.commit_evaluation_attempt(result1, attempt_number=1) is True
    assert storage.commit_evaluation_attempt(result1, attempt_number=1) is False
    with pytest.raises(ConflictError):
        storage.commit_evaluation_attempt(result2, attempt_number=1)
    # A different attempt number for the same (run, case, metric) is a distinct row.
    assert storage.commit_evaluation_attempt(result2, attempt_number=2) is True


def test_metric_result_idempotent_and_conflicting(storage) -> None:
    storage.commit_run(_manifest())
    result1 = EvaluationResult(
        result_id="res1",
        run_id="r1",
        case_id="c1",
        metric_id="faithfulness",
        metric_version="1.0.0",
        status=ExecutionStatus.OK,
        decision=Decision.PASS,
    )
    result2 = result1.model_copy(update={"decision": Decision.FAIL})
    assert storage.commit_metric_result(result1) is True
    assert storage.commit_metric_result(result1) is False
    with pytest.raises(ConflictError):
        storage.commit_metric_result(result2)
    assert storage.list_metric_results("r1")[0].decision == Decision.PASS


def test_evaluator_error_status_is_not_confused_with_a_low_score(storage) -> None:
    storage.commit_run(_manifest())
    errored = EvaluationResult(
        result_id="res1",
        run_id="r1",
        case_id="c1",
        metric_id="faithfulness",
        metric_version="1.0.0",
        status=ExecutionStatus.ERROR,
        decision=Decision.NOT_EVALUATED,
        value=None,
    )
    storage.commit_metric_result(errored)
    fetched = storage.list_metric_results("r1")[0]
    assert fetched.status == ExecutionStatus.ERROR
    assert fetched.decision == Decision.NOT_EVALUATED
    assert fetched.value is None


# --------------------------------------------------------------------------- artifacts
#
# These tests deliberately use `commit_artifact_unverified` with fake, non-existent paths
# (e.g. "/x") to exercise DB-layer idempotency/conflict logic in isolation, without needing a
# real `ArtifactStore`. Real callers must go through `commit_verified_artifact`
# (`storage/artifacts.py`), which verifies a ref against the real file before ever reaching
# this method — see `tests/test_storage_artifacts.py` for that contract.


def test_commit_artifact_idempotent_and_conflicting(storage) -> None:
    ref1 = ArtifactRef(
        artifact_id="art1", digest="sha256:aaa", uri="/x", mime_type="text/plain", size_bytes=3
    )
    ref2 = ArtifactRef(
        artifact_id="art1", digest="sha256:bbb", uri="/x", mime_type="text/plain", size_bytes=3
    )
    assert storage.commit_artifact_unverified(ref1) is True
    assert storage.commit_artifact_unverified(ref1) is False
    with pytest.raises(ConflictError):
        storage.commit_artifact_unverified(ref2)


@pytest.mark.parametrize(
    "overrides",
    [
        {"uri": "/different-path"},
        {"mime_type": "application/json"},
        {"size_bytes": 999},
        {"redaction": RedactionClass.RESTRICTED},
        {"run_id": "some-run"},
    ],
)
def test_commit_artifact_conflicts_on_metadata_mismatch_even_with_same_digest(
    storage, overrides
) -> None:
    """02-review finding: an earlier version only compared `digest`, so a retry with the
    same artifact_id/digest but different uri/mime_type/size_bytes/redaction/run_id was
    silently accepted, potentially leaving incorrect metadata in place."""
    base = {
        "artifact_id": "art1",
        "digest": "sha256:aaa",
        "uri": "/x",
        "mime_type": "text/plain",
        "size_bytes": 3,
        "redaction": RedactionClass.NONE,
        "run_id": None,
    }
    ref1 = ArtifactRef(**base)
    ref2 = ArtifactRef(**{**base, **overrides})
    storage.commit_artifact_unverified(ref1)
    with pytest.raises(ConflictError):
        storage.commit_artifact_unverified(ref2)


def test_referenced_artifact_digests(storage) -> None:
    storage.commit_artifact_unverified(
        ArtifactRef(
            artifact_id="a1", digest="sha256:aaa", uri="/x", mime_type="text/plain", size_bytes=1
        )
    )
    storage.commit_artifact_unverified(
        ArtifactRef(
            artifact_id="a2", digest="sha256:bbb", uri="/y", mime_type="text/plain", size_bytes=1
        )
    )
    assert storage.referenced_artifact_digests() == {"sha256:aaa", "sha256:bbb"}


# --------------------------------------------------------------------------- usage events / approvals


def test_commit_usage_event_is_idempotent(storage) -> None:
    storage.commit_run(_manifest())
    event = UsageEvent(usage_event_id="u1", run_id="r1", role=UsageRole.APPLICATION, cost=None)
    assert storage.commit_usage_event(event) is True
    assert storage.commit_usage_event(event) is False
    fetched = storage.list_usage_events("r1")
    assert len(fetched) == 1
    assert fetched[0].cost is None  # unknown cost stays unknown, not 0


def test_commit_usage_event_conflict_on_mismatched_content(storage) -> None:
    storage.commit_run(_manifest())
    event1 = UsageEvent(usage_event_id="u1", run_id="r1", role=UsageRole.APPLICATION, cost=1.0)
    event2 = UsageEvent(usage_event_id="u1", run_id="r1", role=UsageRole.EVALUATOR, cost=2.0)
    storage.commit_usage_event(event1)
    with pytest.raises(ConflictError):
        storage.commit_usage_event(event2)


def test_commit_approval_is_idempotent(storage) -> None:
    approval = Approval(approval_id="ap1", scope_hash="sha256:scope", allowed_actions=("run",))
    assert storage.commit_approval(approval) is True
    assert storage.commit_approval(approval) is False
    assert storage.get_approval("ap1").allowed_actions == ("run",)


def test_commit_approval_conflict_on_mismatched_content(storage) -> None:
    approval1 = Approval(approval_id="ap1", scope_hash="sha256:scope", allowed_actions=("run",))
    approval2 = Approval(approval_id="ap1", scope_hash="sha256:scope", allowed_actions=("cancel",))
    storage.commit_approval(approval1)
    with pytest.raises(ConflictError):
        storage.commit_approval(approval2)
