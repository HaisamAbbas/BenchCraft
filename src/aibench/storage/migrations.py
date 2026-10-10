"""SQLite schema migrations (§14). Session/conversation tables (`sessions`,
`conversation_turns`, `decision_records`, `pending_questions`, `action_requests`,
`run_events`) are explicitly reserved for Prompt 08 and are not created here (02-T4).

Migrations are plain SQL, applied in order inside one transaction each, and tracked in
`schema_migrations` so re-running `apply_migrations` on an already-migrated database is a
no-op. Add new migrations by appending to `MIGRATIONS`; never edit an already-shipped one.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from aibench.core.errors import WorkspaceTooNew


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    # Optional Python step run after `sql`, inside the same open transaction, for changes
    # SQL alone can't express — e.g. backfilling a new column from existing row data using
    # our own hashing logic (SQLite has no built-in SHA-256). Most migrations don't need one.
    post_apply: Callable[[sqlite3.Connection], None] | None = None


_0001_initial = Migration(
    version=1,
    name="initial_schema",
    sql="""
    CREATE TABLE datasets (
        content_hash        TEXT PRIMARY KEY,
        dataset_id          TEXT NOT NULL,
        schema_version      TEXT NOT NULL,
        case_count          INTEGER NOT NULL,
        source_refs         TEXT NOT NULL,   -- JSON array
        split               TEXT,
        duplicate_case_ids  TEXT NOT NULL,   -- JSON array
        created_at          TEXT NOT NULL,
        committed_at        TEXT NOT NULL
    );
    CREATE INDEX idx_datasets_dataset_id ON datasets(dataset_id);

    CREATE TABLE cases (
        dataset_content_hash TEXT NOT NULL REFERENCES datasets(content_hash),
        case_id              TEXT NOT NULL,
        source_line          INTEGER,
        duplicate_of_line    INTEGER,
        group_id             TEXT,
        data                 TEXT NOT NULL,  -- canonical JSON of BenchmarkCase
        committed_at         TEXT NOT NULL,
        PRIMARY KEY (dataset_content_hash, case_id, source_line)
    );
    CREATE INDEX idx_cases_group_id ON cases(dataset_content_hash, group_id);

    CREATE TABLE applications (
        application_id      TEXT PRIMARY KEY,
        content_hash         TEXT NOT NULL,
        runner               TEXT NOT NULL,
        target                TEXT NOT NULL,
        data                 TEXT NOT NULL,  -- canonical JSON of ApplicationSpec
        committed_at         TEXT NOT NULL
    );

    CREATE TABLE profiles (
        observation_id       TEXT PRIMARY KEY,
        application_id       TEXT NOT NULL REFERENCES applications(application_id),
        capability           TEXT NOT NULL,
        state                TEXT NOT NULL,
        data                 TEXT NOT NULL,  -- canonical JSON of ObservationClaim
        committed_at         TEXT NOT NULL
    );
    CREATE INDEX idx_profiles_application_id ON profiles(application_id);

    CREATE TABLE plans (
        plan_id              TEXT PRIMARY KEY,
        content_hash         TEXT NOT NULL,
        policy_hash          TEXT,
        data                 TEXT NOT NULL,  -- canonical JSON of EvaluationPlan
        committed_at         TEXT NOT NULL
    );

    CREATE TABLE runs (
        run_id                TEXT PRIMARY KEY,
        dataset_hash          TEXT NOT NULL,
        application_hash      TEXT NOT NULL,
        plan_hash              TEXT NOT NULL,
        content_hash          TEXT NOT NULL,  -- hash of the full manifest, for idempotency checks
        status                TEXT NOT NULL DEFAULT 'created',
        data                 TEXT NOT NULL,  -- canonical JSON of RunManifest
        created_at            TEXT NOT NULL,
        committed_at          TEXT NOT NULL,
        updated_at            TEXT NOT NULL
    );

    CREATE TABLE work_items (
        work_item_id          TEXT PRIMARY KEY,
        run_id                TEXT NOT NULL REFERENCES runs(run_id),
        task_key              TEXT NOT NULL,
        kind                  TEXT NOT NULL,
        state                 TEXT NOT NULL,
        attempt               INTEGER NOT NULL DEFAULT 0,
        data                 TEXT NOT NULL,  -- canonical JSON of WorkItem
        committed_at          TEXT NOT NULL,
        updated_at            TEXT NOT NULL,
        UNIQUE (run_id, task_key)
    );
    CREATE INDEX idx_work_items_run_id ON work_items(run_id);

    CREATE TABLE execution_attempts (
        execution_id          TEXT PRIMARY KEY,
        run_id                TEXT NOT NULL REFERENCES runs(run_id),
        case_id               TEXT NOT NULL,
        repetition_id         INTEGER NOT NULL,
        attempt_id            INTEGER NOT NULL,
        status                TEXT NOT NULL,
        content_hash          TEXT NOT NULL,
        data                 TEXT NOT NULL,  -- canonical JSON of ExecutionResult
        committed_at          TEXT NOT NULL
    );
    CREATE INDEX idx_execution_attempts_run_case
        ON execution_attempts(run_id, case_id);

    CREATE TABLE evaluation_attempts (
        run_id                TEXT NOT NULL REFERENCES runs(run_id),
        case_id               TEXT NOT NULL,
        metric_id             TEXT NOT NULL,
        attempt_number        INTEGER NOT NULL,
        status                TEXT NOT NULL,
        decision              TEXT NOT NULL,
        content_hash          TEXT NOT NULL,
        data                 TEXT NOT NULL,  -- canonical JSON of EvaluationResult
        committed_at          TEXT NOT NULL,
        PRIMARY KEY (run_id, case_id, metric_id, attempt_number)
    );

    CREATE TABLE metric_results (
        result_id             TEXT PRIMARY KEY,
        run_id                TEXT NOT NULL REFERENCES runs(run_id),
        case_id               TEXT NOT NULL,
        metric_id             TEXT NOT NULL,
        status                TEXT NOT NULL,
        decision              TEXT NOT NULL,
        content_hash          TEXT NOT NULL,
        data                 TEXT NOT NULL,  -- canonical JSON of EvaluationResult
        committed_at          TEXT NOT NULL
    );
    CREATE INDEX idx_metric_results_run_case_metric
        ON metric_results(run_id, case_id, metric_id);

    CREATE TABLE artifacts (
        artifact_id           TEXT PRIMARY KEY,
        digest                TEXT NOT NULL,
        uri                    TEXT NOT NULL,
        mime_type              TEXT NOT NULL,
        size_bytes             INTEGER NOT NULL,
        redaction              TEXT NOT NULL,
        run_id                 TEXT REFERENCES runs(run_id),
        committed_at           TEXT NOT NULL
    );
    CREATE INDEX idx_artifacts_digest ON artifacts(digest);
    CREATE INDEX idx_artifacts_run_id ON artifacts(run_id);

    CREATE TABLE usage_events (
        usage_event_id         TEXT PRIMARY KEY,
        run_id                 TEXT NOT NULL REFERENCES runs(run_id),
        role                   TEXT NOT NULL,
        provider               TEXT,
        cost                   REAL,
        data                   TEXT NOT NULL,  -- canonical JSON of UsageEvent
        committed_at           TEXT NOT NULL
    );
    CREATE INDEX idx_usage_events_run_id ON usage_events(run_id);

    CREATE TABLE approvals (
        approval_id            TEXT PRIMARY KEY,
        scope_hash              TEXT NOT NULL,
        data                    TEXT NOT NULL,  -- canonical JSON of Approval
        committed_at             TEXT NOT NULL
    );
    CREATE INDEX idx_approvals_scope_hash ON approvals(scope_hash);
    """,
)

_0002_run_lookup_indexes = Migration(
    version=2,
    name="run_lookup_indexes",
    sql="""
    CREATE INDEX idx_runs_status ON runs(status);
    CREATE INDEX idx_runs_created_at ON runs(created_at);
    """,
)


def _backfill_content_hash_columns(conn: sqlite3.Connection) -> None:
    """Populate the `content_hash`/`manifest_hash` columns `_0003` just added, for any rows
    that existed before this migration ran. Computing these requires our own hashing logic
    (SQLite has no SHA-256 builtin), hence a Python `post_apply` step rather than pure SQL.

    For tables whose `data` column already holds the model's full canonical JSON (`cases`,
    `profiles`, `work_items`, `usage_events`, `approvals`), the backfilled hash is simply
    `content_hash(data)` — exactly what a fresh commit of byte-identical content would
    compute, since `Storage`'s commit methods hash that same already-serialized string
    rather than re-serializing. For `datasets` and `artifacts`, which store individual
    columns instead of one JSON blob, the row is reconstructed into the corresponding core
    model and re-serialized the same way `Storage.commit_dataset`/`commit_artifact` do
    before hashing, so the backfilled value matches what those methods would compute for
    identical field values — a later identical commit is correctly treated as a no-op, not a
    false-positive conflict.
    """
    import json

    from aibench.core.hashes import content_hash
    from aibench.core.models import ArtifactRef, DatasetManifest

    for table in ("cases", "profiles", "work_items", "usage_events", "approvals"):
        for row in conn.execute(f"SELECT rowid, data FROM {table}").fetchall():
            digest = content_hash(row[1])
            conn.execute(
                f"UPDATE {table} SET content_hash = ? WHERE rowid = ?",
                (digest, row[0]),
            )

    for row in conn.execute(
        "SELECT content_hash, dataset_id, schema_version, case_count, source_refs, "
        "split, duplicate_case_ids, created_at FROM datasets"
    ).fetchall():
        manifest = DatasetManifest(
            content_hash=row[0],
            dataset_id=row[1],
            schema_version=row[2],
            case_count=row[3],
            source_refs=tuple(json.loads(row[4])),
            split=row[5],
            duplicate_case_ids=tuple(json.loads(row[6])),
            created_at=row[7],
        )
        digest = content_hash(manifest.model_dump_json())
        conn.execute(
            "UPDATE datasets SET manifest_hash = ? WHERE content_hash = ?",
            (digest, row[0]),
        )

    for row in conn.execute(
        "SELECT artifact_id, digest, uri, mime_type, size_bytes, redaction, run_id FROM artifacts"
    ).fetchall():
        ref = ArtifactRef(
            artifact_id=row[0],
            digest=row[1],
            uri=row[2],
            mime_type=row[3],
            size_bytes=row[4],
            redaction=row[5],
            run_id=row[6],
        )
        digest = content_hash(ref.model_dump_json())
        conn.execute(
            "UPDATE artifacts SET content_hash = ? WHERE artifact_id = ?",
            (digest, row[0]),
        )


_0003_content_hash_columns = Migration(
    version=3,
    name="content_hash_columns_for_conflict_detection",
    sql="""
    ALTER TABLE datasets ADD COLUMN manifest_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE cases ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE profiles ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE work_items ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE artifacts ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE usage_events ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
    ALTER TABLE approvals ADD COLUMN content_hash TEXT NOT NULL DEFAULT '';
    """,
    post_apply=_backfill_content_hash_columns,
)


def _backfill_evaluation_attempt_keys(conn: sqlite3.Connection) -> None:
    """Fill the key columns `_0004` added from each row's stored `EvaluationResult` JSON."""
    import json

    rows = conn.execute("SELECT rowid, data FROM evaluation_attempts").fetchall()
    for rowid, data in rows:
        record = json.loads(data)
        conn.execute(
            "UPDATE evaluation_attempts SET repetition_id = ?, binding_hash = ?, "
            "scoring_id = ? WHERE rowid = ?",
            (
                record.get("repetition_id", 0),
                record.get("binding_hash") or "",
                record.get("scoring_id"),
                rowid,
            ),
        )


