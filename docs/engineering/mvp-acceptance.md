# MVP acceptance audit (Prompt 12)

This checks every §17 MVP requirement, and each §23 validation area, against evidence that was actually produced.

Status values:
- **Verified**: an automated test ran and passed on the platform named.
- **Verified (manual)**: a recorded command run.
- **Partial**: some of it is verified, and the rest is named.
- **Blocked**: it needs something not available here, which is named.
- **Not done**: out of what this prompt could do; the reason is given.

Environment for everything below: Windows 11 Pro (10.0.26200), Python 3.12.10, 14 logical CPUs, loopback-only networking for applications. Per-prompt detail is in `docs/engineering/reports/NN.md`.

## §17 "Ship" list

| MVP item | Status | Evidence |
|---|---|---|
| JSONL shorthand plus versioned native contracts | Verified | reports/01; `tests/test_normalize.py`, `test_ingest.py`, `test_models.py`; exported `schemas/1.0.0/*.json` |
| CLI and HTTP runners with explicit mappings and input isolation | Verified | reports/03; `test_cli_runner.py`, `test_http_runner.py`, `test_runner_bindings.py`; the 100-case acceptance run uses the HTTP runner |
| Immutable case, execution, plan and typed result models | Verified | reports/01, 04; `test_models.py`; `test_scoring_invariants_hold_for_generated_cases` (600 generated cases through the real scorer, with an evaluator that misbehaves on purpose: a Golden is never mutated, an error never carries a score) |
| Native exact-match / JSON-schema checks and one custom evaluator example | Verified | reports/04; `test_native_evaluators.py`, `examples/evaluators/refund_window.py`; calibration in this report |
| DeepEval faithfulness adapter with explicit applicability handling | Partial | Offline: recorded-response contract tests, worker isolation and applicability (reports/05, `test_deepeval_adapter.py`, `test_worker_evaluator.py`). Blocked: the live judge smoke (`AIBENCH_LIVE_DEEPEVAL=1`, needs a key and authorization for paid calls) |
| Persistent two-way benchmark chat as the default CLI, with focused clarification and plan refinement | Verified (scripted model) | reports/08–10; `test_a_fresh_user_completes_the_conversational_acceptance_journey`; ConPTY tests. No live model was used |
| Basic LLM planner using declared capabilities and installed manifests | Partial | The planner loop, bounds and fallback are verified with a scripted provider and a local HTTP provider (reports/07). Its quality against the fixture set has not been measured with a live model (blocked, as above) |
| Live status, responsive input, pause/resume/stop, session recovery, conversational failure exploration | Verified | reports/09–10; the acceptance journey (question during a run, `/pause`, `/resume`, close and reopen with no duplicate call, failure discussion) |
| Manual-plan execution without an LLM | Verified | reports/06; the 100-case acceptance run |
| SQLite persistence, bounded scheduling, timeouts, cancellation, conservative resume | Verified | reports/02, 06, 10; the killed-engine acceptance run (section "Acceptance run" below); fault tests `test_engine_faults.py` |
| JSON/Markdown/HTML reports with coverage, errors, costs, evidence links | Verified | reports/11; `test_reports.py`. The acceptance run's HTML and JSON reports are regenerated from stored facts |
| Fixture apps for text, instrumented RAG and mock tool use | Verified | `examples/apps/` (text chatbot, black-box text, HTTP RAG, effect counter); `examples/acceptance/rag_service.py` (100-case RAG); tool-use fixtures in planner and runner tests |

## §17 acceptance clauses

