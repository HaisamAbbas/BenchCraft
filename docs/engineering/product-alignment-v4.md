# Product alignment and repository delta map — v4.1

Date: 2026-09-25. Scope: evidence/planning checkpoint only; no product contract or code
changed by Prompt 24.

Matrix updates after Prompts 25 and 26: V4-03 and V4-04 include bounded-inspection/profile
evidence from `reports/25.md`. Prompt 26 adds schema-level JSONL candidate validation,
evidence-backed metric opportunities, and sole-dataset reuse under the existing inspection
policy (`reports/26.md`). V4-05 remains partial because test/evaluator paths are not
semantically validated suites, and repository compatibility does not establish reference
quality or objective fit.

## Audit basis and limits

- Product requirements: root `AI-Bench-Product-Aligned-Codex-Prompt-Pack-v4 (1).md`,
  §§2–9 and its normative §11 v3 feature-preservation map.
- Prior contract: `docs/spec/implementation-plan.md` (v1.1, source identity in
  `docs/spec/SOURCE.md`) and `docs/spec/prompt-pack.md` (v1.0).
- Prior acceptance evidence: `docs/engineering/reports/22.md` and
  `docs/engineering/evidence/22/`. The user clarified that “Prompt 23” is a numbering
  mistake and Prompt 22 is the intended final acceptance audit; it is accepted as the
  prerequisite without creating a duplicate report. No standalone v3 vision pack was found;
  the v4 §11 map is the available v3-derived scope record. This prevents a direct
  line-by-line comparison against the original v3 source.
- Prior engineering records: `tickets.md`, `ticket-test-matrix.md` (82 rows for prompts
  00–20 plus 17-P18), `requirements-matrix.md`, `phase-status.md`, and reports 00–22.
- Repository instructions: no `AGENTS.md` was found by `rg --files -g AGENTS.md`; the
  applicable persisted contract is `docs/engineering/implementation-contract.md` and the
  user supplied `Constitution/production_quality_software_engineering_prompt-3.md`.
- Worktree was already dirty before this checkpoint, including code, tests, ledgers, and
  reports. Those edits were treated as current repository state and left untouched except
  for the Prompt 24 ledger additions described in report 24.

### Prompt 23 prerequisite and executed evidence

**Prerequisite accepted:** the supplied v4 names Prompt 23, but the user clarified this is a
numbering mistake and Prompt 22 is the intended final acceptance audit. Accordingly,
`reports/22.md` and its saved evidence satisfy the prerequisite; no duplicate Prompt 23
report or replay is created. Prompt 22 explicitly limits its result to local technical scope.

Independent targeted verification run:

```powershell
.venv\Scripts\python -m pytest -q -p no:cacheprovider tests/test_source_inspection.py tests/test_inspection.py tests/test_candidate_workflow.py tests/test_conversation.py tests/test_conversation_hardening.py tests/test_mvp_acceptance.py tests/test_http_runner.py tests/test_endpoint_policy.py tests/test_e2e_cli_journey.py
```

Initial normal-permission attempts could not obtain a valid result because pytest cleanup
was denied by the workspace sandbox. After rerunning with elevated permissions, the same
targeted test set passed 84 tests and skipped 3 (see `docs/engineering/reports/24.md`).
The earlier restricted attempts emitted setup errors;
isolating `tests/test_source_inspection.py` showed pytest session cleanup failed with
`PermissionError: [WinError 5] Access is denied` while scanning the selected temp tree.
Retried with `AIBENCH_TEST_TMPDIR=D:\BenchCraft\.prompt24-tests` and later an explicit
`--basetemp=D:\BenchCraft\p24-tests-tmp`; the explicit base ran all nine test bodies but
pytest reported 9 errors because it could not remove the basetemp directory. No clean pass
count is claimed for those attempts. Prompt 22's saved full-suite artifact reports 984 passed, 0 failed, 3
skipped. By user clarification it satisfies the intended Prompt 23 audit prerequisite, but
it is historical evidence and not a rerun of the current dirty worktree.

## Requirement alignment matrix

Status meanings: **implemented** means the cited contract is directly supported by code and
executed test evidence; **partial** means a narrower supported behavior exists; **missing**
means no implementation/evidence found; **conflicting** means current behavior/evidence
contradicts the new contract; **deferred** means deliberately out of the current repository-
aware scope. “Changes contract” refers to the existing v1.1/Prompt 00–22 behavior.

