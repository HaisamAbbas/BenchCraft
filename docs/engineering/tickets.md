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

## Prompt 10 — Conversation recovery and adversarial interaction

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 10-T1 | Reconcile resumed state: persisted conversation plus authoritative engine/plan state; replay missed events by sequence without replaying actions; distinguish active, paused, interrupted and unknown-effect work | DONE | `services/runs.py` (`lease_state`), `SessionController.run_condition` / `reconcile` / `missed_events` / `acknowledge_events`, `BenchmarkSession.event_cursors`, `tui/app.py` (reopen banner, `notable_event`); `test_reopening_after_a_kill_shows_the_real_state_and_restarts_nothing`, `test_a_killed_effectful_run_leaves_unknown_effect_work_for_the_user`, `test_missed_events_replay_by_sequence_without_repeating_actions`, `test_reopening_replays_notable_missed_events_in_order` |
| 10-T2 | Long conversations: bounded summaries referencing structured decisions/artifacts; unanswered questions and user corrections preserved; summaries never authoritative over run data | DONE | `src/aibench/sessions/summary.py`, `conversation/agent.py` (`_messages`, `max_turn_chars`); `test_long_conversations_keep_corrections_and_open_questions_within_bounds`, `test_the_summary_stays_bounded_whatever_the_session_holds`, `test_corrections_made_in_conversation_are_kept_as_user_corrections` |
| 10-T3 | Harden boundaries: stale answers after dataset changes, delayed model mutations, tool-output prompt injection; redact secrets and terminal control content from history and rendering | DONE | `src/aibench/security/redaction.py`, `tui/render.py` (`safe`, `out`); `test_a_late_model_response_cannot_revert_a_newer_revision`, `test_tool_output_cannot_authorize_actions_or_supply_plan_values`, `test_terminal_control_and_secrets_never_reach_history_or_the_screen`, `test_split_or_quoted_credentials_are_redacted` |
| 10-T4 | Graceful and abrupt loss: chat disconnect, model interruption, worker crash, process kill; conversation interruption separate from run cancellation; session deletion vs retained run artifacts | DONE | `SessionController._redelivered_start` / `delete`, `SessionStore.delete_session`, `aibench sessions delete`; `test_retrying_a_start_after_a_kill_never_starts_a_second_run`, `test_a_crash_after_the_run_was_created_but_before_it_was_recorded_is_adopted`, `test_controls_work_during_a_provider_outage_and_stay_separate_from_replies`, `test_deleting_a_session_keeps_its_runs_and_their_results` |

Gates: 10-G1..G5. See `docs/engineering/reports/10.md`. Decisions: ADR 0009.
Review remediation: `tests/test_recovery_review_regressions.py`.

## Prompt 11 — Evidence reports, command composition, and packaging

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 11-T1 | Trustworthy reports: typed metric profiles, coverage denominators, app/evaluator failures, latency definitions, cost completeness, case evidence and provenance; HTML escaped; raw sensitive artifacts kept separate | DONE | `src/aibench/services/reports.py` (`build_report`, `report_facts`, `export_report`), `src/aibench/reporting/render.py`, `reporting/aggregation.py` (`planned`/`pending`), `services/runs.py` (`metric_profiles`, `scoring_pass` event), `core/plans.py` (`ReleaseGate`), `aibench report`; `test_every_number_reconciles_with_the_stored_records`, `test_reports_regenerate_without_invoking_the_app_or_any_evaluator`, `test_a_partial_snapshot_keeps_pending_work_in_every_denominator`, `test_hostile_evidence_is_inert_in_html_and_markdown`, `test_case_content_can_be_withheld_and_raw_artifacts_are_only_referenced`, `test_a_rescoring_pass_is_reported_separately_and_is_not_run_spend`, `test_runs_without_frozen_profiles_derive_them_and_say_so`, `test_missing_accounting_is_never_totalled` |
| 11-T2 | Conversational analysis: show failures, explain case, export report from stored facts; each quantitative claim tied to an aggregate/case query; hypotheses and partial snapshots labelled | DONE | `conversation/agent.py` (`get_report`, `export_report`, `check_claims`, status-line labels, prompt rules), `SessionController.report` / `report_facts` / `export_report`, `/report` (`tui/commands.py`, `tui/render.report`); `test_a_new_user_plans_runs_and_discusses_the_quickstart_in_conversation`, `test_the_assistant_cannot_export_a_report_the_user_did_not_ask_for`, `test_claims_are_linked_to_their_query_and_identifiers_are_not_claims`, `test_status_lines_label_partial_snapshots_and_unverified_numbers`, `test_the_quickstart_runs_and_reports_in_a_real_terminal` |
| 11-T3 | Command composition: init, doctor, benchmark, documented CLI operations; command/chat parity for authorization and exit codes; compare explicitly deferred | DONE | `cli/project.py` (`init`, `doctor`, `plugins list`, `compare`), `cli/benchmark.py`, `cli/report.py`, `cli/run.py` (`run [DIR\|DATASET]`), `cli/chat.py` (`--objective`, `_send` exit codes), `services.runs.run_exit_code`, `config.model.AibenchConfig.plan_path`; `tests/test_cli_project.py` (11 tests), `test_exit_codes_map_run_outcomes`, `test_release_gates_use_selected_cases_and_drive_exit_code_1`, `test_command_and_chat_exit_codes_agree`, `test_the_quickstart_works_in_conversation_without_a_model` |
| 11-T4 | Realistic quickstart: 10-case local example, optional dependency instructions, support/limitations docs, fresh-install smoke; credentials through secret references | DONE | `src/aibench/quickstart/` (package data), `pyproject.toml` (`package-data`), `docs/quickstart.md`, `docs/support.md`, `README.md`; clean-install smoke from the built wheel in a fresh venv (report 11, section 2); `test_init_writes_the_quickstart_and_never_overwrites`, `test_doctor_reports_missing_secrets_without_their_values` |

