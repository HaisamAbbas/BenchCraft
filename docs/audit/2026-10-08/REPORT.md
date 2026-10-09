# BenchCraft end-to-end codebase audit

Audit started: **8 October 2026**, continued into **9 October 2026, Asia/Karachi**. Source: **`154fb45a`**, `aibench 0.1.0rc34`. Environment: Windows 11, Python 3.12.10.

## 1. Executive assessment

**The main application works, but I would not yet rely on it for cost-bounded evaluation or automated release decisions.** The clean installed wheel passed all 32 checks across the repository's eight end-to-end journeys. Additional adversarial and cross-workflow probes found failures in data isolation, measurement integrity, rescoring budgets, reproducibility, and scripting behavior.

This audit identifies **19 findings: 8 P1 and 11 P2**. Sixteen concern runtime behavior or data handling, one concerns the failing type-check gate, one concerns the terminal test harness, and one concerns dependency hygiene. Dependency advisories are a risk inventory, not proof of exploitation through BenchCraft.

The most consequential results are:

- A rescore performed **two evaluator calls despite a plan and policy ceiling of one**.
- Carry-forward reused **two old results after the plugin implementation version changed**, made zero new evaluator calls, and attached the new implementation identity to those old values.
- A fixture with **`"app_visible": "false"` reached the evaluated application**.
- A dataset input written as **`NaN` was accepted, became `null`, and executed successfully**.
- An interrupted run resumed after its application source changed and stored **both old and new implementation outputs under one unchanged application identity**.
- A scoring command returned **exit 0 when every evaluation failed**.
- A rescore CLI reported **100% completed coverage**, while the stored report correctly reported **50%** for the same pass.
- Comparison of two fully completed two-case runs reported **20% paired coverage**, using all ten workspace-cataloged dataset cases instead of the selected two.

There are also substantial CLI feature gaps. Several capabilities already exist inside plans or services but lack a practical command interface. Other missing capabilities—particularly load testing, streaming performance measurement, and distributed execution—are extensions beyond the stated MVP and should be planned separately.

Application source was **not modified**. The additions are this report, audit reproduction scripts, and evidence. Scratch projects were created under `.pytest-tmp`; the existing project workspace was not used for benchmark probes. All application/model fixtures were local. No paid model call or hosted evaluation job was submitted.

## 2. Scope and evidence

The source inventory contains **161 Python files and 50,214 lines** across core and plugin source, plus **79 registered command paths**. The root and all command paths were checked with `--help`: **80 successful help checks**. The inventory is an enumeration and structural scan, not a claim that every source line received equal manual scrutiny.

Manual review focused on the CLI, project/config resolution, ingestion and normalization, input/output bindings, runner lifecycle, scheduler, budgets, retries, recovery, scoring, caches, plugin identities, report aggregation, comparison statistics, sessions, conversation controls, experiments, traces, integrations, packaging, installers, and CI. The specification and documented support boundaries were checked against the implemented command surface.

| Check | Result | Evidence |
|---|---|---|
| Fresh wheel build and installation | Passed; installed core is rc34 from `site-packages` | [Clean-install record](evidence/clean-install-e2e.json) |
| Installed end-to-end journeys | **32 passed, 0 skipped**, E2E-01 through E2E-08 | [JUnit](evidence/clean-install-junit.xml), [test output](evidence/clean-install-pytest.txt) |
| Full regression suite with coverage | **1,289 passed, 16 failed, 5 skipped**, 1,310 collected; 64m12s | [Output](evidence/pytest.txt), [JUnit](evidence/pytest.xml) |
| Core statement coverage | **88.21%**, 18,671 / 21,166 statements | [Coverage](evidence/coverage.json) |
| Ruff, `src tests plugins` | **Passed** | [Ruff](evidence/ruff.txt) |
| Mypy, `src` | **Failed: one error**, 140 source files checked | [Mypy](evidence/mypy.txt) |
| CLI help surface | **80/80 passed** | [Help checks](evidence/help-smoke.json) |
| Installed core dependency consistency | **Passed**, no broken requirements | [Pip check](evidence/pip-check.txt) |
| Template planner's 40-fixture benchmark | **17/21 recall = 80.95%**; below the proposed 85% target | [Planner measurement](evidence/planner-fixtures.json) |
| Native evaluator calibration | Executed; labels explicitly **unreviewed** | [Calibration](evidence/native-calibration.json) |
| Independent ConPTY regression recheck | **3 failed, 1 passed**, without coverage instrumentation | [Terminal recheck](evidence/pty-recheck.txt), [JUnit](evidence/pty-recheck.xml) |
| Failure-group recheck in fresh install / normal terminal environment | **21 passed, 0 failed**; includes all 16 originally failing cases | [Recheck](evidence/failure-recheck.txt), [JUnit](evidence/failure-recheck.xml) |
| Fresh installed bootstrap checks | **3 passed** | [Bootstrap](evidence/clean-bootstrap.txt), [JUnit](evidence/clean-bootstrap.xml) |
| Opt-in 1,000-case interruption/resume workload | **Passed**; 64.6s execution and 17.1 MiB peak traced Python allocations | [Workload output](evidence/workload-1000.txt), [JUnit](evidence/workload-1000.xml) |
| Targeted boundary reproductions | Findings and command outputs recorded below | [Probe evidence](evidence/probes.json) |
| Core locked dependency audit | No known vulnerabilities reported for the scanned pins | [Core audit](evidence/dependencies.json) |
| Installed DeepEval environment dependency audit | Six distinct advisory IDs, all concerning its installed `pip` | [DeepEval audit](evidence/deepeval-dependencies.json) |
| Installed Ragas environment dependency audit | Five distinct advisories across five packages | [Ragas audit](evidence/ragas-dependencies.json) |

The initial editable environment reported distribution metadata rc33 while its source imported rc34. The separately built and installed wheel was verified as rc34, so the installed journey result does not depend on that metadata discrepancy.

### Full-suite failure triage

The 16 failures must **not** be presented as 16 established application bugs:

- **One bootstrap failure:** source version rc34 differed from editable-install metadata rc33. Fresh-wheel bootstrap checks passed.
- **Eleven worker failures:** the interpreter could not import `aibench.registry.worker` after the worker's environment/path isolation, before the plugin ran. All affected worker checks passed against the fresh installed wheel.
- **Three ConPTY failures:** the test process inherited `TERM=dumb`, which selects prompt_toolkit's simplified prompt and bypasses the normal composer processors.
- **One color-rendering failure:** the test inherited `NO_COLOR=1`, but expected RGB background ANSI output.

All 16 failing cases, together with related module cases, were rerun in the fresh wheel environment with `TERM=xterm-256color` and `NO_COLOR` unset: **21/21 passed**. This resolves those observed failures for that setup; it does not turn the original run into an all-green full suite or claim that the entire suite was rerun under the normalized environment. The source-level Mypy failure and the independently reproduced audit findings remain.

The five original skips were: a case-sensitive filesystem scenario, unavailable directory symlinks, the opt-in paid live-provider smoke, a TLS certificate test requiring unavailable `openssl`, and the opt-in 1,000-case workload. The workload is exercised separately below. The live provider was not enabled.

The 1,000-case workload was enabled separately against the fresh installed wheel and passed. It interrupted after approximately 400 loopback HTTP requests, resumed, and finished with **1,000 succeeded execution items and 1,000 succeeded evaluation items**. The test verified 1,000 distinct received case IDs and no more than two attempts per case. Execution plus recovery took **64.6 seconds**, with **17.1 MiB peak traced Python allocations**. The complete pytest invocation took 70.03 seconds. This is one cheap local workload at application/evaluation concurrency 16, with tracing enabled; traced allocation peak is not process RSS, and the result is not a production load-capacity claim.

Coverage is statement coverage of the core during the original suite. Several subprocess-only modules show zero coverage because their worker processes are outside this measurement; that is not proof those workers were untested. The focused worker recheck exercised their actual protocols.

### Positive behavior actually checked

- Quickstart initialization, doctor, dataset validation, plan validation, run, run status, and JSON/Markdown/HTML export worked in isolated projects.
- A healthy two-case plan completed with exit 0. The deliberately failing quickstart produced exit 3; this is expected fixture behavior, not a newly discovered defect.
- A plan denied by the conservative policy returned exit 4 before dispatch.
- The installed journeys exercised conversation, steering during execution, recovery, offline rescoring, reference isolation, manual/generated plan equivalence, and repository-aware discovery.
- A local HTTP 307 redirect followed by a timeout was correctly classified as **unknown effect** for an effectful application. This suspected issue was tested and excluded from the findings.
- Strict run comparison, grouped bootstrap primitives, cache provenance, transactional storage, and separate application/evaluator failure categories are substantial existing functionality.
- The subset comparison probe's nonzero coverage verdict was investigated further and reproduced between distinct runs; it is counted as F19.

### Limits of the audit

The current run of this audit covers Windows/Python 3.12. It does not establish Linux, macOS, or Python 3.11 parity. Hosted providers and hosted integrations were not exercised live; deterministic local tests cannot establish their production behavior. No large-volume memory-exhaustion attack or real full-disk incident was created. Existing documentation's capacity measurements are historical; the separate 1,000-case result above is the fresh workload evidence for this audit.

The clean-bootstrap invocation printed a Windows WMI diagnostic (`0x8007000e`) while pytest collected machine information for JUnit. It nevertheless completed with exit 0 and three passing cases. This host diagnostic was not classified as a BenchCraft defect.

## 3. Findings index

**P1:** address before relying on affected workflows for meaningful benchmarking. **P2:** material reliability, usability, statistical, or maintenance issue. No P0 incident was established.

| ID | Priority | Finding | Evidence level |
|---|---|---|---|
| F01 | P1 | Rescoring bypasses runtime budget enforcement | Runtime reproduction |
| F02 | P1 | Carry-forward ignores implementation compatibility and relabels old values | Runtime reproduction |
| F03 | P1 | String false exposes a hidden fixture to the application | CLI end-to-end reproduction |
| F04 | P1 | Resume mixes changed application code under an unchanged identity | Engine/recovery reproduction |
| F05 | P1 | Non-finite dataset input is silently changed during execution | CLI end-to-end reproduction |
| F06 | P1 | Scoring/rescoring returns success for incomplete or failed evaluation | CLI reproduction |
| F07 | P1 | Rescore CLI shrinks selected denominators and inflates coverage | CLI/report reproduction |
| F08 | P2 | Project settings ignore the resolved `project_root` | Config/doctor reproduction |
| F09 | P2 | Headless slash commands lose the configured provider | CLI reproduction |
| F10 | P2 | Malformed input escapes CLI error handling and breaks JSON output | Multiple CLI reproductions |
| F11 | P2 | JSON report export omits the redaction applied to other formats | CLI/report reproduction |
| F12 | P2 | Numeric expected judge repeats are undercounted | Statistical helper reproduction |
| F13 | P2 | Standalone reports use repetition-weighted means instead of default case macro means | Aggregation reproduction and spec |
| F14 | P2 | The current type-check gate fails | Mypy execution |
| F15 | P2 | Some advertised size caps act after the whole response is read | Mock stream reproduction and source |
| F16 | P2 | Plugin environments retain advisory-affected dependencies | Dependency scan; exploitability unproven |
| F17 | P2 | Smoke testing rejects a changed revision of an existing application | CLI reproduction |
| F18 | P2 | Terminal regression tests inherit capability/color flags and report false failures | Original and normalized-environment rechecks |
| F19 | P1 | Comparison ignores selected work and uses workspace-wide dataset cases as its denominator | Distinct-run CLI reproduction |

## 4. Detailed findings

### F01 — Rescoring bypasses runtime budgets

**Location:** `src/aibench/services/runs.py:858`; `src/aibench/services/scoring.py:361`.

`evaluate_run` checks that the declared plan fits the policy, then hands the work to `score_recorded_run`. That service loops over recorded executions without the engine's budget ledger. The plan's runtime call ceiling, wall limit, token ceiling, cost projection, and quota scheduling are not enforced by that loop.