# Evaluation attempts were keyed by (run, case, metric, attempt), so two bindings of one
# metric, or two repetitions of one case, shared one attempt counter. Attempts are now
# numbered per (run, case, repetition, metric, binding): attempt N means "the Nth time
# this binding scored this execution". SQLite cannot alter a primary key, so the table is
# rebuilt; existing rows keep their attempt numbers and gain their key values.
_0004_evaluation_attempt_identity = Migration(
    version=4,
    name="evaluation_attempts_keyed_by_repetition_and_binding",
    sql="""
    CREATE TABLE evaluation_attempts_v4 (
        run_id                TEXT NOT NULL REFERENCES runs(run_id),
        case_id               TEXT NOT NULL,
        repetition_id         INTEGER NOT NULL DEFAULT 0,
        metric_id             TEXT NOT NULL,
        binding_hash          TEXT NOT NULL DEFAULT '',
        attempt_number        INTEGER NOT NULL,
        scoring_id            TEXT,
        status                TEXT NOT NULL,
        decision              TEXT NOT NULL,
        content_hash          TEXT NOT NULL,
        data                  TEXT NOT NULL,
        committed_at          TEXT NOT NULL,
        PRIMARY KEY (run_id, case_id, repetition_id, metric_id, binding_hash, attempt_number)
    );
    INSERT INTO evaluation_attempts_v4
        (run_id, case_id, metric_id, attempt_number, status, decision, content_hash, data,
         committed_at)
    SELECT run_id, case_id, metric_id, attempt_number, status, decision, content_hash, data,
           committed_at
    FROM evaluation_attempts;
    DROP TABLE evaluation_attempts;
    ALTER TABLE evaluation_attempts_v4 RENAME TO evaluation_attempts;
    CREATE INDEX idx_evaluation_attempts_scoring ON evaluation_attempts(scoring_id);
    """,
    post_apply=_backfill_evaluation_attempt_keys,
)