Gates: 11-G1..G5. See `docs/engineering/reports/11.md`. Decisions: ADR 0010.
Review remediation: `tests/test_report_review_regressions.py`.

## Prompt 12 — MVP acceptance and harness validation

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 12-T1 | Full product acceptance: 100-case fixture with injected RAG failures, black-box missing evidence, interruption/resume, stored-output rescore; goal → clarification → revision → run → live question → pause/resume → failure discussion | DONE | `examples/acceptance/` (`rag_service.py`, `rag100.jsonl`, `make_dataset.py`, `run_acceptance.py`); `tests/test_mvp_acceptance.py` (`test_the_100_case_workflow_survives_a_kill_finds_the_injected_failures_and_rescores_offline`, `test_a_black_box_endpoint_yields_a_missing_evidence_gap_not_a_score`, `test_a_fresh_user_completes_the_conversational_acceptance_journey`, opt-in `test_a_larger_cheap_workload_stays_bounded_and_resumes`); `docs/engineering/evidence/12/acceptance-*` |
| 12-T2 | Planner measurement: versioned 30–50 fixture set across app families with required concepts, acceptable alternatives and forbidden choices; comparison with the static template; reviewer status recorded | DONE (baseline; `acceptable` is supported in scoring but not yet annotated in v1); model planner not measured (blocked) | `benchmarks/planner/v1`, `planning/benchmark.py` (`load_fixture_set`, `run_fixture_set`, `benchmark_report`), `aibench plan benchmark`; `tests/test_planner_fixture_set.py`; `evidence/12/planner-template-v1.json` |
| 12-T3 | Judges and invariants: deterministic calibration and contract cases, scoped live checks where available; planner, engine reliability and cost completeness with actual denominators | DONE (native); live checks blocked | `benchmarks/judges/v1`, `services/calibration.py`, `aibench evaluators calibrate`; `tests/test_judge_calibration.py` (calibration and randomized invariants); `evidence/12/judge-calibration-v1.json`, `evidence/12/acceptance-summary.json` (reliability 200/200, cost accounting) |
| 12-T4 | Audit against the specification: every requirement and gate with evidence; MVP defects fixed; deferred scope, unsupported platforms and blocked live checks recorded | DONE | `docs/engineering/mvp-acceptance.md`; `requirements-matrix.md` (Status and Evidence columns); fixes in `inspection/profile.py`, `planning/catalog.py`, `services/reports.py`, `reporting/render.py` |

Gates: 12-G1..G5. See `docs/engineering/reports/12.md`. Decisions: ADR 0011.
Review remediation: `tests/test_acceptance_review_regressions.py`.
Closeout: the time bound in the Prompt 10 blocking-judge test went from 110 s to 200 s after cold worker starts measured 111 s under load; the judge is still proven killed, since 2 x 120 s would exceed the bound.

## Prompt 13 — Release candidate and pilot handoff

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 13-T1 | Resolve acceptance findings: triage Prompt 12's open items against §23's release bar, fix release-blocking defects, rerun affected checks | DONE | Fixed: storage failure → resumable interruption (`engine.is_storage_failure`, `_stop_for_storage`; `tests/test_storage_failures.py`); atomic recovery (`Storage.settle_work_items`; `test_a_crash_during_recovery_never_loses_the_uncommitted_dispatch`, which fails on the old code); terminal resizing (`test_resizing_the_terminal_keeps_the_chat_working`); stop reasons surfaced (`RunOutcome.warnings`, `runs status`, chat). Recorded, not changed: planner recall, throughput, hosted/live and human review; Linux CI and macOS remain unverified (ADR 0012) |
| 13-T2 | Distribution: versioned artifacts, migration/recovery instructions, changelog, quickstart, plugin/Python/platform matrix; install from built artifacts | DONE | `0.1.0rc1`; four artifacts and verified `SHA256SUMS` in `docs/engineering/evidence/13/dist/`; 3.12: 16/16 steps; 3.11: 19/19 including installed DeepEval plugin tests (29 passed, 1 live skip); `CHANGELOG.md`; `docs/release/upgrade-and-recovery.md`; `platform-matrix.md`; `WorkspaceTooNew` regression tests; CI job configured, result not observed |
| 13-T3 | Pilot evidence: two recipes, feedback forms (setup effort, value over direct evaluator use), local trials; real-team trials marked pending | DONE (local); real-team trials PENDING | `docs/pilot/` (README, recipe A HTTP RAG, recipe B CLI assistant plus own oracle, feedback form); `examples/pilot/`; `tests/test_pilot_recipes.py` (both recipes run as written through the CLI) |
| 13-T4 | Honest readiness decision: executable acceptance, external validation, residual risks, next actions; local artifacts only | DONE | `docs/engineering/release-readiness.md`; nothing published, deployed or sent; no pilot user contacted |

Gates: 13-G1..G5. See `docs/engineering/reports/13.md`. Decisions: ADR 0012.

## Prompt 14 — Second evaluator ecosystem and comparisons

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 14-T1 | Add one independent ecosystem, pin and verify its official API, and score stored outputs without re-running the application | DONE | Chosen Ragas over Promptfoo in `docs/adr/0013-ragas-adapter-and-comparison-compatibility.md`; `plugins/ragas/` pins `ragas==0.4.3`; real-package mapping, applicability, raw-artifact, version-drift and worker tests in `tests/test_ragas_adapter.py`; `tests/test_cross_ecosystem.py` uses both real adapters over one stored execution set |
| 14-T2 | Implement comparable run accounting: pair case/content/repetition, freeze metric/judge/rubric/plugin/instrumentation/dependency identity, distinguish rescore from execution, and refuse incompatible strict comparisons | DONE | `src/aibench/services/comparison.py`; `EvaluationCompatibilityIdentity` and frozen profiles in `core/models.py`/`services/scoring.py`; result-level identity, frozen-plan/application fail-closed checks, lineage, pass-completion, compatibility and coverage checks in `tests/test_comparison_service.py` |
| 14-T3 | Add coverage gates, paired/grouped uncertainty, repeated-judge stability, and one shared CLI/chat/TUI comparison service | DONE | `src/aibench/reporting/statistics.py`; `aibench compare` read-only path, `/compare`, session `compare_runs`; selected-side coverage, cached-repeat exclusion, interval/denominator tests and CLI/TUI/conversation integration tests |
| 14-T4 | Check independence/calibration against the same outputs, preserve framework semantics, and report disagreement without averaging | DONE (local deterministic judges) | Real DeepEval-versus-Ragas same-output test (`tests/test_cross_ecosystem.py`); cross-framework matrix and no-combined-score assertions; live provider/calibration and time-saved pilot remain explicitly pending |

