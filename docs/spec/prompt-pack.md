# AI-Bench — Sequential Codex Implementation Prompt Pack

**Pack version:** 1.0 · **Target specification:** implementation plan v1.1, 21 September 2026  
**Specification file:** `AI-Application-Evaluation-Harness-Implementation-Plan.md`  
**Source SHA-256:** `2bbf2119408b2f5f6bdecfb60b97bd4eef5553d8fc7cb8a8b4f4a564f4865d19`  
**Purpose:** Copy-ready implementation prompts, engineering tickets, deliverables, definitions of done, and auditable phase reports. This pack contains instructions; no implementation or tests have been performed by creating it.

## How to use

1. Give Codex the latest specification Markdown and this prompt pack in the intended repository/workspace.
2. Paste **Prompt 00**, including its instructions and ticket/gate lists. Let Codex finish and review its report.
3. Paste the next numbered prompt only after required predecessor gates are satisfied. Use the repair prompt below for failed prerequisites.
4. In a new Codex session, provide the repository and both documents again. The persisted engineering contract and phase ledger restore context; do not rely on chat memory.
5. Prompts **00–13 build the conversational MVP**. Prompts **14–17 cover optional Phase 2**. Prompts **18–21 cover optional Phase 3**. These are implementation steps within the product phases, not competing phase numbering.
6. Optional prompts authorize their named scope when you send them. They do not authorize publishing, production effects, cloud spend, or contacting people.

The target experience is: open `aibench`, discuss a benchmark, refine the plan, execute, ask questions during the run, inspect failures and continue in the same session. Deterministic execution and scriptable CI commands support that experience.

## Phase map

| Product phase | Prompt | Deliverable | Dependencies |
|---|---|---|---|
| MVP | 00 — Bootstrap and specification traceability | Installable project scaffold, engineering contract, phase ledger, requirements matrix | None |
| MVP | 01 — Canonical models, configuration, and datasets | Versioned schemas and streaming JSONL ingestion | 00 |
| MVP | 02 — Durable run storage and artifacts | SQLite metadata, immutable artifacts, migrations and stable run identities | 01 |
| MVP | 03 — Application runners and observation capture | Working CLI and HTTP runners with isolated input bindings | 02 |
| MVP | 04 — Evaluator contracts, native checks, and registry | Framework-independent evaluator pipeline and stored-output scoring | 03 |
| MVP | 05 — DeepEval adapter | Pinned optional DeepEval faithfulness integration | 04 |
| MVP | 06 — Deterministic scheduling, policy, budgets, and recovery | Executable manual-plan engine with conservative resume | 05 |
| MVP | 07 — Evaluation planning and bounded LLM reasoning | Validatable draft plans, deterministic fallback, basic model-backed planner | 06 |
| MVP | 08 — Persistent two-way conversation and typed actions | Benchmark session controller supporting clarify → revise → act → discuss | 07 |
| MVP | 09 — Interactive terminal and live controls | Default conversational CLI with responsive input and direct controls | 08 |
| MVP | 10 — Conversation recovery and adversarial interaction | Reliable session resumption and interruption behavior | 09 |
| MVP | 11 — Evidence reports, command composition, and packaging | JSON/Markdown/HTML reports and complete guided workflow | 10 |
| MVP | 12 — MVP acceptance and harness validation | Evidence-backed MVP acceptance report and planner baseline measurements | 11 |
| MVP | 13 — Release candidate and pilot handoff | Local release candidate, compatibility matrix and pilot package | 12 |
| Phase 2 | 14 — Second evaluator ecosystem and comparisons | Independent adapter plus compatible paired comparisons | 13 technical gates; explicitly requested extension |
| Phase 2 | 15 — Richer runners and agent outcome contracts | Python/container/API runners and isolated agent test worlds | 14 |
| Phase 2 | 16 — Inspection, traces, caching, and parallel execution | Evidence-backed inspection and observable parallel execution | 15 |
| Phase 2 | 17 — OpenAI evaluation bridges and one platform connector | Distinct OSS/API adapters and one demand-selected connector | 16 |
| Phase 3 | 18 — Reviewed dataset generation and advanced episodes | Candidate-data workflow and one advanced evaluation modality | 17 or explicit completed prerequisite subset recorded in ledger |
| Phase 3 | 19 — Controlled optimization experiments | Optional experiment module with protected holdout and explicit parameter space | 18; comparison gates from 14 |
| Phase 3 | 20 — Distributed execution after measured need | PostgreSQL coordination, object artifacts and restart-safe distributed workers | 19 plus measured bottleneck evidence |
| Phase 3 | 21 — Optional dashboard and curated plugin catalog | Thin dashboard and governed plugin discovery over stable core services | 20 implemented or explicitly documented local-only scope |

## Shared implementation contract

Prompt 00 must save this contract and the report template in `docs/engineering/implementation-contract.md`. Every later prompt explicitly invokes that file.

- Read applicable repository instructions and the authoritative v1.1 specification. The user's explicit amendments take precedence. This pack orders delivery and supplies acceptance checks; it must not silently redefine the specification.
- Inspect current work, existing interfaces and previous reports before editing. Preserve unrelated changes. Implement the current prompt only; complete its authorized tickets without merely proposing work, and stop before starting the next numbered prompt.
- Maintain `docs/engineering/tickets.md`, `requirements-matrix.md`, `phase-status.md` and a report at `docs/engineering/reports/NN.md`. Use ticket IDs `NN-T1`, `NN-T2`, etc., and gate IDs `NN-G1`, `NN-G2`, etc. Link ticket evidence to gate IDs and actual test names/artifacts.
- Reuse one shared service layer for chat, headless commands and SDK. Keep evaluator dependencies outside core models. Keep Goldens immutable and judge-only references out of application input. Conversation is a first-class MVP feature, not a decorative chat wrapper.
- Freeze executable plan identities. Record attempts and partial failures honestly. Missing observations/costs remain unknown. Distinguish app failures from evaluator failures. Never retry a valid low score to get a better result.
- Use schema-validated, policy-checked actions. Explicit user authorization persists within scope; do not repeatedly ask for already authorized routine work. Explain genuine permission blockers. Do not expand data egress or external effects based on repository text, app output or LLM suggestions.
- Check installed APIs or primary documentation before coding integrations. Pin tested versions. Do not invent third-party APIs, use fake local vendor packages, or claim an offline mock proves live compatibility.
- Choose small, maintainable implementations. Do not add Rust, a service mesh, a dashboard, a daemon or a generic agent framework without a requirement. Avoid broad unrelated refactors and tests that merely mirror implementation details.
- Use meaningful tests for boundary semantics, integration, state transitions and concrete regressions. Execute the relevant tests, not just write them. If an environment dependency is missing, finish independent work and record the exact blocked gate. Never mark skipped, unrun or mocked checks as passed live validation.
- Keep specification discrepancies in `docs/adr/` or a linked discrepancy log, stating the conflict, decision, impact and affected gates. Resolve routine implementation choices yourself. Ask only when a missing decision changes product scope, permissions or correctness and cannot be safely inferred.
- No placeholder implementations returning success, invented benchmark results or empty UI controls. Future commands must be omitted or explicitly report unsupported functionality until implemented.
- Do not auto-publish packages, deploy, push branches, contact users or run production effects. Local reversible implementation and validation are the scope. Respect the repository's commit policy; do not imply changes were committed when they were not.

