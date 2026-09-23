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

## Prompt 05 — DeepEval adapter

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 05-T1 | Inspect and pin the real upstream API; record tested dependency and judge configuration; keep dependencies isolated from core | DONE | `deepeval==4.2.5` inspected in the installed package (ADR 0004 "Findings"); `plugins/deepeval/pyproject.toml` (exact pin), separate environment `plugins/deepeval/.venv`; `test_deepeval_is_imported_only_inside_its_plugin_package`, `test_version_drift_is_refused` |
| 05-T2 | Exact field semantics; never fill retrieval from reference.context; explicit missing/empty policies | DONE | `plugins/deepeval/src/aibench_deepeval/faithfulness.py` (`build_test_case`, policies); `test_test_case_conversion_matches_the_pinned_deepeval_api`, `test_missing_retrieval_is_never_filled_from_reference_context`, `test_empty_context_and_unscorable_output_follow_the_documented_policy` |
| 05-T3 | Independent metric instances, controlled worker execution, timeouts, declared nested retries/concurrency, raw outputs and unknown accounting, no publishing | DONE | `src/aibench/registry/eval_worker.py`, `src/aibench/evaluators/worker_client.py`, `registry.load_plugin_environment`, CLI `--plugin-env/--plugin-secret/--plugin-path`; `tests/test_worker_evaluator.py`, `test_concurrent_cases_never_share_metric_or_judge_state`, `test_blocking_judge_is_killed_and_the_next_case_runs_in_a_fresh_worker`, `test_no_deepeval_files_or_types_leak_into_the_harness` |
| 05-T4 | Honest compatibility checks: real package with an injected deterministic judge; optional budgeted live smoke | DONE (live smoke not run: no credentials/budget authorized) | `tests/fixtures/deepeval_judges/aibench_test_judges.py` (real `DeepEvalBaseLLM` subclasses), `tests/test_deepeval_adapter.py`, `test_live_provider_smoke` (opt-in), CI job `deepeval-plugin` |

Gates: 05-G1..G5. See `docs/engineering/reports/05.md`. Decisions: ADR 0004.

## Prompt 06 — Deterministic scheduling, policy, budgets, and recovery

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 06-T1 | Compile and schedule validated work: structural validation, dependency order, bounded queues, per-role concurrency caps, single writer, shared services | DONE | `src/aibench/core/plans.py` (`ExecutablePlan`), `src/aibench/engine/compile.py`, `src/aibench/engine/engine.py` (`RunEngine`), `storage/repositories.py` (`transition_work_item`, `acquire_run_lease`), migrations 5–6; `test_application_concurrency_cap_bounds_in_flight_calls`, `test_evaluation_concurrency_cap_bounds_in_flight_evaluations`, `test_a_second_session_cannot_resume_a_live_run` |
| 06-T2 | Policy and accounting: approved targets, data scope, credentials, effects, evaluator/data egress, plugin environments and paths; reserve/reconcile; separate app/evaluator/planner costs; hard call/token limits vs soft monetary estimates | DONE | `src/aibench/security/policy.py`, `src/aibench/engine/budget.py`; `tests/test_engine_policy.py`, `tests/test_retry_budget.py`, `test_plugin_import_paths_need_policy_approval_and_nothing_loads_when_denied`, `test_data_roots_scope_the_plans_data`, `test_a_call_dispatched_before_a_crash_counts_against_the_hard_limit` |
| 06-T3 | Attempts and effect-aware retries: bounded backoff, no multiplication, every attempt and cost recorded, unknown effects preserved, low scores never retried | DONE | `src/aibench/engine/retry.py`, `runners/http_runner.py` (Retry-After); `test_execution_retry_classification`, `test_evaluation_retries_never_repeat_valid_results_or_multiply`, `test_transient_timeouts_are_retried_and_every_attempt_is_recorded`, `test_recovery_never_retries_past_max_attempts` |
| 06-T4 | `run`/`evaluate`/`resume` commands; internal status/pause/resume/cancel; durable events; frozen manifests; safe checkpoint on interrupt | DONE | `src/aibench/services/runs.py`, `src/aibench/cli/run.py`, `RunController`; `tests/test_cli_run.py`, `tests/test_engine_faults.py`, `test_interrupt_then_resume_completes_without_duplicates`, `test_a_second_interrupt_aborts_in_flight_work_but_keeps_the_run_resumable` |

Gates: 06-G1..G5. See `docs/engineering/reports/06.md`. Decisions: ADR 0005.
Review remediation: `tests/test_engine_review_regressions.py`.

