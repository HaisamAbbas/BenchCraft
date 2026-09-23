# ADR 0003: Evaluator protocol, registry discovery and stored-output scoring

Status: Accepted
Date: 2026-09-23
Prompt: 04 — Evaluator contracts, native checks, and registry

## Context

Prompt 04 adds the framework-independent evaluation pipeline (§9, §12, §14). Its choices
shape the DeepEval adapter (05), the engine (06) and reports (11).

## Decisions

1. **Contracts live in `core/models.py`** (`EvaluatorManifest`, `MetricBinding`,
   `DecisionRule`, `FieldRequirement`, `MetricDirection`, `MetricScope`) so plans and results
   can reference them without importing evaluator code. The runtime protocol (`Evaluator`,
   `EvaluationView`, `EvaluatorContext`, `EvaluationOutcome`) lives in `aibench.evaluators`.
   `tests/test_dependency_boundaries.py` enforces that `core` imports only the standard
   library, pydantic and itself, and that importing `core` loads no evaluator framework or
   terminal UI package (04-G4).
2. **The harness decides; evaluators measure.** An evaluator returns a status (`ok`,
   `error`, `not_applicable`) and a typed value. The canonical decision comes from the
   binding's frozen `DecisionRule` (or the manifest default). Rules must suit the value
   kind (`is_true` for booleans, numeric comparators for scalars, `in` for categories);
   mismatches are rejected before scoring.
3. **One status per selected execution, per binding:**
   - `skipped` means there was no usable execution (the app failed, or the case isn't recorded).
   - `not_applicable` means required evidence is missing or empty. Missing and empty are distinct reasons.
   - `ok`, `error` and `cancelled` come from evaluation itself.

   Evaluator exceptions, timeouts and outcomes that break the manifest's contract are
   `error` / `not_evaluated`, never scores.
4. **Selection.** Scoring selects the final attempt per (case, repetition): the highest
   `attempt_id`, recorded as `final_attempt_rule` in provenance. Selection is the run's
   recorded executions. A dataset case that was never executed isn't part of a smoke run's
   selection. **Prompt 06 must derive selection from planned work items**, so a planned but
   unexecuted case counts as lost coverage instead of silently disappearing from the
   denominator.
5. **Identity and rescoring.** Each scoring pass has a `scoring_id`. `result_id` =
   `scoring_id:case:repetition:metric@version:binding-hash`. Evaluation attempt numbers
   increase per (run, case, repetition, metric, binding) across passes (migration 4; see
   "Changes after independent review"). Rescoring therefore adds records and never
   overwrites or conflicts with earlier ones. Two bindings of the same metric with different
   parameters are distinguished by the binding hash.
6. **Aggregation** (`reporting/aggregation.py`) produces one summary per binding and never
   averages across metrics. The counts are selected, eligible, attempted, completed, errors,
   cancelled, not_applicable and unavailable. They must add up, and the code raises
   otherwise. Coverage is measured against `selected`. The value summary follows the
   manifest's aggregation contract: `rate` for booleans, `mean` only where declared,
   `category_counts`, or `none` for structured values. Floats are rounded to 6 places and
   inputs are ordered, so summaries are deterministic.
7. **Accounting.** Evaluators without models record `model_calls=0, cost=0.0,
   accounting=complete`. Model-backed evaluators must report usage through the context.
   Unreported usage is `cost=null, accounting=unknown`, never zero. Reported usage is also
   stored as `UsageEvent`s with role `evaluator`.
8. **Registry and discovery.**
   - IDs are `namespace.name`. A reference can use `@major`, `@major.minor` or an exact
     version; resolution picks the highest version by number.
   - `core_schema` ranges are checked against `SCHEMA_VERSION`, and unsupported range
     syntax counts as incompatible.
   - Installed plugins are found through `aibench.evaluators` entry points using
     `importlib.metadata`, so nothing is imported. Their manifests are read by
     `python -m aibench.registry.worker` in a subprocess with a minimal environment and a
     timeout. Third-party evaluators are listable but never instantiated in-process;
     executing them in a worker is Prompt 05's job (DeepEval requires it anyway).
   - Local evaluator files run in-process only with explicit `--trust-local-code`.
   - Applicability: a metric that needs an observation the run's application doesn't
     expose (for example `execution.retrieved_context` without
     `output_binding.retrieved_context`) is refused before scoring. `RunManifest` gains an
     optional `application_id` for this.
9. **JSON Schema: `jsonschema` 4.26 (pinned `>=4.23,<5`)** instead of a hand-written
   validator. It adds attrs, jsonschema-specifications, referencing and rpds-py. Schemas
   come from params and case data, so they are untrusted. By default jsonschema's legacy
   resolver **fetches remote `$ref` URLs over the network**. We found that with a test
   against a local server, and fixed it by validating with an explicit
   `referencing.Registry` that refuses all retrieval. Local `#/$defs` references still
   work. `test_json_schema_never_fetches_a_remote_ref` asserts that the server receives
   zero requests.
10. **Schema version stays 1.0.0.** All new fields are optional and additive (as in ADR 0002).

## Consequences

- Prompt 05 implements worker-side evaluation for `requires_worker` manifests and reuses
  `EvaluatorContext.report_usage` for judge usage.
- Prompt 06 owns selection from work items (decision 4) and cancellation wiring (the
  context's `cancel` event is checked before each case; in-flight native evaluations are
  bounded by the per-evaluation timeout).
- The JSON Schema check treats `format` keywords as annotations; they are not asserted.

## Changes after independent review (2026-09-23)

An adversarial review of the Prompt 04 diff reported several findings. Each was reproduced with a failing test
(`tests/test_scoring_review_regressions.py`, plus the worker test in
`tests/test_plugin_discovery.py`) before it was fixed:

- **An unusable answer is a failure, not lost coverage (was a blocker).** An `ok` execution
  whose output is null or not text used to be `not_applicable`, which dropped it from the
  pass rate. An app returning null on hard cases therefore scored *higher*. Now:
  - The view returns JSON null as the app's answer.
  - `native.exact_match` scores non-text output as `false`, and the refund example as
    `unusable_output`.
  - The registry refuses any `execution.output` requirement with `non_empty=true`.
  - Principle: `not_applicable` is only for missing *reference-side* or *observation*
    evidence, never for what the application answered.
- **Evaluator defects never crash a pass.** Conformance checks, raw-payload serialization
  (`allow_nan=False`) and a non-`EvaluationOutcome` return value are all handled inside the
  guarded region and become `error` results. Requirement paths are validated at binding
  time (`EvaluationView.path_problem`).
- **Untrusted regex cannot stall scoring.** Schemas containing `pattern` or
  `patternProperties` (from params or case data) are validated in a killable worker with a
  hard timeout (`validation.validate_untrusted`, `evaluators/schema_worker.py`). Regex-free
  schemas stay in-process.
- **Contained helper processes.** `runners.process_tree.run_contained` runs a worker with a
  Job Object or process-group tree kill and bounded reader joins, so an inherited pipe
  can't make the caller wait. The output cap is enforced live (see the second review
  below). The plugin manifest worker and the regex-schema worker use it. The
  worker provides process isolation only, not a sandbox.