# Durable run events (§14 "Store emitted run events with sequence numbers"; 06-T4). Prompt 02
# reserved this table for Prompt 08; the engine needs it first. Sequence numbers are
# per run and strictly increasing, so a reconnecting client can replay what it missed.
_0005_run_events = Migration(
    version=5,
    name="run_events",
    sql="""
    CREATE TABLE run_events (
        run_id       TEXT NOT NULL REFERENCES runs(run_id),
        sequence     INTEGER NOT NULL,
        event_type   TEXT NOT NULL,
        payload      TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        PRIMARY KEY (run_id, sequence)
    );
    """,
)

# One live session per run (06-T1 single writer): a session holds the lease while it
# runs and heartbeats it; a second session is refused unless the lease is stale.
_0006_run_leases = Migration(
    version=6,
    name="run_leases",
    sql="""
    CREATE TABLE run_leases (
        run_id        TEXT PRIMARY KEY REFERENCES runs(run_id),
        owner         TEXT NOT NULL,
        host          TEXT NOT NULL,
        pid           INTEGER NOT NULL,
        acquired_at   REAL NOT NULL,
        heartbeat_at  REAL NOT NULL
    );
    """,
)

# Benchmark sessions (§14, 08-T1): the conversation, its decisions, questions and typed
# action requests. Session revisions advance by compare-and-set, and each revision has at
# most one decision, so a stale patch can never overwrite a newer choice. Action IDs are
# primary keys, so a redelivered action is recognized instead of carried out twice. Runs
# are not owned by sessions: deleting a conversation never deletes run results.
_0007_sessions = Migration(
    version=7,
    name="sessions",
    sql="""
    CREATE TABLE sessions (
        session_id          TEXT PRIMARY KEY,
        revision            INTEGER NOT NULL,
        active_run_id       TEXT,
        data                TEXT NOT NULL,
        created_at          TEXT NOT NULL,
        updated_at          TEXT NOT NULL
    );
    CREATE TABLE conversation_turns (
        turn_id      TEXT PRIMARY KEY,
        session_id   TEXT NOT NULL REFERENCES sessions(session_id),
        sequence     INTEGER NOT NULL,
        role         TEXT NOT NULL,
        message_id   TEXT,
        replies_to   TEXT,
        data         TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        UNIQUE (session_id, sequence)
    );
    CREATE UNIQUE INDEX idx_turns_delivery
        ON conversation_turns(session_id, role, message_id) WHERE message_id IS NOT NULL;
    CREATE UNIQUE INDEX idx_turns_reply
        ON conversation_turns(replies_to) WHERE replies_to IS NOT NULL;
    CREATE TABLE decision_records (
        decision_id  TEXT PRIMARY KEY,
        session_id   TEXT NOT NULL REFERENCES sessions(session_id),
        revision     INTEGER NOT NULL,
        data         TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        UNIQUE (session_id, revision)
    );
    CREATE TABLE pending_questions (
        session_id      TEXT NOT NULL REFERENCES sessions(session_id),
        question_id     TEXT NOT NULL,
        draft_revision  INTEGER NOT NULL,
        status          TEXT NOT NULL,
        data            TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        PRIMARY KEY (session_id, question_id)
    );
    CREATE TABLE action_requests (
        action_id    TEXT PRIMARY KEY,
        session_id   TEXT NOT NULL REFERENCES sessions(session_id),
        kind         TEXT NOT NULL,
        state        TEXT NOT NULL,
        run_id       TEXT,
        data         TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX idx_action_requests_session ON action_requests(session_id);
    """,
)

