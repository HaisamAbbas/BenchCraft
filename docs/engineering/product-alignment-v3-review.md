# Product alignment review — v3 prompt pack

Date: 2026-09-25  
Source: `AI-Bench-Product-Aligned-Codex-Prompt-Pack-v3.md` (the filename says v3;
the document title says “Prompt Pack v2”). The review treats the supplied file as the
requested v3 source and follows its actual contents and prompt sequence.

## Scope and evidence limits

This is a repository comparison, not a new test run. Executed evidence is reused from
reports 25–29 and their saved JUnit/E2E artifacts. Prompt 29's statement that the v3 source
was unavailable describes the evidence available when that report was written. The source
was subsequently supplied and reviewed end to end here.

The current declared local scope is narrower than the v3 vision: bounded repository
inspection, configured runners, policy-checked evaluation, persistent conversations and
reports, plus a configured JSON HTTP API against a local fixture. The HTTP runner's existence
does not mean self-service URL/API discovery or generic website automation exists.

## Prompt-by-prompt map

| v3 prompt | Current implementation and executed evidence | Assessment against v3 |
|---|---|---|
| 00 — Product contract and scaffold | Installable `aibench` CLI, package/core boundary, SQLite persistence and engineering ledgers are established by the existing project and prompts 00–22. Core dependency-boundary tests are included in the Prompt 25 focused batch (29 passed). | **Implemented as foundation.** No duplicate scaffold work is needed. |
| 01 — Codebase intelligence | `src/aibench/inspection/source.py`, `profile.py`, and `cli/inspect.py` provide bounded static inspection, provenance, supported-format/budget reporting and unknown findings. Prompt 25 fixture run: 29 passed, including path/line evidence, secrets, roots/symlinks, size/depth limits, injection-like source text and no project execution. | **Partial.** The inspector/profile exists, but `planning/service.py` calls `inspect_application` without `source_tree`; a fresh conversation does not receive repository findings. Prompt 29 found this missing in journey 1; ticket 30-T1 covers it. Discovery also deliberately supports a bounded format/pattern set, not every language or framework. |
| 02 — Application profile and opportunity discovery | `src/aibench/inspection/profile.py`, `src/aibench/planning/catalog.py`, `opportunities.py`, and `cli/plan.py` produce evidence-aware profiles/opportunities. Prompt 26 focused run: 114 passed, 2 skipped; Prompt 27 exercised profile/opportunity readouts in conversation. | **Partial.** Services exist, but source findings are absent from a fresh chat until 30-T1. Metric mapping is bounded by the current catalog and objective vocabulary; unavailable evidence stays unavailable. Broad semantic coverage for every RAG, agent and multi-turn metric is not established. |
| 03 — Dataset and evaluation contract | Typed cases/plans/observations/results, application-input versus judge-only separation, JSONL discovery, candidate-only generated data and explicit review/promotion exist in core and dataset services. Prompt 26 tests exercised dataset shape, isolation and sole-compatible-dataset reuse; Prompt 29 E2E-06 checked reference isolation. | **Implemented for the local contract; partial for source breadth.** Existing repository/configured datasets and candidate promotion are supported. Nontechnical upload/history selection and historical-conversation ingestion are not a complete user-facing workflow. Do not treat tests or generated candidates as goldens automatically. |
| 04 — Application runners | Typed local process and configured HTTP runners, bindings, policy, secrets, effects, timeouts, cancellation and budgets are in `src/aibench/runners/`, `src/aibench/security/` and `src/aibench/engine/`. Prompt 28: 78 passed, 2 documented skips; clean-installed E2E-01 exercised the configured local HTTP fixture. | **Partial against the discovery/onboarding promise; implemented for configured execution.** Repository invocation findings are not available in fresh chat (30-T1). The runner does not discover arbitrary URLs or silently choose among invocation paths. Broad URL/browser behavior is deferred by v4. |
| 05 — Evaluator abstraction and DeepEval | Core evaluator contracts and registry isolate framework types; DeepEval-specific code stays in its plugin. Prompt 29 real-package run: DeepEval 4.2.5, 18 passed; live-provider smoke was deselected. | **Implemented for tested local/real-package behavior.** No duplicate adapter work. Live provider compatibility is not established by these tests. |
| 06 — Evaluation agent and planning loop | The conversation layer uses bounded model turns and validated typed plan patches; policy and plan validation remain outside model authority. Prompts 26–27 tests cover material clarification, plan-only behavior, clear bounded execution and denial. | **Partial.** The planner and safe conversation loop exist; objective-to-metric coverage is limited by the current catalog, and the agent lacks a repository-grounded profile in a fresh chat until 30-T1. Real-model quality was not tested. |
| 07 — Conversational session | Persistent sessions, revision, validated actions, questions during execution, progress, stored-evidence failure discussion, resume and rescore exist in `src/aibench/conversation/` and `src/aibench/sessions/`. Prompt 27 deterministic batch passed 125 tests; Prompt 29 clean-install suite passed 31 across E2E-01–07. | **Partial.** The main session lifecycle is implemented, but the v3 codebase-aware conversation start is incomplete (30-T1). Tests use scripted providers, not a real model or human usability trial. |
| 08 — Interactive terminal | Bare `aibench`, chat/resume flows, deterministic slash commands and the TUI exist in `src/aibench/cli/chat.py` and `src/aibench/tui/`. Prompt 27 CLI/TUI tests and clean-installed E2E-01/07 passed. | **Implemented for the tested local interface.** The natural-language quality claim remains limited by scripted-provider evidence. |
| 09 — Evidence analysis and failure investigation | Typed report facts and stored case queries are implemented in `src/aibench/services/reports.py`, `src/aibench/reporting/` and the conversation controller. Prompt 27/29 tests verify stored evidence, quantitative facts and observation-versus-hypothesis labeling. | **Implemented for the deterministic local contract.** No new analysis service is needed. |
| 10 — Experiments and comparisons | Comparison and controlled-experiment services exist in `src/aibench/services/comparison.py` and `src/aibench/experiments/`; chat can compare runs and read experiment reports. Existing experiment tests and Prompt 27 rescore/comparison tests exercise these services. | **Partial.** Chat can compare stored runs, but its experiment tools are read/report/propose-adoption tools; it does not start and steer the v3 examples of parameter/model/prompt experiments conversationally. Headless experiment support must not be mistaken for that conversational capability. |
| 11 — Second evaluator adapter | Ragas plugin is separate from core. Prompt 29 real-package run: Ragas 0.4.3, 9 passed; no live provider selected. | **Implemented as a second adapter.** Its tested metric surface is narrow, and scores with differing semantics must not be presented as interchangeable. |
| 12 — MVP acceptance | Prompt 29 clean-install E2E passed 31 tests, and 11 of its 12 mapped acceptance journeys passed. The configured HTTP journey, evidence, progress, failures, rescore, policy denial and adapter checks have executed evidence. | **Partial.** The fresh repository-aware journey is not complete until 30-T1 passes. The v3 acceptance language also presumes a broader no-repository onboarding flow than the current configured HTTP contract; v4 deliberately narrows that release claim. |