- **Values and rules are finite.** Non-finite values anywhere in an outcome are
  conformance errors. `DecisionRule.threshold` is strict and finite, so `True` and NaN are
  rejected.
- **Attempt identity: migration 4.** `evaluation_attempts` is rebuilt, keyed by
  (run, case, repetition, metric, binding hash, attempt), and existing rows are backfilled
  from their JSON. Attempt N now means the Nth time a binding scored that execution.
  Numbering relies on the single-writer convention.
- **Registry hygiene.**
  - Duplicate detection hashes the *resolved* identity (id, version, params, effective rule).
  - `native.*` is reserved for built-ins, and external or local registrations can't
    collide with existing keys.
  - Local evaluator modules get unique names based on their path.
- **Ambiguous Goldens.** An execution whose `case_id` matches more than one stored case is
  `skipped` with reason `duplicate_case_id`, instead of being scored against an arbitrary one.
- **Per-pass reads.** `Storage.list_metric_results(run_id, scoring_id=...)`.

Second review (P2, P3). Both findings were reproduced by failing tests before being fixed:
- Evaluator construction, `prepare()` and `close()` are guarded like `evaluate()`:
  - A failed or timed-out `prepare()` records `error` (`evaluator_prepare_failed:...`)
    for every case it would have evaluated. App failures and not-applicable cases keep
    their own status, and later metrics still run.
  - A failed `close()` can't change recorded results, so it becomes a report warning
    (`ScoringReport.warnings`, printed by `aibench score`).
- `run_contained` enforces its output cap *while the worker runs*. Pipes are drained by
  reader threads that keep at most the cap and discard the rest. Exceeding the cap kills
  the tree immediately, so a flooding worker can't fill memory or temp disk before its
  timeout. Readers are joined with a bounded wait, so an uncontained descendant holding a
  pipe can't block the caller.

Accepted limit: a trusted local evaluator that blocks the event loop synchronously can't
be interrupted by the per-evaluation timeout. Hard limits for arbitrary evaluator code come
with worker execution in Prompt 05.
