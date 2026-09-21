"""Persistence repositories (02-T1). Every commit method is transactional (explicit
`BEGIN`/`COMMIT`/`ROLLBACK`) and idempotent under its logical key: committing byte-identical
content twice is a no-op: safe reporting the identical row without error; committing
different content under an already-used key raises `ConflictError` instead of silently
overwriting an accepted record (02-G2).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from aibench.core.errors import ConflictError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    ApplicationSpec,
    Approval,
    ArtifactRef,
    BenchmarkCase,
    DatasetManifest,
    EvaluationPlan,
    EvaluationResult,
    ExecutionResult,
    ObservationClaim,
    RunManifest,
    UsageEvent,
    WorkItem,
)
from aibench.storage.db import Database


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash_of(model_json: str) -> str:
    return content_hash(model_json)


def _commit_idempotent(
    conn: sqlite3.Connection,
    *,
    table: str,
    pk_col: str,
    pk_value: str,
    compare_col: str,
    compare_value: str,
    insert_sql: str,
    params: tuple[object, ...],
) -> bool:
    """Insert a row, treating an existing row with matching `compare_col` as a no-op and one
    with a mismatched `compare_col` as a conflict. Returns True if a new row was inserted."""
    return _commit_idempotent_composite(
        conn,
        table=table,
        pk_cols=(pk_col,),
        pk_values=(pk_value,),
        compare_col=compare_col,
        compare_value=compare_value,
        insert_sql=insert_sql,
        params=params,
    )


def _commit_idempotent_composite(
    conn: sqlite3.Connection,
    *,
    table: str,
    pk_cols: tuple[str, ...],
    pk_values: tuple[object, ...],
    compare_col: str,
    compare_value: str,
    insert_sql: str,
    params: tuple[object, ...],
) -> bool:
    """Same idempotent-insert-or-conflict contract as `_commit_idempotent`, for tables keyed
    by a composite primary key."""
    where = " AND ".join(f"{c} = ?" for c in pk_cols)
    conn.execute("BEGIN")
    try:
        existing = conn.execute(
            f"SELECT {compare_col} FROM {table} WHERE {where}", pk_values
        ).fetchone()
        if existing is not None:
            if existing[0] != compare_value:
                raise ConflictError(
                    f"{table}[{pk_cols}={pk_values!r}] already committed with different "
                    f"{compare_col}: existing={existing[0]!r} new={compare_value!r}"
                )
            conn.execute("ROLLBACK")
            return False
        conn.execute(insert_sql, params)
        conn.execute("COMMIT")
        return True
    except BaseException:
        conn.execute("ROLLBACK")
        raise


@dataclass
class RunRecord:
    manifest: RunManifest
    status: str
    created_at: str
    committed_at: str
    updated_at: str


class Storage:
    """Facade over one `Database` connection exposing typed repository methods."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @property
    def conn(self) -> sqlite3.Connection:
        return self.db.connection

    # ---------------------------------------------------------------- datasets / cases

    def commit_dataset(self, manifest: DatasetManifest) -> bool:
        manifest_json = manifest.model_dump_json()
        manifest_digest = _hash_of(manifest_json)
        return _commit_idempotent(
            self.conn,
            table="datasets",
            pk_col="content_hash",
            pk_value=manifest.content_hash,
            compare_col="manifest_hash",
            compare_value=manifest_digest,
            insert_sql="""
                INSERT INTO datasets
                    (content_hash, dataset_id, schema_version, case_count, source_refs,
                     split, duplicate_case_ids, manifest_hash, created_at, committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                manifest.content_hash,
                manifest.dataset_id,
                manifest.schema_version,
                manifest.case_count,
                json.dumps(list(manifest.source_refs)),
                manifest.split,
                json.dumps(list(manifest.duplicate_case_ids)),
                manifest_digest,
                manifest.created_at.isoformat(),
                _now(),
            ),
        )

    def get_dataset(self, content_hash_value: str) -> DatasetManifest | None:
        row = self.conn.execute(
            "SELECT dataset_id, schema_version, case_count, source_refs, split, "
            "duplicate_case_ids, created_at FROM datasets WHERE content_hash = ?",
            (content_hash_value,),
        ).fetchone()
        if row is None:
            return None
        return DatasetManifest(
            dataset_id=row["dataset_id"],
            schema_version=row["schema_version"],
            content_hash=content_hash_value,
            case_count=row["case_count"],
            source_refs=tuple(json.loads(row["source_refs"])),
            split=row["split"],
            duplicate_case_ids=tuple(json.loads(row["duplicate_case_ids"])),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def commit_cases(
        self, dataset_content_hash: str, cases: Iterable[BenchmarkCase]
    ) -> int:
        """Idempotent per (dataset_content_hash, case_id, source_line): re-committing the
        identical case is a no-op; a mismatched case at the same key raises `ConflictError`
        for that case (any earlier cases in this call are already committed and retained —
        only work not yet reached rolls back, matching "record attempts honestly" rather
        than discarding otherwise-valid progress within the same call)."""
        inserted = 0
        for case in cases:
            data = case.model_dump_json()
            digest = _hash_of(data)
            newly_inserted = _commit_idempotent_composite(
                self.conn,
                table="cases",
                pk_cols=("dataset_content_hash", "case_id", "source_line"),
                pk_values=(dataset_content_hash, case.case_id, case.source_line),
                compare_col="content_hash",
                compare_value=digest,
                insert_sql="""
                    INSERT INTO cases
                        (dataset_content_hash, case_id, source_line, duplicate_of_line,
                         group_id, content_hash, data, committed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                params=(
                    dataset_content_hash,
                    case.case_id,
                    case.source_line,
                    case.duplicate_of_line,
                    case.group_id,
                    digest,
                    data,
                    _now(),
                ),
            )
            inserted += int(newly_inserted)
        return inserted

    def list_cases(self, dataset_content_hash: str) -> list[BenchmarkCase]:
        rows = self.conn.execute(
            "SELECT data FROM cases WHERE dataset_content_hash = ? ORDER BY source_line",
            (dataset_content_hash,),
        ).fetchall()
        return [BenchmarkCase.model_validate_json(row["data"]) for row in rows]

    # ---------------------------------------------------------------- applications / profiles

    def commit_application(self, spec: ApplicationSpec) -> bool:
        data = spec.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="applications",
            pk_col="application_id",
            pk_value=spec.application_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO applications
                    (application_id, content_hash, runner, target, data, committed_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
            params=(spec.application_id, digest, spec.runner.value, spec.target, data, _now()),
        )

    def get_application(self, application_id: str) -> ApplicationSpec | None:
        row = self.conn.execute(
            "SELECT data FROM applications WHERE application_id = ?", (application_id,)
        ).fetchone()
        return ApplicationSpec.model_validate_json(row["data"]) if row else None

    def commit_observation(self, claim: ObservationClaim, *, application_id: str) -> bool:
        data = claim.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="profiles",
            pk_col="observation_id",
            pk_value=claim.observation_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO profiles
                    (observation_id, application_id, capability, state, content_hash, data,
                     committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                claim.observation_id,
                application_id,
                claim.capability,
                claim.state.value,
                digest,
                data,
                _now(),
            ),
        )

    def list_observations(self, application_id: str) -> list[ObservationClaim]:
        rows = self.conn.execute(
            "SELECT data FROM profiles WHERE application_id = ?", (application_id,)
        ).fetchall()
        return [ObservationClaim.model_validate_json(row["data"]) for row in rows]

    # ---------------------------------------------------------------- plans

    def commit_plan(self, plan: EvaluationPlan) -> bool:
        data = plan.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="plans",
            pk_col="plan_id",
            pk_value=plan.plan_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO plans (plan_id, content_hash, policy_hash, data, committed_at)
                VALUES (?, ?, ?, ?, ?)
            """,
            params=(plan.plan_id, digest, plan.policy_hash, data, _now()),
        )

    def get_plan(self, plan_id: str) -> EvaluationPlan | None:
        row = self.conn.execute(
            "SELECT data FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return EvaluationPlan.model_validate_json(row["data"]) if row else None

    # ---------------------------------------------------------------- runs

    def commit_run(self, manifest: RunManifest, *, status: str = "created") -> bool:
        data = manifest.model_dump_json()
        digest = _hash_of(data)
        now = _now()
        return _commit_idempotent(
            self.conn,
            table="runs",
            pk_col="run_id",
            pk_value=manifest.run_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO runs
                    (run_id, dataset_hash, application_hash, plan_hash, content_hash,
                     status, data, created_at, committed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                manifest.run_id,
                manifest.dataset_hash,
                manifest.application_hash,
                manifest.plan_hash,
                digest,
                status,
                data,
                manifest.created_at.isoformat(),
                now,
                now,
            ),
        )

    def update_run_status(self, run_id: str, status: str) -> None:
        self.conn.execute("BEGIN")
        try:
            cur = self.conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, _now(), run_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"no run committed with run_id={run_id!r}")
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def get_run(self, run_id: str) -> RunRecord | None:
        row = self.conn.execute(
            "SELECT data, status, created_at, committed_at, updated_at FROM runs "
            "WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunRecord(
            manifest=RunManifest.model_validate_json(row["data"]),
            status=row["status"],
            created_at=row["created_at"],
            committed_at=row["committed_at"],
            updated_at=row["updated_at"],
        )

    def list_runs(self, *, status: str | None = None, limit: int = 100) -> list[RunRecord]:
        if status is not None:
            rows = self.conn.execute(
                "SELECT data, status, created_at, committed_at, updated_at FROM runs "
                "WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data, status, created_at, committed_at, updated_at FROM runs "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            RunRecord(
                manifest=RunManifest.model_validate_json(row["data"]),
                status=row["status"],
                created_at=row["created_at"],
                committed_at=row["committed_at"],
                updated_at=row["updated_at"],
            )
            for row in rows
        ]

    # ---------------------------------------------------------------- work items

    def commit_work_item(self, item: WorkItem) -> bool:
        """Idempotent under `work_item_id` (identical retry is a no-op, mismatched content
        under the same `work_item_id` is a `ConflictError`). A *different* `work_item_id`
        reusing the same `(run_id, task_key)` — the actual "unique logical task key" the
        ticket describes — is rejected by the table's own UNIQUE constraint, which is left
        to raise `sqlite3.IntegrityError` rather than being silently swallowed by an
        `INSERT OR IGNORE`, which would have hidden a genuine double-scheduling bug."""
        data = item.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="work_items",
            pk_col="work_item_id",
            pk_value=item.work_item_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO work_items
                    (work_item_id, run_id, task_key, kind, state, attempt, content_hash,
                     data, committed_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                item.work_item_id,
                item.run_id,
                item.task_key,
                item.kind,
                item.state.value,
                item.attempt,
                digest,
                data,
                _now(),
                _now(),
            ),
        )

    def get_work_item_by_task_key(self, run_id: str, task_key: str) -> WorkItem | None:
        row = self.conn.execute(
            "SELECT data FROM work_items WHERE run_id = ? AND task_key = ?",
            (run_id, task_key),
        ).fetchone()
        return WorkItem.model_validate_json(row["data"]) if row else None

    def list_work_items(self, run_id: str) -> list[WorkItem]:
        rows = self.conn.execute(
            "SELECT data FROM work_items WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [WorkItem.model_validate_json(row["data"]) for row in rows]

    # ---------------------------------------------------------------- execution attempts

    def commit_execution_attempt(self, result: ExecutionResult) -> bool:
        data = result.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="execution_attempts",
            pk_col="execution_id",
            pk_value=result.execution_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO execution_attempts
                    (execution_id, run_id, case_id, repetition_id, attempt_id, status,
                     content_hash, data, committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                result.execution_id,
                result.run_id,
                result.case_id,
                result.repetition_id,
                result.attempt_id,
                result.status.value,
                digest,
                data,
                _now(),
            ),
        )

    def get_execution_attempt(self, execution_id: str) -> ExecutionResult | None:
        row = self.conn.execute(
            "SELECT data FROM execution_attempts WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return ExecutionResult.model_validate_json(row["data"]) if row else None

    def list_execution_attempts(self, run_id: str, case_id: str | None = None) -> list[ExecutionResult]:
        if case_id is not None:
            rows = self.conn.execute(
                "SELECT data FROM execution_attempts WHERE run_id = ? AND case_id = ?",
                (run_id, case_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM execution_attempts WHERE run_id = ?", (run_id,)
            ).fetchall()
        return [ExecutionResult.model_validate_json(row["data"]) for row in rows]

    # ---------------------------------------------------------------- evaluation attempts / results

    def commit_evaluation_attempt(self, result: EvaluationResult, *, attempt_number: int) -> bool:
        data = result.model_dump_json()
        digest = _hash_of(data)
        conn = self.conn
        conn.execute("BEGIN")
        try:
            existing = conn.execute(
                "SELECT content_hash FROM evaluation_attempts "
                "WHERE run_id = ? AND case_id = ? AND metric_id = ? AND attempt_number = ?",
                (result.run_id, result.case_id, result.metric_id, attempt_number),
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ConflictError(
                        "evaluation_attempts "
                        f"(run_id={result.run_id!r}, case_id={result.case_id!r}, "
                        f"metric_id={result.metric_id!r}, attempt_number={attempt_number}) "
                        "already committed with different content"
                    )
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """
                INSERT INTO evaluation_attempts
                    (run_id, case_id, metric_id, attempt_number, status, decision,
                     content_hash, data, committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.case_id,
                    result.metric_id,
                    attempt_number,
                    result.status.value,
                    result.decision.value,
                    digest,
                    data,
                    _now(),
                ),
            )
            conn.execute("COMMIT")
            return True
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def commit_metric_result(self, result: EvaluationResult) -> bool:
        data = result.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="metric_results",
            pk_col="result_id",
            pk_value=result.result_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO metric_results
                    (result_id, run_id, case_id, metric_id, status, decision,
                     content_hash, data, committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                result.result_id,
                result.run_id,
                result.case_id,
                result.metric_id,
                result.status.value,
                result.decision.value,
                digest,
                data,
                _now(),
            ),
        )

    def list_metric_results(self, run_id: str, case_id: str | None = None) -> list[EvaluationResult]:
        if case_id is not None:
            rows = self.conn.execute(
                "SELECT data FROM metric_results WHERE run_id = ? AND case_id = ?",
                (run_id, case_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM metric_results WHERE run_id = ?", (run_id,)
            ).fetchall()
        return [EvaluationResult.model_validate_json(row["data"]) for row in rows]

    # ---------------------------------------------------------------- artifacts

    def commit_artifact_unverified(self, ref: ArtifactRef) -> bool:
        """Idempotent under the *complete* `ArtifactRef` content, not just `digest`: a retry
        with the same `artifact_id` and `digest` but a different `uri`, `mime_type`,
        `size_bytes`, `redaction`, or `run_id` is a `ConflictError`, not a silent no-op that
        would otherwise leave stale/incorrect metadata in place.

        Named `_unverified` deliberately: this is pure DB logic with **no filesystem check**
        that `ref` actually describes a real file — it will happily commit a reference to a
        path that does not exist, a forged size, or a URI outside the artifact store. Real
        callers must use `aibench.storage.artifacts.commit_verified_artifact(store, storage,
        ref)` instead, which verifies `ref` against the real file first and then calls this
        method. This method stays public (not underscore-prefixed) because
        `commit_verified_artifact` legitimately calls it cross-module, and because tests that
        specifically exercise DB-layer idempotency/conflict semantics in isolation — without
        needing a real `ArtifactStore` — call it directly by design; both are documented,
        intentional uses, not the bug this split fixes.
        """
        data = ref.model_dump_json()
        digest_of_ref = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="artifacts",
            pk_col="artifact_id",
            pk_value=ref.artifact_id,
            compare_col="content_hash",
            compare_value=digest_of_ref,
            insert_sql="""
                INSERT INTO artifacts
                    (artifact_id, digest, uri, mime_type, size_bytes, redaction, run_id,
                     content_hash, committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                ref.artifact_id,
                ref.digest,
                ref.uri,
                ref.mime_type,
                ref.size_bytes,
                ref.redaction.value,
                ref.run_id,
                digest_of_ref,
                _now(),
            ),
        )

    def get_artifact(self, artifact_id: str) -> ArtifactRef | None:
        row = self.conn.execute(
            "SELECT digest, uri, mime_type, size_bytes, redaction, run_id FROM artifacts "
            "WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            return None
        return ArtifactRef(
            artifact_id=artifact_id,
            digest=row["digest"],
            uri=row["uri"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            redaction=row["redaction"],
            run_id=row["run_id"],
        )

    def referenced_artifact_digests(self) -> set[str]:
        rows = self.conn.execute("SELECT DISTINCT digest FROM artifacts").fetchall()
        return {row[0] for row in rows}

    # ---------------------------------------------------------------- usage events

    def commit_usage_event(self, event: UsageEvent) -> bool:
        data = event.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="usage_events",
            pk_col="usage_event_id",
            pk_value=event.usage_event_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO usage_events
                    (usage_event_id, run_id, role, provider, cost, content_hash, data,
                     committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                event.usage_event_id,
                event.run_id,
                event.role.value,
                event.provider,
                event.cost,
                digest,
                data,
                _now(),
            ),
        )

    def list_usage_events(self, run_id: str) -> list[UsageEvent]:
        rows = self.conn.execute(
            "SELECT data FROM usage_events WHERE run_id = ?", (run_id,)
        ).fetchall()
        return [UsageEvent.model_validate_json(row["data"]) for row in rows]

    # ---------------------------------------------------------------- approvals

    def commit_approval(self, approval: Approval) -> bool:
        data = approval.model_dump_json()
        digest = _hash_of(data)
        return _commit_idempotent(
            self.conn,
            table="approvals",
            pk_col="approval_id",
            pk_value=approval.approval_id,
            compare_col="content_hash",
            compare_value=digest,
            insert_sql="""
                INSERT INTO approvals (approval_id, scope_hash, content_hash, data,
                                       committed_at)
                VALUES (?, ?, ?, ?, ?)
            """,
            params=(approval.approval_id, approval.scope_hash, digest, data, _now()),
        )

    def get_approval(self, approval_id: str) -> Approval | None:
        row = self.conn.execute(
            "SELECT data FROM approvals WHERE approval_id = ?", (approval_id,)
        ).fetchone()
        return Approval.model_validate_json(row["data"]) if row else None