| Requirement ID | Product behavior / invariant | Current implementation path | Actual executed evidence | Status | Changes existing contract? | Recommended ticket / phase and reason |
|---|---|---|---|---|---|---|
| V4-01 | Conversational benchmark operator: natural goal through run, evidence, follow-up in persistent session | `src/aibench/conversation/agent.py`; `src/aibench/sessions/`; `src/aibench/tui/`; shared services | Prompt 27 complete-loop fixture; clean-installed E2E-01; scripted assistant, not live model (`reports/27.md`) | implemented | No core storage/run contract; user intent semantics clarified | No new ticket; 27-T1..T4 complete; live-model/human quality evidence remains outside local implementation status |
| V4-02 | Plain-language objective maps to applicable metrics only where evidence and semantics fit | `src/aibench/planning/catalog.py`; `src/aibench/planning/opportunities.py`; `src/aibench/cli/plan.py`; `src/aibench/engine/compile.py` | Prompt 26 focused batch: 114 passed, 2 environment-dependent skips. RAG/tool fixtures distinguish available/missing app evidence; unknown objectives remain unknown; `plan opportunities` JSON emits no plan and runs no app; existing Prompt 22 planner fixture recall 17/21 | partial | Additive read-only CLI; conversational planner contract remains open | 27-T1 integrates shared opportunities into chat; keep deterministic concept vocabulary limitations explicit |
| V4-03 | Bounded repository inspection, source locations, supported language/file matrix; inspection never executes code | `src/aibench/inspection/source.py`; `src/aibench/inspection/profile.py`; `src/aibench/cli/inspect.py`; fixture `examples/inspection/repository_profile/` | Prompt 25 focused command in `reports/25.md`: 29 passed, including bounds, approved-root/symlink, secret exclusion, no-execution, prompt-injection-text and unsupported-language cases | implemented | Yes, additive repository-aware discovery scope | 25-T1 DONE. Keep the published support matrix narrow; future parsers require separate evidence and tests |
| V4-04 | Evidence-backed app profile labels observed/inferred/declared/unknown and uncertainty | `src/aibench/inspection/profile.py`; `src/aibench/inspection/source.py`; `src/aibench/core/models.py`; `src/aibench/cli/inspect.py` | Prompt 25 fixture asserts source path/line refs, inferred confidence, declared invocation provenance, unknown unsupported source and unknown runtime capability; `tests/test_inspection.py` and CLI tests also ran in the 29-pass focused suite | implemented | Yes, additive repository evidence in profile output; existing claim semantics retained | 25-T2 DONE for the inspector/profile service. Prompt 29 found that source findings are not exposed through a fresh conversation; 30-T1 covers that separate integration gap. No duplicate profile service is planned. |
| V4-05 | Discover candidate datasets, suites, tests/evals and invocation paths with provenance; tests are not goldens | `src/aibench/inspection/source.py`; `src/aibench/inspection/candidates.py`; `src/aibench/cli/inspect.py`; `src/aibench/datasets/candidates.py`; `src/aibench/services/candidates.py` | Prompt 26 focused batch: 114 passed, 2 environment-dependent skips. Tests validate bounded JSONL shape/field counts, `inspection_roots`/`data_roots`; tests/evaluator/invocation findings remain path-only; generated references are excluded; Prompt 18 review/promotion tests retained | partial | Yes, additive repository inventory and schema validation | No duplicate path inspector or candidate workflow; semantic suite recognition and reference correctness are not claimed |
| V4-06 | Discover opportunities, coverage gaps and unavailable metrics without guessing | `src/aibench/planning/catalog.py`; `src/aibench/planning/opportunities.py`; `src/aibench/cli/plan.py`; `src/aibench/engine/compile.py` | Prompt 26 focused batch: 114 passed, 2 environment-dependent skips. RAG/tool fixtures show missing evidence unavailable and keep reference context judge-only; plan opportunity CLI test proves read-only behavior; Prompt 22 stored E2E evidence remains historical | partial | Additive recommendation surface; evaluator result semantics unchanged | 27-T1 connects grounded opportunities to the conversational path; keyword concept mapping remains bounded |
| V4-07 | Clear bounded evaluation request executes under existing policy without a second “run?” confirmation; plan-only requests do not execute | `src/aibench/conversation/agent.py`; `src/aibench/sessions/controller.py`; `src/aibench/services/runs.py`; `src/aibench/tui/commands.py` | Prompt 27 tests prove a current unpresented plan starts on a clear goal, plan-only creates no run, exact case count must match the validated draft, policy denial dispatches nothing, and `/run` starts once with a preview; clean-installed E2E-01 (`reports/27.md`) | implemented | Yes, authorization/clarification contract changes within existing policy | No new ticket; 27-T2 complete; retain material ambiguity, policy and runner validation gates |
| V4-08 | One compatible configured dataset/runner/policy reused; ask one focused question for material ambiguity or blocker | `src/aibench/cli/chat.py`; `src/aibench/inspection/candidates.py`; `src/aibench/security/policy.py`; `src/aibench/sessions/drafting.py`; `src/aibench/conversation/agent.py` | Prompt 26 proves sole content identity reuse only inside current `inspection_roots` and optional `data_roots`; Prompt 27 reuses the session's configured runner/policy and routes execution through the validated draft; distinct candidate and scope ambiguity tests pass (`reports/26.md`, `reports/27.md`) | implemented | Yes, additive default dataset discovery for new sessions | No new ticket; do not infer alternate invocation paths or widen policy |
| V4-09 | Typed runner invocation is validated against policy, args, cwd, env references, timeout and effects; no arbitrary planner shell | `src/aibench/runners/`; `src/aibench/engine/compile.py`; `src/aibench/security/policy.py`; `src/aibench/registry/worker.py` | Prompt 22 E2E denial/no-call evidence; Prompt 15/16 saved tests. Current targeted suite passed (84 passed, 3 skipped; report 24) | implemented | No; existing safety contract is preserved | No new ticket. Reuse these gates in 27-T1; repository inspection itself still must remain non-executing |
| V4-10 | Dataset and repository content are untrusted; references stay judge-only; generated examples remain candidate-only | `src/aibench/core/models.py`; `src/aibench/runners/bindings.py`; `src/aibench/datasets/candidates.py`; `src/aibench/services/candidates.py`; `src/aibench/conversation/agent.py` | Prompt 29 clean-installed E2E-06 and current test_input_binding_cannot_reach_judge_only_data; Prompt 22 E2E-04–07 historical evidence; Prompt 18 candidate workflow evidence | implemented | No; core invariant retained | No new ticket; include existing test names in 27/29 traceability |
| V4-11 | Evidence integrity: unavailable internal retrieval/tool evidence is never imputed from references; distinguish app/evaluator errors, low score, N/A, unavailable and partial | `src/aibench/core/models.py`; `src/aibench/evaluators/`; `src/aibench/reporting/`; `plugins/deepeval/`; `plugins/ragas/` | Prompt 29 E2E-06 and real-package JUnit: DeepEval 18 passed, Ragas 9 passed with deterministic judges; Prompt 22 missing-evidence E2E; no live providers | implemented | No; existing result contracts retained | No new ticket. Acceptance must preserve these contracts |
| V4-12 | Progress queries, interruption, resume and questions do not reset/duplicate a run | `src/aibench/engine/engine.py`; `src/aibench/sessions/`; `src/aibench/tui/`; `src/aibench/storage/` | Prompt 22 clean E2E E2E-01/02/06, 30/30; full-suite report 984 passed historical | implemented | No; existing behavior contract | No ticket; repeat journey in 27/29 acceptance |
| V4-13 | Failure analysis cites stored cases/evidence and labels hypotheses; report states missing coverage and next experiment | `src/aibench/services/reports.py`; `src/aibench/reporting/render.py`; `src/aibench/conversation/agent.py`; `src/aibench/cli/report.py` | Prompt 27 fixture queries stored failures and case evidence, labels cause as a hypothesis, returns provenance and coverage facts, and gives a specific evidence-gap experiment; clean-installed E2E-01 (`reports/27.md`) | implemented | Yes, conversational evidence-analysis output is additive | No new ticket; 27-T3 complete; scripted output does not prove live-model quality |
| V4-14 | Evaluator adapters isolate vendors, pin tested APIs, preserve semantics/provenance/raw refs; mocks do not imply live compatibility | `src/aibench/evaluators/protocol.py`; `src/aibench/registry/`; `plugins/deepeval/`, `plugins/ragas/`, `plugins/openai_evals_*` | Prompt 29 current real-package JUnit: DeepEval 18 passed and Ragas 9 passed with deterministic local judges; reports 05/14/17 record earlier package contracts; live providers explicitly unrun | implemented | No; adapter boundary is established | No new adapter ticket. Do not duplicate DeepEval/Ragas; retain live-check disclosure |
| V4-15 | Dataset source choices explicit; historical data controlled; generated cases candidate-only with provenance/review | `src/aibench/inspection/candidates.py`; `src/aibench/cli/chat.py`; `src/aibench/datasets/candidates.py`; `src/aibench/services/candidates.py`; `src/aibench/connectors/langfuse.py` | Prompt 26 tests reject synthetic-unverified rows from compatible selection, isolate references and expose only paths/counts; Prompt 18 candidate tests and Prompt 17 local connector contract tests; no live connector validation | partial | Yes, repository dataset selection expands | Prompt 26 uses the existing review/promotion workflow; historical-source selection remains outside scope |
| V4-16 | Black-box API has a narrow request/response/auth/timeout/rate/egress/effect contract; no arbitrary URL/browser implication | `src/aibench/core/models.py` (`HttpTransport`, `InputBinding`, `OutputBinding`); `src/aibench/runners/http_runner.py`; `src/aibench/security/endpoints.py`; `src/aibench/security/policy.py`; `src/aibench/engine/` budgets, retries and quotas; `examples/apps/http_rag_app.py`; `examples/acceptance/rag_service.py`; `examples/acceptance/blackbox.app.json`; `tests/test_http_runner.py`, `tests/test_endpoint_policy.py` | Prompt 28 focused suite: 78 passed, 2 documented skips, including secrets, policy denial, endpoint bounds, timeout/cancel/effect state, quotas and cost budgets; Prompt 27 and Prompt 29 clean-installed E2E-01 use the local HTTP RAG service through conversation/session/run/report (`reports/27.md`, `evidence/27/e2e-01.json`) | implemented | Yes; v4 release claim is narrowed to an explicitly configured JSON HTTP API contract already present in v1.1 | No new runtime ticket; 28-T1 audited and reused existing implementation. Keep broad URL/browser claims deferred under V4-17 |
| V4-17 | Generic website/browser automation and broad black-box MVP claim deferred; existing supported HTTP contract may be reported narrowly | Same HTTP runner and policy paths as V4-16; no browser runner found | Saved HTTP fixture tests and Prompt 12 black-box gap test; no evidence for browser automation | deferred | Yes, staging/release scope changes | Prompt 28 confirms the supported narrow mode; no browser or generic URL implementation ticket |
| V4-18 | Hybrid black-box → structured traces → repository enrichment preserves session/run history | src/aibench/sessions/; src/aibench/observations/; src/aibench/connectors/; repository source inspection paths | No transition journey/test in the current matrix or reports 00–28; Prompt 29 explicitly leaves it outside the local-pilot scope | missing | Yes, new cross-mode lifecycle contract | No current-scope ticket. If promoted later, define a separate cross-mode phase and history-preservation acceptance ticket; do not imply it exists. |
| V4-19 | Persistent run identity, reproducibility, rescore from stored outputs, compatible comparison; history immutable | `src/aibench/storage/`; `src/aibench/services/scoring.py`; `src/aibench/services/comparison.py`; `src/aibench/experiments/`; `src/aibench/sessions/controller.py` | Prompt 27 rescores the session's same run through `evaluate_run`, observes zero additional app calls, and passes current comparison/rescore regressions (`reports/27.md`) | implemented | No; existing mandatory contract | No new ticket; Prompt 29 still performs the whole-scope acceptance audit |
| V4-20 | Controlled experiments freeze dataset/app/prompt/model/evaluator/runner/seeds and report coverage, uncertainty, cost/latency without causal overclaim | `src/aibench/experiments/service.py`; `src/aibench/reporting/statistics.py`; `docs/experiments/controlled-experiments.md` | Prompt 19 report and tests; Prompt 22 full suite historical | implemented | No; existing Phase 3 contract | No new ticket; product integration into conversational loop is 27-T4, since current chat reachability is incomplete |
| V4-21 | No source edits, fake scores/packages, unsupported capability claims, or policy expansion from untrusted text | `src/aibench/security/`; `src/aibench/engine/`; adapter packages; v1.0 contract | Prompt 22 denial and isolation E2E evidence; Prompt 5/14 real-package tests; Prompt 24 targeted suite; Prompt 28 HTTP policy/secret regression batch (78 passed, 2 skipped) | implemented | No; retained safety contract | No new ticket; mandatory regression gates in 25/27/28/29 |
| V4-22 | v3 broad black-box MVP claim is deferred unless current safe/tested contract supports only that narrow mode; retain old ticket/test/report discipline | `docs/engineering/tickets.md`; `ticket-test-matrix.md`; `phase-status.md`; HTTP runner paths | Prompt 22 matrix/report exist and are accepted as the intended final audit; Prompt 28 validates the configured narrow HTTP mode; no standalone v3 pack | partial | Yes, release scope is staged and clarified | Prompt 24 and Prompt 28 preserve the narrow API contract and defer broad website/browser claims; absent v3 source remains a scope limitation |
| V4-23 | Treat the product wedge and coding-agent-style analogy as hypotheses; report evidence for goal expression, inspection, end-to-end execution, honest gaps, rescore and persistent steering; make no uniqueness/market claim | src/aibench/conversation/; src/aibench/inspection/; src/aibench/services/scoring.py; src/aibench/sessions/; docs/engineering/reports/22.md; docs/engineering/reports/29.md | Prompt 22 and Prompt 29 scripted local evidence; no real-agent trial, human review, pilot or market evidence | partial | No code contract; product validation claim boundary is clarified | 29-T1 completed the value review; external validation remains unstarted and no uniqueness claim is allowed. |
| V4-24 | Retain engineering controls: ticket/test/report traceability, shared services, immutable run/evidence contracts, no unsupported claims, no policy expansion or external effects | docs/engineering/implementation-contract.md; docs/engineering/tickets.md; docs/engineering/ticket-test-matrix.md; docs/engineering/phase-status.md; docs/engineering/reports/00.md–29.md | Prompt 29 reconciled all 24 rows and ticket/report ledgers; documented clean-install suite passed 31 tests; one required repo-to-chat journey remains open | partial | No; v4 explicitly retains the existing contract | 29-G1 passed; 29-G3 remains partial until 30-T1 supplies the missing journey and all acceptance scope is rerun. |