# Prompt 16: imported trace observations (16-T2) and the explicit cross-run cache (16-T3).
_0008_traces_and_cache = Migration(
    version=8,
    name="traces_and_cache",
    sql="""
    CREATE TABLE trace_observations (
        import_id     TEXT NOT NULL,
        run_id        TEXT NOT NULL REFERENCES runs(run_id),
        trace_id      TEXT NOT NULL,
        execution_id  TEXT,
        complete      INTEGER NOT NULL,
        data          TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        PRIMARY KEY (import_id, trace_id)
    );
    CREATE INDEX idx_trace_observations_run ON trace_observations(run_id);
    CREATE TABLE cache_entries (
        kind        TEXT NOT NULL,
        cache_key   TEXT NOT NULL,
        run_id      TEXT NOT NULL,
        record_id   TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (kind, cache_key)
    );
    """,
)

# Prompt 18: development-only candidate pools and append-only review/verification history.
_0009_candidate_workflow = Migration(
    version=9,
    name="candidate_workflow",
    sql="""
    CREATE TABLE candidate_pools (
        pool_id       TEXT PRIMARY KEY,
        content_hash  TEXT NOT NULL,
        data          TEXT NOT NULL, -- canonical JSON of CandidatePoolManifest
        created_at    TEXT NOT NULL
    );

    CREATE TABLE candidate_cases (
        candidate_id  TEXT PRIMARY KEY,
        pool_id       TEXT NOT NULL REFERENCES candidate_pools(pool_id),
        split_id      TEXT NOT NULL CHECK (split_id = 'development'),
        status        TEXT NOT NULL,
        content_hash  TEXT NOT NULL,
        data          TEXT NOT NULL, -- canonical JSON of DatasetCandidate
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL
    );
    CREATE INDEX idx_candidate_cases_pool_status
        ON candidate_cases(pool_id, status, created_at);

    CREATE TABLE candidate_events (
        event_id      TEXT PRIMARY KEY,
        candidate_id  TEXT NOT NULL REFERENCES candidate_cases(candidate_id),
        kind          TEXT NOT NULL,
        data          TEXT NOT NULL, -- immutable CandidateEvent record
        created_at    TEXT NOT NULL
    );
    CREATE INDEX idx_candidate_events_candidate
        ON candidate_events(candidate_id, created_at, event_id);
    """,
)

