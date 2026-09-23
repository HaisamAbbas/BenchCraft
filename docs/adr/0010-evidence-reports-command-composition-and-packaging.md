# ADR 0010: Evidence reports, command composition and packaging

Status: Accepted
Date: 2026-09-24
Prompt: 11 — Evidence reports, command composition, and packaging

## Context

§3, §12–14, §17 and §25 require:

- JSON, Markdown and HTML reports that render without rerunning applications or judges. They must show typed metric profiles, every denominator, application versus evaluator failures, defined latency, cost completeness, case evidence and provenance, with no arbitrary overall score.
- Conversational analysis grounded in stored facts.
- The documented command set, with shared exit codes.
- A quickstart a new user can follow.

Before this prompt, `run_report` was a JSON summary that reloaded evaluator plugins to recover metric manifests, so a report depended on the evaluator installation still matching.

## Decisions

### Reports come from stored facts only (11-T1, 11-G1)

- **The builder.** `services/reports.build_report` reads committed records only: the frozen manifest and plan, work items, execution attempts, evaluation attempts, final metric results, run events and artifact references. It never creates a runner, loads an evaluator or starts a plugin environment. A test removes the application and makes evaluator loading raise, and the report still regenerates identically apart from the timestamp. `reporting/render.py` holds pure renderers that never compute a new statistic.
- **Frozen metric profiles.** `create_run` stores each binding's manifest, parameters and decision rule in `RunManifest.parameters.metric_profiles`. A rescoring pass (`aibench evaluate`) records its own profiles in a `scoring_pass` run event. Runs recorded before this prompt fall back to a profile derived from their results (the value kind and direction stored with each result), and the report says so.
- **Denominators.**
  - The engine pass counts every planned (case, repetition) item as selected. `aggregation.summarize(planned=...)` adds a `pending` bucket, so a partial snapshot's coverage is, for example, 2/3, not 2/2.
  - Every item lands in exactly one bucket: completed, evaluator error, cancelled, not applicable, unavailable (no usable app output) or pending.
  - A rescoring pass states its basis: the run's recorded executions.
  - Renderers show every percentage next to its fraction (`8/10 = 80.0%`).
- **Failures.** Application failures (app status and error kinds) are reported separately from evaluator failures (results with status `error`, grouped by reason code). Neither is a low score.
- **Latency** is runner-measured wall time of the final attempt of each successful request, with p50 and p95 taken by nearest rank, so the value is always an observed one. Failed and timed-out requests are excluded from the percentiles and counted separately. The definition is included in the report, along with the plan's concurrency.
- **Cost.**
  - Application cost comes from dispatched attempts. Evaluator cost comes from the pass's committed attempts, retries included.
  - `total_cost_usd` is given only when every call reported a cost. Otherwise the report shows the known amount, labelled a lower bound, with an `accounting` value of `partial` or `unknown`.
  - Planner and conversation usage is per session and is labelled "not attributed to runs".
- **Evidence and raw artifacts.**
  - Evidence lists non-passing items (application failures, and failed, indeterminate or errored results) with sanitized excerpts: outputs and reasons are truncated to 300 characters, retrieved context to 3 items of 200 characters. It also carries evidence references.
  - Raw evaluator outputs stay in the artifact store (restricted). The report carries only their ID, digest, size and redaction class, and a test confirms their content never appears in it.
  - `--no-content` withholds excerpts and per-case metric values while preserving aggregate summaries. The assistant receives case content only when the policy allows it.
- **Escaping.**
  - Every string is passed through `sanitize` (credentials and terminal control content removed), then escaped for the format:
    - HTML entities, with quotes, so values are also safe in attributes;
    - Markdown metacharacters, including `<`, `|`, brackets and `&`.
  - The HTML is static and carries a Content Security Policy (`default-src 'none'`, inline styles only, no forms, no base URI).
  - Tests parse the output and allow only the report's own tags and attributes.

### Release gates and exit codes (§12, §13)

- **Gates in the plan.** `ExecutablePlan.gates` holds predeclared gates on one binding each: `min_pass_rate` and/or `min_completed_coverage`, both computed over selected cases. A lost observation can therefore only fail a gate, never pass it. A gate is decided only for a finished run; a partial snapshot leaves it `undecided`.
- **One exit-code function.** `services.runs.run_exit_code(state, report)` is the single mapping used by `run`, `resume`, `benchmark --auto` and `chat --send`:

  | Code | Meaning |
  |---|---|
  | 0 | Complete, and every gate passed |
  | 1 | Complete, but a gate failed |
  | 2 | Invalid input or plan |
  | 3 | Incomplete; this wins over 1, and both are in the JSON output |
  | 4 | Authorization required |
  | 130 | Interrupted |

  `chat --send` returns the code of a run it started and followed. An action the policy denied exits 4. A blocked or rejected action, or a failed command, exits 2.