## V3 conflicts and decisions

1. **“Approve/run?” language:** v4 §3 is the active contract. A clear bounded evaluation
   request with complete configured policy does not receive a second confirmation prompt.
   Material ambiguity, missing permission, missing required binding, budget breach, or
   unsafe/unknown invocation remains a genuine blocker. Prompt 22's explicit “Run it.”
   journey did not prove the v4 behavior; 27-T2 closes and tests that delta.
2. **Broad black-box MVP:** v3's broad MVP framing is superseded by v4 §§4 and 7. Keep only
   the existing HTTP runner's tested, policy-bounded contract visible. Do not claim URL,
   website, browser, or arbitrary authentication support. Prompt 28 confirmed the existing
   configured JSON HTTP API contract; generic browser automation remains deferred.
3. **Engineering discipline:** the v1.0 test/ticket/gate/report rules and v1.1 contracts
   remain in force. Prompt 22's matrix is reused; do not replay completed adapters, runners,
   evidence storage, progress/recovery, candidates, or experiment work.
4. **No standalone v3 file:** the repository contains no separate v3 pack. v4 §11 calls
   itself normative and supplies the capability preservation rows; comparison beyond those
   rows is unverified until the original v3 source is available.

## Dependency-ordered delta and acceptance scope

Prompt 24 was documentation-only. Prompts 25 and 26 implemented the bounded repository
profile and evidence-aware opportunity/dataset inventory; Prompt 27 completed the local
conversational loop, and Prompt 28 audited the existing HTTP API contract. The remaining
acceptance sequence is:

1. **25-T1 → 25-T2 (COMPLETE; report 25).** The supported-format/budget policy,
   approved-root and symlink boundaries, secret exclusions, evidence-backed profile, and
   representative/negative fixtures are implemented. Exact executed tests and gates are in
   `reports/25.md`; 29 passed. The scanner never executes project code and emitted source
   claims carry evidence or remain unknown/inferred with limitations.
2. **26-T1 → 26-T2 → 26-T3 (COMPLETE; report 26).** `aibench plan opportunities` maps
   objective vocabulary through current catalog eligibility and outputs evidence/gaps without
   writing a plan or invoking the app. Repository inventory validates only bounded JSONL
   shape and field counts; tests/evaluators/invocations stay path-only; references are never
   returned; generated unreviewed data stays outside compatible selection. Chat reuses a sole
   compatible content identity only when the current policy approves repository inspection;
   materially different candidates require an explicit choice. Gates 26-G1..G3 passed on the
   named fixture and regression set in `reports/26.md`.
3. **27-T1 → 27-T2 → 27-T3 → 27-T4 (COMPLETE; report 27).** The conversation exposes the
   same profile and opportunity services, starts a clear validated request without repeating
   authorization, and preserves policy denials and case-scope checks. It supports live
   progress, stored-evidence diagnosis, report provenance, exact next-experiment guidance,
   session resume and stored-output rescore without app calls. The complete deterministic
   fixture and clean-installed E2E-01/07 evidence are in `reports/27.md` and
   `evidence/27/`. Scripted-provider evidence does not prove live-model quality; no live model
   or human usability trial was run.
