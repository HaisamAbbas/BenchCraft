# Support and limitations

This describes the current `aibench 0.1.0rc35` source candidate, what has actually
been tested, and what isn't available yet. This candidate is not published to a package
index. The release decision and its open items are in
`docs/engineering/release-readiness.md`. The authoritative design is `docs/spec/implementation-plan.md`, and
per-prompt evidence is in `docs/engineering/reports/`.

## Platforms

| | Declared/configured | Observed |
|---|---|---|
| Python | 3.11, 3.12 in CI | Windows 11, Python 3.12.10; earlier Windows release checks used 3.11.16 and 3.12.10. Current hosted matrix results are pending |
| OS | Windows, Linux (`ubuntu-latest`), macOS (`macos-latest`) in CI for core, plugin, and release checks | Windows 11 only. Linux and macOS runners are configured, but their current results have not yet been observed |
| Terminal chat | Any terminal prompt_toolkit supports | Windows ConPTY (earlier automated regression tests, including resizing); current hosted matrix is pending |
| Model providers | OpenAI-compatible endpoints | Scripted and local test providers only. No live model has been exercised |

Details and the current CI scope are in `docs/engineering/platform-matrix.md`.
`aibench doctor` warns on a platform outside the tested matrix.

## Optional evaluators

The core install ships the native evaluators: `native.exact_match`, `native.json_schema`
and custom Python evaluators you trust. Evaluator frameworks are never imported into the
aibench process. They run in their own environment, driven by a worker.

