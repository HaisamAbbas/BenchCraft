# Requirements matrix

Maps specification requirements to the owning numbered prompt and its observable acceptance
gate. Source: `docs/spec/implementation-plan.md` v1.1. Phase 2/3 requirements are marked
deferred unless the owning prompt has been explicitly implemented and evidenced.

| Spec section | Requirement | Owning prompt | Gate(s) | Status | Evidence |
|---|---|---|---|---|---|
| §2 Core concepts | Immutable Golden/BenchmarkCase, ApplicationSpec, EvaluationPlan, ExecutionResult, EvaluationResult identities | 01 | 01-G1, 01-G2 | Verified | reports/01 |
| §2 | Application input vs judge-only reference separation | 01 | 01-G2 | Verified | reports/01 |
| §5 Internal data models | Versioned Pydantic models with exported JSON Schema | 01 | 01-G1 | Verified | reports/01 |
| §6 Dataset schema | JSONL shorthand + normalization rules (chatbot/RAG/tool/coding examples) | 01 | 01-G1, 01-G3 | Verified | reports/01 |
| §6 | Duplicate ID detection, stable generated IDs, line-precise errors | 01 | 01-G3 | Verified | reports/01 |
| §13 CLI design | `aibench dataset validate PATH` | 01 | 01-G1, 01-G3 | Verified | reports/01 |
| §14 Storage architecture | SQLite repositories, artifact commit protocol, run identity | 02 | 02-G1..G3 | Verified | reports/02 |
| §7 Application interface | CLI/HTTP runners, observation envelopes | 03 | 03-G1..G5 | Verified | reports/03 |
| §7 CLI/HTTP protocol | argv-only CLI, JSON stdin/stdout, text mode, bounded logs; HTTP bindings, secret refs, TLS, endpoint policy, redirect control, size/time limits, correlation IDs | 03 | 03-G1, 03-G3 | Verified | reports/03 |
| §7 State/observability | Missing retrieval/tool/usage/cost recorded as unknown; observability-gap report (`aibench app describe`) | 03 | 03-G4 | Verified | reports/03 |
| §15 Retry policy (runner side) | Runners never retry; `effect_state` marks ambiguous effects | 03 (engine use: 06) | 03-G3 | Verified | reports/03 |
| §16 Security | Trusted-local mode explicit; Goldens never sent to apps; scoped app credentials; control-char/markup scrubbing of app output | 03 | 03-G2 | Verified | reports/03 |
| §9, §12 Evaluator/plugin architecture | Evaluator protocol, registry, native checks, canonical aggregation | 04 | 04-G1..G5 | Verified | reports/04 |
| §9 Plugin manifest / discovery | Manifests; entry-point discovery without import; subprocess manifest worker | 04 (worker execution: 05) | 04-G2 | Verified | reports/04 |
| §9 Adapter contract | describe/validate_binding/prepare/evaluate/evaluate_batch/close; scores recorded outputs by default | 04 | 04-G1, 04-G3 | Verified | reports/04 |
| §12 Unified result schema | Typed value union, status vs decision, frozen rule, evidence, provenance, resources/accounting, raw artifact ref | 04 | 04-G1 | Verified | reports/04 |
| §12 Aggregation | Per-metric summaries with explicit denominators and coverage; no cross-metric averages | 04 | 04-G1 | Verified | reports/04 |
| §14 Storage | evaluation_attempts / metric_results persisted per scoring pass without overwrite | 04 | 04-G3 | Verified | reports/04 |
| ADR 0001 | Dependency-boundary test for core | 04 | 04-G4 | Verified | reports/04 |
| §10 DeepEval adapter | Pinned faithfulness adapter | 05 | 05-G1..G5 | Partial | reports/05: offline recorded-response and worker tests; live judge smoke blocked (key and authorization) |
| §9 Controlled workers for plugin code | Third-party evaluators execute only in workers using the plugin environment's interpreter; enforceable cancellation | 05 | 05-G3, 05-G4 | Verified | reports/05 |
| §10 Field mapping / missing vs empty context | input, recorded output, observed retrieval; reference context never substituted; documented empty-context policy | 05 | 05-G1, 05-G2 | Verified | reports/05 |
| §10 Independent metric instances | New metric and judge per case; concurrency control test | 05 | 05-G3 | Verified | reports/05 |
| §16 Credentials / publishing | Judge secrets passed explicitly; telemetry, .env, legacy key file off; no Confident AI publishing | 05 | 05-G4 | Verified (offline) | reports/05 |
| §15 Execution/concurrency | Deterministic scheduling, budgets, retries, resume | 06 | 06-G1..G5 | Verified | reports/06 |
| §15 Scheduling | Work graph from validated plans; bounded concurrency per role; single writer (compare-and-set transitions + run lease) | 06 | 06-G1, 06-G4 | Verified | reports/06 |
| §15 Budget control | Reserve/reconcile; separate application/evaluator/planner accounting; hard call/token/wall limits; soft cost estimate; unknown never zero; carried across resume | 06 | 06-G4 | Verified | reports/06 |
| §15 Retry policy (engine side) | Effect-aware transient retries, bounded seeded backoff honouring Retry-After, no multiplication with evaluator retries, low scores never retried | 06 | 06-G3, 06-G4 | Verified | reports/06 |
| §15 Recovery | Frozen plan/app/bindings verified on resume; conservative in-flight recovery; ambiguous effects become `unknown_effect`; no exactly-once promise | 06 | 06-G3 | Verified | reports/06 |
| §16 Execution policy | Approved targets, data scope, credentials, effects, evaluator/data-egress, plugin environments and paths, budget ceilings — decided before dispatch | 06 | 06-G2 | Verified | reports/06 |
| §13 CLI design | `aibench plan validate`, `run --plan`, `resume`, `evaluate`, `runs status`; exit codes 0/2/3/4/130 | 06 | 06-G1, 06-G5 | Verified | reports/06 |
| §8 Agent architecture | Bounded LLM planner, plan compiler/validator | 07 | 07-G1..G5 | Partial | reports/07: loop, bounds and fallback with scripted and local-HTTP providers; no live model run |
| §3, §13 `inspect` | Evidence-backed profile from declared config and recorded runs; observability gaps with integration recipes; limited scope stated | 07 | 07-G3 | Verified | reports/07 |
| §5 Observation states | observed / declared / inferred / unknown with evidence locations; dataset hints recorded as inferred with limitations | 07 | 07-G3 | Verified | reports/07 |
| §8 Preventing invented capabilities | Registry-resolved IDs, deterministic eligibility, per-case field requirements, unavailable bindings/selectors/aggregations rejected outside the model | 07 | 07-G1 | Verified | reports/07 |
| §8 Planner tools | Narrow read-only tools (`read_profile` … `write_plan_draft`); no general terminal; bounded repairs/spend; template fallback | 07 | 07-G4 | Verified | reports/07 |
| §3, §13 `plan` / `plan validate` | Draft with objectives, rationale, gaps, pending questions, classified findings, coverage and spend estimate; revisions never silently overwritten | 07 | 07-G3, 07-G5 | Verified | reports/07 |
| §17 Manual vs generated plans | Hand-written and generated plans with identical content produce identical deterministic metrics | 07 | 07-G2 | Verified | reports/07 |
| §23 Benchmark the planner | Annotated fixtures, precision/recall/gap scoring, static template baseline | 07 | 07-G3 | Partial | reports/07, 12; mvp-acceptance.md: template baseline measured, recall target not met, fixtures unreviewed, model planner not measured |
| §2–5, §8 | Persistent session, decisions, typed actions | 08 | 08-G1..G5 | Verified | reports/08 |
| §2, §5 Session records | `BenchmarkSession`, `ConversationTurn`, `PendingQuestion`, `DecisionRecord`, `ActionRequest` with exported schemas; decisions linked to source turn, revision and plan hash | 08 | 08-G4 | Verified | reports/08 |
| §8 Dialogue and action protocol | Typed turn outputs validated outside the model; patches grounded in the user's words; actions need the user's explicit request; no terminal/file/network tool | 08 | 08-G1, 08-G3 | Verified | reports/08 |
| §8 Expected revisions and action IDs | Compare-and-set revisions reject stale patches and stale answers; action IDs deduplicated; one active run per session | 08 | 08-G3, 08-G4 | Verified | reports/08 |
| §8, §15 Live conversation | Questions and explanations during execution never touch the run; scope changes create a new draft; controls are recorded events | 08 | 08-G2 | Verified | reports/08 |
| §4, §8 Shared services | Session tools call `compile_plan`, `create_run`, `execute_run`, `run_status` and stored results — the headless commands' services | 08 | 08-G1 | Verified | reports/08 |
| §14 Session persistence | Migration 7 tables; turns, decisions, questions and actions survive restart; run events replayable; runs not owned by sessions; credentials redacted from chat | 08 | 08-G4 | Verified | reports/08 |
| §16 Data egress | The assistant never receives reference answers; case content only with `share_case_content_with_assistant` | 08 | 08-G5 | Verified | reports/08 |
| §3, §13 | Interactive terminal entry, project/session selection, multiline input, history, completion | 09 | 09-G1, 09-G5 | Verified (Windows) | reports/09: ConPTY tests; no Linux/macOS terminal run |
| §13, §15 | Streamed replies, tool cards, coalesced committed progress, partial metric labels, responsive controls | 09 | 09-G2, 09-G4, 09-G5 | Verified | reports/09 |
| §13, §15 | Deterministic slash controls; provider-independent status/stop; run interruption and graceful exit | 09 | 09-G3..G5 | Verified | reports/09 |
| §8, §13–16 | Conversation recovery, adversarial hardening | 10 | 10-G1..G5 | Verified | reports/10 |
| §8, §14 Resumption | Reopening reconciles conversation with authoritative run state (stored status + lease); interrupted runs stay stopped until a new action; unknown-effect work reported | 10 | 10-G1 | Verified | reports/10 |
| §8, §14 Event replay | Per-session event cursors; missed events replayed by sequence; no action replayed | 10 | 10-G1 | Verified | reports/10 |
| §8 Deduplicated actions under crashes | A redelivered start adopts the run its crashed first delivery created (via its approval) or closes after the starting window; never a second run | 10 | 10-G2 | Verified | reports/10 |
| §8 Expected revisions under races | Late model patches, starts and answers against an older revision (incl. after a dataset change) are rejected | 10 | 10-G3 | Verified | reports/10 |
| §8, §16 Summaries and injected text | Bounded structured summaries labelled non-authoritative; permissions only from policy and the session's grant; tool-output instructions cannot authorize actions or supply values | 10 | 10-G4 | Verified | reports/10 |
| §14, §16 Redaction | Secrets and terminal control/bidi content removed from history, model input and rendering; emoji codes never substituted in data | 10 | 10-G4 | Verified | reports/10 |
| §14 Session deletion | Deleting a conversation keeps runs, results, artifacts and events | 10 | 10-G5 | Verified | reports/10 |
| §12–14 | Evidence reports, command composition, packaging | 11 | 11-G1..G5 | Verified | reports/11 |
| §3, §13 `report` | Reports rendered from stored facts only (no app, evaluator or plugin loaded); JSON, Markdown, static HTML | 11 | 11-G1 | Verified | reports/11 |
| §12 Aggregation | Per-binding metric profiles (frozen with the run or rescoring pass); selected/eligible/attempted/completed/error/NA/unavailable/pending denominators; fractions shown with percentages; no overall score | 11 | 11-G2 | Verified | reports/11 |
| §12 Latency, cost | Successful-request p50/p95 (nearest rank) with definition, failures/timeouts separate; observed cost with accounting completeness, no total when incomplete | 11 | 11-G2 | Verified | reports/11 |
| §12 Release gates | Predeclared plan gates over selected cases; undecided on partial snapshots; exit code 1 | 11 | 11-G2, 11-G4 | Verified | reports/11 |
| §14, §16 Report safety | Sanitized, escaped HTML/Markdown with CSP; raw evaluator artifacts referenced by ID/digest only; `--no-content` | 11 | 11-G2 | Verified | reports/11 |
| §3, §8 Conversational analysis | `get_report` / `export_report` tools; numeric claims linked to queries and unverified numbers flagged; hypotheses and partial snapshots labelled | 11 | 11-G3 | Verified | reports/11 |
| §3, §13 Commands | `init`, `doctor`, `benchmark` (interactive, `--non-interactive`, `--auto --policy`), `run DIR`, `report`, `plugins list`; `compare` reads compatible stored runs with strict/exploratory modes | 11, 14 | 11-G4, 14-G2 | Verified | reports/11, reports/14; tests/test_cli_project.py |
| §13 Exit codes | One mapping for `run`, `resume`, `benchmark --auto`, `chat --send` (0/1/2/3/4/130) | 11 | 11-G4 | Verified | reports/11 |
| §17 Quickstart, packaging | 10-case quickstart as package data; quickstart and support docs; secret references only; clean-install smoke | 11 | 11-G3, 11-G5 | Verified (manual, Windows) | reports/11, 12: clean-install smoke from the built wheel |
| §17, §23–24 | MVP acceptance validation | 12 | 12-G1..G5 | Verified (validation done; release items open) | reports/12; docs/engineering/mvp-acceptance.md lists the open release items |
| §17 Acceptance workflow | 100 fixture cases with injected RAG failures; killed engine resumed; stored-output rescore with zero application calls | 12 | 12-G1, 12-G2 | Verified | tests/test_mvp_acceptance.py; evidence/12/acceptance-summary.json |
| §17 Black-box missing evidence | A black-box endpoint yields a groundedness gap, not a score | 12 | 12-G2 | Verified | test_a_black_box_endpoint_yields_a_missing_evidence_gap_not_a_score |
| §17 Conversational acceptance | Goal, clarification, revision, run, live question, pause/resume, restore without duplicates, failure discussion | 12 | 12-G1 | Verified (scripted model) | test_a_fresh_user_completes_the_conversational_acceptance_journey |
| §23 Planner fixture set | 40 versioned fixtures, 27 families, held-out families, forbidden choices, reviewer status (acceptable alternatives supported, none annotated yet); `aibench plan benchmark` | 12 | 12-G3, 12-G4 | Partial | benchmarks/planner/v1; tests/test_planner_fixture_set.py: recall 17/21 below the 0.85 target; no fixture reviewed; model planner not measured |
| §23 Judge calibration | Labelled calibration set, agreement / false acceptance / false rejection / stability; `aibench evaluators calibrate` | 12 | 12-G4 | Partial | benchmarks/judges/v1; tests/test_judge_calibration.py: native only; labels unreviewed; model judges not measured |
| §23 Invariants | Randomized checks through the real scorer: Goldens never mutated, errors never scored, decisions follow the frozen rule; every branch asserted to occur | 12 | 12-G1 | Verified | test_scoring_invariants_hold_for_generated_cases |
| §23 Larger workload | 1,000 cases, interrupted and resumed, bounded memory | 12 | 12-G1 | Verified (opt-in test) | AIBENCH_WORKLOAD_TESTS=1; throughput limitation recorded in mvp-acceptance.md |
| §23 Platforms | Linux, macOS, Python 3.11 | 12 | 12-G4 | Partial | Python 3.11.16 artifact install and acceptance verified on Windows in Prompt 13; Linux CI has no observed result; macOS not exercised |
| §23 Live checks | Live DeepEval judge, live-model conversation and planner trials | 12 | 12-G4 | Blocked | Needs an API key and authorization for paid calls |
| §17 Distribution and recovery | Versioned core/plugin artifacts, clean install demo, migration and recovery instructions, compatibility matrix | 13 | 13-G1, 13-G3 | Verified (Windows, Python 3.11/3.12) | `reports/13`; `evidence/13/` |
| §22 Pilot trials | Two executable integration recipes and feedback form; local runs observed, real-team validation kept distinct | 13 | 13-G3 | Partial (local trials complete; real-team trials pending) | `tests/test_pilot_recipes.py`; `docs/pilot/`; `reports/13` |
| §17–18, §22–24 | Release candidate and pilot handoff; readiness, residual risks and stopping point | 13 | 13-G1..G5 | Complete (technical candidate for review; external pilot/platform checks remain open) | `reports/13`; `release-readiness.md`; ADR 0012 |
| §9, §12, §18, §23 (Phase 2) | Second evaluator ecosystem, stored-output paired comparisons, grouped uncertainty, judge stability, cross-framework disagreement, and no-reexecution evidence | 14 | 14-G1..G4 | Verified (local deterministic/real-package; live provider and time-saved study pending) | reports/14; ADR 0013; tests/test_ragas_adapter.py; tests/test_cross_ecosystem.py; tests/test_comparison_statistics.py |
| §7, §18 (Phase 2) | Richer runners, agent outcome contracts | 15 | 15-G1..G4 | Verified (Windows; containers via Docker Desktop) | reports/15; ADR 0014 |
| §7 Python callable | Callable in a fresh interpreter through a stdlib shim; CLI-protocol bounds; trusted-local | 15 | 15-G4 | Verified | tests/test_runner_transports.py (end to end through `app smoke`); test_runner_review_regressions.py (UTF-8, prints) |
| §7 OpenAI-compatible endpoint | Chat-completions application transport; usage observed; tool calls as requests; origin/secret policy | 15 | 15-G4 | Verified (local stub only) | test_openai_compatible_endpoint_runs_end_to_end_through_the_cli; no live provider |
| §7, §16 Container | Digest-pinned image, non-root uid/gid, read-only root and mounts, tmpfs, no capabilities, limits, network none unless approved, no engine socket, removed on timeout | 15 | 15-G1 | Verified (Docker Engine 29.7.2, Docker Desktop, Windows 11) | test_a_real_container_fixture_runs_end_to_end_through_the_cli, test_the_container_runs_non_root_read_only_and_offline, test_a_container_timeout_kills_and_removes_the_container; hardening regressions. Not a hostile multi-tenant sandbox |
| §7 State and episodes | Reset hooks; per_case / per_episode / shared; failed reset blocks; broken or interrupted episodes block; concurrency 1 for stateful apps | 15 | 15-G2 | Verified | tests/test_agent_worlds.py (state reset between cases, kept within an episode, reset between episodes; contrast with shared) |
| §7 Test worlds | Declared seeds, policy approval, frozen with the run, reported | 15 | 15-G2 | Verified | test_test_world_rules_are_enforced_before_anything_runs, test_the_seed_is_frozen_with_the_run |
| §7 Tool outcome contracts | tool_calls (names) separate from tool_outcomes (arguments, success, authorization) and final_state (world state) | 15 | 15-G3 | Verified | test_correct_tool_names_never_mask_a_failed_outcome; tests/test_agent_evaluators.py |
| §8, §7 Chat: runner capabilities and test worlds | `/app`, `describe_application` tool, `/world`, grounded `propose_plan_patch(test_world)` | 15 | 15-G4 | Verified (scripted model) | tests/test_chat_test_worlds.py |
| §8, §14, §15, §18 (Phase 2) | Inspection, traces, caching, parallel execution | 16 | 16-G1..G5 | Verified (Windows; local fixtures) | reports/16; ADR 0015 |
| §8 Source inspection | Approved roots only; manifests and imports as inferred findings with evidence locations; secrets never read; inferred never confirms a capability; policy-checked probes produce observations | 16 | 16-G1 | Verified | tests/test_source_inspection.py (misleading imports stay inferred; G1 catalog ineligibility; probe observation; refused probe creates nothing) |
| §14 Imported observations | OTLP/JSON traces, raw preserved, correlation IDs, completeness reasons, lowest-span usage aggregation | 16 | 16-G2 | Verified | tests/test_trace_import.py (end to end against traced_app; partial traces stay partial; nested aggregates not double counted) |
| §14, §15 Caches | Opt-in execution/evaluation caches with version-complete keys, provenance, refused for effectful/stateful apps; hits excluded from latency | 16 | 16-G3 | Verified | tests/test_caches.py (invalidation matrix over input, app revision, policy, reference, parameters, plugin version) |
| §15, §18 Quotas and parallelism | Provider-aware quotas, 429/503 backpressure, bounded tasks, cancellation while throttled, event-loop responsiveness | 16 | 16-G4 | Verified (local rate-limited server; no production-scale claim) | tests/test_parallel_execution.py |
| §11, §18 (Phase 2) | OpenAI evaluation bridges, one platform connector | 17 | 17-G1..G5 | Verified (local contract fixtures; no live service) | reports/17; ADR 0017 (ADR 0016 separately records the `17-P18` subset Prompt 18 used) |
| §11A OpenAI Evals OSS | Pinned `evals`, allowlisted eval types, exact recorded replay, live completion-function bridge as a recorded delegated suite | 17 | 17-G1 | Verified (real upstream package) | tests/test_openai_evals_oss.py |
| §11B OpenAI Evals API | Stored-output grading as remote jobs; state and request persisted before sending; reconcile ambiguous submissions; paginated one-to-one mapping; no generated replacement outputs | 17 | 17-G2, 17-G3 | Verified (real SDK against a local contract stand-in; live service not exercised) | tests/test_openai_evals_api.py |
| §9, §18 Platform connector | Langfuse dataset/trace import and score export with provenance; import/export never computes metrics | 17 | 17-G4 | Verified (local stand-in; live deployment not exercised) | tests/test_langfuse_connector.py |
| §9, §16 Exposure and egress | Integration modes, destinations and availability in CLI/chat/planning; `allowed_egress_origins` | 17 | 17-G4 | Verified | tests/test_integrations.py |
| §6, §19 (Phase 3) | Bounded development candidate generation with source/model/prompt provenance, review state, exact duplicate-source records, protected holdout and explicit promotion | 18 | 18-G1, 18-G2 | Verified | tests/test_candidate_workflow.py (explicit development-only inputs; holdout rejected before read/provider call; unreviewed candidates stay outside datasets and promotion is explicit) |
| §19 (Phase 3) | Multi-turn text episode schema, simulator provenance, per-episode state reset, and independent final-state success checks | 18 | 18-G3 | Verified | examples/multi_turn_text; tests/test_episode_contract.py (two episodes, four turns, real local HTTP fixture, reset and final-state checks) |
| §19 (Phase 3) | Prompt 18 local verification, predecessor regressions, ticket/requirement status and five-part report | 18 | 18-G4 | Verified | reports/18; 88 focused tests passed; Ruff, mypy, schema export and CLI validation passed |
| §19, §24 (Phase 3) | Controlled experiment contract: exposed finite parameters, objective, constraints, development dataset, intended change, budgets and immutable lineage | 19 | 19-T1, 19-G1 | Verified | reports/19; tests/test_experiments.py::test_known_objective_budget_resume_and_protected_holdout |
| §19, §24 (Phase 3) | Development selection with paired uncertainty and one-time baseline/selected protected holdout; hidden labels unavailable to trial search | 19 | 19-T2, 19-G2, 19-G3 | Verified | reports/19; tests/test_experiments.py::test_frozen_rubric_and_exposed_parameter_space_reject_changes and ::test_interrupted_holdout_resumes_same_frozen_plan_and_run_ids; migration 11 protected_dataset_digests |
| §8, §19, §24 | Conversational report and non-applying adoption proposal; source/deployment/production changes need separate explicit authorization | 19 | 19-T3 | Verified | reports/19; tests/test_experiments.py::test_experiment_conversation_tools_are_read_only_proposals |
| §19 (Phase 3) | Prompt 19 verification, predecessor regressions, ticket/requirement status and five-part report | 19 | 19-G4 | Verified | reports/19; final batch 135 passed, 1 skipped; Prompt 14/18 regressions; policy-root and code-identity regression checks; Ruff/mypy; engine timing failure passed isolated rerun |
| §19 (Phase 3) | Distributed execution | 20 (deferred) | — | Deferred | Phase 2/3 scope |
| §19 (Phase 3) | Dashboard, plugin catalog | 21 (deferred) | — | Deferred | Phase 2/3 scope |

No speculative service, dashboard, Rust component, or paid-provider requirement is scheduled
into the core (00-G3 respected).
