# Requirements matrix

Maps specification requirements to the owning numbered prompt and its observable acceptance
gate. Source: `docs/spec/implementation-plan.md` v1.1. Phase 2/3 requirements are marked
deferred; they are owned by prompts 14–21 and are out of scope until explicitly requested.

| Spec section | Requirement | Owning prompt | Gate(s) |
|---|---|---|---|
| §2 Core concepts | Immutable Golden/BenchmarkCase, ApplicationSpec, EvaluationPlan, ExecutionResult, EvaluationResult identities | 01 | 01-G1, 01-G2 |
| §2 | Application input vs judge-only reference separation | 01 | 01-G2 |
| §5 Internal data models | Versioned Pydantic models with exported JSON Schema | 01 | 01-G1 |
| §6 Dataset schema | JSONL shorthand + normalization rules (chatbot/RAG/tool/coding examples) | 01 | 01-G1, 01-G3 |
| §6 | Duplicate ID detection, stable generated IDs, line-precise errors | 01 | 01-G3 |
| §13 CLI design | `aibench dataset validate PATH` | 01 | 01-G1, 01-G3 |
| §14 Storage architecture | SQLite repositories, artifact commit protocol, run identity | 02 | 02-G1..G3 |
| §7 Application interface | CLI/HTTP runners, observation envelopes | 03 | 03-G1..G5 |
| §7 CLI/HTTP protocol | argv-only CLI, JSON stdin/stdout, text mode, bounded logs; HTTP bindings, secret refs, TLS, endpoint policy, redirect control, size/time limits, correlation IDs | 03 | 03-G1, 03-G3 |
| §7 State/observability | Missing retrieval/tool/usage/cost recorded as unknown; observability-gap report (`aibench app describe`) | 03 | 03-G4 |
| §15 Retry policy (runner side) | Runners never retry; `effect_state` marks ambiguous effects | 03 (engine use: 06) | 03-G3 |
| §16 Security | Trusted-local mode explicit; Goldens never sent to apps; scoped app credentials; control-char/markup scrubbing of app output | 03 | 03-G2 |
| §9, §12 Evaluator/plugin architecture | Evaluator protocol, registry, native checks, canonical aggregation | 04 | 04-G1..G5 |
| §9 Plugin manifest / discovery | Manifests; entry-point discovery without import; subprocess manifest worker | 04 (worker execution: 05) | 04-G2 |
| §9 Adapter contract | describe/validate_binding/prepare/evaluate/evaluate_batch/close; scores recorded outputs by default | 04 | 04-G1, 04-G3 |
| §12 Unified result schema | Typed value union, status vs decision, frozen rule, evidence, provenance, resources/accounting, raw artifact ref | 04 | 04-G1 |
| §12 Aggregation | Per-metric summaries with explicit denominators and coverage; no cross-metric averages | 04 | 04-G1 |
| §14 Storage | evaluation_attempts / metric_results persisted per scoring pass without overwrite | 04 | 04-G3 |
| ADR 0001 | Dependency-boundary test for core | 04 | 04-G4 |
| §10 DeepEval adapter | Pinned faithfulness adapter | 05 | 05-G1..G5 |
| §9 Controlled workers for plugin code | Third-party evaluators execute only in workers using the plugin environment's interpreter; enforceable cancellation | 05 | 05-G3, 05-G4 |
| §10 Field mapping / missing vs empty context | input, recorded output, observed retrieval; reference context never substituted; documented empty-context policy | 05 | 05-G1, 05-G2 |
| §10 Independent metric instances | New metric and judge per case; concurrency control test | 05 | 05-G3 |
| §16 Credentials / publishing | Judge secrets passed explicitly; telemetry, .env, legacy key file off; no Confident AI publishing | 05 | 05-G4 |
| §15 Execution/concurrency | Deterministic scheduling, budgets, retries, resume | 06 | 06-G1..G5 |
| §15 Scheduling | Work graph from validated plans; bounded concurrency per role; single writer (compare-and-set transitions + run lease) | 06 | 06-G1, 06-G4 |
| §15 Budget control | Reserve/reconcile; separate application/evaluator/planner accounting; hard call/token/wall limits; soft cost estimate; unknown never zero; carried across resume | 06 | 06-G4 |
| §15 Retry policy (engine side) | Effect-aware transient retries, bounded seeded backoff honouring Retry-After, no multiplication with evaluator retries, low scores never retried | 06 | 06-G3, 06-G4 |
| §15 Recovery | Frozen plan/app/bindings verified on resume; conservative in-flight recovery; ambiguous effects become `unknown_effect`; no exactly-once promise | 06 | 06-G3 |
| §16 Execution policy | Approved targets, data scope, credentials, effects, evaluator/data-egress, plugin environments and paths, budget ceilings — decided before dispatch | 06 | 06-G2 |
| §13 CLI design | `aibench plan validate`, `run --plan`, `resume`, `evaluate`, `runs status`; exit codes 0/2/3/4/130 | 06 | 06-G1, 06-G5 |
| §8 Agent architecture | Bounded LLM planner, plan compiler/validator | 07 | 07-G1..G5 |
| §3, §13 `inspect` | Evidence-backed profile from declared config and recorded runs; observability gaps with integration recipes; limited scope stated | 07 | 07-G3 |
| §5 Observation states | observed / declared / inferred / unknown with evidence locations; dataset hints recorded as inferred with limitations | 07 | 07-G3 |
| §8 Preventing invented capabilities | Registry-resolved IDs, deterministic eligibility, per-case field requirements, unavailable bindings/selectors/aggregations rejected outside the model | 07 | 07-G1 |
| §8 Planner tools | Narrow read-only tools (`read_profile` … `write_plan_draft`); no general terminal; bounded repairs/spend; template fallback | 07 | 07-G4 |
| §3, §13 `plan` / `plan validate` | Draft with objectives, rationale, gaps, pending questions, classified findings, coverage and spend estimate; revisions never silently overwritten | 07 | 07-G3, 07-G5 |
| §17 Manual vs generated plans | Hand-written and generated plans with identical content produce identical deterministic metrics | 07 | 07-G2 |
| §23 Benchmark the planner | Annotated fixtures, precision/recall/gap scoring, static template baseline | 07 | 07-G3 |
| §2–5, §8 | Persistent session, decisions, typed actions | 08 | 08-G1..G5 |
| §2, §5 Session records | `BenchmarkSession`, `ConversationTurn`, `PendingQuestion`, `DecisionRecord`, `ActionRequest` with exported schemas; decisions linked to source turn, revision and plan hash | 08 | 08-G4 |
| §8 Dialogue and action protocol | Typed turn outputs validated outside the model; patches grounded in the user's words; actions need the user's explicit request; no terminal/file/network tool | 08 | 08-G1, 08-G3 |
| §8 Expected revisions and action IDs | Compare-and-set revisions reject stale patches and stale answers; action IDs deduplicated; one active run per session | 08 | 08-G3, 08-G4 |
| §8, §15 Live conversation | Questions and explanations during execution never touch the run; scope changes create a new draft; controls are recorded events | 08 | 08-G2 |
| §4, §8 Shared services | Session tools call `compile_plan`, `create_run`, `execute_run`, `run_status` and stored results — the headless commands' services | 08 | 08-G1 |
| §14 Session persistence | Migration 7 tables; turns, decisions, questions and actions survive restart; run events replayable; runs not owned by sessions; credentials redacted from chat | 08 | 08-G4 |
| §16 Data egress | The assistant never receives reference answers; case content only with `share_case_content_with_assistant` | 08 | 08-G5 |
| §3, §13 | Interactive terminal entry, project/session selection, multiline input, history, completion | 09 | 09-G1, 09-G5 |
| §13, §15 | Streamed replies, tool cards, coalesced committed progress, partial metric labels, responsive controls | 09 | 09-G2, 09-G4, 09-G5 |
| §13, §15 | Deterministic slash controls; provider-independent status/stop; run interruption and graceful exit | 09 | 09-G3..G5 |
| §8, §13–16 | Conversation recovery, adversarial hardening | 10 | 10-G1..G5 |
| §8, §14 Resumption | Reopening reconciles conversation with authoritative run state (stored status + lease); interrupted runs stay stopped until a new action; unknown-effect work reported | 10 | 10-G1 |
| §8, §14 Event replay | Per-session event cursors; missed events replayed by sequence; no action replayed | 10 | 10-G1 |
| §8 Deduplicated actions under crashes | A redelivered start adopts the run its crashed first delivery created (via its approval) or closes after the starting window; never a second run | 10 | 10-G2 |
| §8 Expected revisions under races | Late model patches, starts and answers against an older revision (incl. after a dataset change) are rejected | 10 | 10-G3 |
| §8, §16 Summaries and injected text | Bounded structured summaries labelled non-authoritative; permissions only from policy and the session's grant; tool-output instructions cannot authorize actions or supply values | 10 | 10-G4 |
| §14, §16 Redaction | Secrets and terminal control/bidi content removed from history, model input and rendering; emoji codes never substituted in data | 10 | 10-G4 |
| §14 Session deletion | Deleting a conversation keeps runs, results, artifacts and events | 10 | 10-G5 |
| §12–14 | Evidence reports, command composition, packaging | 11 | 11-G1..G5 |
| §17, §23–24 | MVP acceptance validation | 12 | 12-G1..G5 |
| §17–18, §22–24 | Release candidate and pilot handoff | 13 | 13-G1..G5 |
| §9, §18 (Phase 2) | Second evaluator ecosystem, comparisons | 14 (deferred) | 14-G1..G4 |
| §7, §18 (Phase 2) | Richer runners, agent outcome contracts | 15 (deferred) | 15-G1..G4 |
| §16, §18 (Phase 2) | Inspection, traces, caching, parallel execution | 16 (deferred) | — |
| §11, §18 (Phase 2) | OpenAI evaluation bridges, one platform connector | 17 (deferred) | — |
| §19 (Phase 3) | Reviewed dataset generation, advanced episodes | 18 (deferred) | — |
| §19 (Phase 3) | Controlled optimization experiments | 19 (deferred) | — |
| §19 (Phase 3) | Distributed execution | 20 (deferred) | — |
| §19 (Phase 3) | Dashboard, plugin catalog | 21 (deferred) | — |

No speculative service, dashboard, Rust component, or paid-provider requirement is scheduled
into the core (00-G3 respected).
