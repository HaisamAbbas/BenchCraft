# BenchCraft user journey and scenario map

**For review:** product owner feedback
**Snapshot date:** 2026-09-25
**Product shape:** local-first, conversational CLI for evaluating AI applications
**Scope note:** this describes the repository's implemented contract and tested boundaries. It is not a promise that every runner, provider, language, or external integration has been validated in production.

## What the product is for

A developer or evaluation owner brings an AI application, evaluation cases, and a question they want answered. BenchCraft helps them inspect what the configured application can expose, choose checks only when the necessary evidence exists, run a bounded benchmark under an explicit policy, and review stored case-level evidence.

The core promise is:

> Turn a concrete evaluation goal into a policy-bounded run, then explain what the run observed, what it could not measure, and where each conclusion came from.

The product is a terminal application. Natural-language conversation is optional: slash commands and headless commands remain usable without an assistant model. BenchCraft does not autonomously edit application code, change deployment settings, or expand policy.

## Journey at a glance

~~~mermaid
flowchart TD
    A[Choose a starting path] --> B{What do you have?}
    B -->|Local demo| C[Initialize example project]
    B -->|Application repository| D[Inspect approved files and config]
    B -->|JSON HTTP endpoint| E[Configure a bounded API runner]
    C --> F[Doctor and validate]
    D --> F
    E --> F
    F --> G[Profile app and summarize cases]
    G --> H[State evaluation goal]
    H --> I{Evidence and scope sufficient?}
    I -->|No| J[Show gap or ask one material question]
    J --> H
    I -->|Yes| K[Draft and validate typed plan]
    K --> L{Policy, budget, invocation safe?}
    L -->|Denied or ambiguous| M[Explain blocker; make no forbidden call]
    L -->|Allowed and request is clear| N[Run without redundant confirmation]
    N --> O[Query progress, pause, resume, or stop]
    O --> P[Stored cases, traces, scores, report]
    P --> Q[Diagnose, rescore, or compare]
    Q --> R[Optional: reviewed data or bounded experiment]
~~~

The same planning, policy, runner, storage, scoring, and report services are used by conversational and headless paths.

## The primary user journey

### 1. Install and choose a project

A first-time user can install a supplied wheel or run from a repository clone. The current package is a local release candidate, not a published package-index release. The quickstart creates a self-contained support-assistant project:

~~~powershell
aibench init support-bench
cd support-bench
aibench doctor
~~~

The project includes a project config, fixture app, app binding, ten-case dataset, executable plan, and conservative policy. Initialization does not install packages, run the app, or overwrite existing files. Doctor checks configuration, referenced files, dataset, plan, interpreter, workspace, and whether referenced secrets are set; it does not print secret values.

The sample deliberately contains two different problems:

- **support-004:** a wrong answer caused by retrieving the shipping passage for a warranty question.
- **support-010:** the application fails. This is recorded as an app failure, not as a low evaluator score.

This makes the first report demonstrate both quality and reliability evidence.

### 2. Connect the application and understand what can be measured

There are three common entry paths.

#### A. Use the provided example

The example app and plan are already configured. A user can start at conversation and inspect the draft.

#### B. Bring an application repository

The user supplies an app configuration and asks for an evidence-backed profile, for example:

~~~powershell
aibench inspect app.json --source . --dataset cases.jsonl --policy policy.json --json
~~~

If the session policy permits the project root, a fresh chat can use bounded static repository findings alongside the configured app profile. Static inspection:

- Reads only supported files within approved roots and configured size/count/depth budgets.
- Recognizes selected Python and JavaScript/TypeScript patterns and a bounded set of manifests.
- Records path/line evidence, provenance, confidence, and caveats.
- Labels static source clues as inferred; unsupported languages and patterns stay unknown.
- Treats source text, test names, and repository instructions as untrusted data. They cannot grant permissions or authorize a run.
- Does not import or execute repository code.

An explicit probe is a separate action. The inspect command with the probe option invokes the configured runner on up to N dataset cases under policy; it is not static inspection and can have application effects.

Repository inventory can find candidate dataset files, test/evaluator files, and plausible invocation paths. Those are clues, not proof: arbitrary tests are not promoted to goldens, and imports do not prove runtime retrieval. Dataset discovery validates bounded JSONL shape and reports paths/content identity/field counts rather than exposing case values in the profile.

#### C. Connect a JSON HTTP API without its repository

