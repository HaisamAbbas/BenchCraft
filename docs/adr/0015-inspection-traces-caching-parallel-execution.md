# ADR 0015: Inspection, traces, caching and parallel execution

Status: Accepted
Date: 2026-09-24
Prompt: 16 — Inspection, traces, caching, and parallel execution

## Context

Before this prompt:
- §8 wanted inspection to separate what an application *declares*, what the harness
  *infers*, and what it has *observed*. `aibench inspect` only read the application config
  and recorded executions.
- §14 wanted imported observations such as OpenTelemetry traces. The engine could observe
  only what a runner captured.
- §15 wanted explicit caches and provider-aware concurrency. Every run re-invoked every
  case, and concurrency was a single per-kind cap with no knowledge of provider rate
  limits.
- §18 wanted parallel execution that stays controllable. A load test (below) showed the
  event loop blocking for 1.7 s, and the live terminal shares that loop.

## Decisions

### Source inspection infers, it never confirms (16-T1)

- `inspect --source DIR` reads only a tree inside the policy's `inspection_roots`, and is
  refused otherwise. It reads:
  - manifests (`pyproject.toml`, `requirements*.txt`, `package.json`);
  - Python imports, via the AST;
  - JS/TS `import`/`require`.
- It never reads secret-like files (`.env*`, keys, credentials, token files), binaries,
  files over 256 KB or vendored directories. It never follows symlinks, Windows directory
  junctions, or any entry that resolves outside the root. Skipped files are counted by
  reason.
- Every finding is `inferred` and carries evidence: path, line, kind, and one context out
  of `manifest`, `code`, `test_code`, `type_checking`, `guarded` or `commented_out`.
  Contexts that don't show runtime use add a caveat.
- **16-G1.** An inferred finding never satisfies an evaluator's applicability check. Only
  a declared binding or an observation can. The catalog treats a source finding as a
  missing-evidence hint.
- **Probes** (`inspect --probe N`) are the only way to turn a declaration into an
  observation:
  - they are engine smoke runs of N dataset cases, through the same runner and policy
    checks as a run;
  - the policy is checked before a workspace is opened, and the application's effects
    must be `none`;
  - a refused probe exits 4 and creates nothing.

  **Rejected:** letting the planner run shell commands to probe an application.

### Trace import keeps the raw data and the partiality (16-T2)

- `aibench traces import RUN FILE` accepts OTLP/JSON, as one document or as JSON Lines.
  - The raw bytes are stored as a RESTRICTED artifact.
  - Each trace is normalized under a versioned normalization (`otel-gen-ai/1`).
  - A trace is matched to an execution by correlation ID (`aibench.correlation_id`, or
    the `x-request-id` request header attribute).
  - Unmatched traces are kept and counted, never dropped.
- A trace is complete only if nothing shows it isn't. It is marked partial for:
  - a span whose parent is missing (`missing_parent:N`);
  - no root span (`no_root_span`);
  - a sampled-out trace (`not_sampled`);
  - dropped spans, events or links (`dropped:N`).
- Partial traces stay partial in storage, in `traces show` and in reports. Their usage is
  reported as a lower bound.
- **No double counting (16-G2).** Token usage is summed only over the lowest spans that
  report it. A span whose descendants also report usage is treated as an aggregate and
  excluded (and counted).
- **One observation per trace.** A run keeps one current observation per trace ID. A
  later file with more of a trace is merged with the earlier imports' raw spans, and the
  trace is normalized again. This covers an exporter that appends to its file, and a trace
  split across batches.
  - Exact duplicate spans are counted once.
  - Two different spans with the same ID (`conflicting_span_id`), or parent links that
    loop (`parent_cycle`), make the trace partial.
  - Usage on looping spans is left out.

  Re-importing the same file adds nothing.

### Caches are opt-in, version-complete and visible (16-T3)

- **Opt-in.** A plan opts in with `cache: {executions: bool, evaluations: bool}`. Nothing
  is cached by default.
- **Execution keys** hash:
  - the case input;
  - the repetition;
  - the application identity (config hash, including revision and transport);
  - the application's code: every source file in the directory tree of a CLI or Python
    entry point, its configured working directory, and explicit Python import paths;
  - the values of inherited and explicitly referenced secret environment variables (hashed,
    never stored);
  - the aibench version;
  - the frozen world seed;
  - the policy.

  An application whose code can't be read must declare `revision` or
  `environment_digest` for its executions to be cached. That covers HTTP and
  OpenAI-compatible endpoints, and entry points that aren't local source files. A
  An unmounted container uses its image digest as its code identity. A container with host
  bind mounts must declare `revision` or `environment_digest` and change it when mounted
  content changes.