## Cross-cutting v3 vision gaps

### Needed to accept the repository-aware local loop

1. **Fresh-chat source inspection (30-T1).** Pass bounded, approved-root findings into the
   shared profile used by planning and conversation. Show path/line and provenance, preserve
   unknown states, prevent source-content/secret leakage, then prove inspect → plan → run →
   evidence → report with resume/rescore and no duplicate application calls. This is the
   specific Prompt 29 acceptance blocker.
2. **Broader conversational experiment steering.** Run comparison and rescore exist. The
   remaining v3 Prompt 10 delta is a conversational, typed way to define and launch a safe
   parameter/model/prompt experiment using the existing frozen experiment service. If this is
   in the release vision, plan it after 30-T1; do not rebuild the experiment engine.

### Needed only for the broader nontechnical/no-repository vision

3. **Self-service black-box setup.** Current HTTP evaluation requires an explicit configured
   request/response contract, bindings, endpoint policy and secret references. Missing from
   v3's illustrated workflow are guided endpoint/API setup and discovery, user-facing dataset
   import/history selection, and a clear candidate-generation/review flow. Generic website and
   browser automation remain explicitly deferred under v4; do not imply that entering a URL is
   sufficient today.
4. **Hybrid continuity.** No demonstrated journey enriches a black-box session with structured
   traces and then repository evidence while preserving the session/run lineage. V4-18 records
   this as missing and outside the current local-pilot scope. It needs a separately specified
   phase after supported black-box and trace inputs are chosen.