When the application is only reachable as a JSON API, the user can set up an explicit runner:

~~~powershell
aibench connect http --project support-api-bench --url http://127.0.0.1:8765/answer --dataset cases.jsonl --app-id support-api --effects none
~~~

For a remote HTTPS origin, setup requires authorization for that exact origin. A bearer credential is configured as an environment secret reference, not pasted into a project file. Setup validates the local dataset and writes typed application, policy, and project configuration; it makes **no API request**.

The request and response bindings must be configured explicitly. The default sends a case's input at /question and reads the answer from /answer. The caller declares effects (none, reversible, or irreversible) and a hard call ceiling. Later execution uses normal policy, budgets, retries, timeouts, cancellation, evidence, and report services.

This path is for a configured JSON API. It does not discover API schemas, crawl a website, infer arbitrary URLs, or control a browser.

### 3. Provide evaluation cases and choose a dataset deliberately

A dataset is JSONL with unique case IDs and application inputs. It can also contain reference answers, expectations, labels, group IDs, and other typed evidence. The user can validate or summarize it:

~~~powershell
aibench dataset validate cases.jsonl
aibench inspect app.json --dataset cases.jsonl
~~~

Reference answers and judge-only fields are isolated from application inputs. The app receives only its configured input binding. A reference is available to an evaluator only when that evaluator's typed binding allows it.

For a new repository-aware session:

- A configured dataset or explicit dataset option takes precedence.
- If exactly one compatible, policy-approved dataset is found, it can be reused transparently.
- If several materially different candidates are found, the user chooses; BenchCraft does not silently pick one.
- Synthetic, unreviewed candidates are not treated as trusted evaluation cases.
- File shape and field presence do not prove that references are correct or that the cases represent production.

### 4. State the goal and review opportunities

A user can work conversationally:

~~~text
I want to check that answers are correct and that retrieved passages support them.
~~~

Or use a headless planner:

~~~powershell
aibench plan opportunities --app app.json --dataset cases.jsonl --objective "answers are correct" --json
~~~

BenchCraft maps goals to metrics only when the required observations and inputs exist. Examples:

- Exact-match correctness needs an answer and a reference.
- JSON-schema checks need a declared schema and structured output.
- Retrieval-grounding checks need the application's actually retrieved context and an allowed judge configuration.
- Tool-call and final-state checks need the app or test world to emit those structured observations.
- Internal retrieval, token usage, or tool behavior that was not observed stays unavailable. A source import or a reference passage is not substituted for missing runtime evidence.

The proposal shows the chosen metric and rationale, applicable-case coverage, unavailable evidence, and other gaps. It does not treat test suites as goldens or pretend unknown cost is zero.

### 5. Resolve only material ambiguity and validate the plan

When a user request leaves out a necessary choice, BenchCraft asks a focused question instead of guessing. Typical questions include:

- Which of two materially different datasets should be used?
- What should “quality” mean for this request?
- Which case count or subset should the existing plan use?
- Which invocation path should run when the repository suggests more than one?
- Is a required evaluator/provider allowed by policy?
- What output field or expected evidence is missing?

The typed draft records objectives, selected metrics, dataset and app identity, selection/seed, budgets, evaluator configuration, gates when supplied by a plan file, and pending questions. The plan must validate before execution.

A clear, bounded user request starts under the existing policy without a second “approve/run?” question. An explicit /run command also starts the current validated draft and shows its preview. A plan-only request does not run the application. A real policy denial, missing required evidence, unsafe or ambiguous invocation, or budget violation blocks the action and explains why.

The assistant cannot use repository text, dataset text, or model output to add tools, grant a permission, or broaden the plan beyond its typed contract.

#### Illustrative conversation

This hypothetical example shows the intended shape of a successful, unambiguous turn sequence; actual findings and wording depend on the user's repository:

~~~text
User: Check whether answers are correct on the first 10 cases.
Assistant: I found the configured app and one policy-approved 10-case dataset. Eight cases have references, so exact-match is available for those 8. Retrieval grounding is unavailable because this app does not expose observed retrieved passages. I drafted a 10-case plan.
User: Run that evaluation now.
Assistant: Starting the validated plan under the project's current policy. [run ID and progress]
User: /status
Assistant: [stored progress for the same run]
User: Why did case-004 fail?
Assistant: The stored case shows that the app returned a shipping passage for a warranty question. That is the observation; a retrieval-ranking issue is a hypothesis.
User: Rescore using the stricter rule in other.plan.json.
Assistant: Rescored the stored outputs as a new scoring pass. The application was not called again.
~~~