- **Evaluation keys** hash:
  - the execution's evaluable fields (status, output, retrieved context, tool events,
    world state, usage, cost, observation completeness);
  - the whole case, including its references;
  - the binding (evaluator, version, parameters, rubric);
  - the plugin version;
  - the policy;
  - the repetition, so a judge's repeats stay independent within a run.
- **16-G3.** A change to any of those is a miss. The invalidation matrix test changes
  each one in turn.
- **Where caching is refused.** Execution caching is refused at compile time for:
  - applications with effects and no test world;
  - per-episode reset;
  - shared state.

  Replaying those would claim an effect or a state that didn't happen.
- **Provenance.** A cache hit is stored as a new result with `cache` provenance (the key,
  and the source run and record). A cached execution has effect state `not_dispatched`.
  A cached evaluation reports 0 model calls. A `cache_hit` event is recorded.
- **Claims.**
  - Reports count cache hits and exclude them from latency and from any
    independent-repetition claim. The report says so next to the latency.
  - A comparison involving a run with cached executions is blocked
    (`cached_executions_present`). Cached evaluations are reported without blocking.
  - A cache hit records no usage event: its spend belongs to the source run.
- `aibench cache list/clear [--kind]` manages entries.

### Quotas throttle before a task exists (16-T4)

- **Quotas.** A plan declares named quotas. Each applies to `application`, or to
  `evaluator:<glob>` (e.g. every judge using one provider). Each has an optional
  `max_in_flight`, and a token bucket (`requests_per_second`, `burst`).
- **Checked before dispatch.** The engine consults every matching gate before it creates a
  task. Throttled work stays a queue entry, never a sleeping coroutine, so the number of
  tasks is bounded by the concurrency caps whatever the queue length.
- **Backpressure.** A provider's HTTP 429 or 503 pauses every quota the call ran under,
  for `Retry-After` (capped by the quota's `max_backpressure_seconds`, default 60 s) or
  the quota's `backoff_seconds`. It records a `backpressure` event, and the existing retry
  policy retries the call.
  - **Evaluator quotas limit, but don't react.** They bound work in flight and the start
    rate, but can't react to a judge's 429: the evaluation contract has no throttling
    signal, and the adapters retry internally. Adding one is recorded as a follow-up.
- **No head-of-line blocking.** An evaluation held by its quota doesn't hold back other
  evaluators. Dispatch looks past up to 256 held entries per pass.
- **Cancellation.** Pause and cancel still work while throttled. The engine waits on the
  controller, not on the bucket. A cancelled run consults no quota, so queued evaluations
  are recorded as cancelled at once.
- **Terminal responsiveness.** The load test measured 1.7 s event-loop stalls. They are
  fixed:
  - httpx client creation (SSL and certifi loading) runs in a thread;
  - capture file writes and verification run in a thread;
  - the httpx/anyio backends are imported up front.

  Measured lag is now ≤ 0.25 s under 16-way load. SQLite writes stay on the loop's thread
  (single writer).
  - **Races from threaded writes.** Two consequences needed handling:
    - Identical capture bytes written from two threads could collide on Windows. Writers
      of one digest now take turns, and a replace refused because another process
      committed the same digest keeps that identical file.
    - Recording an answered call is shielded from cancellation, so a dispatched attempt
      is always committed, as before.
- **Summaries.** Quota summaries (started, max in flight seen, backpressure events) are
  recorded in `run_session_ended`.

## Consequences

- **Workspace schema 8** adds `trace_observations` and `cache_entries`. Older workspaces
  migrate forward. See the changelog.
- A cached run is cheaper, but its latency and repeat counts cover only fresh executions.
- **Capacity.** Throughput was measured only against a local rate-limited server (45 cases
  at a 15 rps quota). **We make no claim about production-scale capacity (e.g. million-case
  runs)**. The engine still keeps all work items in memory, and SQLite has a single
  writer.
- Source inspection covers Python and JS/TS manifests and imports. Other languages give no
  findings, and the report states that as a limitation.