4. **28-T1 COMPLETE (report 28).** The user started the optional phase. The existing typed
   `HttpRunner`, endpoint/effect policy, shared engine quotas and budgets, and configured local
   HTTP fixture already satisfy the narrow JSON API contract. Prompt 28 ran the relevant
   boundary/regression set (78 passed, 2 skipped) and reused the clean-installed E2E-01
   journey; no second runner or onboarding path was added. Browser and generic website
   automation remain deferred.
5. **29-T1 (acceptance/value review) depends on declared scope above.** Reconcile all v4
   requirements and tickets; rerun deterministic local E2E and relevant unit tests; distinguish
   local fixtures, installed real packages, live provider checks, human review and pilots.
   Exact suite: `scripts/e2e_suite.py` / `tests/test_e2e_cli_journey.py`, plus the exact test
   sets named above. External checks remain separate and require access/authorization.

### Duplicate, unnecessary, and already-satisfied work

- Do not recreate core typed runners, HTTP transport, policy, session persistence, progress,
  resume/recovery, evidence artifact integrity, stored-output rescoring, reports, comparison,
  DeepEval/Ragas adapters, candidate review/promotion, or controlled experiment services.
  These already have implementation and prior executed test evidence.
- Do not add arbitrary website/browser automation to repository-aware MVP; v4 explicitly
  stages it later.