# Prompt 17: remote evaluation jobs (17-T2). The request fingerprint and the request itself
# are stored before anything is sent; remote identifiers as soon as they are known.
_0010_remote_jobs = Migration(
    version=10,
    name="remote_jobs",
    sql="""
    CREATE TABLE remote_jobs (
        job_id       TEXT PRIMARY KEY,
        run_id       TEXT NOT NULL REFERENCES runs(run_id),
        plugin_id    TEXT NOT NULL,
        state        TEXT NOT NULL,
        fingerprint  TEXT NOT NULL,
        data         TEXT NOT NULL, -- JSON: remote IDs, request artifact, counts, history
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL
    );
    CREATE INDEX idx_remote_jobs_run ON remote_jobs(run_id);
    """,
)

# Prompt 19: frozen experiment contracts, reproducible trials and protected holdout digests.
_0011_experiments = Migration(
    version=11,
    name="controlled_experiments",
    sql="""
    CREATE TABLE experiments (
        experiment_id  TEXT PRIMARY KEY,
        status         TEXT NOT NULL,
        content_hash   TEXT NOT NULL,
        data           TEXT NOT NULL, -- frozen ExperimentRecord plus current phase state
        created_at     TEXT NOT NULL,
        updated_at     TEXT NOT NULL
    );
    CREATE TABLE experiment_trials (
        trial_id        TEXT PRIMARY KEY,
        experiment_id   TEXT NOT NULL REFERENCES experiments(experiment_id),
        ordinal         INTEGER NOT NULL,
        run_id          TEXT NOT NULL UNIQUE,
        status          TEXT NOT NULL,
        parameter_hash  TEXT NOT NULL,
        data            TEXT NOT NULL, -- ExperimentTrial; immutable parameters and lineage
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL,
        UNIQUE (experiment_id, ordinal)
    );
    CREATE INDEX idx_experiment_trials_status
        ON experiment_trials(experiment_id, status, ordinal);

    CREATE TABLE experiment_events (
        event_id       TEXT PRIMARY KEY,
        experiment_id  TEXT NOT NULL REFERENCES experiments(experiment_id),
        kind           TEXT NOT NULL,
        data           TEXT NOT NULL, -- append-only ExperimentEvent
        created_at     TEXT NOT NULL
    );
    CREATE INDEX idx_experiment_events_experiment
        ON experiment_events(experiment_id, created_at, event_id);

    CREATE TABLE protected_dataset_digests (
        digest         TEXT PRIMARY KEY,
        experiment_id  TEXT NOT NULL REFERENCES experiments(experiment_id),
        split_id       TEXT NOT NULL CHECK (split_id = 'holdout'),
        registered_at  TEXT NOT NULL
    );
    """,
)

