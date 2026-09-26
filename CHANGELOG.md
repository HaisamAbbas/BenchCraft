# Changelog

Each release lists new capabilities separately from changes to metric semantics. A metric
whose meaning changes gets a new semantic version, and is listed under "Metric semantics".
Workspace schema changes are listed under "Workspace". Upgrade steps are in
[docs/release/upgrade-and-recovery.md](docs/release/upgrade-and-recovery.md).

## Unreleased — DeepEval metrics for the assistant

- `aibench-deepeval` 0.2.0rc1 wraps every DeepEval 4.2.5 single-turn metric aibench's
  recorded data can feed (21, including G-Eval with plan-stated criteria), each with a
  documented field mapping and not-applicable policy. A built-in `openai_compatible` judge
  runs them on any Chat Completions endpoint (e.g. GLM on Z.ai) and counts its calls.
- `aibench plugins install NAME` / `/plugins install NAME` create a plugin environment for
  the project, record it and its judge default in `aibench.json`, and apply the policy
  lines it needs after showing them (the old policy is kept as `.bak`). `aibench plugins
  status` and `/plugins` show each optional plugin's state. The assistant's new
  `list_optional_plugins` tool lets it explain a plugin and suggest the command; it cannot
  install anything.
- Chat sessions load the project's plugin environments. Planning knows new concepts
  (relevancy, retrieval quality, bias, toxicity, privacy, misuse, advice, role adherence,
  instruction following, summarization, task completion, tool arguments and permissions,
  patterns, custom criteria) and fills project defaults such as the judge.
- Evaluator manifests may declare `concepts` and `parameter_requirements` (fields a
  parameter adds); a worker-run evaluator now receives the fields its parameters name.
- Metric semantics: `deepeval.faithfulness@1` is unchanged; its plugin version moved to
  0.2.0rc1, so strict comparisons against earlier faithfulness runs report a plugin
  identity difference. Workspace: sessions gain `evaluator_defaults` (optional).

## Unreleased — DeepEval conversation metrics

- `aibench-deepeval` adds DeepEval 4.2.5's 12 conversational metrics (conversation
  completeness, knowledge retention, role adherence, goal accuracy, topic adherence, tool
  use, turn relevancy, turn faithfulness, turn contextual precision/recall/relevancy and
  conversational G-Eval). Each turn of an episode (cases sharing a `group_id`) is scored on
  the conversation up to and including it, built from the run's recorded executions of the
  same repetition; the episode's last turn carries the whole conversation's score.
- The evaluation view gains `episode.turns` and `case.group_id`. A metric requiring
  `episode.turns` gets the conversation assembled by the scorer (retrieved passages and tool
  calls only if it requires them), is not applicable outside an episode or after an earlier
  turn that did not complete, and bypasses the evaluation cache (earlier turns change it).
- DeepEval scores outside 0..1 are evaluator errors, never recorded scores.
- Dataset summaries list `case.group_id` only for datasets with episodes. No metric
  semantics of existing evaluators changed.

## Unreleased — chat input box