**Observed:** a plan with `max_evaluator_calls: 1`, under a policy whose evaluator ceiling was also 1, rescored two recorded executions. Both completed, and the CLI returned exit 0. See `commands.rescore_budget_one` in the probe evidence. The direct `score` command also uses this scoring service.

**Impact:** a policy-compliant declaration can dispatch more work than authorized. With model-backed evaluators this can increase charges and data egress. The audit used native evaluators; actual paid overspend was not attempted.

**Remediation:** put rescoring under the same dispatch/accounting mechanism as engine scoring, or provide a dedicated rescore scheduler that consumes the same budget/quota abstractions. Count retries, report unknown accounting, and avoid charging carried results as new work.

**Acceptance:** two eligible outputs with a ceiling of one produce no more than one new evaluator invocation; the other item remains explicitly unfinished. Exercise call, wall, token, and soft-cost limits with local deterministic judges.

### F02 — Carry-forward does not verify implementation compatibility

**Location:** `src/aibench/services/scoring.py:552` and `:569`; `src/aibench/registry/__init__.py:333`.

`carry_from` checks case/repetition, metric ID/version, binding hash, status, and execution ID. The binding hash covers semantic metric version, parameters, and rule; it does **not** identify the plugin implementation or dependency environment. `_carried` builds a fresh result using the currently resolved manifest and compatibility identity, then copies the old value into it.

**Observed:** after changing the evaluator plugin version to `999.0.0` while preserving the metric's semantic version and binding, two results were carried forward and the replacement evaluator made **zero calls**. The copied results' provenance named the new plugin version. See `probes.carry_forward_changed_plugin`.

**Impact:** old values can appear to have been produced under a new implementation. This undermines reproducible rescoring and compatibility claims. The source result remains traceable by ID, but that does not make its newly assigned producer identity correct.

**Remediation:** require a complete compatible producer identity before reuse, including implementation/dependency and relevant judge/instrumentation identity. Preserve the actual value-producing identity in carried-result lineage.

**Acceptance:** an implementation or dependency change causes a fresh evaluation or an explicit incompatibility, and never silently stamps a new producer identity on an old value.

### F03 — `"false"` becomes application-visible

**Location:** `src/aibench/datasets/normalize.py:131`.

Fixture normalization uses `bool(entry.get("app_visible", False))`. A nonempty string is truthy, including `"false"`, `"False"`, and `"0"`. This conversion bypasses the typed boolean interpretation downstream.

**Observed:** a dataset containing `"app_visible": "false"` was accepted. A real CLI fixture application received and echoed `AUDIT_ONLY_HIDDEN_SENTINEL` from that fixture. Boolean `false` correctly withheld the same content. See `commands.fixture_leak_e2e` and `probes.fixture_visibility_coercion`.

**Impact:** judge-only fixture content can enter application inputs and contaminate results. This violates an explicit data-isolation boundary.

**Remediation:** validate the flag before coercion. Prefer requiring actual JSON booleans for a trust-bearing field; if string booleans are supported deliberately, interpret them explicitly and reject other shapes.

**Acceptance:** hidden content is absent from stdin, request payloads, argv, and environment unless visibility was explicitly and validly enabled. Invalid flag types fail validation before dispatch.

### F04 — Application source drift is not detected on resume

**Location:** `src/aibench/services/runs.py:131` and `:636`.

Run creation freezes the application **configuration** and hashes that spec. The normal run manifest does not freeze or verify the local application's source identity. Resume reconstructs the runner against the live filesystem. Code identity is computed for execution-cache keys, and experiments have additional checks, but ordinary resumed runs do not have the same guard.

**Observed:** a four-case local application produced `before`, was interrupted, then had its source changed. Resume completed and stored `after` for the remaining cases. The run's frozen application hash stayed unchanged. See `probes.application_code_drift_resume`.

**Impact:** a run may combine different implementations while presenting one application revision/identity. A declared revision only helps if the owner changes it correctly; the harness does not detect the drift in this workflow.

**Remediation:** record and verify source/environment fingerprints where available. Refuse a normal resume on detected drift, or require an explicit new run with parent lineage. For remote applications, require and clearly identify owner-supplied revision guarantees.

**Acceptance:** changing local source or a relevant execution environment between sessions produces a clear refusal/new-run requirement; unchanged implementations still resume without duplicate invocations.

### F05 — Dataset `NaN` is accepted and silently becomes `null`

**Location:** `src/aibench/datasets/ingest.py:215`; `src/aibench/core/models.py:259`; case serialization in `src/aibench/storage/repositories.py`.

The ingestion JSON parser accepts Python's nonstandard `NaN`/Infinity values. Arbitrary nested dataset values permit those numbers. Pydantic's JSON serialization turns non-finite values into `null`, and execution loads the stored case.

**Observed:** `{"case_id":"nan-input","input":NaN}` validated successfully. The in-memory input was NaN, its JSON model representation contained null, and the real run sent null to an echo application and exited 0. See `probes.nonfinite_dataset_input`, `commands.nan_dataset_validation`, and `probes.nan_mutation_e2e`.

**Impact:** the benchmark runs different input from the dataset the user supplied, without a validation error. Exponent overflow such as `1e999` needs the same treatment, not only explicit NaN tokens.

**Remediation:** reject nonstandard constants and recursively reject non-finite numbers during dataset parsing/normalization. Keep failure messages line-specific. Match the stricter application-output parser's treatment of invalid JSON numbers.

**Acceptance:** non-finite values at any depth fail with exit 2 and zero dispatch; valid finite data survives ingest/store/load without semantic change.

### F06 — Evaluation failures return exit 0

**Location:** `src/aibench/cli/score.py:221`; `src/aibench/cli/run.py:266`.

These commands print scoring summaries and return normally after the service returns. They do not translate summary errors/cancellations/unavailable work into the documented incomplete-evaluation exit code.