| Clause | Status | Evidence |
|---|---|---|
| A fresh user opens aibench, states a goal, answers a material clarification, revises the draft and starts a run | Verified (scripted model) | `test_a_fresh_user_completes_the_conversational_acceptance_journey` (goal, a selection question, a revision to the first 20 cases, "Run it") |
| Asks a question during execution; pauses/resumes through slash controls | Verified | Same test: the question is explained while the run stays `running_here`; `/pause` stops dispatch (no new call over 0.6 s), `/resume` completes it |
| Closes and restores the session without duplicate execution | Verified | Same test: after reopening, the call counter stays at 20 (each case exactly once). That journey closes after the run has finished. Restoring during a run is `test_reopening_after_a_kill_shows_the_real_state_and_restarts_nothing` (reports/10): the run shows as interrupted, nothing restarts until resumed, and every case is committed once |
| Discusses a failed case with evidence | Verified | Same test: `list_failures` and `get_case_evidence` for rag-003; the numbers in the reply trace to the queries |
| Runs 100 fixture cases and identifies an injected RAG failure | Verified | `test_the_100_case_workflow_survives_a_kill_finds_the_injected_failures_and_rescores_offline`; manual run (below): 92 pass, and the 8 failures are exactly the 8 injected retrieval failures |
| Observes a missing-evidence warning on a black-box endpoint | Verified | `test_a_black_box_endpoint_yields_a_missing_evidence_gap_not_a_score`: `app describe` shows retrieval as `unknown`; planning a groundedness objective yields a gap, not a metric |
| Resumes an interrupted safe run | Verified | Same 100-case test: the engine process is killed after 30 calls; nothing runs until resumed; resume completes 200/200 work items |
| Rescores stored outputs without invoking the app again | Verified | Same test and manual run: 0 application calls during `aibench evaluate` |
| A hand-authored plan produces the same deterministic metrics as an LLM-authored plan with identical content | Verified | reports/07; `tests/test_plan_equivalence.py` |

## Acceptance run (manual evidence)

Command: `python examples/acceptance/run_acceptance.py --out DIR`. The result is recorded in `docs/engineering/evidence/12/acceptance-summary.json`, with its HTML and JSON reports alongside.

- **Workload:** 100 cases, one exact-match binding, application concurrency 4, loopback HTTP.
- **Interruption:** the engine was killed after 30 application calls. No call happened while it was stopped. Resume exited with code 1 (complete, and the gate failed).
- **Reliability:** (successful executions + completed evaluations) / (planned executions + evaluations with a usable execution) = (100 + 100)/(100 + 100). The error taxonomy is empty. An evaluation skipped because its execution failed is excluded from the eligible count, never counted as completed.
- **Duplicate calls:** 4 cases were called twice. They were in flight at the kill and are effect-free, so they were dispatched again (ADR 0005).
- **Application calls in the report:** 104, the same as the service received this time: 100 recorded attempts plus 4 `uncommitted_dispatches`, with unknown cost (see Defects fixed). The uncommitted count is an **upper bound**. Recovery counts work that was marked running, which can be just before its request went out. The independent review saw, for example, 103 reported against 101 received in 3 of 14 kill/resume cycles. It never reports fewer calls than the application saw.
- **Wall time:** 28.1 s for the whole sequence (run, kill, resume, report, rescore).
- **Cost accounting:**
  - application: unknown for every call, because the fixture reports no cost;
  - evaluator: complete, USD 0 (native).
- **Quality:** 92/100 pass. The `correct-answers` gate (≥ 0.95) fails at 92/100.
- **Rescore:** 0 application calls; 92/100 pass with the case-insensitive binding.

## §23 validation areas

| Area | Status | Evidence and numbers |
|---|---|---|
| **Engine and contract validation** | Verified | See the rows below |
| • Malformed/large JSONL, duplicates, migrations | Verified | reports/01–02; `test_ingest.py`, `test_storage_migrations.py` |
| • Immutable Goldens, reference leakage, typed values, missing vs empty | Verified | reports/01, 03–05 |
| • Artifact atomicity, cancellation | Verified | reports/02, 06 |
| • Invariants | Verified | `test_scoring_invariants_hold_for_generated_cases`: seeded randomized, 600 cases through the real scorer (`BindingScorer`), with an evaluator that raises, returns errors, the wrong value kind or non-booleans. The test asserts that pass, fail, error, not-applicable and skipped each occurred, so no branch is vacuous. No property-testing library is used |
| • Fault injection (kill before, during and after invocation and persistence; unknown effects; preserved attempt costs) | Verified | reports/06, 10; `test_engine_faults.py`, `test_session_recovery.py` |
| • Disk-full and partial-artifact failure | Partial | Partial-artifact and orphan handling are verified (reports/02). A real disk-full condition was not exercised |
| • Invalid plans cause zero app and evaluator calls | Verified | reports/06 |
| **Plugin conformance** | Verified offline | DeepEval recorded responses (reports/05). The live drift test is blocked (no key or authorization) |
| **Conversational product validation** | Verified with a scripted model | reports/08–10 and the acceptance journey: corrections, interruptions, chat during a run, retried turns, stale revisions, recovery, `/status`/`/pause`/`/stop` during a model outage |
| • Terminal checks | Partial | Piped and non-TTY use is verified; terminal resizing is not tested; multiline input is covered by the ConPTY tests (Windows only) |
| • Conversation-quality metrics (task completion, clarification count, correction retention, action-intent accuracy) | Not done | No live-model trials, which need an authorized provider |
| **Benchmark the planner** | Partial | See the planner section below. The template baseline is measured. The model planner is not measured (blocked). No fixture has been reviewed |
| **Judge and outcome validation** | Partial | Native evaluators are calibrated (below). A human-labelled set and the model judges are not done (no human labelers; live judge calls blocked) |
| **Performance and release gates** | Partial | See the rows below |
| • 100-case real fixture suite | Verified | Acceptance run above |
| • Larger cheap workload for bounded memory and resume | Verified (opt-in test) | 1,000 cases, interrupted after about 400 calls and resumed: 1,000/1,000 completed, no case called more than twice. Output of the closeout run is in `evidence/12/workload-1000.txt` (time and traced peak memory). Throughput is low; see the limitation below |
| • Million-record ingestion | Not done | §23 schedules it for when scale work begins |
| • Fresh-install demo | Verified (manual) | reports/11; repeated for Prompt 12 (report 12) |
| • No known reference leakage | Verified | reports/03, 05, 08 |