Gates: 14-G1..G4. See `docs/engineering/reports/14.md`. Decision: ADR 0013. Ragas 0.4.3's open multi-modal SSRF advisory is tracked as a residual risk; the adapter exposes only validated text Faithfulness and runs in an isolated worker.

## Prompt 15 — Richer runners and agent outcome contracts

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 15-T1 | Python callable and OpenAI-compatible endpoint runners behind existing contracts; container execution with pinned images, non-root/read-only mounts, resource limits and explicit network policy | DONE | `runners/python_runner.py` + `python_shim.py`, `runners/openai_runner.py`, `runners/container_runner.py`; policy `allowed_container_images`, `allow_container_network`; `tests/test_runner_transports.py` (real container, real interpreter, local OpenAI-compatible stub); `tests/test_runner_review_regressions.py` |
| 15-T2 | Session reset fixtures, tool attempts/results, argument constraints, final-world-state assertions; tool names separate from successful authorized effects | DONE | Engine reset modes and episodes (`engine.py`), `reset_argv`/`reset_callable`/`reset_url` with seeds, `test_worlds` + plan `test_world` + policy `allowed_test_worlds`, `world_state` observation, `evaluators/agent.py` (`native.tool_calls`, `native.tool_outcomes`, `native.final_state`); `examples/agent_world/`, `examples/apps/booking_world.py`; `tests/test_agent_worlds.py`, `tests/test_agent_evaluators.py` |
| 15-T3 | Chat explains runner capabilities and missing evidence; approved test worlds chosen through validated plan changes | DONE | `services/applications.py`, `/app`, `/world`, assistant `describe_application`, `PlanPatch.test_world` with grounding; `tests/test_chat_test_worlds.py` |

Gates: 15-G1..G4. See `docs/engineering/reports/15.md`. Decisions: ADR 0014.
Review remediation: `tests/test_runner_review_regressions.py`.

## Prompt 16 — Inspection, traces, caching, and parallel execution

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 16-T1 | Repository inspection of approved source/manifests with evidence locations; declared/inferred/observed kept distinct; probes are policy-controlled engine actions | DONE | `inspection/source.py`, `inspection/probe.py`, `inspection/profile.py` (`source_findings`), `inspect --source/--policy/--probe`, policy `inspection_roots`; `examples/inspection/`; `tests/test_source_inspection.py` |
| 16-T2 | Normalize selected OpenTelemetry traces; preserve raw attributes, correlation IDs and sampling completeness; no parent/child usage double counting | DONE | `observations/otel.py`, `services/traces.py`, `aibench traces import/show`, migration 8 `trace_observations`, report "Imported traces" row; `examples/apps/traced_app.py`; `tests/test_trace_import.py` |
| 16-T3 | Version-complete execution/evaluation cache keys, invalidation and provenance; effectful unsnapshotted execution caching refused; hits excluded from latency/repeat claims | DONE | `engine/cache.py`, plan `cache`, engine + `BindingScorer` cache paths, `compile._check_cache`, migration 8 `cache_entries`, `aibench cache list/clear`, report "Cache" row; `tests/test_caches.py` (invalidation matrix) |
| 16-T4 | Provider-aware quotas, backpressure, bounded batching, cancellation; terminal responsiveness independent of worker load | DONE | `engine/quota.py`, plan `quotas`, gates checked before task creation, `backpressure` events, quota summaries; event-loop blocking removed (`http_runner._build_client`, threaded capture writes); `examples/apps/rate_limited_app.py`; `tests/test_parallel_execution.py` |

Gates: 16-G1..G5. See `docs/engineering/reports/16.md`. Decisions: ADR 0015.

## Prompt 17 — OpenAI evaluation bridges and one platform connector

Status: COMPLETE (report `docs/engineering/reports/17.md`, ADR 0017). The earlier Prompt 18
prerequisite subset `17-P18` is kept below as it was recorded.

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 17-T1 | Pin and bridge supported OpenAI Evals OSS completion functions with exact replay matching | DONE | `plugins/openai_evals_oss` (`evals==3.0.1.post1`, allowlist match/includes/fuzzy_match/json_match, `replay.py`, `bridge.py`), `services/delegated.py`, `aibench openai-evals-oss run`; `examples/openai_evals/`; `tests/test_openai_evals_oss.py` |
| 17-T2 | Implement hosted Evals submit/poll/fetch/cancel with explicit egress and persisted remote identities | DONE | `plugins/openai_evals_api` (`openai==3.19.2`, `contract.py`, `worker.py`), `services/remote_jobs.py`, migration 10 `remote_jobs`, `aibench openai-evals-api ...`, policy `allowed_egress_origins`; `examples/openai_evals/evals_api_stub.py`; `tests/test_openai_evals_api.py` |
| 17-T3 | Implement one demand-selected Langfuse, Phoenix, or Braintrust import connector | DONE | Langfuse (default; reversible choice, ADR 0017): `connectors/langfuse.py`, `aibench langfuse import-dataset/import-traces/export-scores/status`; `examples/langfuse/`; `tests/test_langfuse_connector.py` |
| 17-T4 | Expose exact supported integration modes and data destinations to planning/chat | DONE | `services/integrations.py`, `aibench integrations list`, `/integrations`, assistant `list_integrations`, catalog `consumes`/`network_destinations`, `remote_job` refused in plans; `tests/test_integrations.py` |