If the exact dataset, objective, required binding, or invocation route differs from the draft, the assistant should ask about that specific choice before execution.

### 6. Run and monitor the benchmark

The run uses the configured and validated runner. Supported runner families include:

- CLI/subprocess JSON protocols;
- HTTP JSON APIs with explicit bindings;
- Python callables;
- containers, with constrained execution settings;
- OpenAI-compatible application endpoints, where returned tool calls are recorded observations and are not executed by BenchCraft.

Local programs run with the user's OS permissions; a subprocess is not automatically a security sandbox. Container mode is constrained and offline by default but shares the host kernel. The policy controls application/evaluator IDs, approved roots/origins, secrets, effects, call budgets, evaluator permissions, and related limits.

During an interactive run, the user can ask for progress or use controls:

| User action | Result |
|---|---|
| /help | Shows the available conversational commands |
| /plan | Shows metrics, gaps, missing inputs, coverage, and estimates |
| /run | Starts the validated draft and shows its preview |
| /status | Reads committed status without a model call or run reset |
| /pause | Stops new dispatch; in-flight work settles under the runner contract |
| /resume | Continues eligible unfinished work for the existing run |
| /stop | Cancels further dispatch and preserves completed evidence |
| /failures | Lists failed and errored results for the current run |
| /case CASE_ID | Shows stored evidence for one case |
| /budget | Shows ceilings, committed spend, and unknown accounting |
| /app | Explains the configured runner, observations, evidence gaps, resets, and test worlds |
| /world NAME or /world none | Selects an application-declared test world and creates a new draft |
| /report | Shows a report from stored facts |
| /compare BASELINE CURRENT | Compares stored runs without rerunning the app |
| /integrations | Shows integration modes, destinations, and availability |
| /sessions | Lists sessions in the project |
| /new | Starts a fresh session |
| /exit | Leaves the terminal; active work remains resumable |
| Ask a follow-up question | The active run remains queryable; the question does not start a duplicate run |
| Close and reopen | Restores session and run state; does not automatically restart execution |

A run is bounded by its validated selection, call limits, wall-time limits, policy, and applicable quotas. A secret reference must be present in the environment and approved by policy before use. Denial prevents the corresponding app, evaluator, or external call.

For stateful or multi-turn applications, the app can declare reset boundaries (per_case, per_episode, or shared), named test worlds, and structured tool/world observations. Stateful execution is more constrained: for example, episode calls are not freely retried into unknown state, and failed/interrupted episodes can block later turns.

### 7. Inspect failures and evidence

After or during execution, the user can ask:

- /failures — list failed or errored cases.
- /case CASE_ID — inspect stored inputs/outputs and available evidence for one case.
- /report — summarize results and gaps.
- /app — explain declared observations, missing evidence, resets, and test worlds.
- /budget — inspect ceilings, committed spend, and unknown accounting.
- /integrations — inspect integration modes, destinations, and policy/credential availability.

Analysis distinguishes:

- **Observation:** recorded app output, retrieved context, tool event, evaluator outcome, trace field, or run state.
- **Hypothesis:** a possible explanation inferred from observed cases.
- **Unavailable / not applicable / error / pending:** distinct result states, never silently converted to a pass or zero.

Failure explanations cite stored case evidence. A proposed next experiment should target a demonstrated gap; it is a recommendation, not automatic code repair.

### 8. Read, export, rescore, and compare results

Reports are rebuilt from stored records; report generation does not rerun the app or judge. They can be emitted as JSON, Markdown, static HTML, JUnit XML, or SARIF 2.1.0, and include:

- run, app, policy, dataset, evaluator, and seed provenance;
- metric values with selected/completed/pass/fail/error/not-applicable/unavailable/pending denominators;
- release-gate outcomes over selected cases;
- app failures and successful-request latency definitions;
- cost and completeness, with unknown cost shown as unknown;
- sanitized case excerpts and artifact references;
- partial-snapshot labeling for an unfinished run.

Use the no-content option to withhold case excerpts and per-case values while keeping aggregates. Export from conversation writes the current run's report to the product-managed report location; users do not supply an arbitrary write path to the assistant.

Stored outputs can be rescored with a different compatible plan or evaluator:

~~~powershell
aibench evaluate RUN_ID --plan other.plan.json
~~~