## Planner benchmark (12-T2)

Command: `aibench plan benchmark --fixtures benchmarks/planner/v1 --out FILE`. The fixture set has 40 fixtures in 27 families: 23 development and 17 held out (15 held-out families). The annotations were written by the implementing agent. **No fixture has been reviewed**, and no reviewer is claimed.

Template baseline, after the fix below. Each value has its count and 95% Wilson interval.

| Measure | Observed | Target (§23, engineering) | Status |
|---|---|---|---|
| Selection precision | 1.0 = 17/17 (0.82–1.0) | ≥ 0.90 | met |
| Selection recall | 0.81 = 17/21 (0.60–0.92) | ≥ 0.85 | **not met** |
| Gap precision | 0.79 = 22/28 (0.60–0.90) | — | reported |
| Gap recall | 1.0 = 22/22 (0.85–1.0) | — | reported |
| Unnecessary evaluator rate | 0.0 = 0/17 | — | reported |
| First-pass plan validity | 1.0 = 37/37 (0.91–1.0) | ≥ 0.95 | met |
| Rejection of invalid security fixtures | 1.0 = 3/3 (0.44–1.0) | 100% | met |
| Unsupported selections | 0 | 0 | met |

By family group:

| Group | Fixtures | Recall | Gap precision |
|---|---|---|---|
| Development | 23 | 1.0 | 1.0 |
| Held out | 17 | 0.50 | 0.57 |

The template misses objectives phrased without its keywords:
- "make sure it doesn't make things up";
- "does it give the right answer";
- "respuestas correctas";
- "captions describe the image correctly";
- "not worried about latency", where it reports an unmapped gap;
- a spending-limit objective, whose annotation itself needs review.

These are what a model planner is meant to handle. That comparison has not been measured, because it needs an authorized live provider. The recall target stays not met; it is not relabelled.

Scoring rules, after the independent review:
- **Fallback.** A planner run in which any executable fixture fell back to the template reports every target as "not measured" and warns. Its numbers would otherwise be the template's under another name.
- **Unsupported selections.** Any evaluator the catalog marked ineligible counts as unsupported, not only the IDs an annotator listed as `forbidden`.
- **Rejections.** A refusal counts only when it cites a missing permission.
- **Acceptable alternatives.** Supported by the scoring (precision counts them; recall does not need them) and unit-tested, but no v1 fixture annotates any yet.

The review also questioned some annotations. They are recorded here for human review and not changed after measurement; changing one to `acceptable` would, by itself, lift recall to exactly 0.85:
- `image_captions`: is exact match against a reference caption really correctness?
- `structured_extraction`: does it also need a correctness gap?
- `budget_objective`: already marked as needing review.
- The security fixtures annotate `select: correctness` for plans that must be refused, which penalises a planner that proposes no metrics at all.

Before the fix, the same set gave precision 17/18 and 1 unsupported selection. The failing fixture was `retriever_disabled_runtime`: retrieval is declared, but recorded runs show it always empty, and the planner selected the groundedness judge anyway. The defect was fixed (see Defects fixed). That fixture was then moved from held out to development, because it was used to change the planner.

§23 also names comparisons that are not measured:
- **Human-authored plan:** not measured separately; the annotations are the reference.
- **Objective coverage under a budget:** only through the over-broad-request fixture.
- **Reproducibility:** the template is deterministic; no stochastic planner was run.