Gates: 17-G1..G5. See `docs/engineering/reports/17.md`. Decisions: ADR 0017.

**Completed dependency subset `17-P18`:** The bounded generation call reuses the existing
policy-checked OpenAI-compatible provider and explicitly selected development source input;
episode runs reuse the Prompt 15 reset/world-state contracts. Evidence: `tests/test_candidate_workflow.py`,
`tests/test_episode_contract.py`, ADR 0016. No Prompt 17 OSS/API bridge or connector capability
is claimed.

## Prompt 18 — Reviewed dataset generation and advanced episodes

Prompt 18 prerequisite subset: `17-P18` is recorded in `phase-status.md` and ADR 0016.
That note records the dependency available when Prompt 18 ran. Prompt 17 was completed
afterwards under its separate tickets, gates and report (`docs/engineering/reports/17.md`,
ADR 0017).

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 18-T1 | Generate bounded development candidates with exact source spans, source/model/prompt provenance, split identity, durable candidate state and exact duplicate-source records; add review/promotion operations | DONE | `src/aibench/datasets/candidates.py`, `src/aibench/services/candidates.py`, migration 9, `src/aibench/cli/candidates.py`, `tests/test_candidate_workflow.py` |
| 18-T2 | Keep candidates and unreviewed synthetic references outside regular datasets; require recorded human or executable verification and explicit promotion | DONE | `candidate_pools`, `candidate_cases`, append-only `candidate_events`; human/executable review and explicit promotion coverage in `tests/test_candidate_workflow.py` |
| 18-T3 | Define and execute multi-turn text application episodes with simulator provenance, per-episode reset and independent final-state success checks | DONE | `core/models.py`, `datasets/episodes.py`, `cli/episodes.py`, `examples/multi_turn_text/`, `examples/apps/multi_turn_support.py`, `tests/test_episode_contract.py` |

Gates: 18-G1..G4 PASS. Report: `docs/engineering/reports/18.md`. Decision: ADR 0016.

## Prompt 19 — Controlled optimization experiments

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 19-T1 | Define a finite experiment over application-exposed settings, objective, constraints and development data; preserve intended change, exact parameters, budgets and trial lineage | DONE | `core/models.py`, `experiments/service.py`, migration 11; `tests/test_experiments.py::test_known_objective_budget_resume_and_protected_holdout`; 135-test final batch |
| 19-T2 | Reuse frozen run/evaluator contracts, deterministic manifests and uncertainty-aware comparisons; lock development selection before paired baseline/candidate holdout evaluation | DONE | `services/runs.py`, `services/comparison.py`, `experiments/service.py`; `test_frozen_rubric_and_exposed_parameter_space_reject_changes`, `test_interrupted_holdout_resumes_same_frozen_plan_and_run_ids`, comparison regressions |
| 19-T3 | Expose experiment state, tradeoffs and a non-applying adoption proposal in CLI and conversation; require separate authorization for source/deployment/production changes | DONE | `cli/experiments.py`, `conversation/agent.py`, `docs/experiments/controlled-experiments.md`; `tests/test_experiments.py::test_experiment_conversation_tools_are_read_only_proposals` |

Gates: 19-G1..G4 PASS. Report: `docs/engineering/reports/19.md`. Decision: ADR 0018.

## Prompt 20 — Distributed execution after measured need