Rescoring creates a separate scoring pass under the same run identity and does not call the application again. Run comparison also reads stored evidence only. Strict comparisons enforce run/metric/evaluator/judge/instrumentation compatibility and paired-case coverage; exploratory comparisons are labeled non-qualified. Framework scores are shown separately rather than averaged into a false common scale.

### Headless and project-management path

The same services can be driven from scripts or CI. The main command groups are:

| Task | Commands and behavior |
|---|---|
| Initialize and check a project | init, doctor |
| Inspect an app/repository or run a small smoke | inspect, app describe, app smoke |
| Validate data, episodes, and plans | dataset validate, episodes validate, plan validate, plan opportunities |
| Draft and execute | plan, benchmark, run, resume, runs status/retry |
| Read run history | runs list/show/status/retry; sessions list/show/delete |
| Score and report | score, evaluate, report, compare |
| Inspect evaluator availability | evaluators list/describe/plugin; plugins list |
| Candidate-data lifecycle | dataset candidates generate/list/show/review/verify/promote |
| Controlled experiments | experiments create/run/resume/status/report/evaluate-holdout/propose-adoption |
| Traces and performance controls | traces import/show; cache list/clear |
| External evaluation/data connectors | openai-evals-oss, openai-evals-api, langfuse, integrations list |
| Maintainer evaluation of planning/judges | plan benchmark and evaluators calibrate; fixture annotations are not human-reviewed gold standards |

The one-command benchmark path can draft, validate, and optionally run a plan. A missing objective or denied permission produces a visible error instead of an invented choice or implicit policy grant. Exit codes distinguish complete/pass, complete/gate-fail, invalid, incomplete, authorization-denied, and interruption cases. See the quickstart for the exact exit-code table and output formats.

### Representative resource limits

These limits are guardrails, not performance promises. Project policy and command options can impose lower limits.

| Workflow | Current documented bound |
|---|---|
| Repository inventory | At most 5,000 walked files, 256 KiB per parsed file, 16 MiB total read, depth 32, and 2,000 discovery records, with directory/entry caps also applied |
| Dataset candidate discovery | At most 32 candidate JSONL files, 8 MiB per file, and 32 MiB total |
| Generated candidate cases | At most 50 Q/A candidates from at most 8 UTF-8 text files, with 512 KiB selected source text |
| HTTP project setup | Default 20 application calls, configurable up to 1,000; setup does not contact the endpoint |
| Controlled experiment search | Deterministic grid of at most 128 parameter combinations; a separate per-execution trial budget applies |

## Optional and advanced journeys

These are separate workflows layered on the same policy, evidence, and storage boundaries. They are not required for a first local benchmark.

### Use evaluator frameworks

The core includes native exact-match and JSON-schema checks, plus trusted custom Python evaluators. Optional DeepEval and Ragas adapters run in separate plugin environments; model-backed judges require allowed origins and secret references and may incur provider charges. Only supported metrics and bindings are exposed.

Other integration paths include:

- **OpenAI Evals OSS:** selected replay/live-bridge evaluation types in an isolated environment; no hosted service call for the OSS framework.
- **OpenAI Evals API:** submit recorded inputs/outputs/references for remote grading, inspect status, resume/cancel, and fetch results. This sends evaluation data to the approved external origin and is not a metric that can be silently added to a normal plan.
- **Langfuse:** explicitly import datasets or traces and export eligible recorded scores. It moves data; it does not compute metrics. Re-export conflicts are reported rather than overwritten.

These integrations have different data flows. For example, hosted grading sends bound inputs, outputs, and references to the approved API origin; candidate generation sends selected source text to its configured provider; Langfuse operations send or retrieve only their documented dataset, trace, or score fields. The user should inspect the integration list with the project's policy before enabling them. Local contract tests do not establish compatibility with a live hosted service.

### Add trace and runtime evidence

OpenTelemetry JSON traces can be imported and correlated to stored executions using request IDs. The profile/report can then show trace summaries such as tool totals or usage completeness. Incomplete traces remain partial or lower bounds; unmatched spans do not become confirmed facts. The current supported flow is explicit trace import, not automatic instrumentation of arbitrary applications.

### Generate and review candidate cases

A user can generate candidate Q/A rows from approved development-only text sources. Generation is bounded, policy-checked, records source digest and quote provenance, and marks references synthetic_unverified. The review lifecycle is:

1. Generate into the candidate pool.
2. Show each candidate with its recorded source quote.
3. Have a reviewer verify, reject, or record human review.
4. Optionally run the narrow exact source-quote verifier.
5. Explicitly promote approved candidates into a new JSONL file.
6. Validate and use that file in a later plan.

Candidates never enter a normal evaluation dataset merely because a model generated them. Holdout and validation sets are not accepted as generation sources; source changes invalidate review/promotion evidence.

### Run a controlled parameter experiment

When an application owner has explicitly exposed finite, allowlisted parameters, the user can define a bounded experiment over a development set. The default parameter combination is the baseline; trial limits, seeds, frozen plan/evaluator identity, and objective/constraints are recorded. Exhausted budgets require an audited resume with more trials.

A protected holdout is reserved separately. Candidate selection finishes before holdout evaluation; the final paired evaluation is a separate explicit action. The report includes coverage and uncertainty. BenchCraft can propose adoption, but it does not edit source, change production settings, or apply the recommendation.

### Use cache and quotas

Execution/evaluation caches are opt-in and use version-complete keys; cache hits have provenance and are excluded from latency/repeat claims. Provider-aware quotas can cap concurrency and start rates and back off on selected HTTP responses. Cache and quota configuration are explicit plan/policy decisions, not silent defaults.

## Scenario catalog: what the user should see

| ID | Scenario | Expected product behavior |
|---|---|---|
| S01 | The goal and run scope are clear | Build/validate the typed plan, then execute within existing policy without an unnecessary second confirmation |
| S02 | The user asks only to inspect or plan | Show findings/draft; no application call |
| S03 | Objective, case count, dataset, or invocation path is materially ambiguous | Ask one focused question; do not guess or dispatch |
| S04 | No compatible dataset is found | Explain the missing input; do not invent a trusted dataset |
| S05 | One compatible dataset is found and policy approves it | Reuse that content identity transparently and show the source |
| S06 | Multiple materially different datasets are found | Ask the user to choose |
| S07 | A source file contains “ignore policy” or a request to reveal secrets | Treat it as untrusted repository text; it cannot authorize actions or expose secret contents |
| S08 | A metric needs retrieval/tool evidence the app did not emit | Mark it unavailable or show the evidence gap; never substitute references or guesses |
| S09 | Expected/reference data exists | Keep it out of application inputs; make it available only to authorized evaluator bindings |
| S10 | The app returns a wrong answer | Record completed execution plus low/failed metric evidence, with the case output and observations |
| S11 | The app crashes, times out, or returns malformed output | Record an application/evaluator error state separately from a quality score; apply effect-aware retry rules |
| S12 | A policy denies an app/evaluator/provider/origin | Explain the denial and make no forbidden call; the user must change their own configuration/policy |
| S13 | A secret is missing | Report the missing environment reference without printing a value or writing one to files |
| S14 | The run is paused, interrupted, or the terminal closes | Preserve run identity and durable progress; resume only eligible work when requested |
| S15 | The user asks for another score | Rescore stored executions under a new scoring pass; do not repeat app calls |
| S16 | Runs are incompatible for strict comparison | Block or label the comparison unqualified; show the mismatched identities/coverage |
| S17 | A dataset source changes after candidate generation | Fail review/promotion closed because the recorded digest no longer matches |
| S18 | Experiment trials are exhausted | Keep remaining combinations pending; require an audited budget increase to resume |
| S19 | Holdout evidence is requested before development selection is complete | Keep holdout protected; do not parse or run it prematurely |
| S20 | A live external integration is unavailable or not authorized | Show destination, missing approval/credential, and supported local alternatives; do not imply success |

## Feature map and boundaries