**Observed:** a deterministic custom evaluator failed on both selected cases; `score --json` returned exit **0** with two evaluation errors. `evaluate` of the quickstart's failed application case also returned 0 with unavailable evaluation. See `commands.score_evaluator_errors` and `commands.evaluate_incomplete`.

**Impact:** CI can pass even though requested checks were not performed successfully. This is distinct from a valid low score and from a deliberately failed quality gate.

**Remediation:** define one outcome function for scoring passes and use it in CLI, headless chat, and conversational actions. Preserve the distinction between infrastructure incompleteness, legitimate not-applicable evidence, low scores, and explicit release-gate failure.

**Acceptance:** all-evaluator-error passes exit 3; mixed failures remain visible; complete valid scoring exits 0 unless a declared scoring gate fails. JSON includes the same outcome and exit code.

### F07 — Rescore summaries shrink denominators

**Location:** `src/aibench/services/scoring.py:399` and its `summarize` call; compare report reconstruction in `src/aibench/services/reports.py:600`.

The service scores only recorded executions and summarizes without a planned count. The report builder separately restores the original planned-execution denominator. Therefore the CLI and the report disagree about the same pass.

**Observed:** a two-case run stopped after one application call. Rescoring returned `selected: 1` and `completed_coverage: 1.0`. The report for that scoring pass returned `selected: 2` and `completed_coverage: 0.5`. See `probes.rescore_denominator_mismatch`.

**Impact:** missing application work makes immediate CLI coverage look better. This contradicts the specification's denominator rule and can mislead automation that consumes CLI JSON.

**Remediation:** use one selected-work definition and one summary implementation for service results and exported reports. Add missing executions as explicit unavailable items or pass the frozen planned count into aggregation.

**Acceptance:** CLI summaries, report JSON, conversation summaries, and comparison coverage agree for interrupted/budget-exhausted runs, including items with no recorded execution.

### F08 — `project_root` is resolved but ignored

**Location:** `src/aibench/cli/chat.py:74`, especially `:85`.

`resolve_config` computes the configured project root, but `project_settings` resolves paths against the config file's parent instead of `resolved.root`.

**Observed:** a parent config with `project_root: "subproject"` and relative app/dataset/plan paths pointed doctor at nonexistent parent-directory files. Doctor exited 2 even though the initialized subproject contained those files. See `probes.project_root_ignored` and `commands.doctor_nested_root`.

**Impact:** configured layouts, including monorepos, break across chat, doctor, and default-plan resolution.

**Remediation/acceptance:** resolve all project-relative settings using the resolved root consistently, preserve explicit absolute CLI overrides, and verify both commands and policy containment for nested layouts.

### F09 — Headless slash commands discard provider wiring

**Location:** `src/aibench/cli/chat.py:628`; compare `src/aibench/tui/app.py:301`.

The interactive terminal supplies provider/judge configuration to `Commands`. The headless `_send` path constructs `Commands(controller, new_session)` without that context.

**Observed:** `chat --provider-config offline.provider.json --send "/cases generate source.md" --json` returned “no assistant model is configured.” The provider was valid and policy-permitted, and no model request was attempted. See `commands.headless_cases_with_provider`.

**Impact:** a command available in the interactive CLI cannot be automated through the documented headless command path. Related provider-dependent commands need the same wiring audit.

**Remediation/acceptance:** construct deterministic commands through one shared factory; verify generation and plugin/judge setup use the selected provider in both entry points, with a local mock recording the expected request.

### F10 — Error handling and JSON failures are inconsistent

**Location:** `src/aibench/tui/commands.py:118`, `:264`, `:475`; `src/aibench/config/resolve.py:34`; `src/aibench/observations/otel.py` parser; root entry point in `src/aibench/cli/main.py`.

Common input errors fall outside the caught domain exceptions. Examples include `shlex`'s unmatched-quote `ValueError`, invalid UTF-8 configuration bytes, and invalid nested OTLP shapes. Several other `--json` failure paths emit only prose on stderr with empty stdout.

**Observed:** unmatched quotes in headless `/compare` and `/cases`, invalid UTF-8 passed to `doctor --json`, and `resourceSpans: [null]` passed to `traces import --json` exited **1 with a traceback**. Missing dataset/plan paths returned exit 2 but no machine-readable error document. See the corresponding `commands.*` records.

**Impact:** normal user mistakes look like internal crashes; scripts cannot parse a stable failure envelope. Verbose tracebacks with local variables are also undesirable around credential-bearing configuration. Audit subprocesses were isolated from real credential variables.

**Remediation/acceptance:** validate command syntax and trace shapes, wrap file decoding/parsing errors in domain exceptions, disable local-variable tracebacks for normal CLI errors, and use a shared JSON error envelope. Expected invalid input should exit 2; denied work should exit 4; stdout should remain valid JSON when requested.

### F11 — JSON report exports bypass redaction

**Location:** `src/aibench/reporting/render.py:25`; `src/aibench/services/reports.py` aggregation/evidence output.

Markdown and HTML sanitize strings while rendering. The JSON branch directly serializes the report. Not every string in the built report has already passed through sanitization—for example, category aggregate keys from custom evaluator values.

**Observed:** a custom category metric returned a deliberately fake `sk-...` credential string. The exported JSON report contained it verbatim; the Markdown report did not. See `probes.json_report_redaction` and the report commands.

**Impact:** export format changes the confidentiality behavior of the same stored results. A report intended for sharing can expose data that another renderer redacts.

**Remediation/acceptance:** apply a common safe-export transformation to report strings and keys before all rendering formats, and test both per-case and aggregate content. Keep numeric values and denominator semantics intact.

**Related risk, distinguished from the defect:** trace import stores original OTLP bytes as a `restricted` artifact. The fake Authorization credential in the audit trace remained in that file (`probes.trace_secret_at_rest`). This is intentional raw-evidence behavior in the implementation, but the restriction label is metadata; it does not itself scrub or encrypt content. Documentation and sharing/export defaults should clearly explain this, and callers should have a sanitized-import/export option.

### F12 — Missing judge-repeat counts collapse to one

**Location:** `src/aibench/reporting/statistics.py:880`.