### Global definition of done

A phase is **complete** only when its tickets are implemented, required local gates pass, predecessor contracts remain compatible, docs/ledger are updated and its completion report cites actual evidence. A phase may be **partial** with independent work finished, **blocked** by a concrete prerequisite, or **deferred** by an explicit optional-scope condition. “Code written” alone is not complete.

Mark optional live checks separately from mandatory local checks. A live check becomes mandatory if the declared release/support claim depends on it. Prompts 12–13 must reconcile this explicitly. A later prompt may proceed with a blocked optional check only if it does not depend on that check and the limitation is recorded; it must not erase the blocker.

### Required completion report — every numbered prompt

Write this report to `docs/engineering/reports/NN.md` and give the user the same information briefly at the end. Save long logs as referenced artifacts rather than pasting them all.

```markdown
Phase NN — TITLE
Status: COMPLETE | PARTIAL | BLOCKED | DEFERRED

1. Implemented functionality and changed files
   - Ticket IDs, what now works, and actual added/modified/deleted paths.

2. Tests/commands actually run and their results
   - Exact command, result/exit code, and concise evidence.
   - Distinguish fake-provider, real-package, local integration and live-service checks.
   - Unrun/skipped checks and reasons; do not describe them as passed.

3. Acceptance gates
   - Satisfied: gate IDs and evidence.
   - Pending: gate IDs and remaining work.
   - Blocked: gate IDs, concrete cause, and required unblock action.

4. Decisions or specification discrepancies recorded
   - ADR/log paths, decision and practical consequence; “None” when applicable.

5. Exact next command or numbered prompt
   - If complete: “Paste Prompt NN — TITLE” or the specified review checkpoint.
   - Otherwise: the exact repair command, missing input, or resume instruction.
```

### Standard check convention

Prompt 00 chooses and records runnable project commands rather than assuming a particular environment manager already exists. Suggested command shapes are `python -m pytest ...`, `python -m aibench --help`, and the selected formatter/type checker/build tool. These are suggestions until implemented and run. Later prompts must use the exact commands supported by the repository and print what was actually executed.

Never treat a command listed in this pack as evidence that it has already run.

## Numbered implementation prompts

For each phase, paste the complete block from “Prompt NN” through its suggested next step. The ticket list and gates are part of the prompt.

## Prompt 00 — Bootstrap and specification traceability

**Product phase:** MVP  
**Prerequisite:** None  
**Specification sections:** 1–5, 13, 17, 20–24  
**Deliverable:** Installable project scaffold, engineering contract, phase ledger, requirements matrix

Read both supplied documents completely. Establish the project and save the Shared implementation contract and Required completion report above in docs/engineering/implementation-contract.md. Treat the ticket list and gates below as authorized work.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **00-T1 — Establish source of truth.** Read the supplied v1.1 plan completely and applicable repository instructions. Preserve user work. Locate an existing project before creating files. Copy the supplied plan to docs/spec/implementation-plan.md with its content hash and source version. Do not substitute the older AI-Engineer-Bench documents.
- **00-T2 — Create development foundation.** Create pyproject.toml, src/aibench, a real CLI entry point, locked development dependencies, and a supported Python/platform matrix. Start with Python, Pydantic, Typer/Rich, and SQLite; defer optional framework dependencies. Provide aibench --help, not fake working commands.
- **00-T3 — Persist engineering controls.** Write docs/engineering/implementation-contract.md from this pack’s shared contract and report template; create docs/engineering/phase-status.md, requirements-matrix.md, and tickets.md with numbered gates. Record a small ADR for core/adapters dependency direction and the conversational-first interface.
- **00-T4 — Create repeatable developer checks.** Choose and document environment setup, lint/type checking, tests, package build, and smoke commands. Add a small bootstrap import/CLI test and CI for these actual checks; do not create empty tests to satisfy counts.

### Definition of done / acceptance gates

- **00-G1:** Fresh environment installs the project and displays CLI help.
- **00-G2:** Every MVP requirement maps to an owning numbered prompt and an observable acceptance gate; Phase 2/3 requirements are marked deferred.
- **00-G3:** No speculative service, dashboard, Rust component, or paid-provider requirement enters the core.
- **00-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/00.md` are updated with the required five-part report.

### Verification to perform

Packaging/import test and CLI help in a fresh environment; execute only configured lint/type checks. Record exact tool versions and commands.

### Scope and decisions

Use the table in this pack to seed tickets for later phases, but implement only bootstrap work. No empty production stubs claiming unsupported functionality.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 01 — Canonical models, configuration, and datasets.

---

## Prompt 01 — Canonical models, configuration, and datasets

**Product phase:** MVP  
**Prerequisite:** 00  
**Specification sections:** 2, 5–6, 12–13  
**Deliverable:** Versioned schemas and streaming JSONL ingestion

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **01-T1 — Model immutable inputs and typed outputs.** Implement dataset/case, application, observation, plan, execution, result, run and artifact identities. Separate application-visible input from judge-only references. Model score, execution status and decision separately. Export versioned JSON Schemas.
- **01-T2 — Implement dataset normalization.** Accept the plan’s shorthand examples, normalize references, validate namespaced extensions and field types, distinguish missing/empty/truncated evidence, and report line-specific errors. Detect duplicate IDs; generate stable documented IDs when omitted without silently deduplicating cases.
- **01-T3 — Implement config and identity rules.** Implement explicit precedence, safe parsing, relative path resolution, secret references, canonical hashes and resolved-config redaction. Dataset fields cannot override policy. Preserve provenance and split/review status.
- **01-T4 — Deliver dataset command and fixtures.** Implement aibench dataset validate PATH and small valid/invalid JSONL fixtures for chatbot, RAG, tool expectations, and unsupported coding requirements.

### Definition of done / acceptance gates

- **01-G1:** All four brief examples normalize or report precisely missing execution prerequisites.
- **01-G2:** Golden runtime mutation and accidental sending of references through the input projection are prevented.
- **01-G3:** Malformed inputs fail before any app/provider call; large inputs stream with bounded memory.
- **01-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/01.md` are updated with the required five-part report.