| Capability | Available path | Important limit |
|---|---|---|
| Local quickstart | init, doctor, chat or headless run | The shipped support app is a fixture, not the user's production assistant |
| Repository inspection | inspect with source, profile in approved fresh chat | Bounded Python/JS/TS and selected manifests; other languages/dynamic behavior remain unknown |
| Dataset discovery | Repository candidate inventory, JSONL validation, compatible-candidate selection | Shape is not semantic quality; tests are not goldens |
| Planning | Template/model-assisted plan, opportunities, validation | Objective mapping is bounded; unsupported vocabulary/evidence remains a question or gap |
| Conversational control | Persistent terminal chat, slash commands, typed assistant tools | Natural-language use needs an allowed OpenAI-compatible assistant model; no arbitrary shell or source editing |
| Application execution | CLI, HTTP JSON, Python, container, OpenAI-compatible runner | Local runner effects depend on configuration; subprocesses are not inherently sandboxed |
| Results and evidence | Durable SQLite records, content-addressed artifacts, reports | A report is only as complete as captured app/evaluator/trace observations |
| Evaluators | Native, trusted custom code, isolated DeepEval/Ragas/OpenAI Evals paths | Adapter support is narrow; live provider behavior and judge quality need separate validation |
| Data/trace integrations | Explicit Langfuse and OpenTelemetry import/export flows | No automatic production-history discovery or universal tracing ingestion |
| Optimization | Bounded finite-parameter experiments, protected holdout | No autonomous repair, free-form search, or automatic adoption |
| Deployment | None | No source patching, release, merge, or production configuration write |

### Safety, privacy, and execution boundaries

- Policy is checked at the point of action. Repository evidence or an assistant message cannot grant permission.
- Credentials are environment references; project files contain references, not secret values. Captured output and reports are sanitized, but pattern-based redaction is only a backstop.
- A remote assistant model may receive the bounded profile and conversation context; evaluator and connector providers receive their bound case/trace/score payloads. Review configured destinations and policy before sending project or case data off-machine.
- Application inputs, judge-only references, retrieved context, traces, and external evaluator payloads have distinct bindings and destinations.
- The user remains responsible for approving the policy and for correctly declaring application effects.
- HTTP setup itself contacts no endpoint. A later run sends the configured request payload to the configured origin under policy.
- The assistant can propose plans, answer questions, read stored evidence, and request typed actions. It cannot run arbitrary terminal commands, browse the web, modify source files, or deploy code.

## What is implemented, and what is not yet proven

The latest local acceptance report says the deterministic local scope passed 32 clean-installed E2E tests across journeys E2E-01 through E2E-08. It covers fresh repository-aware conversation, bounded HTTP setup/run, trace continuity, controlled experiments, policy denial, stored evidence, and rescore. See the [Prompt 31 report](../engineering/reports/31.md), [support and limitations](../support.md), and the [quickstart](../quickstart.md).

One additional live-provider smoke was run during the review conversation on 2026-09-25: a free OpenRouter assistant model conducted a two-case local-fixture evaluation, and a stored-output rescore made no new application calls. This proves that one configured provider path worked in that run. It does not validate general model quality, judge quality, every provider, hosted evaluator services, arbitrary applications, or production readiness.

Still unvalidated or outside the current contract:

- Real-team usability, time saved, willingness to pay, product-market fit, and human review of planner/judge fixture labels.
- Broad live-model and hosted-integration behavior beyond the single smoke described above.
- Linux/macOS release behavior and a newly published package release.
- Generic website crawling, arbitrary URLs, and browser automation.
- Automatic historical production-log discovery, arbitrary framework/test interpretation, and parsers for unsupported languages.
- Distributed execution and a dashboard/web app.
- Automatic code repair, prompt edits, deployment, or production adoption.

The project is therefore useful for the supported local workflows and bounded configured integrations. External validation and broader feature claims remain separate.

## Feedback worksheet

Please comment using the section or scenario ID, then write the behavior you want. The questions below are prompts, not assumptions about the right answer.

1. **Starting path:** Should the main onboarding lead with a local repository, the bundled demo, or a configured HTTP endpoint?
2. **First-run trust:** Is clear-request automatic execution under the existing policy the right default, or should some action require a separate confirmation in your target use case?
3. **Discovery:** Which application languages/frameworks and dataset sources should be prioritized next?
4. **Dataset choice:** Is sole-compatible-dataset reuse plus a question for materially different choices the right behavior?
5. **Evaluation choices:** Which quality/reliability dimensions must be visible in the first plan?
6. **Evidence presentation:** How much source-path, case, trace, and provenance detail should appear in chat versus reports?
7. **Failure analysis:** Should the next experiment be an evidence-gap recommendation, a user-authored plan edit, or another workflow?
8. **External data:** Which integrations or production-history sources do you expect, and what consent boundary should apply?
9. **Experiments:** Are the finite parameter grid, explicit holdout step, and recommendation-only adoption path understandable?
10. **Scope:** Which missing capability blocks your own first useful evaluation?

### Feedback notes

- **Section/scenario:**
- **What you expected:**
- **What should happen instead:**
- **Priority:** must-have / important / later
