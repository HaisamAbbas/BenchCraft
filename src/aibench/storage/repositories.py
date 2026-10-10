"""Persistence repositories (02-T1). Every commit method is transactional (explicit
`BEGIN`/`COMMIT`/`ROLLBACK`) and idempotent under its logical key: committing byte-identical
content twice is a no-op: safe reporting the identical row without error; committing
different content under an already-used key raises `ConflictError` instead of silently
overwriting an accepted record (02-G2).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aibench.core.errors import ConflictError
from aibench.core.hashes import content_hash
from aibench.core.models import (
    ApplicationSpec,
    Approval,
    ArtifactRef,
    BenchmarkCase,
    CandidateEvent,
    CandidatePoolManifest,
    CandidateStatus,
    DatasetCandidate,
    DatasetManifest,
    EvaluationPlan,
    EvaluationResult,
    ExecutionResult,
    ExperimentEvent,
    ExperimentRecord,
    ExperimentStatus,
    ExperimentTrial,
    ExperimentTrialStatus,
    ObservationClaim,
    RunManifest,
    UsageEvent,
    WorkItem,
    WorkItemState,
)
from aibench.storage.db import Database


@dataclass(frozen=True)
class WorkItemSettlement:
    """One compare-and-set transition for `Storage.settle_work_items`."""

    task_key: str
    from_states: frozenset[WorkItemState]
    to_state: WorkItemState
    last_error: str | None = None


@dataclass(frozen=True)
class DatasetSuiteRecord:
    suite_name: str
    suite_version: str
    dataset_content_hash: str
    dataset_path: str
    case_count: int
    description: str
    created_at: str


@dataclass(frozen=True)
class CandidateTransition:
    candidate: DatasetCandidate
    from_status: CandidateStatus
    event: CandidateEvent


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sqlite_casefold(value: object) -> str | None:
    """Unicode-aware text folding for literal run-history searches."""
    return value.casefold() if isinstance(value, str) else None


@dataclass(frozen=True)
class RunLease:
    run_id: str
    owner: str  # one session's random token
    host: str
    pid: int
    acquired_at: float  # epoch seconds
    heartbeat_at: float


@dataclass(frozen=True)
class RunControlState:
    run_id: str
    desired_state: str
    sequence: int
    updated_at: str


class LeaseHeld(ConflictError):
    """Another live session is running this run."""

    def __init__(self, lease: RunLease) -> None:
        self.lease = lease
        super().__init__(
            f"run {lease.run_id} is being run by another session (host {lease.host}, "
            f"pid {lease.pid})"
        )


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


@dataclass(frozen=True)
class RunBaseline:
    alias: str
    run_id: str
    approved_by: str
    promoted_at: str


@dataclass(frozen=True)
class BaselinePromotion:
    alias: str
    run_id: str
    previous_run_id: str | None
    approved_by: str
    promoted_at: str


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

    def register_dataset_suite(
        self,
        *,
        suite_name: str,
        suite_version: str,
        dataset_content_hash: str,
        dataset_path: str,
        case_count: int,
        description: str,
    ) -> bool:
        """Register one immutable dataset suite version, idempotently."""
        identity = {
            "suite_name": suite_name,
            "suite_version": suite_version,
            "dataset_content_hash": dataset_content_hash,
            "dataset_path": dataset_path,
            "case_count": case_count,
            "description": description,
        }
        record_hash = _hash_of(json.dumps(identity, sort_keys=True, separators=(",", ":")))
        return _commit_idempotent_composite(
            self.conn,
            table="dataset_suites",
            pk_cols=("suite_name", "suite_version"),
            pk_values=(suite_name, suite_version),
            compare_col="record_hash",
            compare_value=record_hash,
            insert_sql="""
                INSERT INTO dataset_suites
                    (suite_name, suite_version, dataset_content_hash, dataset_path,
                     case_count, description, record_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params=(
                suite_name,
                suite_version,
                dataset_content_hash,
                dataset_path,
                case_count,
                description,
                record_hash,
                _now(),
            ),
        )

    def get_dataset_suite(self, suite_name: str, suite_version: str) -> DatasetSuiteRecord | None:
        row = self.conn.execute(
            "SELECT suite_name, suite_version, dataset_content_hash, dataset_path, case_count, "
            "description, created_at FROM dataset_suites WHERE suite_name = ? "
            "AND suite_version = ?",
            (suite_name, suite_version),
        ).fetchone()
        return DatasetSuiteRecord(**dict(row)) if row is not None else None

    def list_dataset_suites(self, suite_name: str | None = None) -> list[DatasetSuiteRecord]:
        if suite_name is None:
            rows = self.conn.execute(
                "SELECT suite_name, suite_version, dataset_content_hash, dataset_path, "
                "case_count, description, created_at FROM dataset_suites "
                "ORDER BY suite_name, suite_version"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT suite_name, suite_version, dataset_content_hash, dataset_path, "
                "case_count, description, created_at FROM dataset_suites WHERE suite_name = ? "
                "ORDER BY suite_version",
                (suite_name,),
            ).fetchall()
        return [DatasetSuiteRecord(**dict(row)) for row in rows]

    def commit_cases(self, dataset_content_hash: str, cases: Iterable[BenchmarkCase]) -> int:
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

    # ---------------------------------------------------------------- candidate generation / review

    def commit_candidate_pool(
        self, manifest: CandidatePoolManifest, candidates: Iterable[DatasetCandidate]
    ) -> bool:
        """Commit one development-only pool and its generated candidates atomically."""
        items = tuple(candidates)
        if tuple(item.candidate_id for item in items) != manifest.candidate_ids:
            raise ValueError("candidate IDs do not match the pool manifest")
        if any(
            item.pool_id != manifest.pool_id
            or item.split_id != "development"
            or item.status is not CandidateStatus.CANDIDATE
            for item in items
        ):
            raise ValueError("new candidate pools accept only unreviewed development candidates")

        manifest_data = manifest.model_dump_json()
        manifest_hash = _hash_of(manifest_data)
        self.conn.execute("BEGIN")
        try:
            existing = self.conn.execute(
                "SELECT content_hash FROM candidate_pools WHERE pool_id = ?",
                (manifest.pool_id,),
            ).fetchone()
            if existing is not None and existing["content_hash"] != manifest_hash:
                raise ConflictError(
                    f"candidate pool {manifest.pool_id!r} already exists with different content"
                )
            if existing is None:
                self.conn.execute(
                    "INSERT INTO candidate_pools (pool_id, content_hash, data, created_at) "
                    "VALUES (?, ?, ?, ?)",
                    (manifest.pool_id, manifest_hash, manifest_data, _now()),
                )
            for candidate in items:
                data = candidate.model_dump_json()
                digest = _hash_of(data)
                current = self.conn.execute(
                    "SELECT content_hash FROM candidate_cases WHERE candidate_id = ?",
                    (candidate.candidate_id,),
                ).fetchone()
                if current is not None:
                    if current["content_hash"] != digest:
                        raise ConflictError(
                            f"candidate {candidate.candidate_id!r} already exists with different content"
                        )
                    continue
                self.conn.execute(
                    "INSERT INTO candidate_cases "
                    "(candidate_id, pool_id, split_id, status, content_hash, data, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate.candidate_id,
                        manifest.pool_id,
                        candidate.split_id,
                        candidate.status.value,
                        digest,
                        data,
                        candidate.created_at.isoformat(),
                        _now(),
                    ),
                )
                event = CandidateEvent(
                    event_id=content_hash(
                        {"candidate_id": candidate.candidate_id, "kind": "generated"}
                    ),
                    candidate_id=candidate.candidate_id,
                    kind="generated",
                    actor=candidate.case.provenance.generator_identity or "unknown generator",
                    details={"pool_id": manifest.pool_id, "split_id": "development"},
                    created_at=candidate.created_at,
                )
                self.conn.execute(
                    "INSERT INTO candidate_events (event_id, candidate_id, kind, data, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        event.event_id,
                        event.candidate_id,
                        event.kind,
                        event.model_dump_json(),
                        event.created_at.isoformat(),
                    ),
                )
            self.conn.execute("COMMIT")
            return existing is None
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def get_candidate_pool(self, pool_id: str) -> CandidatePoolManifest | None:
        row = self.conn.execute(
            "SELECT data FROM candidate_pools WHERE pool_id = ?", (pool_id,)
        ).fetchone()
        return CandidatePoolManifest.model_validate_json(row["data"]) if row else None

    def list_candidate_pools(self) -> list[CandidatePoolManifest]:
        rows = self.conn.execute(
            "SELECT data FROM candidate_pools ORDER BY created_at, pool_id"
        ).fetchall()
        return [CandidatePoolManifest.model_validate_json(row["data"]) for row in rows]

    def get_candidate(self, candidate_id: str) -> DatasetCandidate | None:
        row = self.conn.execute(
            "SELECT data FROM candidate_cases WHERE candidate_id = ?", (candidate_id,)
        ).fetchone()
        return DatasetCandidate.model_validate_json(row["data"]) if row else None

    def list_candidates(
        self, pool_id: str, *, status: CandidateStatus | None = None
    ) -> list[DatasetCandidate]:
        if status is None:
            rows = self.conn.execute(
                "SELECT data FROM candidate_cases WHERE pool_id = ? ORDER BY created_at, candidate_id",
                (pool_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM candidate_cases WHERE pool_id = ? AND status = ? "
                "ORDER BY created_at, candidate_id",
                (pool_id, status.value),
            ).fetchall()
        return [DatasetCandidate.model_validate_json(row["data"]) for row in rows]

    def list_candidate_events(self, candidate_id: str) -> list[CandidateEvent]:
        rows = self.conn.execute(
            "SELECT data FROM candidate_events WHERE candidate_id = ? ORDER BY created_at, event_id",
            (candidate_id,),
        ).fetchall()
        return [CandidateEvent.model_validate_json(row["data"]) for row in rows]

    def transition_candidates(self, transitions: Iterable[CandidateTransition]) -> None:
        """Apply a set of reviewed/promoted states and append their events atomically."""
        items = tuple(transitions)
        if not items:
            raise ValueError("at least one candidate transition is required")
        if len({item.candidate.candidate_id for item in items}) != len(items):
            raise ValueError("a candidate can transition at most once per operation")
        self.conn.execute("BEGIN")
        try:
            for transition in items:
                candidate = transition.candidate
                if transition.event.candidate_id != candidate.candidate_id:
                    raise ValueError("candidate event identity does not match its candidate")
                row = self.conn.execute(
                    "SELECT status FROM candidate_cases WHERE candidate_id = ?",
                    (candidate.candidate_id,),
                ).fetchone()
                if row is None:
                    raise ConflictError(f"candidate {candidate.candidate_id!r} does not exist")
                if row["status"] != transition.from_status.value:
                    raise ConflictError(
                        f"candidate {candidate.candidate_id!r} is {row['status']!r}, expected "
                        f"{transition.from_status.value!r}"
                    )
                data = candidate.model_dump_json()
                digest = _hash_of(data)
                self.conn.execute(
                    "UPDATE candidate_cases SET status = ?, content_hash = ?, data = ?, updated_at = ? "
                    "WHERE candidate_id = ? AND status = ?",
                    (
                        candidate.status.value,
                        digest,
                        data,
                        _now(),
                        candidate.candidate_id,
                        transition.from_status.value,
                    ),
                )
                self.conn.execute(
                    "INSERT INTO candidate_events (event_id, candidate_id, kind, data, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        transition.event.event_id,
                        transition.event.candidate_id,
                        transition.event.kind,
                        transition.event.model_dump_json(),
                        transition.event.created_at.isoformat(),
                    ),
                )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # ---------------------------------------------------------------- controlled experiments

    def commit_experiment(
        self,
        experiment: ExperimentRecord,
        trials: Iterable[ExperimentTrial],
        created_event: ExperimentEvent,
    ) -> bool:
        """Commit a frozen experiment, its full deterministic grid and protected holdout.

        The holdout digest is reserved in the same transaction, before any trial executes.
        Its case labels are not loaded or copied into the optimizer's trial records.
        """
        items = tuple(trials)
        if created_event.experiment_id != experiment.experiment_id:
            raise ValueError("experiment creation event identity does not match")
        if tuple(item.experiment_id for item in items) != (experiment.experiment_id,) * len(items):
            raise ValueError("experiment trial identities do not match their experiment")
        if tuple(item.ordinal for item in items) != tuple(range(len(items))):
            raise ValueError("experiment trials must be supplied in ordinal order")
        if any(item.status is not ExperimentTrialStatus.PENDING for item in items):
            raise ValueError("new experiment trials must be pending")

        data = experiment.model_dump_json()
        digest = _hash_of(data)
        self.conn.execute("BEGIN")
        try:
            existing = self.conn.execute(
                "SELECT content_hash FROM experiments WHERE experiment_id = ?",
                (experiment.experiment_id,),
            ).fetchone()
            if existing is not None:
                if existing["content_hash"] != digest:
                    raise ConflictError(
                        f"experiment {experiment.experiment_id!r} already exists with different content"
                    )
                self.conn.execute("ROLLBACK")
                return False

            protected = self.conn.execute(
                "SELECT experiment_id FROM protected_dataset_digests WHERE digest = ?",
                (experiment.holdout_dataset_hash,),
            ).fetchone()
            if protected is not None:
                raise ConflictError(
                    "this holdout dataset is already protected by experiment "
                    f"{protected['experiment_id']!r}"
                )
            self.conn.execute(
                "INSERT INTO experiments "
                "(experiment_id, status, content_hash, data, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    experiment.experiment_id,
                    experiment.status.value,
                    digest,
                    data,
                    experiment.created_at.isoformat(),
                    experiment.updated_at.isoformat(),
                ),
            )
            self.conn.execute(
                "INSERT INTO protected_dataset_digests "
                "(digest, experiment_id, split_id, registered_at) VALUES (?, ?, 'holdout', ?)",
                (
                    experiment.holdout_dataset_hash,
                    experiment.experiment_id,
                    experiment.created_at.isoformat(),
                ),
            )
            for trial in items:
                trial_data = trial.model_dump_json()
                self.conn.execute(
                    "INSERT INTO experiment_trials "
                    "(trial_id, experiment_id, ordinal, run_id, status, parameter_hash, "
                    "data, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        trial.trial_id,
                        trial.experiment_id,
                        trial.ordinal,
                        trial.run_id,
                        trial.status.value,
                        trial.parameter_hash,
                        trial_data,
                        trial.created_at.isoformat(),
                        trial.updated_at.isoformat(),
                    ),
                )
            self._insert_experiment_event(created_event)
            self.conn.execute("COMMIT")
            return True
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def get_experiment(self, experiment_id: str) -> ExperimentRecord | None:
        row = self.conn.execute(
            "SELECT data FROM experiments WHERE experiment_id = ?", (experiment_id,)
        ).fetchone()
        return ExperimentRecord.model_validate_json(row["data"]) if row else None

    def list_experiments(self) -> list[ExperimentRecord]:
        rows = self.conn.execute(
            "SELECT data FROM experiments ORDER BY created_at, experiment_id"
        ).fetchall()
        return [ExperimentRecord.model_validate_json(row["data"]) for row in rows]

    def transition_experiment(
        self,
        experiment: ExperimentRecord,
        *,
        from_status: ExperimentStatus,
        event: ExperimentEvent,
    ) -> None:
        if event.experiment_id != experiment.experiment_id:
            raise ValueError("experiment event identity does not match")
        data = experiment.model_dump_json()
        digest = _hash_of(data)
        self.conn.execute("BEGIN")
        try:
            row = self.conn.execute(
                "SELECT status FROM experiments WHERE experiment_id = ?",
                (experiment.experiment_id,),
            ).fetchone()
            if row is None:
                raise ConflictError(f"experiment {experiment.experiment_id!r} does not exist")
            if row["status"] != from_status.value:
                raise ConflictError(
                    f"experiment {experiment.experiment_id!r} is {row['status']!r}, expected "
                    f"{from_status.value!r}"
                )
            self.conn.execute(
                "UPDATE experiments SET status = ?, content_hash = ?, data = ?, updated_at = ? "
                "WHERE experiment_id = ? AND status = ?",
                (
                    experiment.status.value,
                    digest,
                    data,
                    experiment.updated_at.isoformat(),
                    experiment.experiment_id,
                    from_status.value,
                ),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ConflictError("experiment state changed concurrently")
            self._insert_experiment_event(event)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def get_experiment_trial(self, trial_id: str) -> ExperimentTrial | None:
        row = self.conn.execute(
            "SELECT data FROM experiment_trials WHERE trial_id = ?", (trial_id,)
        ).fetchone()
        return ExperimentTrial.model_validate_json(row["data"]) if row else None

    def list_experiment_trials(self, experiment_id: str) -> list[ExperimentTrial]:
        rows = self.conn.execute(
            "SELECT data FROM experiment_trials WHERE experiment_id = ? ORDER BY ordinal",
            (experiment_id,),
        ).fetchall()
        return [ExperimentTrial.model_validate_json(row["data"]) for row in rows]

    def transition_experiment_trial(
        self,
        trial: ExperimentTrial,
        *,
        from_status: ExperimentTrialStatus,
        event: ExperimentEvent,
    ) -> None:
        if event.experiment_id != trial.experiment_id:
            raise ValueError("experiment trial event identity does not match")
        data = trial.model_dump_json()
        self.conn.execute("BEGIN")
        try:
            row = self.conn.execute(
                "SELECT status, experiment_id FROM experiment_trials WHERE trial_id = ?",
                (trial.trial_id,),
            ).fetchone()
            if row is None or row["experiment_id"] != trial.experiment_id:
                raise ConflictError(f"experiment trial {trial.trial_id!r} does not exist")
            if row["status"] != from_status.value:
                raise ConflictError(
                    f"experiment trial {trial.trial_id!r} is {row['status']!r}, expected "
                    f"{from_status.value!r}"
                )
            self.conn.execute(
                "UPDATE experiment_trials SET status = ?, parameter_hash = ?, data = ?, updated_at = ? "
                "WHERE trial_id = ? AND status = ?",
                (
                    trial.status.value,
                    trial.parameter_hash,
                    data,
                    trial.updated_at.isoformat(),
                    trial.trial_id,
                    from_status.value,
                ),
            )
            if self.conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ConflictError("experiment trial state changed concurrently")
            self._insert_experiment_event(event)
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def append_experiment_event(self, event: ExperimentEvent) -> bool:
        self.conn.execute("BEGIN")
        try:
            inserted = self._insert_experiment_event(event)
            self.conn.execute("COMMIT")
            return inserted
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _insert_experiment_event(self, event: ExperimentEvent) -> bool:
        data = event.model_dump_json()
        row = self.conn.execute(
            "SELECT data FROM experiment_events WHERE event_id = ?", (event.event_id,)
        ).fetchone()
        if row is not None:
            if row["data"] != data:
                raise ConflictError(f"experiment event {event.event_id!r} conflicts")
            return False
        self.conn.execute(
            "INSERT INTO experiment_events (event_id, experiment_id, kind, data, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                event.event_id,
                event.experiment_id,
                event.kind.value,
                data,
                event.created_at.isoformat(),
            ),
        )
        return True

    def list_experiment_events(self, experiment_id: str) -> list[ExperimentEvent]:
        rows = self.conn.execute(
            "SELECT data FROM experiment_events WHERE experiment_id = ? "
            "ORDER BY created_at, event_id",
            (experiment_id,),
        ).fetchall()
        return [ExperimentEvent.model_validate_json(row["data"]) for row in rows]

    def is_protected_dataset_digest(self, digest: str) -> bool:
        return (
            self.conn.execute(
                "SELECT 1 FROM protected_dataset_digests WHERE digest = ?", (digest,)
            ).fetchone()
            is not None
        )

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
        row = self.conn.execute("SELECT data FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
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
            "SELECT data, status, created_at, committed_at, updated_at FROM runs WHERE run_id = ?",
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
        return self.search_runs(status=status, limit=limit)

    def search_runs(
        self,
        *,
        status: str | None = None,
        query: str | None = None,
        tag: str | None = None,
        baseline: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RunRecord]:
        """Search committed run identity and annotations in stable newest-first pages."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("run list limit must be between 1 and 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("run list offset must be a non-negative integer")
        # Every SQLite rowid-backed table has fewer than 2**63 rows, so a larger offset
        # cannot match anything and must not be bound as an overflowing SQLite integer.
        if offset > 2**63 - 1:
            return []
        conditions: list[str] = []
        values: list[object] = []
        if status is not None:
            conditions.append("r.status = ?")
            values.append(status)
        if query:
            self.conn.create_function("ai_casefold", 1, _sqlite_casefold, deterministic=True)
            escaped = (
                query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            ).casefold()
            pattern = f"%{escaped}%"
            conditions.append(
                "(ai_casefold(r.run_id) LIKE ? ESCAPE '\\' "
                "OR ai_casefold(r.status) LIKE ? ESCAPE '\\' "
                "OR ai_casefold(r.dataset_hash) LIKE ? ESCAPE '\\' "
                "OR ai_casefold(r.application_hash) LIKE ? ESCAPE '\\' "
                "OR ai_casefold(r.plan_hash) LIKE ? ESCAPE '\\' "
                "OR ai_casefold(r.data) LIKE ? ESCAPE '\\' "
                "OR EXISTS (SELECT 1 FROM datasets d WHERE d.content_hash = r.dataset_hash "
                "AND ai_casefold(d.dataset_id) LIKE ? ESCAPE '\\') "
                "OR EXISTS (SELECT 1 FROM run_notes n WHERE n.run_id = r.run_id "
                "AND ai_casefold(n.note) LIKE ? ESCAPE '\\') "
                "OR EXISTS (SELECT 1 FROM run_tags t WHERE t.run_id = r.run_id "
                "AND ai_casefold(t.tag) LIKE ? ESCAPE '\\') "
                "OR EXISTS (SELECT 1 FROM run_baselines b WHERE b.run_id = r.run_id "
                "AND ai_casefold(b.alias) LIKE ? ESCAPE '\\'))"
            )
            values.extend([pattern] * 10)
        if tag is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM run_tags t WHERE t.run_id = r.run_id AND t.tag = ?)"
            )
            values.append(tag)
        if baseline is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM run_baselines b WHERE b.run_id = r.run_id AND b.alias = ?)"
            )
            values.append(baseline)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self.conn.execute(
            "SELECT r.data, r.status, r.created_at, r.committed_at, r.updated_at "
            "FROM runs r" + where + " ORDER BY r.created_at DESC, r.run_id DESC LIMIT ? OFFSET ?",
            (*values, limit, offset),
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

    def list_run_metadata(self, run_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(run_ids))
        metadata: dict[str, dict[str, Any]] = {
            run_id: {"tags": [], "note": None, "baselines": []} for run_id in ids
        }
        if not ids:
            return metadata
        # Keep below SQLite's traditional 999 bind-parameter limit so pages of 1000
        # remain portable to installations that do not use a newer, raised limit.
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            placeholders = ",".join("?" for _ in chunk)
            for row in self.conn.execute(
                f"SELECT run_id, tag FROM run_tags WHERE run_id IN ({placeholders}) ORDER BY tag",
                chunk,
            ):
                metadata[row["run_id"]]["tags"].append(row["tag"])
            for row in self.conn.execute(
                f"SELECT run_id, note FROM run_notes WHERE run_id IN ({placeholders})", chunk
            ):
                metadata[row["run_id"]]["note"] = row["note"]
            for row in self.conn.execute(
                f"SELECT run_id, alias FROM run_baselines WHERE run_id IN ({placeholders}) "
                "ORDER BY alias",
                chunk,
            ):
                metadata[row["run_id"]]["baselines"].append(row["alias"])
        return metadata

    def add_run_tag(self, run_id: str, tag: str) -> bool:
        if self.get_run(run_id) is None:
            raise KeyError(f"no run committed with run_id={run_id!r}")
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO run_tags (run_id, tag, created_at) VALUES (?, ?, ?)",
            (run_id, tag, _now()),
        )
        return cur.rowcount > 0

    def remove_run_tag(self, run_id: str, tag: str) -> bool:
        if self.get_run(run_id) is None:
            raise KeyError(f"no run committed with run_id={run_id!r}")
        cur = self.conn.execute("DELETE FROM run_tags WHERE run_id = ? AND tag = ?", (run_id, tag))
        return cur.rowcount > 0

    def set_run_note(self, run_id: str, note: str | None) -> bool:
        if self.get_run(run_id) is None:
            raise KeyError(f"no run committed with run_id={run_id!r}")
        if note is None:
            cur = self.conn.execute("DELETE FROM run_notes WHERE run_id = ?", (run_id,))
            return cur.rowcount > 0
        cur = self.conn.execute(
            "INSERT INTO run_notes (run_id, note, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at "
            "WHERE run_notes.note <> excluded.note",
            (run_id, note, _now()),
        )
        return cur.rowcount > 0

    def get_baseline(self, alias: str) -> RunBaseline | None:
        row = self.conn.execute(
            "SELECT alias, run_id, approved_by, promoted_at FROM run_baselines WHERE alias = ?",
            (alias,),
        ).fetchone()
        return RunBaseline(**dict(row)) if row is not None else None

    def list_baselines(self) -> list[RunBaseline]:
        rows = self.conn.execute(
            "SELECT alias, run_id, approved_by, promoted_at FROM run_baselines ORDER BY alias"
        ).fetchall()
        return [RunBaseline(**dict(row)) for row in rows]

    def promote_baseline(
        self, alias: str, run_id: str, approved_by: str
    ) -> tuple[RunBaseline, bool]:
        """Atomically move a named baseline and append its promotion audit record."""
        if self.get_run(run_id) is None:
            raise KeyError(f"no run committed with run_id={run_id!r}")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            old = self.conn.execute(
                "SELECT run_id FROM run_baselines WHERE alias = ?", (alias,)
            ).fetchone()
            previous_run_id = old["run_id"] if old is not None else None
            if (
                old is not None
                and previous_run_id == run_id
                and self.conn.execute(
                    "SELECT approved_by FROM run_baselines WHERE alias = ?", (alias,)
                ).fetchone()["approved_by"]
                == approved_by
            ):
                self.conn.execute("COMMIT")
                current = self.get_baseline(alias)
                assert current is not None
                return current, False
            promoted_at = _now()
            self.conn.execute(
                "INSERT INTO baseline_promotions "
                "(alias, run_id, previous_run_id, approved_by, promoted_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (alias, run_id, previous_run_id, approved_by, promoted_at),
            )
            self.conn.execute(
                "INSERT INTO run_baselines (alias, run_id, approved_by, promoted_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(alias) DO UPDATE SET "
                "run_id = excluded.run_id, approved_by = excluded.approved_by, "
                "promoted_at = excluded.promoted_at",
                (alias, run_id, approved_by, promoted_at),
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        current = self.get_baseline(alias)
        assert current is not None
        return current, True

    def list_baseline_promotions(self, alias: str) -> list[BaselinePromotion]:
        rows = self.conn.execute(
            "SELECT alias, run_id, previous_run_id, approved_by, promoted_at "
            "FROM baseline_promotions WHERE alias = ? ORDER BY promotion_id DESC",
            (alias,),
        ).fetchall()
        return [BaselinePromotion(**dict(row)) for row in rows]

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

    def transition_work_item(
        self,
        run_id: str,
        task_key: str,
        *,
        from_states: Iterable[WorkItemState],
        to_state: WorkItemState,
        attempt: int | None = None,
        last_error: str | None = None,
    ) -> WorkItem | None:
        """Compare-and-set a work item's state: applied only if the item is currently in one
        of `from_states`, so a stale or duplicate transition can never overwrite a newer
        one. Returns the updated item, or None if the transition did not apply."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            updated = self._set_work_item_state(
                run_id, task_key, from_states, to_state, attempt, last_error
            )
            self.conn.execute("COMMIT")
            return updated
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def settle_work_items(
        self,
        run_id: str,
        settlements: Iterable[WorkItemSettlement],
        *,
        event_type: str,
        payload: dict[str, object],
    ) -> list[str]:
        """Apply several compare-and-set transitions and append one run event describing
        them, in a single transaction: either every applied transition and the event are
        committed, or none are. Recovery relies on this, because the event is the only
        record of work that may have run without a committed result. Returns the task keys
        whose transition applied."""
        applied: list[str] = []
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            for s in settlements:
                if self._set_work_item_state(
                    run_id, s.task_key, s.from_states, s.to_state, None, s.last_error
                ):
                    applied.append(s.task_key)
            self._insert_run_event(run_id, event_type, payload)
            self.conn.execute("COMMIT")
            return applied
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _set_work_item_state(
        self,
        run_id: str,
        task_key: str,
        from_states: Iterable[WorkItemState],
        to_state: WorkItemState,
        attempt: int | None,
        last_error: str | None,
    ) -> WorkItem | None:
        """The compare-and-set itself; the caller owns the transaction."""
        allowed = [s.value for s in from_states]
        row = self.conn.execute(
            "SELECT data, state FROM work_items WHERE run_id = ? AND task_key = ?",
            (run_id, task_key),
        ).fetchone()
        if row is None or row["state"] not in allowed:
            return None
        current = WorkItem.model_validate_json(row["data"])
        updated = current.model_copy(
            update={
                "state": to_state,
                "attempt": current.attempt if attempt is None else attempt,
                "last_error": last_error,
            }
        )
        data = updated.model_dump_json()
        self.conn.execute(
            "UPDATE work_items SET state = ?, attempt = ?, data = ?, content_hash = ?, "
            "updated_at = ? WHERE run_id = ? AND task_key = ?",
            (to_state.value, updated.attempt, data, _hash_of(data), _now(), run_id, task_key),
        )
        return updated

    # ---------------------------------------------------------------- run events

    def get_run_control_state(self, run_id: str) -> RunControlState | None:
        row = self.conn.execute(
            "SELECT desired_state, sequence, updated_at FROM run_control_state WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if row is None:
            return None
        return RunControlState(run_id, row[0], int(row[1]), row[2])

    def set_run_control_state(
        self,
        run_id: str,
        *,
        desired_state: str,
        action: str,
        requested_by: str,
        allowed_run_statuses: frozenset[str],
    ) -> RunControlState:
        """Persist a last-writer-wins control request and its audit event atomically."""
        if desired_state not in {"running", "paused", "cancelled"}:
            raise ValueError(f"invalid desired run control state {desired_state!r}")
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            run = self.conn.execute(
                "SELECT status FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(f"no run committed with run_id={run_id!r}")
            if run[0] not in allowed_run_statuses:
                raise ValueError(
                    f"run {run_id!r} is {run[0]}; it no longer accepts control requests"
                )
            current = self.conn.execute(
                "SELECT desired_state, sequence FROM run_control_state WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if current is not None and current[0] == "cancelled" and desired_state != "cancelled":
                raise ValueError(f"run {run_id!r} has a durable cancel request and cannot resume")
            sequence = (int(current[1]) if current is not None else 0) + 1
            updated_at = _now()
            self.conn.execute(
                "INSERT INTO run_control_state (run_id, desired_state, sequence, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "desired_state = excluded.desired_state, sequence = excluded.sequence, "
                "updated_at = excluded.updated_at",
                (run_id, desired_state, sequence, updated_at),
            )
            self._insert_run_event(
                run_id,
                "run_control_requested",
                {
                    "action": action,
                    "desired_state": desired_state,
                    "control_sequence": sequence,
                    "requested_by": requested_by,
                },
            )
            self.conn.execute("COMMIT")
            return RunControlState(run_id, desired_state, sequence, updated_at)
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def append_run_event(self, run_id: str, event_type: str, payload: dict[str, object]) -> int:
        """Append an event with the next per-run sequence number; returns that number."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            sequence = self._insert_run_event(run_id, event_type, payload)
            self.conn.execute("COMMIT")
            return sequence
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def _insert_run_event(self, run_id: str, event_type: str, payload: dict[str, object]) -> int:
        """The insert itself; the caller owns the transaction."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()
        sequence = int(row[0]) + 1
        self.conn.execute(
            "INSERT INTO run_events (run_id, sequence, event_type, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, sequence, event_type, json.dumps(payload, sort_keys=True), _now()),
        )
        return sequence

    # ---------------------------------------------------------------- run leases

    def acquire_run_lease(
        self,
        run_id: str,
        *,
        owner: str,
        host: str,
        pid: int,
        now: float,
        is_stale: Callable[[RunLease], bool],
    ) -> RunLease | None:
        """Take the run's single-session lease. Raises `LeaseHeld` if another session holds
        a lease that `is_stale` does not release; returns the stale lease that was replaced
        (its session ended without releasing it), or None."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute(
                "SELECT owner, host, pid, acquired_at, heartbeat_at FROM run_leases "
                "WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            previous = None
            if row is not None:
                previous = RunLease(run_id, row[0], row[1], int(row[2]), row[3], row[4])
                if previous.owner != owner and not is_stale(previous):
                    raise LeaseHeld(previous)
            self.conn.execute(
                "INSERT OR REPLACE INTO run_leases "
                "(run_id, owner, host, pid, acquired_at, heartbeat_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, owner, host, pid, now, now),
            )
            self.conn.execute("COMMIT")
            return previous
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def heartbeat_run_lease(self, run_id: str, owner: str, now: float) -> bool:
        """Refresh the lease; False if this session no longer holds it."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE run_leases SET heartbeat_at = ? WHERE run_id = ? AND owner = ?",
                (now, run_id, owner),
            )
        return cursor.rowcount == 1

    def release_run_lease(self, run_id: str, owner: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM run_leases WHERE run_id = ? AND owner = ?", (run_id, owner)
            )

    def list_run_events(self, run_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT sequence, event_type, payload, created_at FROM run_events "
            "WHERE run_id = ? AND sequence > ? ORDER BY sequence",
            (run_id, after),
        ).fetchall()
        return [
            {
                "sequence": row["sequence"],
                "event_type": row["event_type"],
                "payload": json.loads(row["payload"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # ---------------------------------------------------------------- remote jobs (17-T2)

    def commit_remote_job(
        self, job_id: str, run_id: str, plugin_id: str, state: str, fingerprint: str,
        data: dict[str, Any],
    ) -> None:  # fmt: skip
        """Record a remote job before anything is sent. Refuses a second job with the same
        ID (a resubmission is a new job, never an overwrite)."""
        now = _now()
        with self.conn:
            self.conn.execute(
                "INSERT INTO remote_jobs (job_id, run_id, plugin_id, state, fingerprint, data, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (job_id, run_id, plugin_id, state, fingerprint, json.dumps(data), now, now),
            )

    def update_remote_job(self, job_id: str, state: str, data: dict[str, Any]) -> None:
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE remote_jobs SET state = ?, data = ?, updated_at = ? WHERE job_id = ?",
                (state, json.dumps(data), _now(), job_id),
            )
        if cursor.rowcount != 1:
            raise ConflictError(f"no remote job {job_id!r}")

    def get_remote_job(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM remote_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._remote_job(row) if row else None

    def list_remote_jobs(self, run_id: str | None = None) -> list[dict[str, Any]]:
        if run_id is None:
            rows = self.conn.execute("SELECT * FROM remote_jobs ORDER BY created_at").fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM remote_jobs WHERE run_id = ? ORDER BY created_at", (run_id,)
            ).fetchall()
        return [self._remote_job(row) for row in rows]

    @staticmethod
    def _remote_job(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": row["job_id"],
            "run_id": row["run_id"],
            "plugin_id": row["plugin_id"],
            "state": row["state"],
            "fingerprint": row["fingerprint"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            **json.loads(row["data"]),
        }

    # ---------------------------------------------------------------- traces (16-T2)

    def commit_trace_observations(
        self, import_id: str, run_id: str, rows: list[tuple[str, str | None, bool, str]]
    ) -> int:
        """Store an import's per-trace observations (trace_id, execution_id, complete, data
        JSON) in one transaction. A run keeps one current observation per trace: a row for
        a trace already imported replaces it (the caller merged the spans of both). Returns
        the rows written."""
        now = _now()
        with self.conn:
            for trace_id, execution_id, complete, data in rows:
                self.conn.execute(
                    "DELETE FROM trace_observations WHERE run_id = ? AND trace_id = ?",
                    (run_id, trace_id),
                )
                self.conn.execute(
                    "INSERT INTO trace_observations (import_id, run_id, trace_id, "
                    "execution_id, complete, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (import_id, run_id, trace_id, execution_id, int(complete), data, now),
                )
        return len(rows)

    def list_trace_observations(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT import_id, trace_id, execution_id, complete, data FROM trace_observations "
            "WHERE run_id = ? ORDER BY import_id, trace_id",
            (run_id,),
        ).fetchall()
        return [
            {
                "import_id": row["import_id"],
                "trace_id": row["trace_id"],
                "execution_id": row["execution_id"],
                "complete": bool(row["complete"]),
                **json.loads(row["data"]),
            }
            for row in rows
        ]

    # ---------------------------------------------------------------- cache (16-T3)

    def get_cache_entry(self, kind: str, key: str) -> tuple[str, str] | None:
        """(run_id, record_id) of the cached record for `key`, or None."""
        row = self.conn.execute(
            "SELECT run_id, record_id FROM cache_entries WHERE kind = ? AND cache_key = ?",
            (kind, key),
        ).fetchone()
        return (row["run_id"], row["record_id"]) if row else None

    def put_cache_entry(self, kind: str, key: str, run_id: str, record_id: str) -> None:
        """The first record stored for a key stays its source (a later identical result is
        not a new source)."""
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO cache_entries (kind, cache_key, run_id, record_id, "
                "created_at) VALUES (?, ?, ?, ?, ?)",
                (kind, key, run_id, record_id, _now()),
            )

    def list_cache_entries(self, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT kind, cache_key, run_id, record_id, created_at FROM cache_entries "
            + ("WHERE kind = ? " if kind else "")
            + "ORDER BY created_at",
            (kind,) if kind else (),
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_cache(self, kind: str | None = None) -> int:
        """Invalidate cache entries (never the records they point at)."""
        with self.conn:
            cursor = self.conn.execute(
                "DELETE FROM cache_entries" + (" WHERE kind = ?" if kind else ""),
                (kind,) if kind else (),
            )
        return cursor.rowcount

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

    def list_execution_attempts(
        self, run_id: str, case_id: str | None = None
    ) -> list[ExecutionResult]:
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
            key = (
                result.run_id,
                result.case_id,
                result.repetition_id,
                result.metric_id,
                result.binding_hash or "",
                attempt_number,
            )
            existing = conn.execute(
                "SELECT content_hash FROM evaluation_attempts WHERE run_id = ? AND "
                "case_id = ? AND repetition_id = ? AND metric_id = ? AND binding_hash = ? "
                "AND attempt_number = ?",
                key,
            ).fetchone()
            if existing is not None:
                if existing[0] != digest:
                    raise ConflictError(
                        f"evaluation_attempts {key!r} already committed with different content"
                    )
                conn.execute("ROLLBACK")
                return False
            conn.execute(
                """
                INSERT INTO evaluation_attempts
                    (run_id, case_id, repetition_id, metric_id, binding_hash,
                     attempt_number, scoring_id, status, decision, content_hash, data,
                     committed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *key,
                    result.scoring_id,
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

    def next_evaluation_attempt_number(
        self, run_id: str, case_id: str, repetition_id: int, metric_id: str, binding_hash: str
    ) -> int:
        """Attempt N = the Nth time this binding scored this (case, repetition) of the run,
        across scoring passes. Single-writer convention (ADR 0001): numbering is not safe
        against a second concurrent writer on the same workspace."""
        row = self.conn.execute(
            "SELECT MAX(attempt_number) FROM evaluation_attempts WHERE run_id = ? AND "
            "case_id = ? AND repetition_id = ? AND metric_id = ? AND binding_hash = ?",
            (run_id, case_id, repetition_id, metric_id, binding_hash),
        ).fetchone()
        return 0 if row[0] is None else row[0] + 1

    def list_evaluation_attempts(self, run_id: str) -> list[EvaluationResult]:
        rows = self.conn.execute(
            "SELECT data FROM evaluation_attempts WHERE run_id = ? "
            "ORDER BY case_id, metric_id, attempt_number",
            (run_id,),
        ).fetchall()
        return [EvaluationResult.model_validate_json(row["data"]) for row in rows]

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

    def list_metric_results(
        self, run_id: str, case_id: str | None = None, *, scoring_id: str | None = None
    ) -> list[EvaluationResult]:
        results = self._list_metric_results(run_id, case_id)
        if scoring_id is None:
            return results
        return [r for r in results if r.scoring_id == scoring_id]

    def _list_metric_results(self, run_id: str, case_id: str | None) -> list[EvaluationResult]:
        if case_id is not None:
            rows = self.conn.execute(
                "SELECT data FROM metric_results WHERE run_id = ? AND case_id = ? "
                "ORDER BY committed_at, rowid",
                (run_id, case_id),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT data FROM metric_results WHERE run_id = ? ORDER BY committed_at, rowid",
                (run_id,),
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

    def get_artifact_by_digest(
        self, digest: str, *, mime_type: str | None = None
    ) -> ArtifactRef | None:
        """Return a durable ref for content-addressed bytes, optionally by media type."""
        if mime_type is None:
            row = self.conn.execute(
                "SELECT artifact_id, mime_type FROM artifacts WHERE digest = ? "
                "ORDER BY artifact_id LIMIT 1",
                (digest,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT artifact_id, mime_type FROM artifacts WHERE digest = ? AND mime_type = ? "
                "ORDER BY artifact_id LIMIT 1",
                (digest, mime_type),
            ).fetchone()
        return self.get_artifact(row["artifact_id"]) if row is not None else None

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