- Repository source discovery is not a second implementation of Prompt 16's trace import or
  configured application profile. Reuse `inspection/source.py` and add only missing bounded
  repo-profile behavior.
- Do not duplicate candidate-generation/review as generic dataset auto-import. Discovery
  produces references to candidates; promotion remains explicit.
- Do not repeat adapter tickets 05/14/17; current evidence supports local/real-package
  boundaries, while live services remain unvalidated.

## Scope verdicts

- **Declared current release scope:** Prompt 22 (accepted as the intended Prompt 23 audit)
  reports local Windows technical evidence for the prior declared scope. Its historical
  result is not a fresh certification of the dirty worktree. Repository-aware v4 additions
  are not ready for local-pilot acceptance yet.
- **Full v3-derived vision:** incomplete. The v4 map includes deferred HTTP API onboarding,
  generic website/browser support, hybrid mode transition, richer repository discovery,
  conversational evaluation reasoning, and real-agent/external validation. The full v3
  source was not available to verify whether this list is exhaustive.
## Prompt 29 acceptance reconciliation (2026-09-25)

The full clean-install result and twelve-journey table are in docs/engineering/reports/29.md.
The repository-defined E2E-01–07 suite passed 31 tests from the installed wheel. Fresh focused
runs passed the Prompt 25 inspector set (29), the Prompt 26–27 discovery/conversation set
(23; one host-dependent symlink skip), and the pinned DeepEval/Ragas package suites (18 and 9).
Their JUnit artifacts are under docs/engineering/evidence/29/.

The Prompt 29 requirement for a single fresh repository-aware conversation is **partial**:
static source inspection and evidence-backed profiles are implemented and independently
verified through inspect --source, while a fresh conversation does not receive those source
findings through read_profile. Chat can discover a policy-approved dataset independently;
the clean E2E uses an explicitly configured dataset. This does not prove the full
inspect → conversation → plan → run → report path. Keep V4-03/V4-04's bounded CLI/service
implementation status, but do not claim conversational source inspection is implemented.
The gap is numbered 30-T1 in docs/engineering/tickets.md, with gates and exact planned test
nodes. Repository-aware local-pilot readiness remains not ready until 30-T1 passes.