## Prompt 07 — Evaluation planning and bounded LLM reasoning

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 07-T1 | Evidence and requirement summaries: declared config, dataset field coverage, installed manifests; observed/declared/inferred/unknown; no architecture-discovery claims | DONE | `src/aibench/inspection/profile.py`, `src/aibench/inspection/dataset_summary.py`, `src/aibench/planning/catalog.py`; `tests/test_inspection.py` |
| 07-T2 | Plan compiler/validator: metric eligibility, per-case field requirements, selectors, DAG, sampling seeds, budgets, aggregation semantics, policy; missing information vs missing permission | DONE | `src/aibench/engine/compile.py` (`analyze_plan`, `PlanFinding`, `work_graph`, `dag_problems`), `core/plans.py` (`CasePredicate`, seeded `CaseSelection`), `planning/drafts.py` (`validate_draft`); `tests/test_plan_validation.py` |
| 07-T3 | Bounded planning loop: narrow provider interface, schema-constrained drafts, bounded repairs/tool calls, deterministic template and manual mode, fake provider offline, one real configurable provider | DONE (live provider not run: no credentials/budget authorized) | `src/aibench/planning/planner.py`, `template.py`, `openai_provider.py`; `tests/test_planner.py`, `tests/test_openai_provider.py`, `tests/test_planner_benchmark.py` |
| 07-T4 | `inspect` and `plan` commands: profile, draft, validation results, objective coverage, observability gaps, estimated spend, pending clarification; frozen executable revisions | DONE | `src/aibench/cli/inspect.py`, `src/aibench/cli/plan.py`, `src/aibench/planning/service.py`; `tests/test_cli_plan.py`, `tests/test_plan_equivalence.py` |

Gates: 07-G1..G5. See `docs/engineering/reports/07.md`. Decisions: ADR 0006.
Review remediation: `tests/test_planning_review_regressions.py`.

## Prompt 08 — Persistent two-way conversation and typed actions

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 08-T1 | Persist sessions, turns, pending questions, decision records and typed action requests; link source turns to interpreted choices, plan revisions and run IDs | DONE | `src/aibench/core/sessions.py`, migration 7 (`storage/migrations.py`), `src/aibench/sessions/store.py`, `src/aibench/cli/sessions.py`; `tests/test_session_store.py`, `test_sessions_cli_shows_the_conversation_decisions_and_actions` |
| 08-T2 | Domain conversation loop: explanations, material questions, plan patches, action requests, result queries; reuse prior answers; missing requirements vs authorized actions | DONE | `src/aibench/conversation/agent.py`, `src/aibench/sessions/drafting.py`, `planning/template.py` (`concepts`); `test_dialogue_changes_the_sample_explains_a_metric_and_starts_a_real_run`, `test_answers_are_reused_and_a_dataset_change_invalidates_stale_questions`, `test_missing_information_blocks_and_missing_permission_denies` |
| 08-T3 | Real services behind every tool: profile, dataset summary, evaluator description, plan patch/validation, start_run, status, pause/resume/cancel, case evidence — the same services as headless commands | DONE | `src/aibench/sessions/controller.py` (`compile_plan`, `create_run`, `execute_run`, `run_status`); `test_a_session_run_is_the_headless_run_of_the_same_reviewed_plan`, `test_pause_resume_and_cancel_go_through_run_control`, `test_failures_and_case_evidence_come_from_committed_results` |
| 08-T4 | Action boundaries: expected revisions, stable action IDs, stale patches and duplicate actions rejected, runs target a reviewed revision, scope changes create a new draft, questions never interrupt execution | DONE | `SessionStore.commit_decision` / `record_action` / `claim_active_run`, `SessionController.start_run`; `test_user_corrections_persist_and_stale_model_patches_are_rejected`, `test_redelivery_and_retried_turns_cannot_start_a_second_run`, `test_a_run_targets_the_reviewed_revision_and_one_runs_at_a_time`, `test_a_question_during_execution_leaves_the_run_running` |

Gates: 08-G1..G5. See `docs/engineering/reports/08.md`. Decisions: ADR 0007.
Review remediation: `tests/test_session_review_regressions.py`.

## Prompt 09 — Interactive terminal and live controls

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 09-T1 | Bare and explicit chat entry, project/session selection, TTY fallback, multiline input, history and slash completion | DONE | `src/aibench/cli/main.py`, `src/aibench/cli/chat.py`, `src/aibench/tui/app.py`; `tests/test_cli_chat.py`, `tests/test_tui.py`, `tests/test_cli_chat_pty.py` |
| 09-T2 | Stream replies and tool cards; show session/run identity, coalesced progress and partial metric snapshots while input remains available | DONE | `src/aibench/tui/app.py`, `src/aibench/tui/render.py`, `src/aibench/services/runs.py`; `tests/test_tui.py`, `tests/test_openai_provider_stream.py` |
| 09-T3 | Deterministic slash controls for plan/run/status/pause/resume/stop/results/budget/report/sessions/new/exit | DONE | `src/aibench/tui/commands.py`; `tests/test_tui.py`, `tests/test_cli_chat.py` |
| 09-T4 | Ctrl+C interrupts only the assistant reply; graceful exit interrupts dispatch safely; explicit /stop cancels a run | DONE | `src/aibench/tui/app.py`, `src/aibench/sessions/controller.py`; `tests/test_tui.py`, `tests/test_session_controller.py` |

Gates: 09-G1..G5. See `docs/engineering/reports/09.md`. Decision: ADR 0008.