### Verification to perform

Schema round trips, normalization/error fixtures, duplicate identity tests, input-projection tests, and CLI dataset validation. Measure memory on a bounded generated fixture rather than claiming million-case scale.

### Scope and decisions

Do not decide business thresholds or treat legacy context as observed retrieval. Define contracts needed later without implementing external integrations.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 02 — Durable run storage and artifacts.

---

## Prompt 02 — Durable run storage and artifacts

**Product phase:** MVP  
**Prerequisite:** 01  
**Specification sections:** 5, 14–15  
**Deliverable:** SQLite metadata, immutable artifacts, migrations and stable run identities

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **02-T1 — Implement persistence repositories.** Create migrations and repositories for datasets, cases, apps, profiles, plans, runs, work items, execution/evaluation attempts, results, artifacts, usage and approvals. Use transactions, foreign keys, unique logical task keys and a single writer strategy.
- **02-T2 — Implement artifact commit protocol.** Write bounded payloads to temporary files, flush/atomically rename, then commit references. Hash content and validate paths; track orphan cleanup without deleting referenced artifacts.
- **02-T3 — Record attempts and manifests.** Persist original run hashes, individual attempts and resource accounting. Preserve errors and partial outputs; null usage remains unknown. Expose runs list/show with machine-readable output.
- **02-T4 — Exercise recovery boundary.** Implement restart-safe loading and unique commit behavior. Reserve session storage for Prompt 08; do not confuse storing a run with scheduling one.

### Definition of done / acceptance gates

- **02-G1:** Restart preserves completed records and immutable artifacts.
- **02-G2:** Duplicate logical commits cannot overwrite accepted results.
- **02-G3:** No committed result references a partially written artifact; migration behavior is tested.
- **02-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/02.md` are updated with the required five-part report.

### Verification to perform

SQLite transaction/uniqueness tests, temporary-directory artifact crash simulations, migration tests, and runs list/show CLI checks.

### Scope and decisions

Use local SQLite only. Cross-run caches and distributed coordination are later work.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 03 — Application runners and observation capture.

---

## Prompt 03 — Application runners and observation capture

**Product phase:** MVP  
**Prerequisite:** 02  
**Specification sections:** 7, 15–16  
**Deliverable:** Working CLI and HTTP runners with isolated input bindings

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **03-T1 — Implement runner lifecycle.** Provide describe/prepare/healthcheck/invoke/reset/close contracts with cancellation, timeouts, declared effects and observation envelopes. Map only explicitly app-visible fields.
- **03-T2 — Build CLI transport.** Use argv with shell disabled, JSON stdin/stdout and bounded stderr; support explicit legacy text output mode. Enforce output limits and process-tree cleanup on supported platforms.
- **03-T3 — Build HTTP transport.** Implement request/response bindings, secret references, TLS verification, endpoint policy, size/time limits and correlation IDs. Disable or validate redirects so they cannot escape endpoint policy.
- **03-T4 — Provide real local fixtures.** Add a CLI chatbot, HTTP RAG fixture returning actual retrieved documents, black-box output-only fixture and effect-counting mock app. Persist executions via Prompt 02; expose a developer smoke path without pretending the full scheduler exists.

### Definition of done / acceptance gates

- **03-G1:** Both transports actually invoke local fixtures and record outputs, timing and errors.
- **03-G2:** A sentinel Golden reference never reaches captured app requests or child environment.
- **03-G3:** Timeouts clean up supported local processes; HTTP ambiguous effects are marked, not blindly retried.
- **03-G4:** Missing retrieval/tool/token/cost evidence is unknown, not fabricated.
- **03-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/03.md` are updated with the required five-part report.

### Verification to perform

Local subprocess integration tests, loopback HTTP server tests, timeout/output-limit tests, malicious input interpolation and reference-leakage checks.

### Scope and decisions

Trusted local mode is explicitly not a hostile-code sandbox. No arbitrary installation, source modification, or app effects outside selected fixtures.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 04 — Evaluator contracts, native checks, and registry.

---

## Prompt 04 — Evaluator contracts, native checks, and registry

**Product phase:** MVP  
**Prerequisite:** 03  
**Specification sections:** 9, 12, 14  
**Deliverable:** Framework-independent evaluator pipeline and stored-output scoring

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **04-T1 — Implement evaluator protocol.** Define manifests and describe/validate_binding/prepare/evaluate/close plus optional batch contracts; pass artifact, cancellation and accounting services through typed context.
- **04-T2 — Implement registry and controlled discovery.** Resolve versioned namespaced IDs, schema compatibility, applicability and required observations. Discover metadata without importing arbitrary third-party modules into the CLI; use controlled workers where plugin code must execute.
- **04-T3 — Add useful native/custom evaluators.** Implement exact match, JSON-schema validation and a domain-authored example. Separate legitimate low scores from evaluator errors and not-applicable results.
- **04-T4 — Normalize and aggregate.** Persist typed values, semantic identities, decisions, raw refs and resource completeness. Compute coverage/count denominators deterministically. Score recorded execution views through a shared service without invoking the app.

### Definition of done / acceptance gates

- **04-G1:** Two different native checks and one custom example yield valid canonical results.
- **04-G2:** Unsupported IDs/bindings fail before execution.
- **04-G3:** Rescoring recorded outputs leaves an app invocation counter unchanged.
- **04-G4:** No core model imports DeepEval, Hermes, OpenAI Evals or terminal UI packages.
- **04-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/04.md` are updated with the required five-part report.

### Verification to perform

Adapter conformance fixtures, missing-field applicability tests, deterministic aggregation tests, dependency-boundary checks and a stored-output rescore integration test.

### Scope and decisions

Do not flatten every result to float or average unrelated metrics. General third-party package installation is not part of discovery.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 05 — DeepEval adapter.

---

## Prompt 05 — DeepEval adapter

**Product phase:** MVP  
**Prerequisite:** 04  
**Specification sections:** 9–10, 12, 16  
**Deliverable:** Pinned optional DeepEval faithfulness integration

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **05-T1 — Inspect and pin the actual upstream API.** Check the installed package or official documentation for the selected version; record tested dependency and supported judge configuration. Keep dependencies isolated from the core.
- **05-T2 — Translate exact field semantics.** Bind case input, recorded actual output and observed retrieved context. Never fill missing retrieval from reference.context. Implement explicit missing/empty-context policies from the plan.
- **05-T3 — Bound metric execution.** Use independent mutable metric instances, controlled worker execution, timeouts and declared nested retries/concurrency. Preserve raw outputs/reasons and unknown accounting; avoid unconfigured cloud publishing.
- **05-T4 — Verify framework compatibility honestly.** Test the real installed DeepEval package using an injected deterministic judge where supported, plus an optional budgeted live-provider smoke. Preserve fixture-only coverage if dependencies or credentials are unavailable.

### Definition of done / acceptance gates

- **05-G1:** Real DeepEval test-case conversion matches the pinned API.
- **05-G2:** Missing context does not become an invented score; empty context follows the documented policy.
- **05-G3:** Concurrent tasks cannot share mutable metric state unsafely.
- **05-G4:** Framework-specific types remain inside the adapter.
- **05-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/05.md` are updated with the required five-part report.