- The message being typed is now drawn as a box across the bottom of the terminal: a tinted
  band in the theme's colour, a marker on its left edge and the hint `Ask BenchCraft to do
  anything` while it is empty. It stays on the last rows, just above the status bar, with spare
  rows above it instead of under it. The box follows the terminal's width, and every further line of
  a multiline message (or a line that wraps) keeps the same left edge. It is drawn by the
  prompt itself, so streamed replies and run progress still print above it, and it takes its
  colour from the active theme (`/themes`). A terminal that cannot print the marker's block
  character gets a plain ASCII marker on unbanded text. No metric semantics or workspace
  schema changed.

## Unreleased — terminal welcome screen and themes

- `aibench chat` opens with a BENCHCRAFT block logo over a bordered panel showing the
  project, session, assistant model and grouped slash commands. Consoles that cannot encode
  block characters get an ASCII logo; terminals narrower than the logo skip it.
- New `/themes` command lists colour themes (`crimson` red by default, plus `ember`, `gold`,
  `ocean`, `forest`, `violet`, `mono`, and `paper` for light backgrounds). `/themes NAME`
  switches immediately and saves the choice in `.aibench/ui.json`. No metric semantics or
  workspace schema changed.

## Unreleased v4 local-scope work — Prompts 30/31

- Fresh conversational planning can include the existing bounded, policy-approved repository
  profile. Findings retain evidence and unknown states; inspection does not execute project
  code.
- Added `aibench connect http` for bounded no-repository setup of the configured JSON HTTP
  runner. Setup validates a selected local JSONL dataset and writes typed config and policy
  without contacting the endpoint.
- Conversations can read imported trace summaries alongside approved repository findings for
  one stored run and can start/resume bounded experiments through the existing experiment
  service. Protected holdout evaluation remains a separate explicitly requested action.
- The clean-installed deterministic suite now includes E2E-08 for a fresh repository-aware
  conversation. No metric semantics or workspace schema changed.

## Unreleased Phase 3 working tree — Prompt 20

- Added `scripts/measure_capacity.py`. It runs the real CLI against the local rate-limited
  fixture, and records throughput, process-tree CPU, peak memory, system load, server-side
  concurrency and workspace growth per case.
- Measured the local single-process ceiling, and recorded the distributed-execution
  decision in ADR 0019. Distributed workers are deferred, because no distribution need was
  demonstrated. No coordinator, queue, object store or worker service was added.

## Unreleased Phase 3 working tree — Prompt 19

- Added optional controlled parameter experiments over finite, app-exposed environment
  settings. Trial contracts freeze the development plan, evaluator identities, budget, seed,
  constraints, exact parameter values and run lineage.
- Added a durable budgeted grid runner with explicit resume, coverage/constraint checks and
  paired uncertainty comparisons. A development candidate is selected only when its
  95% paired interval supports an improvement over baseline.
- Added a protected final evaluation that locks development selection first, then compares
  baseline and selected settings on all holdout cases under one frozen evaluator plan.
  Reports separate selection and final holdout results; adoption is proposed for review and
  never applied automatically.
- Added the synthetic known-objective app, CLI experiment commands and conversation report
  and adoption tools.

### Workspace

Schema version 11 adds experiment contracts, trial lineage, append-only events and reserved
holdout digests. Existing workspaces migrate forward on first use.

## Unreleased Phase 3 working tree — Prompt 18

- Added bounded, policy-checked question/answer candidate generation from explicit
  development text sources. Candidate rows remain separate from runnable datasets and
  record source hashes/spans, model and prompt identity, exact duplicate source documents,
  and every review/verification/promotion action.
- Added human source review, expert review, a strict exact-source-answer oracle, and an
  explicit promotion command that writes a new ordinary JSONL dataset. Synthetic
  unreviewed references cannot be promoted.
- Added a typed multi-turn text episode manifest and a validation command for ordered turns,
  simulator provenance, resettable test worlds, and independent final-state checks.
- Added an executable local support-conversation fixture using the existing per-episode
  engine reset behavior and `native.final_state` evidence.

### Workspace

Schema version 9 adds the candidate-pool, candidate-case, and append-only candidate-event
tables. Older workspaces migrate forward on first use.

## Unreleased Phase 2 working tree — Prompt 17

- **openai/evals bridge** (plugin `aibench-openai-evals-oss`, `evals==3.0.1.post1`):
  - the `match`, `includes`, `fuzzy_match` and `json_match` evals, run by the upstream code;
  - recorded replay needs the eval's request to equal the recorded input exactly, once;
  - `aibench openai-evals-oss run` bridges the upstream completion function to the
    application's runner as a recorded `delegated_suite` run.
- **Hosted OpenAI Evals API bridge** (plugin `aibench-openai-evals-api`, `openai==3.19.2`):
  - `aibench openai-evals-api submit/status/resume/cancel/fetch/jobs` grade recorded
    outputs as remote jobs;
  - requests are stored before sending;
  - ambiguous submissions are reconciled, never resent without `--resend`;
  - results map one-to-one to cases;
  - generating data sources and `{{sample.*}}` templates are refused.
- **Langfuse connector:** `aibench langfuse import-dataset/import-traces/export-scores/status`
  import datasets and traces, and export recorded results as scores, with provenance on
  both sides.
- **Integrations:** `aibench integrations list`, `/integrations` in chat and the
  assistant's `list_integrations` tool show modes, data destinations and availability.
- **Policy:** new `allowed_egress_origins`. Every destination that receives benchmark
  data or credentials must be listed, loopback included.
- **Behaviour change:** a `remote_job` metric cannot be bound in plans or rescoring.

### Workspace

Schema version 10 adds `remote_jobs` (after Prompt 18's migration 9).

## Unreleased Phase 2 working tree — Prompt 16

- **Evidence-backed inspection.**
  - `aibench inspect APP --source DIR --policy P` reads manifests and Python/JS imports
    from a tree inside the policy's new `inspection_roots`.
  - Findings are *inferred*, with file, line and context (code, tests, `TYPE_CHECKING`,
    guarded, commented out). They never satisfy an evaluator's applicability check.
  - Secret-like files are never read.
  - `--probe N` runs N dataset cases through the runner under the policy (effects must be
    `none`), turning declarations into observations.
- **Trace import.**
  - `aibench traces import RUN FILE` and `aibench traces show RUN` read OTLP/JSON,
    preserve the raw export as a restricted artifact, and match traces by correlation ID.
  - Partial traces are marked with reasons.
  - Token usage is summed over the lowest reporting spans only.
  - Reports gain an "Imported traces" row.
- **Opt-in caches.**
  - Plan `cache: {executions, evaluations}`, with version-complete keys. The execution
    key includes the application's source files and inherited environment. Endpoints need
    a declared `revision`.
  - Comparisons against cached executions are blocked.
  - Every hit carries provenance.
  - Execution caching is refused for effectful apps without a test world, for
    per-episode state and for shared state.
  - `aibench cache list/clear`.
  - Reports gain a "Cache" row. Hits are excluded from latency.
- **Provider-aware quotas.** Plan `quotas` (`application` or `evaluator:<glob>`) with
  `max_in_flight`, `requests_per_second`/`burst`, and backpressure on HTTP 429/503
  (`Retry-After` or `backoff_seconds`). Quota summaries and `backpressure` events are
  recorded in the run events.
- **Responsiveness fix.**
  - HTTP client creation and capture-file writes no longer block the event loop. The loop
    stalled for up to 1.7 s under load; it now stays below 0.25 s.
  - This affects the live terminal, which shares the loop.
  - Concurrent writes of identical artifacts are safe on Windows.

### Workspace

Schema version 8 adds the tables `trace_observations` and `cache_entries`. Older
workspaces migrate forward on first use.

## Unreleased Phase 2 working tree — Prompt 15

- **New application transports** behind the same runner contract:
  - `python`: a callable, run in a fresh interpreter through a standard-library shim;
  - `openai_compatible`: a chat-completions endpoint. Usage is observed; tool calls are
    recorded as requests, not effects;
  - `container`: an image pinned by digest, non-root, read-only, capability-free,
    resource-limited and offline by default. The container is removed on timeout, and
    nothing is pulled (including between preflight and run). App config cannot select a
    host executable or set host-sensitive environment variables on the engine client. The
    policy approves images and network.
- **State between cases.** The engine now honours `reset_policy`:
  - it resets through the application's hook (`reset_url`, `reset_argv`,
    `reset_callable`) before every case, or before each episode (cases sharing a
    `group_id`);
  - a failed reset blocks the case;
  - a failed or interrupted episode turn blocks the rest of that episode.
  - **Behaviour change:** an application whose config declared `reset_url` is now actually
    reset, and its plans need `concurrency.application: 1`.
- **Test worlds.** Named seeds declared by the application, selected by a plan
  (`test_world`) or in chat (`/world`), approved by the policy (`allowed_test_worlds`),
  and frozen with the run. A new `world_state` observation. Reports record the reset mode,
  the world and its seed hash, and the reset counts.
- **Agent outcome metrics:** `native.tool_calls@1.0.0` (names), `native.tool_outcomes@1.0.0`
  (arguments, success, authorization) and `native.final_state@1.0.0` (assertions on the
  world state). They are separate, so a correct tool name never masks a failed outcome.
- **Chat.** `/app` and the assistant's `describe_application` explain what the runner
  observes, what evidence is missing, and how state is reset.
- **Known limits.**
  - Containers were exercised with Docker Engine 29.7.2 (Docker Desktop, Windows) only, and
    are not a hostile multi-tenant sandbox.
  - The OpenAI-compatible transport was exercised against a local stub only.

## Unreleased Phase 2 working tree — Prompt 14

- Added the separately packaged `ragas.faithfulness@1` adapter, pinned to `ragas==0.4.3`
  and restricted to text stored outputs in an isolated worker.
- Added strict/exploratory stored-run comparison with case/repetition pairing, compatibility
  identities, coverage gates, case-level macro differences, grouped bootstrap intervals and
  stored-pass stability summaries.
- Added `aibench compare` plus the session-owned `compare_runs` tool and `/compare`; neither
  workflow invokes an application, evaluator or judge.
- Cross-framework results retain separate score semantics. Disagreement is diagnostic and
  is never averaged into a combined quality score.
- Known limitation: Ragas 0.4.3 has an open multi-modal SSRF advisory. The adapter exposes
  only text faithfulness; ADR 0013 records the compensating control.

## 0.1.0rc1 — MVP release candidate (not published)

The first release candidate of the MVP. It is a local technical candidate. **No real team
has piloted it yet** (see [docs/pilot/](docs/pilot/README.md)), and it is not published to
a package index. The readiness decision, with everything that is still open, is in
[docs/engineering/release-readiness.md](docs/engineering/release-readiness.md).

### Capabilities

- **Benchmark conversation.**
  - Bare `aibench` in a terminal opens a persistent two-way session: state a goal, answer clarifications, revise the draft plan, and run it.
  - While it runs you can ask questions and use `/status`, `/pause`, `/resume` and `/stop`.
  - A session is restored after exit or a crash without repeating any action.
  - Failures are discussed with evidence, and every number is traced to a query.
- **Headless commands** for everything the conversation does:
  - `init`, `doctor`;
  - `dataset validate`, `inspect`, `app describe` / `smoke`;
  - `plan`, `plan validate`, `plan benchmark`;
  - `run`, `resume`, `evaluate`, `score`;
  - `runs`, `report`, `benchmark`, `sessions`, `evaluators`, `plugins`.
- **Applications.** CLI and HTTP runners with explicit input and output bindings. Reference answers never reach the application. Retrieval, tools, usage and cost are recorded only when the application reports them.
- **Evaluators.**
  - Native exact match and JSON schema checks.
  - Trusted custom Python evaluators.
  - The DeepEval faithfulness adapter (`aibench-deepeval`, pinned to `deepeval==4.2.5`), run in its own environment.
  - An evaluator that lacks the evidence it needs reports a gap, not a score.
- **Planning.** Evidence-aware plans from declared capabilities and installed evaluators, within a policy. A bounded model planner with a deterministic template fallback. Plans are frozen and hashed before execution.
- **Execution.** Bounded concurrency, timeouts, retries, budgets, cancellation, and conservative resume after interruption or a crash. Effectful calls are never repeated automatically.
- **Reports.** JSON, Markdown and HTML, rebuilt from stored facts, with:
  - full denominators;
  - release gates;
  - latency definitions;
  - cost completeness (unknown is never $0);
  - case evidence.
- **Validation tools.** `aibench plan benchmark` (a planner fixture set) and `aibench evaluators calibrate` (judge calibration).

### Changes in this candidate (after the Prompt 12 acceptance audit)

- **A full disk no longer fails cases.** A workspace storage failure (disk full, quota, I/O error, read-only filesystem, database full) stops dispatching and leaves the run resumable, exit code 130, with the reason shown.
  - Before this, every remaining case was still sent to the application and marked `failed`, and the calls that couldn't be recorded were left out of the accounting.
- **Recovery can't lose a call from the accounting.** Recovery now commits its settlements together with the record of calls that may have reached the application. A crash in between used to lose that record.
- **Why a run stopped early** is shown in `run` and `resume` output, in `runs status` (`warnings`), and in the chat.
- **A newer workspace is refused.** A workspace upgraded by a newer aibench is refused with exit code 2, instead of being written by software that doesn't know its schema.
- **Version metadata.** The version is `0.1.0rc1` for both packages, from one source each. `aibench-deepeval` requires `aibench>=0.1.0rc1,<0.2`.

### Metric semantics

First release: `native.exact_match@1.0.0`, `native.json_schema@1.0.0` and
`deepeval.faithfulness@1.0.0`. Their limitations are listed in `aibench evaluators describe`.

### Workspace

Schema version 7, created on first use. See the upgrade notes for the forward-only
migration rule and the refusal of newer workspaces.

### Known limitations

- **Tested platforms.**
  - Tested on Windows 11 with Python 3.12 and 3.11.
  - Linux runs in CI, but no result has been observed.
  - macOS is not run anywhere.
- **No live model or judge tested.** Nothing in the conversation, model planner or DeepEval judge has been exercised against a live model or judge.
- **Planner recall.** The template planner's selection recall on the v1 fixture set is 17/21, below the 0.85 engineering target. Fixtures and calibration labels haven't been reviewed by people.
- **Throughput.** About 7 cases/s against an instant local service. Durable capture is the bottleneck.
- **Disk-full testing.** Handling is tested by making the real write path fail. A real full volume wasn't used.
