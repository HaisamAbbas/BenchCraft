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
| §10 DeepEval adapter | Pinned faithfulness adapter | 05 | 05-G1..G5 |
| §15 Execution/concurrency | Deterministic scheduling, budgets, retries, resume | 06 | 06-G1..G5 |
| §8 Agent architecture | Bounded LLM planner, plan compiler/validator | 07 | 07-G1..G5 |
| §2–5, §8 | Persistent session, decisions, typed actions | 08 | 08-G1..G5 |
| §3, §13 | Interactive terminal, slash controls | 09 | 09-G1..G5 |
| §8, §13–16 | Conversation recovery, adversarial hardening | 10 | 10-G1..G5 |
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