# Goal 06: searchable run annotations and explicitly approved named baselines.
_0012_run_catalog = Migration(
    version=12,
    name="run_catalog",
    sql="""
    CREATE TABLE run_tags (
        run_id      TEXT NOT NULL REFERENCES runs(run_id),
        tag         TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (run_id, tag)
    );
    CREATE INDEX idx_run_tags_tag_run ON run_tags(tag, run_id);

    CREATE TABLE run_notes (
        run_id      TEXT PRIMARY KEY REFERENCES runs(run_id),
        note        TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE run_baselines (
        alias        TEXT PRIMARY KEY,
        run_id       TEXT NOT NULL REFERENCES runs(run_id),
        approved_by  TEXT NOT NULL,
        promoted_at  TEXT NOT NULL
    );
    CREATE INDEX idx_run_baselines_run ON run_baselines(run_id, alias);

    CREATE TABLE baseline_promotions (
        promotion_id     INTEGER PRIMARY KEY AUTOINCREMENT,
        alias            TEXT NOT NULL,
        run_id           TEXT NOT NULL REFERENCES runs(run_id),
        previous_run_id  TEXT REFERENCES runs(run_id),
        approved_by      TEXT NOT NULL,
        promoted_at      TEXT NOT NULL
    );
    CREATE INDEX idx_baseline_promotions_alias
        ON baseline_promotions(alias, promotion_id DESC);
    """,
)

# External headless supervisors update a durable desired state; a running engine polls it.
_0013_run_control = Migration(
    version=13,
    name="run_control",
    sql="""
    CREATE TABLE run_control_state (
        run_id         TEXT PRIMARY KEY REFERENCES runs(run_id),
        desired_state  TEXT NOT NULL CHECK (desired_state IN ('running', 'paused', 'cancelled')),
        sequence       INTEGER NOT NULL CHECK (sequence > 0),
        updated_at     TEXT NOT NULL
    );
    """,
)