Status: DEFERRED (20-T1's optional-scope condition). The local bottleneck was measured and is
real, but it is a single-process bookkeeping cost that distribution would not remove, and no
declared workload exceeds it. No distributed component is implemented or claimed.

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 20-T1 | Measure the local bottleneck; define target workload, resource budget and failure model; defer if no distribution need is demonstrated | DONE (finding: need not demonstrated) | `scripts/measure_capacity.py`; `docs/engineering/evidence/20/capacity.json`, `capacity-3000.json`, `profile-300-c64.txt`; ADR 0019; `tests/test_capacity_measurement.py::test_capacity_measurement_records_the_run_it_actually_executed` |
| 20-T2 | Distributed coordinator: durable queue, PostgreSQL leases/fencing, object storage, stable task keys, deduplicated commits | DEFERRED | Not implemented. Contract recorded in ADR 0019 ("Failure model") |
| 20-T3 | Distributed recovery: duplicate delivery, expired leases, late workers, node failure, partial uploads, remote-authoritative session controls | DEFERRED | Not implemented or exercised. Depends on 20-T2 |
| 20-T4 | Honest scale evidence: hardware, traces, costs, queue behaviour, mock versus real application throughput | PARTIAL: single host, local mock only | ADR 0019 measurement table and profile; `docs/support.md` known limits. Measured: hardware, a CPU profile, server-side in-flight concurrency and workspace growth. Not measured: real application throughput and monetary cost (no authorized application or provider). No distributed throughput is claimed |

Gates: 20-G1, 20-G2, 20-G3 are NOT APPLICABLE while deferred, and are not reported as passed.
20-G4 and 20-G5 PASS. Report: `docs/engineering/reports/20.md`. Decision: ADR 0019.

## Prompt 21 — Optional dashboard and curated plugin catalog

Not started. No tickets were opened, and nothing was implemented or claimed.

## Prompt 22 — End-to-end acceptance and ticket-value audit

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 22-T1 | Reconcile every implemented ticket to code, specification, gates and executed checks; detect orphans, duplicates, missing requirements, untested tickets, contradictions | DONE | `docs/engineering/ticket-test-matrix.md` (82 rows: 81 tickets plus `17-P18`), with per-prompt and consolidated findings |
| 22-T2 | Complete the ticket-test matrix; map cross-component features to E2E-01–07; label evidence kinds | DONE | `ticket-test-matrix.md` journey table and evidence-kind legend. The journey definitions were derived because the repository pack does not define them (report 22 §4) |
| 22-T3 | Run and fix the deterministic vertical suite from a clean environment | DONE | `scripts/e2e_suite.py`; `examples/e2e/scripted_assistant.py`; `tests/test_e2e_cli_journey.py`; engine fix, a pause during start-up is now committed as `pausing` (`engine/engine.py`, `tests/test_engine.py::test_a_pause_while_the_run_is_starting_is_committed_as_pausing_at_once`); clean-install run 30/30 (`evidence/22/e2e-suite.json`) |
| 22-T4 | Check critical safety and reproducibility flows with side-effect counters | DONE | E2E-03–E2E-07 in the clean-install run. E2E-01 checks 20 application calls, at most 1 per case, across pause, resume and reopen |
| 22-T5 | Classify ticket value; judge the conversational loop; record recommendations without deleting code | DONE | The value-class column of the matrix; report 22 §4 (recommendations and decisions for the user) |
| 22-T6 | Separate technical readiness from external validation | DONE | `docs/engineering/release-readiness.md` (Prompt 22 section); report 22 §3 |

Gates: 22-G1..G7 PASS for local technical scope. External validation (human review, live
providers, installed-agent trials, pilots, public release) is NOT STARTED and is not claimed.
Report: `docs/engineering/reports/22.md`.

### Ledger corrections recorded by the Prompt 22 audit (history kept)

- **05-T1:** the cited test `test_deepeval_is_imported_only_inside_its_plugin_package` was
  renamed in commit `df5937b5` to
  `tests/test_dependency_boundaries.py::test_evaluator_framework_imports_stay_inside_their_adapter_packages`.
- **11-T3:** "compare explicitly deferred" was true at Prompt 11. Compare was implemented in
  Prompt 14 (`cli/project.py`, `tests/test_cli_project.py::test_compare_uses_stored_runs_and_reports_paired_coverage`).
- **07-T3:** the bounded model planner is implemented, but it is reachable only through
  `aibench plan`. Chat and `benchmark` use the template. The requirements matrix's
  "Partial" is the accurate status for the conversational product.
- **02-T2:** `services/execution.py` records captures with `commit_artifact_unverified`
  after an inline verification. So "every production path uses `commit_verified_artifact`"
  is no longer literally true, although it remains true that no result references an
  unverified artifact.
- **Prompt 17 status:** ADR 0016 and report 18 describe Prompt 17 as not started. That was
  true when they were written. Prompt 17 is COMPLETE (report 17).

## Prompt 24 — Product alignment and repository delta map

Status: COMPLETE. Product contracts were mapped and follow-up work was numbered. The user
confirmed Prompt 22 is the intended Prompt 23 final acceptance audit, satisfying the
prerequisite without a duplicate report. The targeted tests passed with elevated sandbox
permissions; the full suite was not rerun. See `reports/24.md` and `product-alignment-v4.md`.

| Ticket | Description | Status | Evidence |
|---|---|---|---|
| 24-T1 | Map all v4 §2–9 and §11 product requirements to real paths, executed evidence, honest status, contract impact, and a scoped next ticket | DONE | `docs/engineering/product-alignment-v4.md` (24 requirement rows, with Prompt 22 artifact citations and Prompt 24 targeted test result: 84 passed, 3 skipped) |
| 24-T2 | Record Prompt 22 as the user-confirmed Prompt 23 numbering equivalent, v3 conflicts, release-scope verdicts, and reuse/duplicate decisions without changing product behavior | DONE | `product-alignment-v4.md` §§Audit basis, V3 conflicts, Duplicate work, Scope verdicts; user confirmed Prompt 22 is the intended final audit; no standalone v3 pack found |
| 24-T3 | Convert verified gaps into dependency-ordered numbered follow-up tickets with definitions of done, gates, dependencies, exact tests, and report entries | DONE | Prompt 25–30 ticket sections below; dependencies 25 → 26 → 27 → optional 28 → 29 acceptance → 30-T1 gap closure |

Gates: 24-G1 PASS (all v4 feature-preservation rows mapped); 24-G2 PASS (targeted current
test set: 84 passed, 3 skipped with elevated sandbox permissions; full suite not rerun);
24-G3 PASS (contract conflicts and staged black-box scope explicit); 24-G4 PASS (real gaps
have numbered tickets and satisfied work is not duplicated). Prompt 22 is accepted as Prompt
23 and satisfies the prerequisite; current worktree validation remains pending. Report:
`reports/24.md`.

## Prompt 25 — Bounded codebase inspector and evidence-backed profile

Status: COMPLETE. The existing source inspector was extended with explicit budgets,
path-only discovery candidates, and profile provenance. See `reports/25.md`.

| Ticket | Description | Status | Definition of done, gates, dependencies, tests, report entry |
|---|---|---|---|
| 25-T1 | Define supported file/language types, inspection budget, root/symlink/size boundaries, secret/generated exclusions, and no-execution inspection | DONE | Depends on the existing Prompt 16 source inspector and `ExecutionPolicy`; DoD: bounded static inventory/parsing and unknown for unsupported source. Gate 25-G1. Exact tests: `tests/test_source_inspection.py::test_secret_files_are_skipped_and_never_echoed`, `::test_reading_source_needs_an_approved_root`, `::test_a_directory_junction_or_link_never_leads_outside_the_root`; `tests/test_application_profile_discovery.py::test_inspection_budgets_report_file_size_total_bytes_and_discovery_truncation`, `::test_file_size_limit_and_unsupported_files_are_reported_without_reading`, `::test_credential_and_vendored_directories_are_excluded_by_default`, `::test_directory_depth_and_entry_budgets_are_enforced`, `::test_repository_profile_has_bounded_provenance_without_execution_or_content_leaks`. Report: `reports/25.md` |
| 25-T2 | Produce application profile findings with evidence references, observed/declared/inferred/unknown labels, confidence/limits | DONE | Depends on 25-T1. DoD: typed repository result is attached to the compatible application profile; evidence and limitations remain visible. Gates 25-G2/G3. Paths: `src/aibench/inspection/profile.py` (`ApplicationProfiler`, `ApplicationProfile.repository_inspection`), `src/aibench/inspection/__init__.py`, `src/aibench/cli/inspect.py`; fixture `examples/inspection/repository_profile/`. Exact tests: `tests/test_application_profile_discovery.py::test_repository_profile_has_bounded_provenance_without_execution_or_content_leaks`, `tests/test_inspection.py::test_declared_config_gives_declared_or_unknown_with_evidence_and_recipes`, `::test_inspect_cli_uses_only_runs_of_the_same_app_config`, `tests/test_dependency_boundaries.py::test_core_imports_only_stdlib_pydantic_and_core`, `::test_no_first_party_module_imports_an_evaluator_framework`, `tests/test_cli_app.py`. Report: `reports/25.md` |

Gates: 25-G1 PASS (bounded, no-execution scan; secrets, roots/symlinks, file/byte,
directory/depth/entry budgets and untrusted text covered); 25-G2 PASS (typed
evidence/provenance/confidence, unknown unsupported source, candidate-only dataset/test/eval);
25-G3 PASS (core dependency boundary and existing CLI profiles). Tests: 29 passed; Ruff and
targeted mypy passed. Report: `reports/25.md`.

## Prompt 26 — Evaluation opportunities and candidate dataset discovery

Status: COMPLETE for bounded opportunity reporting and repository candidate discovery.
Repository test/evaluator paths remain candidates; no path or generated case is promoted to a golden.

| Ticket | Description | Status | Definition of done, gates, dependencies, tests, report entry |
|---|---|---|---|
| 26-T1 | Map objectives to compatible metrics only when observed/required evidence and metric semantics fit; report coverage gaps | DONE | DoD met: RAG and native tool-call fixtures expose available vs missing evidence; unknown objectives ask a focused concept question. Gate 26-G1 PASS: no unavailable metric is recommended. Depends on 25-T2. Exact tests: `tests/test_opportunity_discovery.py::test_rag_opportunity_uses_application_retrieval_and_reports_case_coverage`, `::test_reference_context_does_not_make_unobserved_retrieval_available`, `::test_native_tool_call_opportunity_requires_declared_events`, `::test_unrecognized_objective_remains_unknown_and_requests_clarification`, `::test_opportunities_cli_emits_json_without_writing_a_plan_or_running_app`; reuse `tests/test_plan_validation.py`, `tests/test_planner_fixture_set.py`. Report `reports/26.md`. |
| 26-T2 | Discover candidate datasets, test/evaluation suites and invocation references with provenance; preserve review/promotion boundaries | DONE | DoD met: bounded JSONL validation is path-backed; both inspection roots and optional policy data roots are enforced before reading; test/evaluator/invocation clues remain path-only; generated unreviewed cases are incompatible and candidate workflow remains explicit; references never enter discovery output or app projection. Gate 26-G2 PASS: no auto-promotion or reference leakage. Depends on 25-T1/T2. Exact tests: `tests/test_dataset_discovery.py::test_valid_jsonl_inventory_reports_field_counts_and_never_returns_values`, `::test_test_and_evaluation_paths_stay_path_only_and_are_not_datasets`, `::test_unsupported_format_and_oversized_dataset_remain_unknown`, `::test_generated_unreviewed_references_are_not_compatible_sources`, `::test_selection_reuses_only_one_content_identity`, `::test_policy_data_roots_block_content_reads_and_automatic_selection`, `::test_inventory_refuses_unapproved_root_and_symlinked_candidate`, `::test_distinct_candidates_report_review_boundary_without_auto_promotion`, `::test_inspect_json_includes_path_only_repository_candidate_inventory`; reuse `tests/test_candidate_workflow.py::test_unreviewed_synthetic_references_cannot_be_promoted`, `tests/test_runner_bindings.py::test_input_binding_cannot_reach_judge_only_data`. Report `reports/26.md`. |
| 26-T3 | Select/reuse one compatible repository dataset transparently under the existing policy and ask one focused question for material ambiguity | DONE | DoD met: only a sole compatible content identity inside `inspection_roots` and any configured `data_roots` is reused; identical copies collapse to one choice; non-interactive chat reports the competing paths and asks for `--dataset`; plan-only opportunity inspection writes no plan and runs no app. Gate 26-G3 PASS. Depends on 26-T1/T2. Exact tests: `tests/test_dataset_discovery.py::test_selection_reuses_only_one_content_identity`, `::test_policy_data_roots_block_content_reads_and_automatic_selection`, `tests/test_cli_chat.py::test_new_chat_reuses_the_only_compatible_policy_approved_dataset`, `::test_noninteractive_new_chat_asks_for_material_dataset_ambiguity`, `tests/test_opportunity_discovery.py::test_opportunities_cli_emits_json_without_writing_a_plan_or_running_app`; reuse `tests/test_conversation.py`, `tests/test_plan_validation.py`. Report `reports/26.md`. All 114 focused tests passed; two environment-dependent cases skipped; see JUnit and report 26. |

## Prompt 27 — Complete conversational evaluation loop

Status: COMPLETE for the deterministic local loop with configured runners. The provider is
scripted; live-model quality and human usability remain unverified. Reuses shared services and
preserves v1.1 run, policy and evidence contracts. See `reports/27.md`.

| Ticket | Description | Status | Definition of done, gates, dependencies, tests, report entry |
|---|---|---|---|
| 27-T1 | Connect grounded profile/opportunity results to conversation and a validated executable plan | DONE | DoD met: conversation reads the same profile, dataset summary and `discover_opportunities` result as headless planning; the current objective's evidence and gaps are exposed without dataset values. Gate 27-G1 PASS. Depends on 25 and 26. Exact tests: `tests/test_conversation.py::test_conversational_evaluation_plan_progress_failure_report_and_rescore`, `tests/test_conversation.py::test_clear_evaluation_request_starts_the_current_unpresented_plan`, `tests/test_session_controller.py`, `tests/test_plan_validation.py`. Report `reports/27.md`. |
| 27-T2 | Apply v4 §3 intent/authorization semantics: clear bounded request starts inside existing policy; plan-only request does not run; clarify only material ambiguity/blockers | DONE | DoD met: a clear evaluation or exact case-count request starts once under the existing policy; case-count text must match the validated draft; plan-only, questions, negations, denied policy and mismatched scope dispatch nothing. Explicit `/run` now starts the current validated draft and shows a preview without a repeat-command gate. Gate 27-G2 PASS. Depends on 26-T3. Exact tests: `tests/test_conversation.py::test_clear_evaluation_request_starts_the_current_unpresented_plan`, `::test_clear_case_count_request_cannot_run_a_different_draft_scope`, `::test_vague_negated_or_questioning_words_do_not_start_a_run`, `::test_a_run_the_policy_does_not_permit_dispatches_nothing`, `tests/test_engine_policy.py`, `tests/test_conversation_hardening.py`, `tests/test_tui.py::test_explicit_run_starts_unpresented_validated_draft_with_a_preview`, and `tests/test_e2e_cli_journey.py::test_a_user_benchmarks_an_app_from_the_first_message_to_evidence_in_the_cli`. Report `reports/27.md`. |
| 27-T3 | Complete progress and evidence-linked failure analysis with observations distinct from hypotheses | DONE | DoD met: active progress is a partial snapshot; stored failures/case evidence remain read-only; reports expose run/dataset/application/plan provenance and metric provenance; the assistant labels causal explanations as hypotheses. Gate 27-G3 PASS. Depends on 27-T1. Exact tests: `tests/test_conversation.py::test_conversational_evaluation_plan_progress_failure_report_and_rescore`, `::test_a_question_during_execution_leaves_the_run_running`, `tests/test_session_controller.py::test_failures_and_case_evidence_come_from_committed_results`, `tests/test_reports.py::test_report_facts_copy_numbers_without_recomputing`, and the E2E-01 terminal journey. Report `reports/27.md`. |
| 27-T4 | Add next-experiment and rescore follow-up using stored executions and existing experiment/comparison contracts | DONE | DoD met: `rescore_run` delegates to policy-checked `evaluate_run` on the session's validated draft; it preserves the stored run ID and makes no application calls. The assistant gets evidence gaps and report provenance to state a reviewable next experiment; no source/app change is applied. Gate 27-G4 PASS. Depends on 27-T3. Exact tests: `tests/test_conversation.py::test_conversational_evaluation_plan_progress_failure_report_and_rescore`, `tests/test_comparison_service.py::test_explicit_rescore_selection_and_same_stored_execution_ids`, `tests/test_experiments.py::test_experiment_conversation_tools_are_read_only_proposals`, and `tests/test_reports.py`. Report `reports/27.md`. |

27-G5 PASS: deterministic complete-loop fixture includes one passing and one failing score,
tool-use unavailable for missing `execution.tool_events`, failure discussion, plan-only, scope
clarification, direct clear-run, policy denial, progress, session resume and stored-output
rescore. The source regression batch passed 125 tests. The clean-installed E2E-01 journey and
direct slash-run regressions are recorded in `reports/27.md`. Scripted-model results are not
live-agent evidence; no live provider or human evaluation was run.

## Prompt 28 — Optional black-box HTTP evaluation

Status: COMPLETE for the existing configured JSON HTTP API contract. The user's Prompt 28
request explicitly started the optional phase. Audit found the required narrow runner,
policy, budget, session and evidence behavior already implemented; no duplicate runtime code
was added. Generic URL discovery and browser automation remain deferred.

| Ticket | Description | Status | Definition of done, gates, dependencies, tests, report entry |
|---|---|---|---|
| 28-T1 | Audit and reuse the narrow configured HTTP request/response contract: secret references/redaction, timeout, retry, quota, budget, cancellation, egress, effects and local fixture integration | DONE | DoD met by existing typed `HttpTransport`/bindings, `HttpRunner`, policy and shared run/session/report services. Gates 28-G1..G4 PASS. Depends on Prompt 27 service contracts and explicit phase start (provided with this request). Exact tests and skips: `reports/28.md`; `docs/engineering/evidence/28/focused-junit.xml`. No browser or arbitrary website behavior is claimed. |

Gates: **28-G1 PASS** (configured local HTTP API flows through the conversation/session/run/report path; clean-installed E2E-01); **28-G2 PASS** (secret redaction, request isolation, endpoint policy and denied requests); **28-G3 PASS** (timeout/cancel/effect truth, effect-aware retries, app quotas and hard/estimated-cost budgets); **28-G4 PASS** (typed configured API only; browser and arbitrary URL automation remain deferred). Exact results and limitations: `docs/engineering/reports/28.md`.

## Prompt 29 — v4 product acceptance and value review

Status: COMPLETE for the declared deterministic local scope after the Prompt 30/31 closure.
The original acceptance snapshot remains in reports/29.md; final gates and its superseding
result are recorded in reports/31.md.

| Ticket | Description | Status | Definition of done, gates, dependencies, tests, report entry |
|---|---|---|---|
| 29-T1 | Reconcile every in-scope requirement/ticket to code and executed evidence; run deterministic journeys; distinguish scripted fixtures, real packages and live checks; record both scope verdicts | DONE | Final gates 29-G1..G4 PASS after E2E-08 closed journey 1. Clean-installed wheel suite: 32 passed across E2E-01..08; focused changed-path batch: 16 passed. Pinned real-package evidence is retained from report 29 (DeepEval 4.2.5: 18 passed/1 live deselected; Ragas 0.4.3: 9 passed); no adapter code changed. Repository-aware deterministic local scope READY FOR REVIEW; live/human/PMF scope remains unvalidated. Final evidence: docs/engineering/evidence/31/ and docs/engineering/reports/31.md. |

Gates:

- **29-G1 PASS:** all 24 v4 requirements have paths, executed evidence, honest status, contract impact and next action; matrix, tickets and report agree.
- **29-G2 PASS:** the repository-defined clean-install suite passes 32 tests across E2E-01..08, including fresh-repository E2E-08. The wheel imports from isolated site-packages.
- **29-G3 PASS:** all twelve required acceptance journeys pass under the local deterministic scope. Journey 1 is exercised in E2E-08 and the report-31 journey map.
- **29-G4 PASS:** pinned real-package runs are separate from fixtures: DeepEval 4.2.5 passed 18 tests (live smoke deselected); Ragas 0.4.3 passed 9. Those adapter packages and adapters were not modified in this closure. No live provider was called.

## Prompt 30 — Close Prompt 29 acceptance gap

Status: COMPLETE. See `docs/engineering/reports/30.md` and the final acceptance in report 31.

| Ticket | Description | Status | Definition of done, gates, dependencies, tests, report entry |
|---|---|---|---|
| 30-T1 | Surface approved-root static repository findings in a fresh conversational evaluation path, using the existing bounded inspector and profile services | DONE | DoD met by path/line findings, unknown unsupported source, denied-root behavior, injection/secret isolation, configured run, stored report, session reopen and zero-call rescore. Gates 30-G1..G4 PASS. Exact tests: `tests/test_conversation.py::test_repository_findings_are_available_to_a_fresh_conversation`; `tests/test_conversation.py::test_repository_findings_stay_unknown_when_inspection_is_not_approved`; `tests/test_e2e_repository_conversation.py::test_fresh_repository_inspection_runs_and_reports_with_evidence` (also clean-installed E2E-08). Dependencies 25-T1/T2, 26-T1/T2/T3 and 27-T1/T2/T3/T4. Report `reports/30.md`. |

## Prompt 31 — Close remaining in-scope v3 gaps and rerun acceptance

Status: COMPLETE for the declared, bounded local product scope. Generic browser automation,
unsupported parser breadth, live-provider quality and human/market validation remain deferred
or unvalidated; see `reports/31.md`.

| Ticket | Description | Status | Definition of done, gates, dependencies, tests, report entry |
|---|---|---|---|
| 31-T1 | Provide bounded no-repository HTTP setup that writes typed config/policy without probing the endpoint and validates local JSONL cases | DONE | Gate 31-G1 PASS. DoD: generated project config is schema-valid, validates selected local JSONL, stores only secret references, has exact-origin/effect/call policy and performs zero setup requests; refuses unsafe URL parts and overwrite. Depends on existing 26-T2 dataset validation and 28-T1 HTTP contract. Exact tests: `tests/test_cli_connect.py::test_http_setup_creates_a_bounded_no_repository_project_without_network_calls`; `::test_remote_http_setup_requires_exact_origin_authorization`; `::test_loopback_setup_needs_no_remote_authorization`; `::test_http_setup_rejects_url_credentials_queries_and_secret_literals`; `::test_http_setup_never_overwrites_existing_project_files`. The sixth test, integrated fixture, is under 31-T2. Existing HTTP runner is reused. |
| 31-T2 | Preserve one session/run while a configured HTTP app gains imported OpenTelemetry evidence and approved repository profile findings | DONE | Gate 31-G2 PASS. DoD: one loopback app call, one persisted run ID, imported trace summary and inferred `rag.py` finding are visible in the same session; raw trace file is not exposed. Depends on 16-T2 trace import, 25-T2 profile, 30-T1 conversational integration and 31-T1 setup. Exact test: `tests/test_cli_connect.py::test_generated_http_project_runs_through_session_evidence_and_report_services`. No auto-instrumentation or hosted trace connector is claimed. |
| 31-T3 | Expose bounded conversational controlled experiment start, progress, stored-state resume, and separate protected-holdout authorization through the existing experiment service | DONE | Gate 31-G3 PASS. DoD: user-grounded values, finite budgets, session-owned experiment, stored progress/resume, and separate holdout authorization all pass. Depends on 19-T1..T3 frozen experiment services and 27-T3 session evidence/recovery. Exact tests: `tests/test_conversational_experiments.py::test_conversation_runs_experiment_reports_progress_and_separately_evaluates_holdout`; `::test_conversational_experiment_rejects_question_and_ungrounded_values`; `::test_conversational_experiment_holdout_must_be_inside_configured_data_roots`; `::test_noninteractive_chat_waits_for_a_controlled_experiment_it_started`; `::test_conversation_resumes_a_stored_running_experiment`; reuse `tests/test_experiments.py::test_experiment_conversation_tools_are_read_only_proposals`. App-exposed values only; no source repair/adoption. |
| 31-T4 | Rerun final acceptance, reconcile v3 conflicts, verify sourced product-capability statements and publish readiness limits | DONE | Gates 31-G4..G6 PASS. DoD: all in-scope tickets and 12 journeys mapped to executable evidence; declared readiness scope and unrun live/human checks are explicit. Depends on 30-T1 and 31-T1..T3. Clean-installed script: 32 passed across E2E-01..08, zero skipped, `pytest_exit_code=0`; changed-path batch: 16 passed. Ruff, `mypy src` (129 files), and `aibench --help` passed. Real-package results are reused from report 29 because adapter code and pinned environments did not change; live tests stay deselected. Full report/artifacts: `docs/engineering/reports/31.md`, `docs/engineering/evidence/31/`. |
