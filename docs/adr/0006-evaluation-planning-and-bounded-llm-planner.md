# ADR 0006: Evaluation planning and the bounded LLM planner

Status: Accepted
Date: 2026-09-23
Prompt: 07 — Evaluation planning and bounded LLM reasoning

## Context

§3, §8, §9, §17 and §23 require:

- `inspect` and `plan` commands built from declared configuration, dataset field coverage and installed evaluator manifests;
- deterministic eligibility, with registry IDs resolved outside the model;
- a model planner limited to schema-constrained drafts, narrow tools and bounded repairs, with a deterministic template fallback;
- structured pending questions;
- identical measurements from equivalent manual and generated plans;
- no general terminal for the planner.

Prompt 06 already provides the executable plan format, policy and engine.

## Decisions

### Evidence (07-T1): `aibench/inspection/`

- **`ApplicationProfile`.** One `ObservationClaim` per capability:
  - `observed`: seen in *successful* recorded executions of the *same* app config (`--run`). Runs made with a different config hash are refused as evidence, and failed attempts are not counted.
  - `declared`: mapped in the config. If the capability is declared but absent from every recording, it stays `declared`, the contradiction is stated, and a gap is raised.
  - `unknown`: otherwise, with a minimal integration recipe as the gap.

  Evidence references point at config JSON pointers or execution IDs. The endpoint is recorded as its HTTP origin only. The scope string states: no source code or architecture discovery.
- **`DatasetSummary`.** Counts of present/empty/missing values per evaluation-view path, and reference-status counts. It never contains values; only key names of metadata, expectations and fixtures appear. `inferred` claims (`retrieval_task` from reference context, `tool_use_task` from expected tools) carry their limitation, for example "reference context ... is not observed retrieval".

### Validation (07-T2): `engine/compile.py`

- **`analyze_plan` collects every finding and classifies it:**
  - `missing_permission`: any policy denial.
  - `missing_information`: an observation the app does not expose, or a Golden field no selected case has.
  - `invalid`: unknown IDs, bad parameters or rules, duplicate bindings, undefined selector paths, empty or oversized selections, aggregation/value-kind mismatches, work-graph defects.

  Warnings (non-blocking) cover partial per-case coverage, budgets that can't cover the plan, and metrics without aggregation.
- **One gate for both paths.** `compile_plan` is now `analyze_plan` plus a refusal on any blocking finding, so the planner validator and the execution gate cannot disagree.
- **Behaviour change from 06.** A metric whose required Golden field no selected case has is now rejected before the run. Before, it ran and scored `not_applicable` for every case (07-G1: "unavailable inputs are rejected outside the model").
- **Plan format additions** (optional, backwards compatible):
  - `selection.where`: predicates over `case.*` paths only — `exists`/`equals`/`in`.
  - `selection.sample_size` + `selection.seed`: a seeded sample kept in dataset order. Cases are ranked by SHA-256 of seed, case ID, source line and position, so the same seed picks the same cases on every platform and Python version (`random.sample` makes no such promise). A seed is required, and `limit` cannot be combined with sampling.

  The order is case_ids → where → sample or limit.
- **DAG.** The v1 plan format has no user-authored dependencies. The only edges are execution → evaluation, built by the compiler. `work_graph` and `dag_problems` still check the derived graph (undefined dependencies, cycles), so a compiler regression is caught. Named selector graphs and gates are not in the v1 format; they are Prompt 11 (reports and gates).
- **Aggregation semantics.** `rate` needs boolean values, `mean` scalar values, `category_counts` category values; `none` produces a warning.
- **Plugin workers are not restarted per validation.** `analyze_plan(registry=...)` reuses an already-loaded registry, and only when the policy permits the plan's plugin environments. `plugin_denials` is checked before any worker starts: in compilation, in validation and in `gather_inputs`.

### Planning (07-T3): `aibench/planning/`

- **Catalog.** Each installed manifest becomes a `MetricOption` with deterministic eligibility:
  - `execution.*` fields must be declared or observed;
  - `case.*` fields must be usable in at least one case;
  - the policy must allow the evaluator (including model judges);
  - required parameters are listed, including `oneOf`/`anyOf` alternatives such as `schema|schema_field`.

  Concepts come from what a metric reads: reference answer → correctness, retrieved context → groundedness, tool events or reference tools → tool_use, expectations → expectations. Only `native.json_schema` → format needs a table entry. Latency and reliability are recorded by the engine for every execution and need no metric.
- **The planner's output is limited.** It writes a `DraftProposal` and nothing else: objectives with their concepts, metric choices (ID, rationale, objective IDs), repetitions, gaps and questions. The following come from the user or policy, never the model:
  - dataset and application paths
  - **case selection** (`--sample/--seed/--limit`)
  - budgets (policy ceilings fill in unset limits)
  - concurrency and retries
  - plugin environments
  - **metric parameters and pass/fail rules** (`--params ID=JSON`, `--rule ID=JSON`)

  Extra fields are schema errors (`extra="forbid"`). Selection was removed from proposals after the review: `where` predicates over reference answers plus `estimate_cost` counts let a model learn label values.
- **Validation rules for drafts:**
  - Every user objective must appear verbatim, with `source: "user"`; dropping or rewording one is blocking.
  - Every concept of every objective needs a metric that measures it, an explicit gap, or must be engine-recorded. For a user's objective this includes concepts its wording names (`catalog.concepts_in`), so relabelling "catch hallucinations" as latency is blocking unless a gap explains it.
  - Parameters or a rule the planner set that the user didn't supply are blocking (§3: a harness must not invent a schema or success threshold). The evaluator's documented default rule applies when the rule is left empty.
  - Metric objective references must exist. A metric serving none of its objectives' concepts is a warning (unjustified evaluator).
  - Plugin load problems met while building the catalog are blocking, as they are in the execution gate.