Prompt 28 was explicitly started and is in the declared scope; the local configured HTTP API
journey passes in the same E2E-01 flow. Generic browser/website automation remains deferred
(V4-17). V4-18 hybrid HTTP-to-trace-to-repository history continuity remains missing and
outside the declared local-pilot scope. No standalone v3 source exists, so the full v3-derived
vision remains unverified and not ready. Prompt 29 records all live-provider and human checks
as unrun; no requirement is implicitly promoted to implemented by historical phase status.

### Supplied v3 source follow-up

After the Prompt 29 report was written, the user supplied
`AI-Bench-Product-Aligned-Codex-Prompt-Pack-v3.md`. It has now been read end to end. Its
filename says v3, while its internal title says “Prompt Pack v2”; the direct review treats
the supplied file as the v3 source. The earlier “no standalone v3 source” statements record
the audit-time inventory and are superseded for current planning by
`docs/engineering/product-alignment-v3-review.md`. That review does not change the Prompt 29
execution results or current release scope: 30-T1 remains open; v4 continues to defer generic
website/browser onboarding and hybrid continuity; broader conversational experiments and
no-repository onboarding need explicit scope before implementation.

## Prompt 30/31 final reconciliation (2026-09-25)

The repository-aware acceptance gap and the additional v3-derived local-scope gaps are now
implemented and tested. This addendum supersedes the historical open-gap statements above;
the original Prompt 24/29 snapshots remain preserved in their reports.

| Requirement ID | Final current evidence and status | Final next action |
|---|---|---|
| V4-03 / V4-04 | **Implemented for the supported inspection contract.** Planning supplies an approved session root to `CodebaseInspector`; the shared application profile carries source evidence into a fresh chat. `tests/test_e2e_repository_conversation.py::test_fresh_repository_inspection_runs_and_reports_with_evidence` passes from the clean-installed wheel as E2E-08. Unsupported code remains unknown. | No duplicate inspector/profile ticket. Add a parser only against a named fixture and explicit file/budget contract. |
| V4-15 | **Partial.** Candidate discovery, review boundaries, local JSONL setup and the bounded HTTP wizard are implemented. General production-history sourcing and choosing among provider history stores are not present. | Defer history import until a source, consent boundary and provenance contract are selected. |
| V4-16 / V4-17 | V4-16 is **implemented narrowly**: `aibench connect http` creates a typed configured JSON POST runner, exact-origin policy, secret reference and call ceiling without contacting the endpoint. The runner uses shared budgets/evidence. V4-17 remains **deferred** for generic URL, arbitrary website and browser automation. | No generic web runner ticket in current scope. |
| V4-18 | **Implemented for the explicitly imported local trace contract.** A configured HTTP app run, imported OpenTelemetry trace summary, and approved repository findings appear under one session/run identity; one local request is made and no raw trace file enters the assistant briefing. Auto-instrumentation and arbitrary hosted trace connectors are outside the declared contract. | No duplicate trace service. Keep other trace sources/instrumentation out of this release unless separately specified. |
| V4-20 | **Implemented for bounded app-exposed parameter experiments.** Conversation starts the existing frozen experiment service, exposes stored progress, resumes a stored RUNNING record and gates protected holdout evaluation behind separate explicit wording. | No code repair, prompt editing, production adoption or unbounded model search. |
| V4-22 | **Reconciled.** v4 §3 governs clear-run authorization; the v3 “approve/run?” wording cannot add a duplicate confirmation. The old ticket/test/gate/report discipline remains active. The broad v3 black-box MVP claim is deferred except for capabilities that pass the narrow safe HTTP contract. | No additional ticket. |
| V4-24 | **Implemented and audited.** Tickets 30/31, the matrix, phase ledger, reports and saved JUnit/clean-install manifests are reconciled. The final deterministic suite passed 32 tests over E2E-01..08. | Continue to use exact executed evidence and keep external validation separate. |

Final Prompt 29 status: all twelve required journeys pass for the declared deterministic local
scope. That does not promote V4-05's semantic test-suite classification, V4-02's bounded
objective vocabulary, unsupported parser/metric patterns, or the product-value hypotheses to
implemented facts. The complete gate and scope discussion is in `docs/engineering/reports/31.md`.

The user-provided v3-named file has been reviewed end to end; its internal title says Prompt
Pack v2. `product-alignment-v3-review.md` records the full reconciliation. v3 capabilities
outside this staged local scope remain deferred or unvalidated rather than silently marked
implemented.
