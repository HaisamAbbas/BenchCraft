# Support and limitations

This covers what aibench 0.1.0rc1 (the MVP release candidate) supports, what has actually
been tested, and what isn't available yet. The release decision and its open items are in
`docs/engineering/release-readiness.md`. The authoritative design is `docs/spec/implementation-plan.md`, and
per-prompt evidence is in `docs/engineering/reports/`.

## Platforms

| | Supported | Tested |
|---|---|---|
| Python | 3.11, 3.12 | Full suite on 3.12.10 (Prompt 12); clean wheel install, quickstart and 100-case acceptance on 3.12.10 and 3.11.16 (Prompt 13) |
| OS | Windows, Linux, macOS | Windows 11. Linux is configured in CI (`ubuntu-latest`), but no result has been observed. macOS is not run anywhere |
| Terminal chat | Any terminal prompt_toolkit supports | Windows ConPTY (automated tests, including resizing) |
| Model providers | OpenAI-compatible endpoints | Scripted and local test providers only. No live model has been exercised |

Details are in `docs/engineering/platform-matrix.md`. `aibench doctor` warns on a platform
outside the tested matrix.

## Optional evaluators

The core install ships the native evaluators: `native.exact_match`, `native.json_schema`
and custom Python evaluators you trust. Evaluator frameworks are never imported into the
aibench process. They run in their own environment, driven by a worker.

**DeepEval faithfulness** (`deepeval.faithfulness@1`, pinned to `deepeval==4.2.5`). From
the repository root:

```bash
python -m venv plugins/deepeval/.venv
plugins/deepeval/.venv/Scripts/pip install -e . -e plugins/deepeval    # Windows
plugins/deepeval/.venv/bin/pip install -e . -e plugins/deepeval        # Linux/macOS
```

A plan uses it through `plugin_environments`, and the policy must allow each of these:

- `allowed_plugin_environments`: that interpreter;
- `allowed_evaluators`: `deepeval.*`;
- `allow_model_evaluators`: `true`;
- `allowed_secret_refs`: the judge's key reference.

The judge model is a paid external call. The adapter's behaviour is described in
`plugins/deepeval/README.md`. Faithfulness needs the app to report the passages it
actually retrieved (`output_binding.retrieved_context`). The quickstart app does, so its
dataset works with faithfulness once a judge is configured.

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
open multi-modal SSRF advisory; the adapter does not expose the affected metric and runs in
an isolated worker. See ADR 0013 and `plugins/ragas/README.md`.

OpenAI Evals and Promptfoo are not integrated.

## Run comparison

`aibench compare BASELINE CURRENT` reads stored executions and metric results only. It never
calls the application, evaluator or judge. Strict mode pairs `(case_id, repetition_id)`,
checks dataset/case, repetition, metric/plugin, judge/rubric and instrumentation identities,
and blocks an unqualified claim when they differ. `--mode exploratory` is visibly
non-qualified. `--baseline-scoring` and `--current-scoring` select explicit stored scoring
passes, which is how two passes over the same run are compared.

Complete-pair coverage must pass the predeclared threshold before a result supports a
quality claim (`claim_qualified=true`). Numeric differences are case-level macro averages
with a seeded grouped bootstrap interval. DeepEval and Ragas results are shown as separate
frameworks; their scores are never averaged or treated as equivalent. Conversation exposes
the same service through `compare_runs` and `/compare BASELINE CURRENT`, restricted to runs
started by that session. Ragas' cold catalogue import is bounded by worker preparation; use
an explicit startup-timeout override on unusually slow hosts.

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

## Supported now

- **Conversational benchmarking:** in a terminal, `aibench`/`aibench chat` handles planning, runs, live status, pause/resume/stop, failures, case evidence and reports. Sessions survive exit and crashes, and are never restarted automatically.
- **Headless commands:**
  - `init`, `doctor`;
  - `dataset validate`, `inspect`;
  - `plan`, `plan validate`, `plan benchmark` (planner fixture set);
  - `run`, `resume`, `evaluate` (rescore stored outputs);
  - `runs list/show/status`, `report`, `compare`, `benchmark`;
  - `sessions list/show/delete`, `evaluators list/describe/plugin/calibrate`, `plugins list`, `score`, `app describe/smoke`.
- **Reports:** JSON, Markdown and static HTML, from stored facts. They include typed metric profiles, full denominators, release gates, latency definitions, cost completeness and case evidence.
- **Release gates** in plans (`gates`): a minimum pass rate or minimum completed coverage for one metric binding, always over selected cases.
- **Application transports** (Phase 2, Prompt 15): CLI, HTTP, Python callable (`python`), container (`container`) and OpenAI-compatible endpoint (`openai_compatible`). See `docs/runner-protocol.md`.
- **Stateful applications and agents** (Prompt 15):
  - reset hooks, with `per_case`, `per_episode` (cases sharing a `group_id`) and `shared` state;
  - named test worlds whose seeds are frozen with the run;
  - `world_state` observations;
  - three separate outcome metrics: `native.tool_calls` (names), `native.tool_outcomes` (arguments, success, authorization) and `native.final_state` (world state).

  In chat, `/app` explains what the runner observes and what evidence is missing, and `/world NAME` selects an approved test world.