## Judge calibration (12-T3)

Command: `aibench evaluators calibrate --set benchmarks/judges/v1`. It runs 33 labelled cases. The labels were written by the implementing agent and are **unreviewed**.

| Binding | Agreement | False acceptance | False rejection | Repeat stability |
|---|---|---|---|---|
| `native.exact_match` (strict) | 8/13 | 0/6 | 5/7 | 13/13 |
| `native.exact_match` (case-insensitive, whitespace-collapsed) | 10/13 | 0/6 | 3/7 | 13/13 |
| `native.json_schema` | 7/7 | 0/4 | 0/3 | 7/7 |

- **Correct outputs rejected** (the documented surface-match limitation, quantified): paraphrases, verbose correct answers and non-text output. Strict matching also rejects case and whitespace differences.
- **Repeat stability** is trivially complete for these deterministic evaluators. It becomes informative only for model judges.
- **No false acceptance:** injected instructions and appended contradictions were rejected.
- **Not measured:** the DeepEval faithfulness judge's calibration, stability and order/style sensitivity. These need live judge calls (paid, not authorized).

## Defects found and fixed in Prompt 12

1. **The planner selected a judge for a retriever that returns nothing at runtime** (07-T2, §23). `inspection/profile.py` now records capabilities that were `always_empty` in recorded executions, and `planning/catalog.py` makes evaluators that need that field non-empty ineligible, stating why. Pinned by `test_a_retriever_that_returns_nothing_at_runtime_is_a_gap_not_a_metric`.
2. **Reports under-counted application calls after a crash** (11-T1, 11-G2). In-flight calls lost with a killed process were counted by the budget ledger (104) but not by the report (100). The report now includes the `uncommitted_dispatches` recorded at recovery, with unknown cost. The acceptance test pins it as an upper bound: the report never shows fewer calls than the service received, and never more than 100 plus the concurrency.

Fixed after the independent review of this prompt (`tests/test_acceptance_review_regressions.py`):

3. **Fallback.** A model planner's template fallbacks were reported as model results. Targets now read "not measured" whenever an executable fixture fell back.
4. **Unrun invariant branches.** The invariant test never produced an error or not-applicable result, and exact match never passed. It now runs through the real scorer with an erratic evaluator and asserts every branch.
5. **Tiny samples.** `always_empty` fired on a single empty observation, or on one empty among 99 unreported. It now needs at least 3 observations, all empty.
6. **Reliability.** The formula counted skipped evaluations as completed. It is now split into executions and evaluations over their eligible denominators.
7. **Unsupported selections** depended on which IDs annotators listed. Any ineligible selection now counts.
8. **Rejections** counted without checking the reason. A missing permission is now required.
9. **Malformed sets** crashed with tracebacks. They are now refused with a clear error.

## Known limitations recorded, not fixed

- **Engine throughput.** 1,000 cases took 152.6 s in the closeout run (`evidence/12/workload-1000.txt`; traced peak 15.0 MiB) and 233 s in an earlier development run, at concurrency 16 against an instant local service. Profiling 200 cases (`evidence/12/engine-profile-200.txt`: 28.6 s, 7.0 cases/s) shows almost no CPU time. Each call waits on a new HTTP connection being set up and torn down, and on its capture artifacts being written, fsynced and read back for verification (`artifacts.write_bytes`, `commit_verified_artifact`). Cumulative profile times overlap across concurrent tasks, so this shows where calls wait, not an exact breakdown. This is a performance limitation, not a correctness one, and it is recorded for later scale work.
- **Platforms not exercised:**
  - Linux: CI is configured, but no result was observed (no `gh` here). WSL here has no Python distribution.
  - macOS: not run anywhere.
  - Python 3.11: not installed here.
  - A Docker-based Linux / Python 3.11 run would need starting Docker Desktop and pulling an image, which was not done without your go-ahead.
- **Live checks still blocked:** the DeepEval live judge, live-model conversation trials, and the model planner benchmark. All need an API key and authorization for paid calls.
- **Human review:** neither the planner fixtures nor the calibration labels have been reviewed by people. §23's two reviewers and adjudication are pending.
- **Planner scoring:** gap and unnecessary-evaluator scoring use the concepts the planner assigns to its own objectives, so a model planner could over-label them. That needs checking once a model planner is measured.
- **Crash between recovery and its event:** if a process dies after recovery has changed work-item states but before the `recovered` event is written, those uncommitted dispatches are lost from both the report and the ledger. This is pre-existing and rare.