Without explicit scoring IDs, the code represents missing repeats as `{ "" for _ in range(...) }`. A set contains only one identical empty-string item, regardless of how many repeats are missing.

**Observed:** one observed scoring pass with five expected repeats reported **one missing repeat**, instead of four. See `probes.missing_judge_repeats`.

**Impact:** the numeric-expectation helper underreports missing judge observations. Normal comparison services can pass explicit scoring IDs and avoid this particular branch; this finding does not imply every `/compare` stability result is affected.

**Remediation/acceptance:** preserve missing multiplicity independently of identifiers, and verify 0/1/multiple missing repeats with numeric and explicit-ID expectations.

### F13 — Report averaging does not follow the default case-macro contract

**Location:** `src/aibench/reporting/aggregation.py`, `_value_summary`; specification section 12.

The standalone report mean averages all completed repetition values. The specification calls for case-level macro averages by default. When cases have different numbers of usable repetitions, the current report gives some cases more weight. Comparison statistics already have a case-macro implementation, so the behaviors differ across workflows.

**Observed:** case A had two completed values of 0; case B had one completed value of 1 and one missing value. The report helper returned **0.333333**. Equal weighting of the two case means gives **0.5**. See `probes.report_macro_averaging`.

**Remediation/acceptance:** explicitly define and implement the primary estimand, aggregate repetitions within a case first where required, and retain any repetition-weighted diagnostic under a distinct label. Verify unequal completion counts and grouped episodes.

### F14 — CI's type-check command fails

**Location:** `src/aibench/services/case_pools.py:170`; `.github/workflows/ci.yml`.

`answer_support` requires a string, but `reference.answer` is typed as `str | None`. The code relies on a preceding verification path for runtime safety without narrowing the type for the checker.

**Observed:** `python -m mypy src` produced one argument-type error across 140 source files. See the saved mypy output. This is a confirmed failing engineering gate; the audit did not establish that this specific expression crashes at runtime.

**Remediation/acceptance:** make the invariant explicit or handle the absent answer, then pass the same type-check command used in CI. Keep a candidate with no answer well-defined in review workflows.

### F15 — Some resource limits reject after full buffering

**Location:** `src/aibench/connectors/langfuse.py:126`; streamed-provider error response in `src/aibench/planning/openai_provider.py:182`; trace import in `src/aibench/services/traces.py:32`.

Langfuse's request helper uses a fully buffered `client.request` and checks size afterward. The assistant's streaming error path calls `response.read()` before slicing to the cap. Trace import reads the whole file before parsing or attempting a capped artifact write.

**Observed:** with the Langfuse cap set to 100 bytes and a benign ten-chunk response, the helper read **1,000 bytes before rejecting it**. See `probes.langfuse_response_cap`. The other two paths are source-confirmed; no memory-exhaustion attack was run.

**Impact:** these caps limit accepted/stored content but do not reliably bound read-time memory use. This differs from the main HTTP application runner's incremental body-cap handling.

**Remediation/acceptance:** stop reading at a bounded byte threshold, including error responses and decompressed data; use bounded file reads or explicit size/preflight checks before trace parsing. A response/file beyond the cap should be rejected without consuming its entire body.

### F16 — Dependency hygiene is inconsistent across environments

**Location:** plugin `pyproject.toml` files and the installed plugin environments. Evidence records exact installed versions.

The pinned core/dev requirements had no advisory hits in this scan. The DeepEval environment had six distinct advisory IDs for **pip 25.0.1**, an installation-tool concern rather than an established metric-execution exploit. The Ragas environment had five distinct advisory hits:

| Package | Installed version | Advisory / affected behavior | Disposition |
|---|---|---|---|
| `ragas` | 0.4.3 | CVE-2026-6587, multimodal SSRF | Adapter exposes text faithfulness; affected multimodal path was not demonstrated as reachable. Track the pinned exception. [Advisory](https://github.com/advisories/GHSA-95ww-475f-pr4f) |
| `langchain-openai` | 1.1.9 | CVE-2026-41488, image token-counting SSRF | Fixed at 1.1.14; the current `<1.2` range can permit that release, subject to adapter validation. [Advisory](https://github.com/advisories/GHSA-r7w7-9xr2-qq2r) |
| `langgraph-sdk` | 0.4.2 | CVE-2026-104873, resource auth actions ignored | Scanner lists 0.4.4 as fixed; BenchCraft was not shown to host the affected authorization service. [Advisory](https://github.com/advisories/GHSA-fvww-7h3r-vfhp) |
| `marshmallow` | 3.26.1 | CVE-2025-68480, excessive CPU in `many=True` loading | Scanner lists 3.26.2 / 4.1.2 as fixes; validate a compatible upgrade. [Advisory](https://github.com/advisories/GHSA-428g-f7cq-pgp5) |
| `diskcache` | 5.6.3 | CVE-2025-69872, unsafe pickle when an attacker can write the cache | No fixed version returned by the scan. Review cache ownership and actual usage. [Advisory](https://github.com/advisories/GHSA-w8v5-vhqr-4h9v) |

Raw scanner records contain duplicate advisory entries: **12 rows** for DeepEval's pip and **9 rows** for Ragas packages. Those should not be presented as 21 independent vulnerabilities. The counts above deduplicate advisory IDs within packages.

**Remediation/acceptance:** audit core and every optional environment in CI, maintain compatible transitive locks/constraints, update installation tooling, and document scoped exceptions with affected-path reasoning. A worker process separates dependencies; it is not a security sandbox that automatically makes advisories irrelevant.

### F17 — A changed application revision cannot be smoke-tested in the same workspace

**Location:** `src/aibench/services/execution.py:174`; application catalog storage in `src/aibench/storage/repositories.py`.

Developer smoke commits the application spec unconditionally to a catalog keyed by application ID. That catalog rejects changed content at the same ID. Normal plan-based run creation already accounts for the first-seen catalog entry and freezes its own per-run application artifact, but smoke uses a different path.

**Observed:** after one successful smoke, the same application ID received a new `revision` value and was smoke-tested again. The CLI exited **2**, reporting that the application ID was already committed with a different content hash. See `commands.smoke_changed_revision`. The scratch configuration was restored afterward.

**Impact:** a routine developer workflow—change app configuration/revision, then smoke-test it—breaks unless the user changes the application ID or abandons the workspace.

**Remediation/acceptance:** freeze the smoke run's actual application spec independently of the first-seen catalog, or version the catalog. Both revisions must remain inspectable and score against the correct spec; a new revision must not overwrite old-run provenance.

### F18 — Terminal tests do not isolate their required capability environment

**Location:** `tests/test_cli_chat_pty.py:30`, `_Terminal` environment setup; `tests/test_tui_reply.py:166`.

The terminal tests inherit the calling environment while asserting the behavior of a capable, colored terminal. The audit runner supplied `TERM=dumb` and `NO_COLOR=1`. This selects a simplified prompt without the normal composer processor and disables color output, respectively.

**Observed:** three ConPTY hint checks and one RGB rendering check failed in the original suite. The ConPTY failures repeated without coverage under the same terminal flags. **All passed** when rechecked with a capable terminal environment and color enabled. Therefore the initial apparent rendering regression was excluded as an application defect and retained as a test-harness issue.

**Impact:** valid environment preferences generate misleading regression failures. The test setup needs to distinguish its intended advanced-terminal scenario from an intentional fallback scenario.

**Remediation/acceptance:** give the advanced ConPTY/color tests explicit fixture-local terminal/color settings. Test `TERM=dumb` and `NO_COLOR` separately against their actual fallback contracts. Do not change application behavior to defeat those user environment preferences just to satisfy the tests.

### F19 — Comparison coverage depends on unrelated dataset history

**Location:** `src/aibench/services/comparison.py:1875` and `:1896`, especially the binding-prefix match in `_selected_keys`.

Engine evaluation work keys retain the **hex-only** short binding prefix, for example `824d1148258dcd26`. Comparison tries to match that prefix against the full binding hash, `sha256:824d1148...`, without removing the algorithm prefix. The match fails. Comparison then falls back to all available cases in the workspace's dataset catalog instead of the run's selected work.

**Observed:** the workspace first ran the full ten-case dataset, then ran a healthy plan selecting only two cases. A second distinct two-case run completed normally. Comparing those two fully completed runs reported **two complete pairs / ten required selected pairs = 20%**, failed the 95% coverage gate, and exited 1. Both runs had exactly two planned execution items. See `commands.compare_subset_distinct` and `probes.comparison_selection_denominator`. The same-run check originally exposed the same symptom.

**Impact:** prior runs change whether a later limited/sample benchmark can qualify, even when its selected cases all completed. Comparison fabricates coverage loss for cases that were never selected. This is the opposite denominator error from F07.

**Remediation/acceptance:** normalize hash prefixes before matching, preferably share the engine's work-key parser, and derive selected pairs from the frozen run selection/work graph. Two completed selected cases should report 100% coverage whether or not a prior run cataloged additional dataset cases. Test limits, samples, predicates, repetitions, and partial runs in a reused workspace.

## 5. Missing and incomplete CLI capabilities

This application is primarily an **AI application evaluation CLI**. Its own specification defines that scope. Warmup, throughput, and arrival-rate controls are useful if it also aims to be a performance benchmark CLI, but a full load generator is a distinct product commitment.

For context, established tools expose practical scriptable controls: Hyperfine documents warmup, repeated measurements, preparation/cleanup, parameter scans, and exports; LM Evaluation Harness documents seeds, example limits, sample logging, and batch controls; pytest-benchmark documents historical comparison and regression-failure thresholds. These are reference points, not claims that BenchCraft should copy every feature. [Hyperfine](https://github.com/sharkdp/hyperfine), [LM Evaluation Harness CLI](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/interface.md), [pytest-benchmark comparison](https://pytest-benchmark.readthedocs.io/en/latest/comparing.html).

**Status meanings:** missing = no implemented user-facing capability found; partial = relevant capability exists but the described workflow is incomplete; deferred = explicitly outside current MVP/support scope. Priorities below are product recommendations, separate from defect severity.

| ID | Priority | Capability | Current state and concrete missing behavior |
|---|---|---|---|
| G01 | P1 | Consistent global configuration and scripting options | **Missing/partial; specified.** Root options do not provide global `--config`, `--json`, `--non-interactive`, or policy selection. Equivalent controls are scattered among commands. |
| G02 | P1 | Stable machine-output API | **Partial.** Successful commands often support JSON; failures, envelope/schema, exit details, and event output are inconsistent. F06/F07/F10 demonstrate the consequences. |
| G03 | P1 | Regression policies for CI | **Partial.** Absolute release gates and compatible run deltas exist. `compare` exits for compatibility/coverage, not a configured degradation in score, latency, or cost. Add predeclared regression tolerances and fail rules. |
| G04 | P1 | Complete case-result export | **Partial.** Report JSON contains summaries and non-passing evidence. There is no general headless export of every passing/failing case and repetition to JSONL/CSV, with explicit content controls. |
| G05 | P1 | CI report adapters | **Missing.** Benchmark-result JUnit/SARIF-style exports are absent. The repository's pytest JUnit files are test evidence, not a user feature. |
| G06 | P1 | Named baselines, tags, and searchable run history | **Partial.** Run IDs, status filters, and a limit exist. Baseline aliases, run tags/notes, metadata search, pagination, and promotion of an approved baseline are missing. |
| G07 | P1 | Convenient bounded run controls | **Partial.** Plans support selection, repeats, concurrency, retries, timeouts, budgets, caches, and quotas. `run` largely requires editing a plan file. Add validated direct overrides and a dry-run that prints the exact frozen scope. |
| G08 | P1 | Reproducibility controls and provenance | **Partial.** Selection/bootstrap seeds and frozen configuration exist. The run CLI does not expose the engine's `run_seed`; local source/dependency/commit provenance is incomplete, as F04 shows. |
| G09 | P1 | Release gates in conversation drafts | **Missing; documented limitation.** Gates can be authored in executable plans but not declared through session draft choices/patches. |
| G10 | P2 | Retry only failed application work | **Implemented.** `runs retry` creates a bounded child from safe final application failures or explicit parent-scoped cases on finished runs; it rechecks current/frozen policy, dataset identity, and frozen test-world seed, previews with `--dry-run`, and records lineage in run manifests and reports. |
| G11 | P2 | Headless run supervision | **Deferred/partial.** Status exists; live pause/stop controls belong to the owning terminal/session. Detached execution and durable external pause/resume/cancel requests are absent. |
| G12 | P2 | Configuration management | **Partial.** Config files, setup, and doctor exist. There is no `config show/validate/set` workflow showing effective values and source precedence with secrets redacted, or named provider profiles. |
| G13 | P2 | Structured progress and durable logs | **Partial.** Terminal progress and persisted run events exist. Scripts lack a consistent JSONL event stream, log-file selection, verbosity/quiet controls, and documented noninteractive logging contract. |
| G14 | P2 | Warmup and measurement phases | **Missing; performance extension.** No bounded warmup count or exclusion phase before latency sampling. Warmup effects and spend need to be accounted explicitly. |
| G15 | P2 | Richer performance statistics | **Partial.** Successful final-attempt p50/p95 and failure counts exist. User-facing throughput, p99, dispersion, retry-inclusive elapsed latency, and cold/warm separation are absent. |
| G16 | P2 | Streaming model performance | **Missing; performance extension.** Application benchmarking records JSON responses, not streaming token events. TTFT, inter-token latency, output tokens/sec, and streaming completion integrity are unavailable. Assistant reply streaming is already implemented and is a separate capability. |
| G17 | P2 | Segment/slice analysis | **Partial.** Case predicates and independent grouping for comparisons exist. Reports lack general summaries by metadata segment, customer, language, difficulty, or model configuration. |
| G18 | P2 | Dataset utilities and versioned suites | **Partial.** Validation, candidate review/promotion, episodes, and content hashes exist. General schema-aware CSV/JSON/Parquet import, diff/dedup/split tools, and a named suite catalog are missing. |
| G19 | P2 | Workspace maintenance and portability | **Partial.** Migrations and an artifact GC library exist. CLI backup/restore, portable run/session bundles, workspace integrity/repair, retention/storage usage, and safe GC commands are missing. Absolute recorded paths complicate migration. |
| G20 | P2 | Judge repeat/calibration workflow | **Partial.** Statistical primitives, native calibration, and comparisons exist. A practical command that repeats/calibrates the configured model judges against reviewed labels is incomplete; F12 affects a helper branch. |
| G21 | P2 | Platform and release confidence | **Partial.** Windows/Linux CI is configured; macOS CI is absent, current cross-platform results were not observed, and published support/readiness documents still center on older candidates. |
| G22 | P3 | Advanced scale, load, and modality workflows | **Deferred.** Distributed workers, a scalable work queue, open-loop/ramping load profiles, multimodal evaluators, and a dashboard are product extensions. Ordinary concurrency and RPS quotas already exist. |

Shell help/completion, terminal history, multiline input, persistent sessions, pause/resume/stop, retries, sampled case selection, HTML/Markdown/JSON reports, stored-output rescoring, run comparison, agent outcomes, multi-turn episodes, experiments, trace import, caches, plugin isolation, and installation commands are **already present**. Treating these as wholly missing would misrepresent the repository.

Whole-workflow cost attribution is another incomplete reporting area: planning/conversation usage is tracked per session and is not attributed to a run's cost report. Application/evaluator spend completeness already exists. A future total-budget/total-cost workflow should distinguish these roles rather than presenting only application and judge spending as the entire cost of producing a benchmark.

### Suggested future command surface

The following is a **proposal**, not runnable documentation for the current release:

```text
benchcraft --config benchcraft.json --json --non-interactive run --plan plan.json
benchcraft run --plan plan.json --dry-run --limit 100 --repetitions 3 --seed 42
benchcraft compare main-baseline RUN_ID --fail-on-regression regression-policy.json
benchcraft runs export RUN_ID --format jsonl --include-passing
benchcraft runs cancel RUN_ID
benchcraft config show --effective --redacted
benchcraft workspace check
```

Every override should be validated and frozen in the run's identity. Exports and event streams should have versioned schemas. Commands that dispatch work must use the same policy/accounting path as the existing engine.

## 6. Architecture and maintainability

The package has sensible broad boundaries: immutable domain models, separate runners/evaluators, SQLite repositories, content-addressed artifacts, and CLI/TUI surfaces over shared services. That architecture is worth preserving.

The failure pattern is **inconsistent guarantees across workflow paths**:

- The engine enforces budgets; rescoring does not.
- Report reconstruction preserves planned denominators; immediate scoring summaries do not.
- Comparison's binding-prefix mismatch substitutes workspace dataset history for selected run work.
- Comparison builds detailed compatibility identities; carry-forward tests a smaller identity.
- Application output parsing rejects non-finite JSON; dataset parsing accepts it.
- Interactive commands receive provider context; headless commands do not.
- Markdown/HTML sanitize exports; JSON assumes the report was already safe.

These should become shared contracts with direct cross-entry-point tests. Adding more parallel orchestration paths will make this harder to maintain.

Large source modules also increase review cost: comparison is 2,894 lines; conversation agent 2,102; session controller 1,668; repositories 1,547; experiments 1,474; models 1,405; statistics 1,087; engine 1,085; scoring 1,032. Size alone is not a bug. Extract cohesive policy/identity/outcome/aggregation helpers when fixing the affected workflows, rather than performing a broad rewrite first. See [inventory](evidence/inventory.json).

Documentation needs a current consolidated support/readiness snapshot. `docs/support.md`, the platform matrix, upgrade examples, and release-readiness history contain useful evidence but prominently name rc1 and earlier phases while the current source is rc34. Readers need one current statement of supported commands, tested versions, open defects, and deferred scope.

### Planning and measurement quality

The template planner benchmark was rerun against the current 40-fixture set. Concept selection precision was **17/17**, recall **17/21 = 80.95%**, gap precision **22/28 = 78.57%**, and gap recall **22/22**. Recall remains below the specification's proposed initial 85% engineering target. This supports a concrete planning-quality concern, particularly for objective wording the template does not recognize; it is not a measured live-assistant failure rate. See [current planner evidence](evidence/planner-fixtures.json).

Native calibration was also rerun. Exact match agreed with **10/13** fixture labels, rejected **3/7** labeled-positive examples, and was repeat-stable on **13/13**. Exact match cannot be assumed to assess semantic correctness of paraphrases. The calibration record explicitly says its labels are unreviewed; these numbers do not establish general evaluator quality or show that the exact-match implementation itself is broken. Reviewed labels and a task-appropriate metric choice remain necessary. See [calibration evidence](evidence/native-calibration.json).

## 7. Recommended remediation sequence

| Order | Work package | Findings / features | Completion criterion |
|---|---|---|---|
| 1 | Protect dataset and application boundaries | F03, F05 | Strict validated visibility and JSON data survive storage without leakage or mutation; zero-dispatch rejection tests pass. |
| 2 | Make scoring and comparison obey selected-work contracts | F01, F06, F07, F19 | Shared budgets, consistent selected denominators, and meaningful exit codes across `score`, `evaluate`, `/rescore`, comparison, and headless chat. |
| 3 | Preserve actual producer identity | F02, F04 | Reuse requires compatible identities; resumed source drift is detected; lineage names the implementation that produced the value. |
| 4 | Repair CLI and report consistency | F08–F11, F17 | Nested projects and changed application revisions work; provider wiring is shared; malformed input yields stable errors; exports apply the chosen content policy. |
| 5 | Repair statistics and engineering gates | F12–F16, F18 | Correct repeat counts and averaging; bounded reads; consistent installation/terminal test fixtures; current type/lint/tests pass; dependency exceptions are scoped and reviewed. |
| 6 | Make the CLI effective for CI | G01–G09 | Configured headless operation, regression policies, all-case exports, baseline management, direct validated controls, reproducible runs. |
| 7 | Improve operational workflows | G10–G21 | Selective reruns, supervised execution, configuration tools, logs, slicing, portable workspaces, current platform evidence. |
| 8 | Expand performance/scale scope deliberately | G14–G16, G22 | Published measurement definitions and local validation for each claimed performance/scale capability. |

Do not put performance polish ahead of data isolation and result correctness. A benchmark that runs quickly but changes inputs, ignores a cost limit, or stamps the wrong evaluator identity produces untrustworthy evidence.

## 8. Required verification before treating this candidate as reliable

1. Add meaningful regressions for the confirmed reproductions, exercising actual input envelopes, invocation counts, stored identities, CLI JSON, and exit codes.
2. Run equivalent fixtures through engine scoring, direct scoring, plan-based evaluation, headless chat, and interactive controls. Compare their selected counts, coverage, budgets, and failure semantics.
3. Test source/dependency changes between interrupted sessions and scoring passes. Require drift refusal or explicit new-run lineage.
4. Verify error and export contracts with nested data, non-finite numbers, unmatched quotes, malformed UTF-8/OTLP, arbitrary category values, and fake credentials.
5. Pass fresh-install journeys plus the full relevant suite, Ruff, and Mypy on the supported CI matrix. Add macOS only when its support claim is backed by execution.
6. Audit optional plugin environments independently; validate compatible dependency upgrades against actual pinned evaluator packages and deterministic judges.
7. Separately obtain live-provider and real-team validation before claiming live integration reliability, billing-limit accuracy, or planner/judge quality. Historical scripted-fixture recall and calibration are not substitutes for reviewed labels and real usage.

## 9. Reproduction and artifact guide

Run from `D:\BenchCraft` using the development interpreter, in this order:

```powershell
.venv/Scripts/python docs/audit/2026-10-08/audit_probes.py
.venv/Scripts/python docs/audit/2026-10-08/extended_probes.py
.venv/Scripts/python docs/audit/2026-10-08/recovery_probes.py
.venv/Scripts/python docs/audit/2026-10-08/statistics_probes.py
.venv/Scripts/python docs/audit/2026-10-08/additional_probes.py
.venv/Scripts/python docs/audit/2026-10-08/comparison_probes.py
```

The scripts create local scratch projects and record observed behavior in [probes.json](evidence/probes.json). They are audit reproductions of this candidate, not permanent release tests: after implementation fixes, some recorded behavior or assumptions should change. The original audit command fixture was corrected before the evaluator-error finding was recorded; the final scripts and JSON contain the corrected fixture/results.

The installed journey suite was run with:

```powershell
.venv/Scripts/python scripts/e2e_suite.py --out .pytest-tmp/audit-2026-10-08-clean
```

Relevant standalone records:

- [Full probe evidence](evidence/probes.json), with exact CLI argv, exit codes, stdout, stderr, parsed JSON, and individual boundary results.
- [Source/command inventory](evidence/inventory.json) and [help smoke](evidence/help-smoke.json).
- [Installed journey report](evidence/clean-install-e2e.json), including wheel checksum, actual import path, per-journey checks, and durations.
- [Full suite output](evidence/pytest.txt), [Ruff](evidence/ruff.txt), and [Mypy](evidence/mypy.txt).
- [Failure-group recheck](evidence/failure-recheck.txt) and [machine-readable summary](evidence/summary.json).
- [1,000-case workload](evidence/workload-1000.txt).
- [Findings CSV](findings.csv) and [feature backlog CSV](feature-backlog.csv).
- [Core dependency audit](evidence/dependencies.json), [DeepEval environment audit](evidence/deepeval-dependencies.json), and [Ragas environment audit](evidence/ragas-dependencies.json).

## 10. Decision

**Keep the architecture and existing working journeys, but address the eight P1 findings before treating benchmark outcomes and budgets as dependable.** The immediate product work should make headless benchmarking and CI regression decisions complete and consistent. Advanced performance and scale features should follow a defined scope and evidence plan.