_0014_dataset_suites = Migration(
    version=14,
    name="dataset_suite_catalog",
    sql="""
    CREATE TABLE dataset_suites (
        suite_name          TEXT NOT NULL,
        suite_version       TEXT NOT NULL,
        dataset_content_hash TEXT NOT NULL,
        dataset_path        TEXT NOT NULL,
        case_count          INTEGER NOT NULL CHECK (case_count > 0),
        description         TEXT NOT NULL,
        record_hash         TEXT NOT NULL,
        created_at          TEXT NOT NULL,
        PRIMARY KEY (suite_name, suite_version)
    );
    CREATE INDEX idx_dataset_suites_hash ON dataset_suites(dataset_content_hash);
    """,
)

MIGRATIONS: tuple[Migration, ...] = (
    _0001_initial,
    _0002_run_lookup_indexes,
    _0003_content_hash_columns,
    _0004_evaluation_attempt_identity,
    _0005_run_events,
    _0006_run_leases,
    _0007_sessions,
    _0008_traces_and_cache,
    _0009_candidate_workflow,
    _0010_remote_jobs,
    _0011_experiments,
    _0012_run_catalog,
    _0013_run_control,
    _0014_dataset_suites,
)


def _ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version     INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            applied_at  TEXT NOT NULL
        )
        """
    )


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def apply_migrations(conn: sqlite3.Connection) -> list[int]:
    """Apply every migration in `MIGRATIONS` not yet recorded in `schema_migrations`, each
    as one atomic transaction (modern SQLite supports transactional DDL). The migration's SQL
    is run via a `BEGIN`-prefixed `executescript` that deliberately leaves the transaction
    open (no trailing `COMMIT` in the script), so its optional `post_apply` Python step — for
    changes SQL alone can't express, like backfilling a new column via our own hashing logic
    — runs inside the *same* transaction, before a final explicit `COMMIT` closes it along
    with recording the migration as applied. If anything fails partway (the SQL or the
    Python step), the whole transaction is rolled back, so a crash mid-migration never leaves
    a half-applied schema or a recorded-but-not-really-applied version.

    Returns the versions actually applied this call (empty on an already up-to-date
    database — idempotent and restart-safe). Refuses a database that records a migration
    this version doesn't know (`WorkspaceTooNew`): it was written by a newer aibench."""
    from datetime import UTC, datetime

    _ensure_migrations_table(conn)
    already = applied_versions(conn)
    unknown = already - {m.version for m in MIGRATIONS}
    if unknown:
        raise WorkspaceTooNew(
            f"this workspace was upgraded by a newer aibench (schema version {max(unknown)}; "
            f"this aibench knows up to {MIGRATIONS[-1].version}). Install that newer version "
            "to use it; an older one could damage it"
        )
    newly_applied: list[int] = []
    for migration in MIGRATIONS:
        if migration.version in already:
            continue
        applied_at = datetime.now(UTC).isoformat()
        script = "BEGIN;\n" + migration.sql  # no trailing COMMIT: stays open for post_apply
        try:
            conn.executescript(script)
            if migration.post_apply is not None:
                migration.post_apply(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (migration.version, migration.name, applied_at),
            )
            conn.execute("COMMIT")
        except BaseException:
            # A mid-transaction failure (SQL or the Python post_apply step) leaves the
            # literal BEGIN's transaction open rather than rolled back — sqlite3's
            # implicit-transaction handling only applies to statements it issues itself, not
            # to a literal BEGIN we supplied — and an open, uncommitted transaction is still
            # visible to *this* connection even though it was never durably committed. Roll
            # it back explicitly so a caller inspecting schema state on this same connection
            # sees the true (unmigrated) state, not a phantom partially-applied one.
            conn.rollback()
            raise
        newly_applied.append(migration.version)
    return newly_applied