### Verification to perform

Real-package adapter tests with a controlled judge, error/timeout/missing-context fixtures; live external judge only when credentials and budget are authorized. Label mock, real-package and live-provider checks separately.

### Scope and decisions

If dependencies cannot be obtained, finish offline work and mark the real-package gate blocked. Never create a fake local deepeval module to make tests pass.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 06 — Deterministic scheduling, policy, budgets, and recovery.

---

## Prompt 06 — Deterministic scheduling, policy, budgets, and recovery

**Product phase:** MVP  
**Prerequisite:** 05  
**Specification sections:** 4, 13–16  
**Deliverable:** Executable manual-plan engine with conservative resume

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **06-T1 — Compile and schedule validated work.** Implement structural validation for manual plans before dispatch, dependency ordering, bounded queues, application/evaluator concurrency caps, single writer commits and shared service actions. Rich planner validation follows in Prompt 07.
- **06-T2 — Implement policy and accounting.** Enforce approved targets, data scope, credentials and effects independently of the LLM. Reserve/reconcile resources and track app/evaluator/planner costs separately. State hard call/token limits versus soft monetary estimates.
- **06-T3 — Implement attempts and effect-aware retries.** Classify retryable failures, bound backoff and prevent SDK/adapter/engine retry multiplication. Record every attempt and its cost. Preserve unknown-effect states after ambiguous operations; never retry valid low scores.
- **06-T4 — Add run control and CLI.** Implement run, evaluate and resume using existing services; provide internal status/pause/resume/cancel operations and durable events for later chat. Preserve frozen manifests; checkpoint safely on interruption.

### Definition of done / acceptance gates

- **06-G1:** A manual plan runs a local fixture end to end and can rescore saved executions.
- **06-G2:** Invalid plans and denied actions dispatch zero app/judge calls.
- **06-G3:** Safe interrupted work resumes without duplicate logical commits; ambiguous effectful work does not auto-repeat.
- **06-G4:** Resource caps and cancellation behavior are tested, including in-flight limits.
- **06-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/06.md` are updated with the required five-part report.

### Verification to perform

End-to-end manual run, fault injection at dispatch/response/commit boundaries, duplicate attempt tests, retry-budget tests and interruption/resume commands.

### Scope and decisions

Engine work belongs here even though live terminal presentation arrives later. Do not promise exactly-once external effects or treat cached results as new latency samples.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 07 — Evaluation planning and bounded LLM reasoning.

---

## Prompt 07 — Evaluation planning and bounded LLM reasoning

**Product phase:** MVP  
**Prerequisite:** 06  
**Specification sections:** 3, 8–9, 17, 23  
**Deliverable:** Validatable draft plans, deterministic fallback, basic model-backed planner

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **07-T1 — Build evidence and requirement summaries.** Inspect declared config, dataset field coverage and installed metric manifests. Record observed/declared/inferred/unknown states. Do not claim repository architecture discovery.
- **07-T2 — Complete plan compiler/validator.** Validate metric eligibility, per-case field requirements, selectors, DAGs, sampling seeds, budgets, aggregation semantics and policy. Separate missing objective information from missing permission.
- **07-T3 — Implement bounded planning loop.** Use a narrow provider interface and schema-constrained drafts with bounded repairs/tool calls. Provide deterministic templates and manual-plan mode; inject a fake provider for offline tests and integrate one real configurable provider.
- **07-T4 — Deliver inspect/plan commands.** Write inspect profile, plan draft and validation results with objective coverage, observability gaps, estimated spend and pending clarification fields. Freeze executable revisions before scoring.

### Definition of done / acceptance gates

- **07-G1:** Unknown evaluator IDs and unavailable inputs are rejected outside the model.
- **07-G2:** Equivalent manual and generated plans produce identical deterministic measurements.
- **07-G3:** Known chatbot/RAG/agent and misleading/partial-evidence fixtures produce justified metrics or explicit gaps.
- **07-G4:** No planner tool provides unrestricted terminal access.
- **07-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/07.md` are updated with the required five-part report.

### Verification to perform

Planner fake-provider tests, invalid-plan fixtures, static-template baseline comparison, real-provider smoke only if available, and inspect/plan/plan validate commands.

### Scope and decisions

Focused clarification may initially be represented as structured pending questions. Conversational presentation is Prompt 08/09. Do not inspect hidden test labels to choose favorable gates.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 08 — Persistent two-way conversation and typed actions.

---

## Prompt 08 — Persistent two-way conversation and typed actions

**Product phase:** MVP  
**Prerequisite:** 07  
**Specification sections:** 2–5, 8, 14, 16  
**Deliverable:** Benchmark session controller supporting clarify → revise → act → discuss

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **08-T1 — Persist sessions and decisions.** Implement sessions, turns, pending questions, decision records and typed action requests. Link source turns to interpreted choices, plan revisions and run IDs.
- **08-T2 — Implement domain conversation loop.** Support explanations, materially useful questions, plan patches, action requests and result queries. Reuse prior answers; distinguish missing requirements from actions already authorized.
- **08-T3 — Connect real services.** Wire read_profile, summarize_dataset, describe_evaluator, plan patch/validation, start_run, status, pause/resume/cancel and case evidence tools to the same services as headless commands. No UI-only simulated successes.
- **08-T4 — Enforce action boundaries.** Use expected revisions and stable action IDs; reject stale model patches and duplicate actions. Run it targets a specific reviewed plan. Scope changes create a new draft; questions do not interrupt execution.

### Definition of done / acceptance gates

