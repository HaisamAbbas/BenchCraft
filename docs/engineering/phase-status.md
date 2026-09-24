# Phase ledger

Tracks status per numbered prompt from `docs/spec/prompt-pack.md`. Status values:
COMPLETE | PARTIAL | BLOCKED | DEFERRED | NOT_STARTED.

| Prompt | Product phase | Title | Status | Report |
|---|---|---|---|---|
| 00 | MVP | Bootstrap and specification traceability | COMPLETE | `docs/engineering/reports/00.md` |
| 01 | MVP | Canonical models, configuration, and datasets | COMPLETE | `docs/engineering/reports/01.md` |
| 02 | MVP | Durable run storage and artifacts | COMPLETE | `docs/engineering/reports/02.md` |
| 03 | MVP | Application runners and observation capture | COMPLETE | `docs/engineering/reports/03.md` |
| 04 | MVP | Evaluator contracts, native checks, and registry | COMPLETE | `docs/engineering/reports/04.md` |
| 05 | MVP | DeepEval adapter | COMPLETE | `docs/engineering/reports/05.md` |
| 06 | MVP | Deterministic scheduling, policy, budgets, and recovery | COMPLETE | `docs/engineering/reports/06.md` |
| 07 | MVP | Evaluation planning and bounded LLM reasoning | COMPLETE | `docs/engineering/reports/07.md` |
| 08 | MVP | Persistent two-way conversation and typed actions | COMPLETE | `docs/engineering/reports/08.md` |
| 09 | MVP | Interactive terminal and live controls | COMPLETE | `docs/engineering/reports/09.md` |
| 10 | MVP | Conversation recovery and adversarial interaction | COMPLETE | `docs/engineering/reports/10.md` |
| 11 | MVP | Evidence reports, command composition, and packaging | COMPLETE | `docs/engineering/reports/11.md` |
| 12 | MVP | MVP acceptance and harness validation | COMPLETE | `docs/engineering/reports/12.md` |
| 13 | MVP | Release candidate and pilot handoff | COMPLETE | `docs/engineering/reports/13.md` |
| 14 | Phase 2 | Second evaluator ecosystem and comparisons | COMPLETE | `docs/engineering/reports/14.md` |
| 15 | Phase 2 | Richer runners and agent outcome contracts | COMPLETE | `docs/engineering/reports/15.md` |
| 16 | Phase 2 | Inspection, traces, caching, and parallel execution | COMPLETE | `docs/engineering/reports/16.md` |
| 17 | Phase 2 | OpenAI evaluation bridges and one platform connector | COMPLETE | `docs/engineering/reports/17.md` |
| 18 | Phase 3 | Reviewed dataset generation and advanced episodes | COMPLETE | `docs/engineering/reports/18.md` |
| 19 | Phase 3 | Controlled optimization experiments | COMPLETE | `docs/engineering/reports/19.md` |
| 20–21 | Phase 3 | Optional expansion | NOT_STARTED | — |

## Prompt 18 prerequisite subset

Prompt 17 was completed afterwards, by its own tickets and gates (`docs/engineering/reports/17.md`,
ADR 0017); the note below records what Prompt 18 relied on at the time it ran.

When Prompt 18 ran, Prompt 17 was not complete. Prompt 18 uses the explicit completed subset `17-P18`, recorded
under ADR 0016: the policy-checked OpenAI-compatible provider boundary, development-only
source input contract, and already-tested per-episode reset/final-state runner contracts.
Prompt 18 does not depend on the OpenAI Evals bridges or a platform connector. `17-P18` does
not satisfy Prompt 17's general acceptance gates or authorize treating Prompt 17 as complete.