5. **Coverage for additional codebases and metrics.** The inspector and opportunity catalog
   intentionally return unknown/unavailable outside supported patterns or required evidence.
   Add a language, framework or metric only when a target fixture and exact evidence contract
   justify it; v3 does not justify claiming universal discovery.

### Evidence that is not implementation acceptance

The deterministic conversation/provider fixtures establish service semantics, not live-model
quality. No live model/provider or human usability/pilot study was run in Prompt 29. Market
adoption, nontechnical setup success, and product differentiation remain unvalidated. No API
credential was needed for this review or for deterministic local acceptance; live-provider
verification would require the corresponding configured credentials and authorization.

## Recommended order

1. Finish 30-T1 and rerun the Prompt 29 local acceptance map.
2. Decide whether the declared product release includes conversational parameterized
   experiments; if yes, ticket that delta against the existing experiment service.
3. Decide whether no-repository black-box onboarding is a near-term product requirement. If
   yes, define a bounded setup/input contract before extending the HTTP runner.
4. Specify hybrid trace/repository continuity only after the supported trace source and
   identity-preservation behavior are chosen.
5. Run live-model and human workflow validation separately from deterministic acceptance.

Already-satisfied foundations to reuse include typed plans, policy-checked configured runners,
evidence integrity, judge-only isolation, candidate review/promotion, session progress/recovery,
stored-evidence analysis, reports, comparison/rescore, and DeepEval/Ragas adapter boundaries.
Do not reimplement these to close the gaps above.

## Implementation follow-up and final status (2026-09-25)

Prompts 30/31 closed the previously identified local-scope gaps. The v3-named input was read
end to end; its filename says v3 and its document title says Prompt Pack v2. Prompt 29's
“source unavailable” sentence is historical and is superseded by this full source review.

