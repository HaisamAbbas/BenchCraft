# ADR 0004: DeepEval adapter and worker execution of third-party evaluators

Status: Accepted
Date: 2026-09-23
Prompt: 05 — DeepEval adapter

## Context

§9–10 require third-party evaluator code to run in a controlled worker, with its dependencies
kept out of the core. They also require DeepEval faithfulness to map recorded fields exactly
and never invent scores from missing evidence. ADR 0003 deferred worker *execution* to this
prompt.

## Findings from inspecting the real package (05-T1)

These were checked against the installed `deepeval==4.2.5`, not taken from memory:

- **Dependencies.** 70 packages, including auto-loading pytest plugins (`pytest-xdist`,
  `pytest-rerunfailures`, `pytest-repeat`, `pytest-asyncio`) and telemetry (`posthog`,
  `opentelemetry`). Its `rich>=13.6,<15` requirement *can* co-install with our `rich<14`
  (`pip check` is clean), but the pytest plugins would change our own test runs.
- **API.** `FaithfulnessMetric(threshold, model, include_reason, async_mode, strict_mode,
  verbose_mode, truths_extraction_limit, penalize_ambiguous_claims, ...)`,
  `a_measure(test_case, _show_indicator)`, and
  `LLMTestCase(input, actual_output, retrieval_context, expected_output, context, ...)`.
  Custom judges subclass `DeepEvalBaseLLM` and receive `a_generate(prompt, schema=<pydantic
  class>)`. The metric stores `truths`, `claims`, `verdicts` and `score` on the instance, so a
  shared instance is unsafe under concurrency (demonstrated by a test control).
- **Cost.** `evaluation_cost` is `None` for non-native (custom) judges.
- **Side effects.** DeepEval creates a `.deepeval/` directory in its working directory, even
  with telemetry off. It also reads `.env` files by default (`DEEPEVAL_DISABLE_DOTENV`) and a
  legacy key file under HOME (`DEEPEVAL_DISABLE_LEGACY_KEYFILE`), which may hold a Confident AI
  key. It retries judge calls twice by default (`DEEPEVAL_RETRY_MAX_ATTEMPTS`).

## Decisions

1. **Separate plugin environment.** `plugins/deepeval/` is a separately installable package,
   `aibench-deepeval` (pins `deepeval==4.2.5`; entry point `aibench.evaluators`). It's
   installed into its own venv (`plugins/deepeval/.venv`, git-ignored) together with an
   editable `aibench`. The core never depends on or imports DeepEval; this is enforced by
   `test_deepeval_is_imported_only_inside_its_plugin_package`.
2. **Generic worker execution.**
   - `aibench.registry.eval_worker` runs one evaluator inside the plugin environment. It speaks
     JSON lines over stdin/stdout, and file descriptor 1 is redirected to stderr, so plugin
     output can't corrupt the protocol.
   - `aibench.evaluators.worker_client.WorkerEvaluator` is the harness-side proxy.
   - `EvaluatorRegistry.load_plugin_environment(python)` finds the environment's site-packages
     by asking its own interpreter, discovers plugins from metadata, reads manifests in a
     worker, and registers them as worker-executed.
   - The CLI exposes this as `--plugin-env`, `--plugin-secret` and `--plugin-path`.
3. **Enforceable cancellation.**
   - Each binding gets one worker, handling one request at a time.
   - If the harness's per-case timeout (or any cancellation) interrupts a call, the worker's
     process tree is killed (Job Object or process group) and the next case starts a fresh
     worker. A blocking judge therefore can't keep running.
   - A timed-out case is `error: timeout`. It is never retried by the adapter.
4. **Worker environment.** The worker gets an allow-list of variables, plus a private
   temporary working directory and HOME that are deleted on close. Only secrets explicitly
   configured for the plugin environment (`--plugin-secret NAME=source:name`) are added.
   Worker error text, reasons and raw payloads are redacted of those secret values before
   they are persisted.
5. **DeepEval settings forced by the adapter:**
   - telemetry off
   - `.env` loading off
   - legacy key file off
   - `DEEPEVAL_RETRY_MAX_ATTEMPTS=1`, so there are no nested retries (§15); the manifest
     declares `internal_retries=0`
   - `internal_concurrency=2`, because `async_mode` extracts truths and claims concurrently

   Nothing is published: no Confident AI key can reach the worker.
