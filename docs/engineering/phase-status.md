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
| 20 | Phase 3 | Distributed execution after measured need | DEFERRED | `docs/engineering/reports/20.md` |
| 21 | Phase 3 | Optional dashboard and curated plugin catalog | NOT_STARTED | — |
| 22 | MVP closeout | End-to-end acceptance and ticket-value audit | COMPLETE (local technical scope; external validation not started) | `docs/engineering/reports/22.md` |
| 23 | Final acceptance audit | Satisfied by Prompt 22 (user confirmed numbering mismatch; no duplicate audit) | COMPLETE (alias of 22; local technical scope only) | `docs/engineering/reports/22.md` |
| 24 | V4 alignment checkpoint | Product alignment and repository delta map | COMPLETE (84 targeted tests passed; 3 skipped; full suite not rerun) | `docs/engineering/reports/24.md` |
| 25 | V4 repository-aware MVP | Bounded codebase inspector and evidence-backed profile | COMPLETE (29 focused tests passed; Windows/Python 3.12) | `docs/engineering/reports/25.md` |
| 26 | V4 repository-aware MVP | Evaluation opportunities and candidate dataset discovery | COMPLETE (114 passed, 2 environment-dependent skips; full suite not rerun) | `docs/engineering/reports/26.md` |
| 27 | V4 repository-aware MVP | Complete conversational evaluation loop | COMPLETE (deterministic scripted local loop; live model/human validation not run) | `docs/engineering/reports/27.md` |
| 28 | Optional Phase 2 | Black-box HTTP evaluation | COMPLETE (existing configured HTTP API contract audited; broad URL/browser scope remains deferred) | `docs/engineering/reports/28.md` |
| 29 | V4 acceptance | Product acceptance and value review | COMPLETE for deterministic local scope (32 clean-install E2E tests, E2E-01..08; all 12 mapped journeys pass) | `docs/engineering/reports/31.md` (supersedes partial snapshot in report 29) |
| 30 | V4 acceptance follow-up | Conversational repository-inspection acceptance gap | COMPLETE (fresh repository findings flow through chat; E2E-08) | `docs/engineering/reports/30.md` |
| 31 | v3-derived local-scope closure | Bounded onboarding, hybrid evidence continuity, conversational experiment control, final value review | COMPLETE for deterministic local scope; live/human/PMF validation remains unrun | `docs/engineering/reports/31.md` |

## Prompt 18 prerequisite subset

Prompt 17 was completed afterwards, by its own tickets and gates (`docs/engineering/reports/17.md`,
ADR 0017); the note below records what Prompt 18 relied on at the time it ran.

When Prompt 18 ran, Prompt 17 was not complete. Prompt 18 uses the explicit completed subset `17-P18`, recorded
under ADR 0016: the policy-checked OpenAI-compatible provider boundary, development-only
source input contract, and already-tested per-episode reset/final-state runner contracts.
Prompt 18 does not depend on the OpenAI Evals bridges or a platform connector. `17-P18` does
not satisfy Prompt 17's general acceptance gates or authorize treating Prompt 17 as complete.