| v3 prompt | Final status against implemented scope | Executed evidence / residual boundary |
|---|---|---|
| 00 — Product contract and scaffold | Implemented foundation | Existing package/ledger contract remains in force; no duplicate scaffold. |
| 01 — Codebase intelligence | Implemented for bounded supported files, path/line provenance, secret exclusion, budgets and no execution | Prompt 25 tests plus clean-installed E2E-08 fresh-chat journey; unsupported language/parser patterns remain unknown. |
| 02 — Application profile and opportunity discovery | Implemented for evidence-backed declared/inferred/unknown profile fields and current objective catalog | Prompt 26/27 suites and E2E-08. Metric coverage remains bounded by required observable data; no metric is inferred from judge-only references. |
| 03 — Dataset and evaluation contract | Partial | JSONL validation, candidate provenance/review, protected datasets and isolation exist. General historical-log discovery/import and universal dataset semantics remain unsupported. |
| 04 — Application runners | Implemented for configured CLI/HTTP contracts under typed plans and policy | The new no-repository wizard writes a specific HTTP contract and makes zero setup requests. No arbitrary executable, URL or browser runner is implied. |
| 05 — DeepEval adapter | Implemented as a pinned isolated adapter | DeepEval 4.2.5 real-package tests: 18 passed, live smoke deselected in Prompt 29 evidence. No live judge provider was called. |
| 06 — Evaluation agent and planning loop | Implemented for the local deterministic service contract; capability breadth is partial | Clear request, bounded plan, grounded opportunities, policy denial and unknown states pass local tests. Scripted assistant evidence does not validate live-model quality. |
| 07 — Conversational session | Implemented for persistent sessions, clear actions, progress, recovery, reports and rescore | Clean-installed E2E-01..08 and focused tests. The run identity survives reopen/rescore without repeating app calls. |
| 08 — Interactive terminal | Implemented for existing CLI/TUI commands and shared session services | E2E-07 in final clean-installed wheel; no separate session-only semantic implementation. |
| 09 — Evidence analysis and failure investigation | Implemented on stored cases with observations separated from hypotheses | Prompt 27 tests plus E2E-01 and stored-report path; claims do not recalculate from untrusted app text. |
| 10 — Experiments and comparisons | Implemented for finite app-exposed parameter experiments; partial against broader arbitrary prompt/model optimization | Prompt 31 conversational start/progress/resume and explicit holdout tests; existing frozen service and adoption proposal reused. No code repair or automatic adoption. |
| 11 — Second evaluator adapter | Implemented as the isolated Ragas adapter with narrow semantics | Ragas 0.4.3 real-package tests: 9 passed in Prompt 29 evidence. No live judge provider was called. |
| 12 — MVP acceptance | Complete for the declared deterministic local scope; not market or pilot acceptance | Final clean-install suite: 32 passed, 0 skipped, E2E-01..08; all twelve Prompt 29 journeys pass. Human, live-model and pilot validation remain unrun. |

The previously missing hybrid progression is now verified as an explicit local workflow by
31-T2: a configured HTTP application, imported OpenTelemetry trace and approved repository
profile share one run/session identity. Automatic instrumentation and additional hosted trace
sources are not implied.

### Remaining v3-derived gaps and deferred claims

- Generic URL/website/browser automation and v3's broad black-box MVP statement remain
  deferred. Only a configured JSON HTTP API with exact endpoint policy, bindings, secret
  references, declared effects and bounded calls is supported.
- Historical production-data import is not a universal capability. The app can validate a
  user-selected local JSONL file and uses existing explicit connector contracts; it does not
  silently select among hosted history stores.
- Source support is narrow by design. Unsupported languages/frameworks stay unknown until a
  named fixture, parser, evidence contract and budget are added.
- Live provider quality, human usability, installed real-agent trials, willingness to pay,
  time saved and product-market fit have no evidence from deterministic fixtures. No API key
  was needed for the local acceptance run. A live model smoke would require the user's
  configured provider secret and an explicit cost budget.

### Acceptance and value review

The final local scope is ready for technical review, not a real-user pilot or product-market
fit conclusion. Primary product documentation shows that adjacent systems already offer
dataset-backed offline evaluations, experiment comparison and production trace workflows:
[LangSmith evaluation types](https://docs.langchain.com/langsmith/evaluation-types),
[Braintrust evaluation](https://www.braintrust.dev/docs/evaluate),
[Braintrust datasets](https://www.braintrust.dev/docs/annotate/datasets), and
[Arize Phoenix quickstart](https://arize.com/docs/phoenix/get-started). These sources verify
those vendors' documented capabilities; they do not establish a feature-by-feature competitive
benchmark. Potential differentiation to test is a local-first path that connects bounded
repository evidence, policy-controlled execution and durable run evidence in one conversation.
No claim is made that this workflow is unique. The hypothesis that it materially helps
nontechnical users requires observed usability sessions and pilot evidence.

The recommended delta order was: 30-T1 (repository profile into chat), 31-T1 (bounded setup),
31-T2 (same-run hybrid evidence), 31-T3 (conversational experiment controls; parallelizable
after the existing Prompt 19/27 foundations), then 31-T4 (final acceptance). All are complete
for the declared local scope. The reports and ledgers record exact tests, dependencies and
remaining deferred claims.