6. **Field semantics.**
   - `case.input` becomes `input` (JSON text if it isn't a string).
   - The recorded text output becomes `actual_output`.
   - The *observed* `execution.retrieved_context` becomes `retrieval_context`.
   - `expected_output` and `context` are not set; faithfulness doesn't use them, and the
     Golden's reference context is never passed. A test asserts all of this on the real
     `LLMTestCase`.
7. **Policies.**
   - Retrieval that was never observed is `not_applicable` with reason
     `missing:execution.retrieved_context`; the harness applies this before the metric runs.
   - Retrieval that was observed but empty is `not_applicable` with reason
     `empty:execution.retrieved_context`. This is the documented `empty_context_policy:
     not_applicable`, and the only policy offered; another needs an explicit rubric
     decision (Prompt 07).
   - An empty or non-text answer is `not_applicable: unscorable_output:<kind>`. This is a
     deliberate, documented exception to ADR 0003's rule that a bad answer counts as a
     failure: faithfulness grades the grounding of *stated claims*, and an answer with no
     claims would otherwise score a vacuous 1.0. Correctness metrics judge answer quality,
     and the coverage denominator shows the loss.
   - A text answer from which the judge extracts no claims is `not_applicable: no_claims`.
     Upstream `score_qag_verdicts` returns 1.0 when there are no verdicts, which would be a
     vacuous pass. Any judge usage for that evaluation is still recorded.
   - Blank retrieved chunks are dropped. If none remain, the context counts as empty.
8. **Instances.** A new `FaithfulnessMetric` and a new judge instance are created for each
   case. `test_concurrent_cases_never_share_metric_or_judge_state` runs six interleaved cases
   on one adapter instance and gets the correct per-case scores. The control run shares one
   upstream metric instance, and it does corrupt the scores.
9. **Decisions and accounting.** The upstream `success` flag and threshold are kept in the
   raw artifact only; pass or fail comes from the binding's frozen rule. A judge whose cost
   is `None` is recorded as unknown (`accounting: unknown`). A native judge's cost and tokens
   are reported as usage, with the call count unknown (`None`) because DeepEval doesn't
   expose it.
10. **Version drift.** `prepare()` refuses to run if the installed DeepEval isn't 4.2.5.
11. **Core additions** (additive; schema version stays 1.0.0):
    `EvaluatorManifest.internal_concurrency`, and `UsageReport.calls` can be `None` (unknown).
    When any reported call count is unknown, `resources.model_calls` is `None`.

## Changes after independent review (2026-09-23)

Each finding was reproduced by a failing test before being fixed
(`tests/test_deepeval_adapter.py` and `tests/test_worker_evaluator.py`, "review regressions"):

- **Vacuous scores.** "No claims extracted" and "all retrieved chunks blank" are now
  `not_applicable`; both previously scored an upstream 1.0 or a meaningless 0.0.
- **Startup was charged to the per-case timeout.** A cold DeepEval worker takes about
  15–20 s to start. Now:
  - `Evaluator.ensure_ready()` is called before each case, *outside* that case's budget.
  - Worker restarts after a timeout happen there.
  - `prepare()` and restarts are bounded by `prepare_timeout_seconds` (default 300 s).
  - The per-case `timeout_seconds` covers only evaluation, so timeouts no longer cascade.
- **Relative `--plugin-env` on POSIX.** The interpreter path is made absolute with
  `os.path.abspath`, not `resolve()`, which would follow a venv's symlink to the base
  interpreter.
- **Version handshake.** The worker reports `schema_version` at startup; a mismatch is refused.
- **Usage validation.** Malformed usage from a worker becomes an evaluator error, not a crash
  of the pass. Counts must be non-negative; configured secrets are redacted from provider and
  token-name fields and from validation diagnostics before persistence.
- **Redaction.** Worker error text is redacted *before* truncation, and evidence strings are
  redacted too. The stderr tail is reset per worker.
- **Configuration errors fail early.** A missing `--plugin-secret` value is refused before
  scoring. An unreadable interpreter probe is a clear error. The manifest worker runs in a
  private directory, so a module in the user's project can't shadow the plugin.
  `internal_concurrency >= 1` and `internal_retries >= 0` are enforced.
- **Native judge tokens.** Tokens are reported even when DeepEval's cost is unknown.
- **Follow-up worker-boundary audit.** A worker could report negative token counts, and
  secret-bearing usage strings or malformed-usage diagnostics could reach stored usage/results.
  Both were reproduced with failing tests and fixed. Regression tests:
  `test_negative_worker_token_counts_are_rejected` and
  `test_worker_usage_strings_are_redacted_before_persistence`.

Accepted, documented: an adapter `not_applicable` without judge calls still shows
`accounting: unknown`, because the scorer cannot tell whether calls were made.

## Verification labels

- **Real package:** `tests/test_deepeval_adapter.py` runs the pinned DeepEval in real worker
  processes with deterministic judges (`tests/fixtures/deepeval_judges`) that implement the
  `DeepEvalBaseLLM` contract. No DeepEval code is mocked.
- **Protocol:** `tests/test_worker_evaluator.py` exercises the worker without DeepEval.
- **Live provider:** `test_live_provider_smoke` runs only with `AIBENCH_LIVE_DEEPEVAL=1` and a
  key. It has not been run, because no credentials or budget were authorized.
- **CI:** a `deepeval-plugin` job builds the plugin environment and runs the real-package
  tests on Ubuntu and Windows. No CI result has been observed yet.

## Consequences

- Other framework adapters (Ragas and others, Phase 2) follow the same pattern: a separate
  package, their own environment, and worker execution.
- The worker is process isolation, not a sandbox: plugin code can use the filesystem and
  network (§16).
- Prompt 06 can run evaluation bindings concurrently. Each binding already owns its worker,
  so no evaluator state is shared.