- **08-G1:** A scripted multi-turn dialogue changes a case sample, answers why a metric was selected, and starts a real fixture run.
- **08-G2:** A follow-up question during execution leaves the run running.
- **08-G3:** Repeated action delivery cannot start a duplicate run.
- **08-G4:** User corrections persist; stale answers cannot overwrite newer choices.
- **08-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/08.md` are updated with the required five-part report.

### Verification to perform

Scripted deterministic-provider dialogue tests, shared-service integration tests, action-id replay and stale-revision tests, policy-negative cases.

### Scope and decisions

This is the conversational product core, not a generic chat demo. Streaming terminal presentation is the next prompt; normal benchmarking features must already be callable.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 09 — Interactive terminal and live controls.

---

## Prompt 09 — Interactive terminal and live controls

**Product phase:** MVP  
**Prerequisite:** 08  
**Specification sections:** 3, 13, 15, 20  
**Deliverable:** Default conversational CLI with responsive input and direct controls

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **09-T1 — Build primary terminal entry.** Bare aibench opens chat in a TTY; chat --project PATH and chat --resume SESSION_ID work. Non-TTY bare invocation prints guidance. Add multiline/history/basic completion and test the chosen input library.
- **09-T2 — Render streams and evidence.** Display streamed replies, compact tool cards, project/session/run identity and coalesced committed progress. Keep input editable while engine workers execute; label partial metric snapshots.
- **09-T3 — Implement deterministic slash controls.** Wire /help /plan /run /status /pause /resume /stop /failures /case /budget /report /sessions /new /exit. At this phase /report may return the existing machine-readable summary; rich HTML comes in Prompt 11. Status and stop never wait for an LLM.
- **09-T4 — Define interaction interruption.** Ctrl+C interrupts an assistant response without silently cancelling a benchmark. /stop explicitly cancels the run. Graceful exit pauses dispatch and records in-flight outcomes; resuming chat never auto-runs work.

### Definition of done / acceptance gates

- **09-G1:** Default CLI supports an actual back-and-forth benchmark session using implemented services.
- **09-G2:** Typing, status and stop remain responsive during slow model or app calls.
- **09-G3:** Provider failure does not disable slash controls.
- **09-G4:** One active run per session is enforced and the display reflects real state.
- **09-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/09.md` are updated with the required five-part report.

### Verification to perform

PTY/input-loop integration tests where available, slow-worker/provider tests, slash-control tests, non-TTY JSON output checks and an actual terminal smoke with observed steps recorded.

### Scope and decisions

Do not build a full-screen UI or background daemon unless necessary for these gates. If the environment cannot test a real TTY, report that gate as unverified rather than substituting screenshots.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 10 — Conversation recovery and adversarial interaction.

---

## Prompt 10 — Conversation recovery and adversarial interaction

**Product phase:** MVP  
**Prerequisite:** 09  
**Specification sections:** 8, 13–16, 23  
**Deliverable:** Reliable session resumption and interruption behavior

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **10-T1 — Reconcile resumed state.** Restore persisted conversation plus authoritative current engine/plan state. Replay missed events by sequence without replaying actions. Distinguish active, paused, interrupted and unknown-effect work.
- **10-T2 — Handle long conversations safely.** Create bounded summaries referencing structured decisions/artifacts; preserve unanswered questions and user corrections. Do not promote summaries into authority over real run data.
- **10-T3 — Harden boundaries.** Reject stale pending answers after dataset changes, delayed model mutations and tool-output prompt injection. Redact secrets/terminal control content from history and rendering.
- **10-T4 — Test graceful and abrupt loss.** Exercise chat disconnect, model interruption, worker crash and process kill. Ensure conversation interruption and run cancellation remain separate. Document session deletion versus retained run artifacts.

### Definition of done / acceptance gates

- **10-G1:** Reopening a session reconstructs current state and never restarts execution without a new action.
- **10-G2:** Crash/retry cannot duplicate an already accepted start_run action.
- **10-G3:** A late model response cannot revert a newer plan revision.
- **10-G4:** Session summaries and imported tool text cannot expand execution permissions.
- **10-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/10.md` are updated with the required five-part report.

### Verification to perform

Restart/kill recovery scenarios, persisted-event replay tests, stale-turn races, injection/redaction fixtures and provider-outage control tests.

### Scope and decisions

Fix bugs in earlier layers when these tests expose them; record the affected gate and avoid unrelated refactoring.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 11 — Evidence reports, command composition, and packaging.

---

## Prompt 11 — Evidence reports, command composition, and packaging

**Product phase:** MVP  
**Prerequisite:** 10  
**Specification sections:** 3, 12–14, 17, 25  
**Deliverable:** JSON/Markdown/HTML reports and complete guided workflow

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **11-T1 — Implement trustworthy reports.** Render typed metric profiles, coverage denominators, app/evaluator failures, latency definitions, cost completeness, case evidence and provenance. Escape HTML and keep raw sensitive artifacts separate.
- **11-T2 — Connect conversational analysis.** Answer show failures, explain case and export report from stored facts. Associate every quantitative claim with an aggregate/case query; label hypotheses and partial snapshots.
- **11-T3 — Complete command composition.** Finish init, doctor, benchmark guided composition and documented CLI operations. Ensure command/chat parity for authorization and exit codes. Comparison stays explicitly deferred to Prompt 14 rather than a fake score-difference command.
- **11-T4 — Package a realistic quickstart.** Ship 10-case local examples, optional dependency installation instructions, support/limitations docs and a fresh-install smoke. Document credentials through secret references, not committed .env contents.

### Definition of done / acceptance gates

- **11-G1:** Reports can be regenerated without rerunning apps or judges.
- **11-G2:** All shown numbers match stored facts and denominators; missing accounting is visible.
- **11-G3:** A new user can follow the quickstart through conversational planning and a real fixture run.
- **11-G4:** MVP commands work or explicitly report unsupported future features.
- **11-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/11.md` are updated with the required five-part report.

### Verification to perform

Report snapshot/semantic tests, hostile HTML evidence tests, numeric reconciliation, clean-install quickstart and headless/interactive shared-service checks.

### Scope and decisions

No invented root-cause statistics or arbitrary overall AI quality score. A static HTML report is sufficient; a dashboard is deferred.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 12 — MVP acceptance and harness validation.

---

## Prompt 12 — MVP acceptance and harness validation