- **Template planner** (deterministic, the baseline and the fallback):
  - Objective text → concepts through a fixed whole-word keyword table that skips negated mentions ("don't care about latency"). Unmapped text becomes a gap plus a question.
  - Per concept, the first eligible option is chosen, natives first.
  - Missing required parameters become a question; the planner uses only parameters and rules the user supplied, and never invents a schema, judge or threshold.
  - A scalar metric without a default rule gets a threshold question. A scalar default rule gets a "confirm" question.
  - An ineligible concept becomes a gap citing each candidate's reasons.
- **Model planner** (`plan_with_model`):
  - Tools: `read_profile`, `summarize_dataset`, `list_evaluators`, `describe_evaluator`, `validate_plan`, `estimate_cost`, `write_plan_draft`. All are read-only functions over in-memory state. There is no terminal, file, network or process tool; unknown tool names are refused and recorded (07-G4).
  - A draft is accepted only through `write_plan_draft` after validation outside the model. Findings go back as the tool result.
  - Default bounds: 6 model calls, 12 tool calls, 2 repairs, 60k tokens.
  - Exceeding a bound, or any provider exception (including malformed responses), falls back to the template. The provenance keeps the model's call and token counts, the replies that reported no usage (where the token cap cannot be enforced), and the fallback reason.
  - The model sees the profile, the dataset counts, the catalog and the user's objectives, parameters and rules — never inputs, reference answers or label values.
  - The model is not contacted at all when the plan already needs a permission the policy does not grant (application, data roots, plugin environments), so no briefing is sent about data the user hasn't authorized.
- **Real provider.** `OpenAICompatibleProvider` targets Chat Completions. The wire format was checked against OpenAI's published OpenAPI description (tools/tool_calls/`role: tool`/usage). It uses httpx with no redirects, a 2 MB response cap and a secret-reference API key, and redacts that key from errors. The endpoint must be loopback, or an **https** origin in the policy's own `allowed_planner_origins` (kept separate from application targets). The key reference must be in `allowed_secret_refs`. Otherwise the model is never contacted: the CLI writes the template draft and exits 4. `examples/planner/openai.provider.json` is an example; set a model your provider offers.

### Commands and drafts (07-T4)

- **`aibench inspect APP [--dataset] [--run ...]`** prints or writes the profile and the dataset summary.
- **`aibench plan --app --dataset [--objective ...] --out plan.json`** writes the executable plan plus `plan.draft.json` containing:
  - objectives, rationale, gaps, pending questions (with `draft_revision`)
  - classified findings and coverage
  - a spend estimate (unknown cost is never $0; judge tokens are unknown)
  - planner provenance, and the profile and dataset hashes

  Exit codes: 0 executable, 2 needs information or invalid, 4 needs permission. A draft is written in every case.
- **Revisions.** An existing different plan is never overwritten silently: `--revise` archives it as `<stem>.rev<N>.json` and records `supersedes` (its hash). Revision numbers come from the draft document and the archives together, so a lost draft document cannot reset them, and an existing archive is never overwritten. Re-drafting an identical plan keeps its revision. `run` freezes the executable revision again (plan artifact plus hash, Prompt 06).
- **`plan validate`** moved to `cli/plan.py` and now reports classified findings, coverage and the estimate.

### Planner benchmark (§23): `planning/benchmark.py`

- Fixtures annotate concepts to measure, concepts to report as gaps, and forbidden metrics. `score` computes selection precision/recall, gap precision/recall, the unnecessary-evaluator rate, unsupported selections (forbidden or uncatalogued metrics) and first-pass validity.
- The template baseline over 10 fixtures (chatbot, RAG, misleading RAG, agent ×2, black box, partial references, judges not permitted, format without schema, unmapped voice objective) scores 1.0 on every measure, with zero unsupported selections.
- **Limitation:** the fixtures were written alongside the template by the same author and haven't been reviewed by a second person. The score shows the rules behave as designed; it is not evidence that the approach generalizes (§23). No live-model comparison was run.

## Changes after independent review

An adversarial review found 4 major and 12 minor issues. All confirmed ones were reproduced and then fixed with regression tests (`tests/test_planning_review_regressions.py`):

- **Major:**
  - label probing through model-authored selection (selection removed from proposals);
  - dropped or relabelled objectives (verbatim preservation, per-concept coverage including wording-named concepts);
  - invented parameters and thresholds (blocking unless user-supplied);
  - malformed provider responses crashing planning (defensive parsing, fallback on any exception).
- **Minor:** revision and archive handling; duplicate question IDs; the benchmark ignoring unjustified selections; keyword false positives and negation; plugin-load consistency between draft and gate; permission misclassification of typos; briefing egress before permission checks; plaintext planner endpoints; tokens without usage; failed attempts counted as evidence; no draft on a denied provider; version-dependent sampling.

## Consequences and limits

- `inspect` validates declared interfaces and recorded evidence only. Repository inspection is Phase 2.
- The template understands only its keyword table; free-text interpretation needs the model planner, whose drafts are still validated outside the model.
- Clarification is structured `PendingQuestion`s in the draft. Answering them conversationally is Prompts 08 and 09.
- `a5f527c` (the Prompt 06 commit) already contains early parts of this work: the selection fields, `case_field`, `resolve_binding`/`applicability_problems`, and a first `analyze_plan`. The rest is uncommitted.