### Conversational analysis (11-T2)

- **Tools.** The assistant gets `get_report`, the report's aggregates copied without recomputation (`report_facts`). It also gets `export_report`, which writes only the run's own report under `.aibench/reports/RUN_ID/`, takes no path argument, and runs only when the user's latest message asks for an export and is quoted.
- **Claim checking.** Every number in a final reply is checked deterministically (`check_claims`) against the results queried in that turn:
  - **What can support a number.** A number is supported when it appears as a numeric value, or as a quantity in the text of a string. Digits inside IDs, versions or hashes do not count. A percentage is also supported by a stored rate, or by a ratio of two counts within one record (an object and its direct child objects, such as `passes` and `selected`).
  - **Which source is credited.** Query results are credited before the session state or the user's own words.
  - **Numbers with no support** are listed in the outcome, and the status line names them.

  This check shows where a number could have come from, not that the sentence is right.
- **Hypotheses.** The system prompt requires causes to be labelled as hypotheses, with the case IDs they rest on, and forbids shares of failures that no result contains. An invented statistic ("42% of failures are retrieval problems") is flagged in a test.
- **Partial snapshots.** Results from an unfinished run are labelled a partial snapshot in the status line. Reports use the 09 terms: `provisional` means not finished; `partial` means not everything completed normally.
- **The terminal's `/report`** prints the aggregates with denominators and writes HTML and JSON through the same services as `aibench report`. A ConPTY test compares the two outputs field by field.

### Command composition (11-T3)

- **`init [DIR]`** writes the packaged quickstart. It never overwrites a file, installs nothing and runs nothing. The fixture's interpreter is written as the absolute path of the Python running `init`.
- **`doctor`** checks the following without executing the application or printing a secret: Python and platform, workspace, config, policy, application and CLI executable, dataset, plan (plugin environments start only when the policy permits them), the assistant model config, and whether each `env:` secret reference is set. It exits 0, 2 if something is invalid, or 3 if a prerequisite is missing.
- **`benchmark APP --dataset D`.** In a terminal it opens a new conversation with the app and dataset selected (`--objective` passes through). Headless, it:
  1. drafts a plan with the template planner and validates it;
  2. stops with a machine-readable blocked result (exit 2 or 4) or with `authorization_required` (exit 4) without `--auto`;
  3. with `--auto --policy`, runs within that policy.

  `--auto` refuses `--trust-local-app`, since it grants nothing, and exports the report.
- **`run [DIR|DATASET]`** resolves the plan from the project config's new `plan_path`. A dataset argument must be the plan's own dataset, since a dataset alone cannot identify an application.
- **Other commands.** `report RUN_ID` writes HTML, Markdown or JSON (`--out -` prints to stdout). `plugins list` shows runners, planners, native evaluators and evaluator plugins from metadata, without importing them.
- **`compare`** exists only to say it is not implemented (exit 2, "nothing was compared"). The paired comparison is Prompt 14.
- **`chat --objective`** lets a new session start with an objective without a model, so the quickstart works model-free.

### Packaging (11-T4)

- `aibench.quickstart` ships as package data (`files/*`): a 10-case support dataset, a standard-library CLI app with observable retrieval (one wrong-retrieval case, one application failure), a plan with two gates, a local policy and a config.
- Docs: `docs/quickstart.md` and `docs/support.md` cover platforms, optional evaluators (the DeepEval plugin environment), credentials as `env:` references (no `.env` file), what is supported now and what is not available yet. README links both.

## Earlier-layer fixes

1. **Budget replay counted rescore spend** (06-T2, ADR 0005). `_replay_prior_spend` replayed evaluation attempts from every scoring pass, so after `aibench evaluate`, resuming the run counted the rescore's judge calls against the run's own evaluator budget. It now replays only the run's scoring pass; `run_budget` does the same. Covered by `test_a_rescoring_pass_is_reported_separately_and_is_not_run_spend`.
2. **The no-model message overstated what works** (08-T2 and 09-T3). It said "Commands still work: change the plan", but no slash command changes the plan. It now lists the slash commands that do work, and names `aibench chat --new --objective TEXT` and `aibench plan` as the model-free ways to change a draft.
3. **`/report` dumped the raw machine summary** (09-T3 allowed this until Prompt 11). It now renders the report.