**Product phase:** MVP  
**Prerequisite:** 11  
**Specification sections:** 17, 23–24  
**Deliverable:** Evidence-backed MVP acceptance report and planner baseline measurements

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **12-T1 — Run full product acceptance.** Execute the plan’s 100-case fixtures including injected RAG failures, black-box missing evidence, interruption/resume and stored-output rescore. Exercise goal → clarification → revision → run → live question → pause/resume → failure discussion.
- **12-T2 — Measure the planner.** Create the versioned 30–50-fixture evaluation set across app families; encode required concepts, acceptable alternatives and forbidden choices. Compare against static templates. Record reviewer status; do not invent two human reviewers.
- **12-T3 — Validate judges and invariants.** Run deterministic calibration/contract cases and explicitly scoped live checks where available. Measure planner selection/validity/gap metrics, engine reliability and cost completeness with actual denominators.
- **12-T4 — Audit against the specification.** Update every requirement and phase gate with evidence. Fix concrete MVP defects and record deferred scope, unsupported platforms and live checks that remain blocked.

### Definition of done / acceptance gates

- **12-G1:** All executable MVP requirements have reproducible evidence or an explicit blocker; no blanket done label hides failures.
- **12-G2:** The sample workflow actually runs, and rescoring provably does not re-invoke the app.
- **12-G3:** Observed planner results are reported against proposed targets, not relabeled to pass.
- **12-G4:** Unreviewed fixtures and unavailable paid/platform checks remain visibly unverified.
- **12-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/12.md` are updated with the required five-part report.

### Verification to perform

Full configured deterministic suite, 100-case end-to-end run, planner benchmark command, selected fault tests and exact packaging/platform checks actually available.

### Scope and decisions

Do not treat user feedback, expert review or live deployment trials as completed by synthetic test execution. If targets fail, document and fix within scope or keep the release gate pending.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 13 — Release candidate and pilot handoff.

---

## Prompt 13 — Release candidate and pilot handoff

**Product phase:** MVP  
**Prerequisite:** 12  
**Specification sections:** 17–18, 22–24  
**Deliverable:** Local release candidate, compatibility matrix and pilot package

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **13-T1 — Resolve acceptance findings.** Review Prompt 12 results; fix release-blocking defects, rerun affected checks and update gate evidence. Do not waive critical requirements silently.
- **13-T2 — Prepare distribution artifacts.** Build versioned package artifacts, migration/recovery instructions, changelog, quickstart and a supported plugin/Python/platform matrix. Validate installation from built artifacts rather than only editable source.
- **13-T3 — Prepare pilot evidence.** Provide two pilot integration recipes and feedback forms measuring setup effort and value over direct evaluator use. Run local integration trials; separately mark real-team trials pending until actually observed.
- **13-T4 — Publish honest readiness decision.** Write a release-readiness report listing executable acceptance, external validation, residual risks and next actions. Produce local artifacts only; do not publish, deploy or contact pilot users without explicit authorization.

### Definition of done / acceptance gates

- **13-G1:** Built artifact installs in a clean environment and executes the documented local demo.
- **13-G2:** No critical open gate is hidden by the release summary.
- **13-G3:** Compatibility, limitations and external pilot status are documented accurately.
- **13-G4:** MVP work has a clear stopping point before optional expansion.
- **13-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/13.md` are updated with the required five-part report.

### Verification to perform

Build/install commands, clean demo, targeted regression checks and all configured required release checks; record which operating systems/providers were actually exercised.

### Scope and decisions

Mark release-ready only if the project’s mandatory gates are satisfied. Distinguish technical candidate readiness from real-user pilot validation. Next: stop for MVP review; Prompt 14 is optional expansion.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** MVP review checkpoint. Do not start expansion automatically. If the user chooses Phase 2, paste Prompt 14 — Second evaluator ecosystem and comparisons.

---

## Prompt 14 — Second evaluator ecosystem and comparisons

**Product phase:** Phase 2  
**Prerequisite:** 13 technical gates; explicitly requested extension  
**Specification sections:** 9, 12, 18, 23  
**Deliverable:** Independent adapter plus compatible paired comparisons

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **14-T1 — Add one independent ecosystem.** Default to Ragas for the existing RAG fixtures; choose Promptfoo instead if repo evidence/user need favors it and record the decision. Verify the pinned official API. Score recorded outputs rather than invoking the app again.
- **14-T2 — Implement comparable run accounting.** Pair cases/repetitions and check dataset, judge, rubric, metric and instrumentation identities. Distinguish rescore from fresh execution. Refuse unqualified comparison of incompatible runs.
- **14-T3 — Add statistics and chat workflow.** Implement coverage gates, appropriate paired/grouped uncertainty and repeated judge stability reporting. Wire compare and conversational requests to the same service.
- **14-T4 — Check independence and calibration.** Run both ecosystems against the same output fixtures, report disagreement without assuming score equivalence, and preserve each implementation’s semantics.

### Definition of done / acceptance gates

- **14-G1:** A real second package consumes the same stored executions without extra app calls.
- **14-G2:** Comparable runs yield correct paired results; incompatible identities yield an explicit warning/block.
- **14-G3:** Intervals and sample denominators are validated against constructed data.
- **14-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/14.md` are updated with the required five-part report.

### Verification to perform

Real-package contract tests, no-reexecution counter check, synthetic paired/grouped statistical fixtures and compare CLI/chat integration tests.

### Scope and decisions

Implement one additional ecosystem, not every vendor. Live credentials remain optional checks unless the chosen release contract requires them.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 15 — Richer runners and agent outcome contracts.

---

## Prompt 15 — Richer runners and agent outcome contracts

**Product phase:** Phase 2  
**Prerequisite:** 14  
**Specification sections:** 7, 16, 18  
**Deliverable:** Python/container/API runners and isolated agent test worlds

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **15-T1 — Extend transport support.** Add Python callable and OpenAI-compatible endpoint runners behind existing contracts. Add container execution with pinned images, non-root/read-only mounts, resource limits and explicit network policy.
- **15-T2 — Model stateful outcomes.** Implement session reset fixtures, tool attempts/results, argument constraints and final-world-state assertions. Separate correct tool names from successful authorized effects.
- **15-T3 — Integrate with chat.** Explain runner capabilities and missing evidence; allow the user to choose approved test-world configurations through validated plan changes.

### Definition of done / acceptance gates

- **15-G1:** At least one real container fixture runs if Docker is available; absence is reported as blocked.
- **15-G2:** App state resets between independent cases and is preserved only within declared episodes.
- **15-G3:** Tool-name success cannot mask failed final-state checks.
- **15-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/15.md` are updated with the required five-part report.

### Verification to perform

Runner contract tests, real container smoke where supported, tool argument/outcome fixtures and state-reset integration tests.

### Scope and decisions

Containerization is not a claim of hostile multi-tenant isolation. Do not access production tools to prove a booking/deletion test.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 16 — Inspection, traces, caching, and parallel execution.

---

