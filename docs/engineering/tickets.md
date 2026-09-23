# Ticket ledger

Ticket IDs follow `NN-T#`; gate IDs follow `NN-G#`. Status: DONE | IN_PROGRESS | BLOCKED | TODO.

## Prompt 00 — Bootstrap and specification traceability

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 00-T1 | Establish source of truth: copy v1.1 plan + prompt pack to `docs/spec/`, record hash | DONE | `docs/spec/implementation-plan.md`, `docs/spec/SOURCE.md` |
| 00-T2 | Create development foundation: pyproject.toml, src/aibench, CLI entry, locked dev deps, platform matrix | DONE | `pyproject.toml`, `src/aibench/cli/main.py`, `requirements-dev.lock.txt`, `docs/engineering/platform-matrix.md` |
| 00-T3 | Persist engineering controls: contract, phase-status, requirements-matrix, tickets, ADR | DONE | `docs/engineering/*.md`, `docs/adr/0001-*.md` |
| 00-T4 | Create repeatable developer checks: lint/type/test/build/smoke, bootstrap tests, CI | DONE | `tests/test_bootstrap.py`, `.github/workflows/ci.yml`, this contract's "Standard check convention" |

Gates: 00-G1 (fresh install + `--help`), 00-G2 (requirements matrix complete),
00-G3 (no speculative components), 00-G4 (tests run + report). See
`docs/engineering/reports/00.md`.

## Prompt 01 — Canonical models, configuration, and datasets

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 01-T1 | Model immutable inputs/typed outputs; export JSON Schemas | DONE | `src/aibench/core/models.py` (deep-frozen via `FrozenValue`; explicit `application_id`/`observation_id`/`execution_id`/`artifact_id` identities), `schemas/*.json` |
| 01-T2 | Dataset normalization (shorthand, extensions, duplicate IDs, line errors) | DONE | `src/aibench/datasets/normalize.py` (namespaced-extension enforcement, explicit type checks on `reference`/`provenance`/`fixtures`/`expectations`/`metadata`, pydantic-error-to-line-error conversion), `src/aibench/datasets/ingest.py` (`retain_cases` bounded-memory mode, defensive exception boundary) |
| 01-T3 | Config precedence, path resolution, secret refs, hashes, redaction | DONE | `src/aibench/config/model.py`, `src/aibench/config/resolve.py` |
| 01-T4 | `aibench dataset validate PATH` + fixtures | DONE | `src/aibench/cli/dataset.py` (uses `retain_cases=False`), `examples/datasets/*` (+ `invalid.nested.jsonl`) |

Gates: 01-G1..G4, all satisfied — see `docs/engineering/reports/01.md` for the two review-driven
remediation passes: (1) deep immutability, bounded-memory ingestion mode, nested-input error
handling, extension namespacing, explicit identities; (2) disk-backed duplicate-ID index for
large files (`src/aibench/datasets/ingest.py::_DedupIndex`) and a more robust, sandbox-aware
pytest temp-directory selection (`tests/conftest.py`).

## Prompt 02 — Durable run storage and artifacts

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 02-T1 | Persistence repositories: datasets, cases, applications, profiles, plans, runs, work items, execution/evaluation attempts, metric results, artifacts, usage, approvals | DONE | `src/aibench/storage/migrations.py` (13 tables + `schema_migrations`, 3 versioned migrations), `src/aibench/storage/repositories.py` (`Storage` facade, transactional/idempotent commits, `ConflictError` on mismatched duplicate commits) |
| 02-T2 | Artifact commit protocol: temp write, flush+fsync, atomic rename, then separate DB commit; content-addressed dedup; orphan GC with grace period; path-validated, content-verified reads and commits; verification enforced at the only production-intended commit entry point | DONE | `src/aibench/storage/artifacts.py` (`ArtifactStore`, `verify_ref`, `_resolve_and_validate_uri`, `commit_verified_artifact`); `src/aibench/storage/repositories.py::Storage.commit_artifact_unverified` (renamed and docstring-flagged; not the recommended call site) |
| 02-T3 | Record attempts/manifests; `runs list`/`runs show` with machine-readable output | DONE | `src/aibench/storage/repositories.py::RunRecord`, `src/aibench/cli/runs.py` |
| 02-T4 | Restart-safe loading; unique commit behavior; session storage explicitly deferred to Prompt 08 | DONE | `src/aibench/storage/db.py::Database.open`/`Workspace`/`open_in_memory`; migrations deliberately exclude `sessions`/`conversation_turns`/`decision_records`/`pending_questions`/`action_requests`/`run_events` |