**DeepEval** (pinned to `deepeval==4.2.5`): faithfulness, answer
relevancy, contextual precision/recall/relevancy, hallucination, bias, toxicity, PII
leakage, misuse, non-advice, role violation, prompt alignment, summarization, task
completion, argument and tool correctness, tool permission, exact and pattern match, and
G-Eval with your own criteria, and DAG: a decision tree you write as JSON that the
judge walks to a score, and a head-to-head judge of two runs' answers (`/compare
BASELINE CURRENT --judge "CRITERIA"`). For multi-turn applications it adds 13 conversation
metrics (completeness, knowledge retention, role and topic adherence, goal accuracy, tool
use, turn-level relevancy, faithfulness and retrieval, conversational G-Eval and DAG), each scored on
the conversation up to every turn of an episode, and 4 agent-trace metrics (step
efficiency, plan quality, plan adherence, agent loop detection) over traces imported with
`aibench traces import` (or `/traces import FILE` in the chat, then `/rescore`). Install it for a project with `aibench plugins install
deepeval --judge-provider PROVIDER.json`, or type `/plugins install deepeval` in the chat to
use the assistant's model as judge. Either one shows its changes before making them:

- a dedicated plugin environment under `.aibench/plugins/deepeval/` for DeepEval and its
  pinned dependencies;
- `plugin_environments` in `aibench.json`, with the judge as the default for `deepeval.*`;
- the policy lines it needs: `allowed_plugin_environments` (that interpreter),
  `allowed_evaluators: deepeval.*`, `allow_model_evaluators: true` and the judge's key
  reference in `allowed_secret_refs`. The previous policy is kept as `policy.json.bak`.

Chat sessions then load it, and the assistant chooses its metrics from what you ask to
measure ("answers must be relevant", "no bias", "must not leak personal data", "polite
tone"). The assistant can only suggest the install; you type the command. `aibench plugins
status` shows each optional plugin's state. The judge model is a paid external call; any
OpenAI-compatible endpoint (such as GLM on Z.ai) works without code. Metrics, field mapping
and judges are described in `plugins/deepeval/README.md`. Multi-turn conversational metrics
are also available.
The plugin also includes bounded image and generated-image evaluation, audio-task scoring,
sampled-video evaluation, stored-episode voice and MCP metrics, text-only provider-transcript
accuracy, transcript WER/CER, and recorded diarization error rate. These adapters score stored
case or episode evidence; they do not capture live calls or connect to MCP servers. The
transcript-accuracy adapter uses G-Eval over text. The dedicated upstream transcription class
is absent from the pinned package and was also absent from DeepEval's latest checked release;
see the [release check](audit/2026-10-08/implementation/G22.19-upstream-audio-release-gate.md).
Bounds and supported media formats are in the [DeepEval plugin guide](../plugins/deepeval/README.md).

**Ragas faithfulness** (`ragas.faithfulness@1`, pinned to `ragas==0.4.3`) is the
independent second ecosystem used by the Phase 2 comparison workflow. Install it separately:

```bash
python -m venv plugins/ragas/.venv
plugins/ragas/.venv/Scripts/pip install -e . -e plugins/ragas    # Windows
plugins/ragas/.venv/bin/pip install -e . -e plugins/ragas        # Linux/macOS
```

A plan names that interpreter in `plugin_environments`, allows `ragas.*` and model-backed
evaluators in policy, and supplies any provider key through `secret_env`. Only text
faithfulness is exposed, and it scores the application's recorded output and observed
retrieved text. It never substitutes the Golden's reference context. Ragas 0.4.3 has an
open [multi-modal SSRF advisory (GHSA-95ww-475f-pr4f)](https://github.com/advisories/GHSA-95ww-475f-pr4f);
the adapter does not expose the affected metric and runs in an isolated worker. See ADR 0013
and `plugins/ragas/README.md`.

Promptfoo is not integrated. OpenAI Evals (open-source and hosted) and Langfuse are: see
[docs/integrations.md](integrations.md).

## Run comparison

`aibench compare BASELINE CURRENT` reads stored executions and metric results only. It never
calls the application, evaluator or judge. Strict mode pairs `(case_id, repetition_id)`,
checks dataset/case, repetition, metric/plugin, judge/rubric and instrumentation identities,
and blocks an unqualified claim when they differ. `--mode exploratory` is visibly
non-qualified. `--baseline-scoring` and `--current-scoring` select explicit stored scoring
passes, which is how two passes over the same run are compared.

Use `--regression-policy policy.json` to fail CI on predeclared native-metric, p95-latency,
or observed-cost degradation. Policies require strict comparisons and complete measurements;
see [regression policies](regression-policies.md) for the schema and exit codes.

Complete-pair coverage must pass the predeclared threshold before a result supports a
quality claim (`claim_qualified=true`). Numeric differences are case-level macro averages
with a seeded grouped bootstrap interval. DeepEval and Ragas results are shown as separate
frameworks; their scores are never averaged or treated as equivalent. Conversation exposes
the same service through `compare_runs` and `/compare BASELINE CURRENT`, restricted to runs
started by that session. Ragas' cold catalogue import is bounded by worker preparation; use
an explicit startup-timeout override on unusually slow hosts.

## Export case results

`aibench export RUN_ID` writes one JSONL row per selected `(case, repetition)`; `--format csv`
is available for spreadsheet and data-frame workflows. Use `--scoring-id` to select a stored
pass and `--no-content` to keep case data, outputs, contexts, values and free-text reasons out
of the export. See [case-result exports](case-result-export.md) for the schema and examples.

`aibench report --format junit` or `aibench report --format sarif` generates CI test-result
and code-scanning artifacts from stored benchmark results. See
[CI report adapters](ci-reports.md) for status mappings and content controls.

## Search and baseline management

`aibench runs list` supports literal metadata search, tag/baseline filters, and stable
offset pagination. Runs can be tagged and annotated with sanitized notes. Operators can
promote a completed, healthy run with all declared gates passing, record who approved it,
inspect promotion history, and compare directly against its alias. See
[run history and named baselines](run-history-and-baselines.md) for commands and approval
rules.

## Direct run controls

Common selection, repetition, concurrency, retry, timeout, budget, and cache options can
override a plan for one `aibench run`. `--dry-run` validates those overrides and prints the
effective frozen plan and exact selected case IDs without dispatching. Policy checks remain
in force. See [direct run controls](run-controls.md) for bounds and examples.

## Run reproducibility

`aibench run --run-seed N` fixes the engine RNG seed. Run records expose the seed and
available application source, runtime, dependency, and Git provenance. See
[run reproducibility and provenance](reproducibility.md).

## Conversation release gates

Session drafts can carry the same selected-case pass-rate and completed-coverage gates as
executable plan files. Ask for the metric and threshold in the conversation; the draft preview
shows each metric's zero-based binding and the gate list before a run can start. A gate patch
replaces the list, so existing gates are preserved when adding a new one and removed only on
an explicit request. Headless plan and run commands continue to use the plan file's `gates`.

## Credentials

Credentials are never written into project files, datasets, plans or chat. Every
credential is a secret reference, `env:NAME`, resolved from the environment when it is
used. It must also be listed in the policy's `allowed_secret_refs`:

| Where | Field |
|---|---|
| CLI app environment | `transport.secret_env: {"TOKEN": "env:APP_TOKEN"}` |
| HTTP app header | `transport.secret_headers: {"Authorization": {"ref": "env:APP_TOKEN", "prefix": "Bearer "}}` |
| Evaluator plugin | `plugin_environments[].secret_env` or `--plugin-secret NAME=env:VAR` |
| Assistant model | `api_key: "env:OPENAI_API_KEY"` in the provider config |

Set the variables in your shell or your CI's secret store. Don't commit a `.env` file: aibench
doesn't read one; the isolated DeepEval and Ragas adapters do not load project `.env` files.
`aibench doctor` reports whether each reference is set, without printing its value. Secrets
are redacted from captured output, stored conversation turns and reports.

## Dataset fixture visibility

Fixture `app_visible` flags must be JSON booleans. Only `"app_visible": true` exposes
fixture content to the application; `false` and an omitted flag keep it judge-only.
Strings such as `"false"` or `"true"`, numbers, and null are invalid and fail dataset
validation before any application call. The same rule applies when constructing cases
through the Python API. Reference answers remain judge-only.

Case numbers must be finite. Dataset validation rejects `NaN`, `Infinity`, `-Infinity`,
and numeric literals that overflow to infinity, including values nested in input,
fixtures, expectations, metadata, and extensions. Invalid rows include their source line
and prevent a run from dispatching. Valid strings such as `"NaN"` remain ordinary text.
The Python case API applies the same validation to supplied container, model, and
dataclass fields before storing a case.

## Supported now

- **Conversational benchmarking:** in a terminal, `aibench`/`aibench chat` handles planning, runs, live status, pause/resume/stop, failures, case evidence and reports. Sessions survive exit and crashes, and are never restarted automatically.
- **Headless commands:** `aibench --help` lists the complete command tree. It includes
  project/configuration setup, dataset and suite utilities, app inspection and smoke tests,
  plan/benchmark/run/resume/evaluate/score workflows, supervised detached runs and durable
  pause/resume/cancel controls, run/session/trace history, report and case export, comparison
  and experiments, evaluators and plugins, cache/workspace maintenance, coordinator workers,
  dashboards, and external integrations. Root options include `--json`,
  `--non-interactive`, `--policy`, `--config`, `--quiet`, and `--verbose`.
- **Reports:** JSON, Markdown, static HTML, JUnit XML and SARIF 2.1.0, from stored facts. They include typed metric profiles, full denominators, release gates, latency definitions, cost completeness and case evidence.
- **Dashboard:** `aibench dashboard` is read-only and shows stored runs, session summaries,
  content-withheld report facts, and on-demand run comparisons. It binds to loopback by
  default. Opt-in remote access requires TLS 1.2 or newer and an environment-sourced bearer
  token; API reads remain authenticated. See [the dashboard guide](dashboard.md).
- **Release gates** in executable plans and conversation drafts: minimum pass rate or
  completed coverage for a metric binding, always over selected cases.
- **Application transports** (Phase 2, Prompt 15): CLI, HTTP, Python callable (`python`), container (`container`) and OpenAI-compatible endpoint (`openai_compatible`). See `docs/runner-protocol.md`.
- **HTTP API boundary** (Prompt 28): HTTP evaluates an explicitly configured JSON API using `POST`/`PUT` request bindings and response-field bindings. TLS verification is on by default; endpoint/origin policy, secret references, redaction, request/response byte caps, timeouts, effect-aware retry behavior, quotas and run budgets apply. Live endpoints require configured targets and policy approval. This transport does not browse websites, infer URLs from source or provide arbitrary browser actions. Live remote endpoints were not tested.
- **Stateful applications and agents** (Prompt 15):
  - reset hooks, with `per_case`, `per_episode` (cases sharing a `group_id`) and `shared` state;
  - named test worlds whose seeds are frozen with the run;
  - `world_state` observations;
  - three separate outcome metrics: `native.tool_calls` (names), `native.tool_outcomes` (arguments, success, authorization) and `native.final_state` (world state).

  In chat, `/app` explains what the runner observes and what evidence is missing, and `/world NAME` selects an approved test world.
- **Evidence-backed inspection** (Prompt 16): `inspect --source DIR --policy P` reads manifests and imports from a tree the policy approves (`inspection_roots`), and never reads secret files. Findings are *inferred*, with file and line, and never count as a confirmed capability. `inspect --probe N` turns declarations into observations by running N cases through the runner, under the policy.
  Prompt 25 adds a bounded repository inventory to the same command/profile: Python (`.py`) imports and `__main__` guards are parsed with AST; JavaScript/TypeScript (`.js`, `.mjs`, `.cjs`, `.ts`, `.tsx`, `.jsx`) uses limited static import matching; `pyproject.toml`, `requirements*.txt`, `package.json`, and Dockerfiles receive narrow manifest parsing. Prompt 26 validates only bounded JSONL dataset candidates, returning content identity and field counts rather than input/reference values. Its validation cap is 32 candidates, 8 MiB per file and 32 MiB total. Candidate content is checked only under the approved `inspection_roots` and, when set, within `data_roots`. Test/evaluation/invocation paths remain path-only clues; arbitrary tests are not goldens and evaluator compatibility is unknown. Generated `synthetic_unverified` rows are not considered compatible datasets and still require the existing review/promotion workflow. Rust, Go, Java, C/C++, Ruby, PHP, C#, Swift, dynamic imports, and unknown patterns are not parsed and stay unknown. The emitted report includes evidence paths/lines, provenance, confidence, and the applied budgets. Prompt 25 defaults cap the walk at 5,000 files, 256 KiB per parsed file, 16 MiB read total, 2,000 directories, 2,000 entries per directory, depth 32, and 2,000 discovery records. Inspection never imports or executes repository code; `--probe` remains a separate, explicit runner action.
- **Evaluation opportunities and dataset choice** (Prompt 26): `aibench plan opportunities --app APP --dataset DATA --objective TEXT --json` reports metrics only when the existing policy, application profile and dataset coverage satisfy evaluator requirements. An inferred vector-store import or judge-only reference context does not establish runtime retrieval. Unknown objective wording produces a clarification; unavailable metrics remain unavailable. For a new chat session without a configured dataset, a sole compatible content identity can be reused only when the existing policy approves the project in `inspection_roots` and any configured `data_roots` also allow the candidate. Identical copies are one choice; materially different candidates require an explicit dataset choice (`--dataset` in headless chat). A configured dataset or CLI `--dataset` continues to take precedence. This repository inventory checks JSONL shape, not reference correctness, metric fit, or suite semantics.
- **Trace import** (Prompt 16): `traces import RUN FILE` / `traces show RUN` attach OpenTelemetry (OTLP/JSON) traces to executions by correlation ID. Partial traces stay partial, and parent spans are not double counted.
- **Opt-in caches** (Prompt 16): plan `cache: {executions, evaluations}`, with version-complete keys, provenance on every hit, and `cache list/clear`. Hits are excluded from latency and repeat claims.
- **Provider-aware quotas** (Prompt 16): plan `quotas` limit work in flight and the start rate for the application or evaluator globs, and back off on HTTP 429/503.
- **Stored-output rescoring**: `evaluate` and `/rescore` enforce the chosen plan's evaluator
  call, reported-token, wall-time, projected-cost and quota limits. `score` inherits those
  controls from the run's verified frozen plan; older smoke runs without a plan have no
  implicit ceilings. Each scoring pass receives its own allowance, separate from original
  execution spend. Retries consume that allowance; carried and inapplicable results do
  not. JSON output and persisted pass events include the allowance, accounting and stop
  reason. Time limits stop new dispatches; already started work retains its evaluation
  timeout. Unknown token use is reported as unenforced, and monetary limits remain soft
  estimates. Rescoring executes serially and never invokes the application.

- **Workspace portability and maintenance:** preview-first garbage collection, workspace
  backup/restore/check/repair, retention and usage reports, safe project-path migration, and
  portable run/session bundle import and export. See [upgrade and recovery](release/upgrade-and-recovery.md).
- **Distributed workers:** workers pull application invocations from an authenticated
  coordinator API and do not share its SQLite workspace. Matching BenchCraft/app versions
  are required and only reset mode `none` is supported. Cross-host performance evidence is
  still pending. See the [worker implementation](audit/2026-10-08/implementation/G22.3-distributed-workers.md)
  and [remote probe](audit/2026-10-08/implementation/G22.7-remote-worker-probe.md).
- **Whole-workflow usage attribution:** session reports separate application, evaluator,
  planner, and combined usage; unknown usage and unavailable planner prices remain explicit.
  See [G23](audit/2026-10-08/implementation/G23-whole-workflow-cost.md).

## Not available yet

| Feature or evidence | Status |
|---|---|
| Phase 2 time-saved study | Not measured; real trials against direct framework configuration are still needed |
| Website crawling or browser automation | Not supported; the configured JSON HTTP API runner is not a browser or generic URL evaluator |
| Source-code inspection beyond Python and JS/TS | Other languages, dynamic imports, and plugins loaded by name are not analyzed |
| Cross-host worker evidence | The coordinator/worker API exists, but real separate-host throughput and failure-rate measurements have not been run |
| Dedicated upstream `TranscriptionAccuracyMetric` integration | Deferred until a released package artifact contains the documented class; the current adapter scores bounded text transcripts only |
| Live model and judge behavior | No live provider-backed run or external pilot is included in the evidence; calibration labels still need human review |
| Planner dollar estimates | Provider prices are not available from token counts alone; reports preserve usage and mark unknown dollar costs explicitly |

## Known limits

- **Capacity evidence is workload-specific.** The earlier 1,000- and 3,000-case profiles in
  `docs/engineering/evidence/12/` and `20/` predate the current streamed compile path and are
  not current capacity guarantees. The current 1,000-case interrupt/resume probe completed
  all work at 13.2 MiB peak traced Python allocations in 90.6 s, compared with the audit
  sample's 17.1 MiB in 64.6 s. This is traced allocation, not RSS; the full-selected case
  snapshot still grows with the selected dataset. Million-case capacity has not been measured.
- **Distributed-worker measurements are synthetic and one-host.** The local HTTP-fixture
  harness recorded 27.22 invocations/s at worker capacity 1 and 72.44 at capacity 4 over
  100 calls with 20 ms fixture latency and zero failures; its rate-limit probe received seven
  HTTP 429 responses out of eight under the configured fixture quota. These numbers do not
  establish cross-host throughput or provider performance. The CLI also offers bounded
  open-loop/ramping profiles, but no external load study has been run.
- **Planner and judge measurements** (`aibench plan benchmark`, `aibench evaluators calibrate`) use fixture annotations and labels that no person has reviewed yet. The template planner misses objectives phrased without its keywords: 17/21 recall on the v1 fixture set, below the 0.85 target.
- **Claim checking** links every number in an assistant reply to a result queried in that turn, and flags numbers it can't trace. It shows where a number could have come from, not that the sentence around it is right. An explanation of why cases failed is a hypothesis unless a stored result states it.
- **Redaction** of credentials is pattern-based, as a safety net. Use secret references rather than relying on it.
- **Latency** is runner-measured wall time per request under the plan's concurrency. It is not a load test. Cache hits are excluded.
- **SQLite remains a single-writer workspace.** Coordinator workers persist run state, retry
  decisions, budgets, artifacts, and evaluations on the coordinator; workers do not share the
  workspace. Cross-host reliability and capacity have not been established.
- **Imported traces** are only as complete as the export. Usage from partial traces is a lower bound, and traces without a correlation ID stay unmatched.
- **Containers** run non-root, read-only, capability-free, resource-limited and offline by default, but they are not a hostile multi-tenant sandbox: they share the host kernel. Only Linux images were exercised, with Docker Engine 29.7.2 through Docker Desktop on Windows 11. The host `docker` client is fixed, engine-sensitive environment overrides are refused, and image pulls are disabled for each invocation. Per-episode state is not supported for containers.
- **OpenAI-compatible endpoints** were exercised against a local stub only. Tool calls from a model are recorded as requests, never as executed effects, and cost stays unknown.
- **Stateful applications** with a reset hook run one case at a time (`concurrency.application: 1`). Episode turns are never retried. After a failure or an interruption, the rest of that episode is blocked rather than replayed into unknown state.
- **Tool events and world state** are as the application or its test double reports them. The evaluators check what is reported, not what happened elsewhere.
- **A full disk or failing workspace** (disk full, quota, I/O error, read-only filesystem, database full) stops the run's dispatching and leaves it resumable (exit code 130), with the reason in the output and in `aibench runs status`. No case is marked failed for it. This is tested by making the real write path fail, not on a real full volume. Recovery steps: `docs/release/upgrade-and-recovery.md`.