## Prompt 16 — Inspection, traces, caching, and parallel execution

**Product phase:** Phase 2  
**Prerequisite:** 15  
**Specification sections:** 8, 14–15, 18  
**Deliverable:** Evidence-backed inspection and observable parallel execution

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **16-T1 — Add repository inspection.** Read approved source/manifests with evidence locations; keep declared/inferred/observed distinctions. Optional probes use policy-controlled engine actions, not arbitrary planner shell access.
- **16-T2 — Import observations.** Normalize selected OpenTelemetry traces and preserve raw attributes, correlation IDs and sampling completeness. Prevent parent/child usage double counting.
- **16-T3 — Implement explicit caches.** Add version-complete execution/evaluation keys, invalidation and cache provenance. Disable effectful unsnapshotted execution caching. Exclude cache hits from fresh latency/independent-repeat claims.
- **16-T4 — Expand concurrency.** Use provider-aware quotas, backpressure, bounded batching and cancellation. Keep terminal responsiveness independent of worker load.

### Definition of done / acceptance gates

- **16-G1:** Misleading imports do not become confirmed runtime capabilities.
- **16-G2:** Partial traces remain partial and usage aggregation avoids known duplicates.
- **16-G3:** Any changed bound reference/rubric/app identity invalidates relevant cache entries.
- **16-G4:** Measured bounded parallelism respects quotas without unbounded coroutine growth.
- **16-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/16.md` are updated with the required five-part report.

### Verification to perform

Static inspection evidence fixtures, sampled trace imports, cache invalidation matrix, controlled rate-limit server/load tests and terminal control regression.

### Scope and decisions

Do not advertise million-case production capacity from mock throughput alone.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 17 — OpenAI evaluation bridges and one platform connector.

---

## Prompt 17 — OpenAI evaluation bridges and one platform connector

**Product phase:** Phase 2  
**Prerequisite:** 16  
**Specification sections:** 9, 11, 18  
**Deliverable:** Distinct OSS/API adapters and one demand-selected connector

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **17-T1 — Implement OSS bridge.** Pin openai/evals, allowlist compatible eval types, and bridge completion functions. Recorded replay requires exact supported input matching; unsupported dynamic follow-ups fail clearly.
- **17-T2 — Implement hosted-job bridge.** Use the official current Evals API contract, explicit egress, persisted remote IDs, submit/poll/fetch/cancel and partial-result correlation. Reconcile ambiguous submission without assuming idempotency.
- **17-T3 — Integrate one existing platform.** Implement one connector selected by actual project need: Langfuse, Phoenix or Braintrust. Without a stated preference, default to Langfuse dataset/trace import and record the reversible choice. Keep import/export distinct from metric evaluation.
- **17-T4 — Validate exposure boundaries.** Wire discoverable capabilities into planning/chat with exact supported modes and data destinations. Preserve unsupported status where credentials or contracts are unavailable.

### Definition of done / acceptance gates

- **17-G1:** OSS and hosted APIs use separate plugin identities, dependencies and capabilities.
- **17-G2:** A hosted adapter cannot silently generate replacement outputs in stored-output scoring mode.
- **17-G3:** Remote result IDs map to canonical cases without loss/duplication.
- **17-G4:** Selected connector round trips preserve provenance; live integration status is explicit.
- **17-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/17.md` are updated with the required five-part report.

### Verification to perform

Pinned-package/API contract fixtures, recorded HTTP failure tests, pagination/partial-job tests and bounded live smoke only when authorized.

### Scope and decisions

These integrations are demand-led. Implement and validate each ticket sequentially; do not call all three complete when only one was exercised. If upstream scope is incompatible, document the supported subset rather than fabricating a bridge.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 18 — Reviewed dataset generation and advanced episodes.

---

## Prompt 18 — Reviewed dataset generation and advanced episodes

**Product phase:** Phase 3  
**Prerequisite:** 17 or explicit completed prerequisite subset recorded in ledger  
**Specification sections:** 6, 19  
**Deliverable:** Candidate-data workflow and one advanced evaluation modality

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **18-T1 — Build candidate workflow.** Generate bounded candidate cases with source spans, model/prompt provenance, split identity and review state. Add review/promotion operations and human/executable verification records.
- **18-T2 — Protect evaluation integrity.** Separate development/generation from holdout data; keep unreviewed synthetic references out of trusted ground-truth summaries. Record user promotion decisions and duplicate-source checks.
- **18-T3 — Implement one advanced modality.** Default to multi-turn text episodes: episode schema, simulator provenance, state reset and independent success checks. If user selects coding or voice instead, define that complete modality contract first and record replacement scope; do not implement all three by assumption.

### Definition of done / acceptance gates

- **18-G1:** Generated cases remain candidates until an explicit documented promotion action.
- **18-G2:** Held-out data is not exposed to generation/optimization tools.
- **18-G3:** Selected modality has a real execution fixture and independent success evidence.
- **18-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/18.md` are updated with the required five-part report.

### Verification to perform

Generation with fake provider plus optional live sample, provenance/review transition tests, split-leakage tests and a full selected-modality integration fixture.

### Scope and decisions

Harness-user chat already exists; this phase evaluates a multi-turn application. Do not confuse those two features or claim voice/coding support from a generic JSON field.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 19 — Controlled optimization experiments.

---

## Prompt 19 — Controlled optimization experiments

**Product phase:** Phase 3  
**Prerequisite:** 18; comparison gates from 14  
**Specification sections:** 19, 24  
**Deliverable:** Optional experiment module with protected holdout and explicit parameter space

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **19-T1 — Define experiment contract.** Accept only exposed configuration parameters, objective metrics, constraints and a development dataset. Preserve trial lineage, budgets and intended changes.
- **19-T2 — Execute controlled trials.** Reuse deterministic run services, fixed evaluation contracts and uncertainty-aware comparisons. Separate candidate selection from final protected evaluation.
- **19-T3 — Expose conversational experiment control.** Explain tradeoffs and propose adoption; source modifications, deployment or production configuration changes require their own explicit authorization.

### Definition of done / acceptance gates

- **19-G1:** Every trial maps to a reproducible parameter set and run manifest.
- **19-G2:** Optimizer cannot alter judge rubrics or inspect hidden test labels to improve its score.
- **19-G3:** Selection on development data and final holdout evaluation are distinguishable in reports.
- **19-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/19.md` are updated with the required five-part report.

### Verification to perform

Known-objective synthetic experiment, budget interruption/resume, holdout isolation and trial lineage tests.

### Scope and decisions

Keep optimization optional and avoid automatic application-code rewriting.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 20 — Distributed execution after measured need.