## Changes after independent review

An adversarial review with executed reproductions confirmed 5 major, 7 minor and 5 nit findings, and suspected 4 more. Regression tests are in `tests/test_report_review_regressions.py`.

- **Major:**
  1. **A rescore of a partial run hid missing work.** It scored only the recorded executions, so 2 of 6 cases read as "2/2 = 100%". A rescoring pass of an engine run now selects every planned execution, and those never executed are `unavailable` (reason `not_executed`, via `summarize(missing="unavailable")`).
  2. **`--no-content` leaked evaluator free text.** A reason without a colon was shown whole as its "code", and it was also tallied in `reasons` and `evaluator_failures`. `reason_code` now moves to `reporting.aggregation` and accepts only an identifier-like code; anything else is `unclassified`.
     A follow-up review found structured per-case metric values could also carry case-derived text. `--no-content` now omits all per-case values while retaining aggregate summaries.
  3. **`doctor` printed credentials:**
     - userinfo and the query string in a provider's `base_url` (and an HTTP app URL);
     - pydantic error text quoting a literal key.

     URLs are now shown without userinfo or query, and validation errors as field and error type only.
  4. **The claim check missed invented numbers:**
     - numbers with units (`999ms`, `42s`, `7x`);
     - percentages "verified" by an arbitrary ratio of two counts, or by the integer 1 read as a rate;
     - numbers taken from the user's own question.

     Units are now claims. Ratios need a denominator field (`selected`, `planned`, `recorded`, `total`, `calls`, `successful_requests`, `attempts`), and only floats in [0, 1] are rates. The user's message is no longer a source, and neither are tool error results.
  5. **`chat --send /run` exited 0 when it only showed the plan.** It now exits 4 (authorization required: nothing ran).
- **Minor:**
  - **Runs without an engine pass** (`app smoke` + `score`) now report one named primary pass (the latest) for evidence and the evaluator cost row; before, evidence merged every pass.
  - **`aibench score` now records its `scoring_pass`** (the event moved into `score_recorded_run`), so its profiles are frozen. The "derived profile" note is worded for what it means.
  - **The terminal `/report` never shows missing accounting as zero:** it says "unknown", "no calls" or "not measured". A zero-call pass has no total, and facts include `cancelled`.
  - **`aibench run DIR`** stores the run in `DIR/.aibench`.
  - **`aibench init FILE`** is invalid input (exit 2), not a traceback.
  - **Gate text** in `run` output is sanitized.
  - **The status line** says "results are partial: run R ended cancelled" for a finished run, and keeps "partial snapshot" for unfinished ones.
- **Nits fixed:**
  - a report of a run made outside a plan says "not a plan run" instead of "Plan None";
  - `doctor`'s help states that plugin environments may start;
  - `benchmark` now defaults to `--out benchmark.plan.json`, so it can't replace a project's `plan.json`.
- **Suspected, fixed:**
  - a `min_pass_rate` gate on a binding without a decision rule is now invalid at validation;
  - `export_report` needs an export verb and a report object in the user's words;
  - tool error results are not claim sources;
  - labels come from the primary pass only.
- **Recorded, not changed:**
  - **Rounding beside a gate verdict.** A one-decimal percentage can read "90.0%" beside a FAIL at 0.9. The fraction (8999/10000) and the gate's reason ("pass rate 8999/10000 below 0.9") are exact.
  - **A copied workspace** fails artifact verification, because artifact references are absolute. This is pre-existing storage behaviour and limits report portability; it is recorded, not fixed here.

## Consequences and limits

- **Report shape.** `run_report` now returns the report document, not the old `{metrics, counts, budget}` summary. Callers in this repository were updated.
- **Gates are authored in plan files.** A session's conversational draft cannot declare gates yet, so exit code 1 comes from plan-file runs.
- **Claim checking is lexical.** A correct number in a misleading sentence passes. A number computed from results of an earlier turn is flagged; the assistant must query again in that turn.
- **Unmatched evidence.** Evidence lists only failed, errored and indeterminate items. Passing cases are only in the counts.
- **No live model run.** The conversation and quickstart tests use a scripted provider, which proves the harness side only.