- **Evidence-backed inspection** (Prompt 16): `inspect --source DIR --policy P` reads manifests and imports from a tree the policy approves (`inspection_roots`), and never reads secret files. Findings are *inferred*, with file and line, and never count as a confirmed capability. `inspect --probe N` turns declarations into observations by running N cases through the runner, under the policy.
- **Trace import** (Prompt 16): `traces import RUN FILE` / `traces show RUN` attach OpenTelemetry (OTLP/JSON) traces to executions by correlation ID. Partial traces stay partial, and parent spans are not double counted.
- **Opt-in caches** (Prompt 16): plan `cache: {executions, evaluations}`, with version-complete keys, provenance on every hit, and `cache list/clear`. Hits are excluded from latency and repeat claims.
- **Provider-aware quotas** (Prompt 16): plan `quotas` limit work in flight and the start rate for the application or evaluator globs, and back off on HTTP 429/503.

## Not available yet

| Feature | Status |
|---|---|
| Phase 2 time-saved study | Not measured. The repeatable comparison workflow exists; §18's demonstrated time saved over direct framework configuration still needs real trials |
| Dashboard or web app | Not planned for the MVP. Reports are static files |
| Release gates in conversation drafts | Gates are authored in plan files. A session's draft can't declare them yet |
| Planning and conversation cost per run | Tracked per session (`/budget`), not attributed to a run's report |
| Source-code inspection beyond Python and JS/TS | Other languages, dynamic imports and plugins loaded by name are not seen |
| Distributed workers, detached runs | Not available. A run stops dispatching when its terminal exits and resumes on request |
| Session export | Not available. Sessions can be deleted (`aibench sessions delete`), and deletion keeps run records |
| Live model checks | The conversation, DeepEval adapter and Ragas adapter are tested against scripted or deterministic local judges. No live provider run is part of this evidence |

## Known limits

- **Engine throughput** is low. On the development machine (Windows 11, 14 CPUs), 1,000 cases took between 152 and 233 s at concurrency 16 against an instant local service. Each call waits on a fresh HTTP connection and on its capture artifacts being durably written and verified. Profiles are in `docs/engineering/evidence/12/`.
- **Planner and judge measurements** (`aibench plan benchmark`, `aibench evaluators calibrate`) use fixture annotations and labels that no person has reviewed yet. The template planner misses objectives phrased without its keywords: 17/21 recall on the v1 fixture set, below the 0.85 target.
- **Claim checking** links every number in an assistant reply to a result queried in that turn, and flags numbers it can't trace. It shows where a number could have come from, not that the sentence around it is right. An explanation of why cases failed is a hypothesis unless a stored result states it.
- **Redaction** of credentials is pattern-based, as a safety net. Use secret references rather than relying on it.
- **Latency** is runner-measured wall time per request under the plan's concurrency. It is not a load test. Cache hits are excluded.
- **Parallel execution** was measured only against a local rate-limited server: a 15 rps quota held 45 cases to at most 17 starts per second and 3 in flight, with event-loop lag under 0.25 s. This says nothing about production-scale capacity; no million-case throughput is claimed. Work items are held in memory, and the workspace has a single SQLite writer.
- **Imported traces** are only as complete as the export. Usage from partial traces is a lower bound, and traces without a correlation ID stay unmatched.
- **Containers** run non-root, read-only, capability-free, resource-limited and offline by default, but they are not a hostile multi-tenant sandbox: they share the host kernel. Only Linux images were exercised, with Docker Engine 29.7.2 through Docker Desktop on Windows 11. The host `docker` client is fixed, engine-sensitive environment overrides are refused, and image pulls are disabled for each invocation. Per-episode state is not supported for containers.
- **OpenAI-compatible endpoints** were exercised against a local stub only. Tool calls from a model are recorded as requests, never as executed effects, and cost stays unknown.
- **Stateful applications** with a reset hook run one case at a time (`concurrency.application: 1`). Episode turns are never retried. After a failure or an interruption, the rest of that episode is blocked rather than replayed into unknown state.
- **Tool events and world state** are as the application or its test double reports them. The evaluators check what is reported, not what happened elsewhere.
- **A full disk or failing workspace** (disk full, quota, I/O error, read-only filesystem, database full) stops the run's dispatching and leaves it resumable (exit code 130), with the reason in the output and in `aibench runs status`. No case is marked failed for it. This is tested by making the real write path fail, not on a real full volume. Recovery steps: `docs/release/upgrade-and-recovery.md`.