---

## Prompt 20 — Distributed execution after measured need

**Product phase:** Phase 3  
**Prerequisite:** 19 plus measured bottleneck evidence  
**Specification sections:** 14–15, 19  
**Deliverable:** PostgreSQL coordination, object artifacts and restart-safe distributed workers

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **20-T1 — Establish need and contracts.** Measure the local bottleneck and define target workload, resource budget and failure model. If no distribution need is demonstrated, deliver findings and mark distributed implementation deferred rather than manufacturing a scale claim.
- **20-T2 — Implement distributed coordinator.** Use durable queues, PostgreSQL leases/fencing, object storage and stable task keys. Preserve at-least-once computation with deduplicated commits; never claim exactly-once external effects.
- **20-T3 — Exercise distributed recovery.** Handle duplicate deliveries, expired leases, late workers, worker/node failure and partial artifact uploads. Keep session controls backed by authoritative remote state.
- **20-T4 — Publish honest scale evidence.** Measure bounded workloads and document hardware, traces, costs, queue behavior and mock versus real application throughput.

### Definition of done / acceptance gates

- **20-G1:** Fenced stale workers cannot overwrite a newer accepted result.
- **20-G2:** Duplicate delivery cannot duplicate a logical result commit.
- **20-G3:** Pause/cancel and recovery remain correct under worker failure.
- **20-G4:** Performance claims describe the actual tested workload.
- **20-G5:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/20.md` are updated with the required five-part report.

### Verification to perform

Local multiworker integration environment, duplicate/lease-expiry fault tests, object-store failures and bounded scale runs with recorded resource usage.

### Scope and decisions

Do not begin costly cloud provisioning without authorization. A deferred outcome is valid if the measured-need prerequisite is absent; never mark implementation complete.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Paste Prompt 21 — Optional dashboard and curated plugin catalog.

---

## Prompt 21 — Optional dashboard and curated plugin catalog

**Product phase:** Phase 3  
**Prerequisite:** 20 implemented or explicitly documented local-only scope  
**Specification sections:** 9, 19  
**Deliverable:** Thin dashboard and governed plugin discovery over stable core services

Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, the phase ledger and predecessor reports. If the engineering contract is absent, recover it from the supplied prompt pack before working. Verify predecessor evidence and implement the tickets below in the existing repository.

Execute this prompt only. Preserve current work, use existing services, and update the ticket/requirement ledger. Finish with the required five-part completion report; do not advance automatically. If a necessary predecessor gate is unsatisfied, resolve a small in-scope defect with a recorded decision or report the specific blocker instead of building on an invalid contract.

### Engineering tickets

- **21-T1 — Expose existing services.** Build a small API and optional UI for sessions, runs, evidence and comparisons. Use the same authorization and service contracts as CLI; keep local-only access default and document authentication for remote use.
- **21-T2 — Build curated discovery.** Read versioned plugin manifests with compatibility, provenance, license and permission metadata. Discovery proposes installation; it cannot silently install or trust executable packages.
- **21-T3 — Add lifecycle controls.** Implement compatibility checks, revocation/disable behavior and audit records. Optional MCP exposure reuses the same typed actions and policies.
- **21-T4 — Validate product parity.** Ensure dashboard/MCP cannot bypass CLI gates, change frozen runs or leak secrets. Preserve a fully usable CLI-only installation.

### Definition of done / acceptance gates

- **21-G1:** UI numbers/evidence match core results and require no duplicate benchmark engine.
- **21-G2:** Untrusted catalog metadata cannot trigger package execution.
- **21-G3:** CLI-only install works without UI or hosting dependencies.
- **21-G4:** Relevant tests actually ran, predecessor regressions are addressed, and ticket/requirement status plus `docs/engineering/reports/21.md` are updated with the required five-part report.

### Verification to perform

API authorization/serialization tests, UI integration flows if implemented, catalog malicious-manifest tests and CLI-only packaging smoke.

### Scope and decisions

No deployment, marketplace publication or plugin installation without explicit authorization. Final next action is review of achieved scope and remaining gates, not automatic expansion.

### Required closeout

Report implemented functionality and changed files; exact tests/commands and results; satisfied/pending/blocked gates; recorded decisions/specification discrepancies; and the exact next command or prompt. List the real evidence, not a proposed test plan.

**Next only when applicable gates pass:** Review the final phase ledger and release-readiness report; choose a concrete remaining gate or explicitly authorized release action.

---

## Repair prompt — use instead of advancing after a failed gate

Read the latest phase report, engineering contract and authoritative specification. Identify the first unsatisfied mandatory gate on the active phase. Diagnose and implement the smallest correct fix; preserve unrelated changes and frozen run records. Do not weaken tests, thresholds, policy boundaries or the definition of done merely to obtain a pass.

Run the affected checks and only broaden testing to resolve concrete regression risk. Update tickets, requirement evidence and the existing phase report. If the problem depends on missing credentials, a system capability, external review or a material user decision, state that exactly and complete independent work where useful. Finish with the same required five-part completion report and name the precise next prompt or unblock action. Do not start the next phase while its prerequisites remain invalid.

## Resume prompt — for a new Codex session

Continue the AI-Bench project from the repository's persisted state. Read applicable repository instructions, docs/spec/implementation-plan.md, docs/engineering/implementation-contract.md, docs/engineering/phase-status.md, the requirement matrix, and the latest phase report. Inspect current changes before editing.

Identify the active numbered prompt and its first unfinished ticket. Continue that already authorized prompt only; do not regenerate completed modules or repeat expensive checks without a concrete reason. If all authorized prompts are complete, report the recorded next review checkpoint rather than beginning optional scope. Finish with the required five-part completion report and exact next command or numbered prompt.

## Final delivery inventory

By the MVP review checkpoint, the repository should contain:

- An installable conversational `aibench` CLI plus scriptable commands.
- Canonical schemas, JSONL dataset validation, CLI/HTTP runners and recorded execution artifacts.
- Native/custom evaluators and the tested optional DeepEval adapter.
- A deterministic plan/execution engine with policy, accounting, cancellation and conservative recovery.
- Persistent two-way sessions, plan refinement, responsive run controls and evidence-based failure discussion.
- Rebuildable JSON/Markdown/HTML reports and working fixture quickstarts.
- Dependency locks, migrations, meaningful tests, CI and packaging instructions.
- The specification snapshot, traceable tickets, requirement matrix, ADRs, all phase completion reports and an honest release/pilot readiness assessment.

Optional Phase 2 and Phase 3 outputs belong to their own gates and release notes. Do not imply they exist merely because their prompts are included here.