Gates: 02-G1..G4, all satisfied — see `docs/engineering/reports/02.md`, including two review
remediation passes. §1a: `commit_artifact` conflict-checks the complete `ArtifactRef` content
(not just `digest`), `ArtifactStore` rejects out-of-root URIs and verifies file existence/
size/digest before every read or verified commit, and the migration/repository pure-logic
test suites moved to in-memory SQLite (`Database.open_in_memory`) to remove their dependency
on a writable temp directory. §1b: the artifacts schema change moved out of the already-numbered
migration 1 into a proper new migration 3 (with a Python `post_apply` backfill hook and a test
proving a pre-migration-3 database upgrades cleanly), and the unverified commit method was
renamed to `commit_artifact_unverified` so `commit_verified_artifact` is unambiguously the only
production-intended entry point. New core identity models added to support this phase: `WorkItem`/`WorkItemState`,
`UsageEvent`/`UsageRole`, `Approval` (`src/aibench/core/models.py`); new `ConflictError`
(`src/aibench/core/errors.py`).

## Prompt 03 — Application runners and observation capture

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 03-T1 | Runner lifecycle: describe/prepare/healthcheck/invoke/reset/close; cancellation, timeouts, declared effects, observation envelopes; only app-visible fields mapped | DONE | `src/aibench/runners/base.py` (`BaseRunner`, `InvocationContext`, `InvocationOutcome`, `race`), `src/aibench/runners/bindings.py` (`AppInputEnvelope`, JSON Pointer `InputBinding`/`OutputBinding`), `core/models.py` (`CliTransport`, `HttpTransport`, `ErrorKind`, `EffectState`); tests `test_lifecycle_order_is_enforced`, `test_cooperative_cancel_*`, `test_task_cancellation_*`, `tests/test_runner_bindings.py` |
| 03-T2 | CLI transport: argv/no shell, JSON stdin/stdout, bounded stderr, legacy text mode, output limits, process-tree cleanup | DONE | `src/aibench/runners/cli_runner.py`, `src/aibench/runners/process_tree.py` (Windows Job Object / POSIX process group); `tests/test_cli_runner.py` (21 tests) |
| 03-T3 | HTTP transport: bindings, secret refs, TLS verification, endpoint policy, size/time limits, correlation IDs, redirect validation | DONE | `src/aibench/runners/http_runner.py`, `src/aibench/security/endpoints.py`, `src/aibench/security/secrets.py`; `tests/test_http_runner.py` (16 tests incl. real TLS handshake), `tests/test_endpoint_policy.py` |
| 03-T4 | Real local fixtures; persistence via Prompt 02; developer smoke path | DONE | `examples/apps/` (CLI chatbot, HTTP RAG, black-box text app, effect counter + `*.app.json`), `examples/datasets/booking.valid.jsonl`, `src/aibench/services/execution.py`, `src/aibench/cli/app.py` (`aibench app describe`, `aibench app smoke`); `tests/test_execution_service.py`, `tests/test_cli_app.py` |

Gates: 03-G1..G5. See `docs/engineering/reports/03.md`. Decisions: ADR 0002
(`docs/adr/0002-runner-transports-and-effect-semantics.md`). App-author guide:
`docs/runner-protocol.md`.

## Prompt 04 — Evaluator contracts, native checks, and registry

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 04-T1 | Evaluator protocol: manifests; describe/validate_binding/prepare/evaluate/evaluate_batch/close; typed context for artifacts, cancellation, accounting | DONE | `core/models.py` (`EvaluatorManifest`, `MetricBinding`, `DecisionRule`, `FieldRequirement`), `src/aibench/evaluators/protocol.py` (`Evaluator`, `EvaluationView`, `EvaluatorContext`, `EvaluationOutcome`), `evaluators/validation.py`; `tests/test_scoring_service.py` |
| 04-T2 | Registry and controlled discovery: versioned namespaced IDs, schema ranges, applicability, required observations; metadata-only discovery; worker for plugin code | DONE | `src/aibench/registry/__init__.py`, `registry/discovery.py`, `registry/worker.py`; `tests/test_registry.py`, `tests/test_plugin_discovery.py` |
| 04-T3 | Native and custom evaluators; low scores vs errors vs not-applicable | DONE | `src/aibench/evaluators/native.py` (`native.exact_match`, `native.json_schema`), `examples/evaluators/refund_window.py` (`acme.refund_window`); `tests/test_native_evaluators.py` |
| 04-T4 | Normalize, persist, aggregate; score recorded executions without invoking the app | DONE | `src/aibench/services/scoring.py`, `src/aibench/reporting/aggregation.py`, `storage/repositories.py` (`next_evaluation_attempt_number`, `list_evaluation_attempts`), `src/aibench/cli/score.py` (`aibench score`, `aibench evaluators list/describe/plugin`); `tests/test_scoring_service.py`, `tests/test_cli_score.py` |

Gates: 04-G1..G5. See `docs/engineering/reports/04.md`. Decisions: ADR 0003.
Review remediation (ADR 0003, "Changes after independent review"): `tests/test_scoring_review_regressions.py`, migration 4 (`storage/migrations.py`), `runners/process_tree.py::run_contained`, `evaluators/schema_worker.py`.
