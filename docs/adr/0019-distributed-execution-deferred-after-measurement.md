# ADR 0019: Distributed execution is deferred after measurement

Status: Accepted
Date: 2026-09-25
Prompt: 20 — Distributed execution after measured need

## Context

- **What the spec and prompt allow.** §15 and §19 allow distributed workers (a durable
  queue, PostgreSQL task leases with fencing tokens, object storage and idempotent logical
  commits) only "after measured local bottlenecks". Prompt 20's first ticket (20-T1) asks
  for the bottleneck to be measured, and for the target workload, resource budget and
  failure model to be defined. If no distribution need is demonstrated, 20-T1 says to
  deliver the findings and mark distributed implementation deferred.
- **What the harness does today.** Everything runs in one process, on one host:
  - a SQLite workspace in WAL mode, with one writer;
  - a single-session lease per run (`run_leases`, 06-T1), heartbeated and taken over
    only when stale;
  - idempotent logical commits keyed by stable task identities;
  - bounded application and evaluator concurrency, up to 64 each (16-T4).

## Measurement (20-T1)

`scripts/measure_capacity.py` runs the real `aibench run` CLI, one fresh project per
scenario, against the loopback rate-limited fixture app. The app's quota is set far above
the offered load, and its response delay stands in for application latency. The metric is
the deterministic `native.exact_match`. CPU is accounted for the whole process tree
through a Windows job object. Raw results are in
`docs/engineering/evidence/20/capacity.json`.

**Machine.** Windows 11 Pro (10.0.26200), Intel64 Family 6 Model 170, 14 logical CPUs,
31.5 GB RAM, Python 3.12.10. The workspace was on a local NVMe SSD (Solidigm
SSDPFKNU512GZH), identified with `Get-PhysicalDisk`; the evidence JSON does not record it.

**Contention.**
- Other sessions ran test suites during the whole measurement.
- The script's system-CPU probe read a constant 0.857 (12 of 14 logical CPUs busy) before
  and during every scenario, idle probes included. The Windows `% Processor Time`
  counter agreed.
- So the probe shows the background load, not the harness's own share. The harness ran
  time-shared with that load, on cores of mixed type.
- Absolute numbers are therefore specific to this loaded machine, and the per-case CPU
  depends on core type. The comparisons between scenarios are the robust part.

**Workload.** 300 cases per scenario, each scenario run twice (`--cases 300 --repeat 2`,
the default scenario set). One deterministic metric; evaluation concurrency 4.

| App delay | App concurrency | Cases/s (runs 1, 2) | Latency-bound cases/s | Peak in flight at server | Engine CPU (cores) | Harness CPU per case |
|---|---|---|---|---|---|---|
| 0 | 1 | 8.5, 7.6 | — | 1 | 0.34–0.35 | 40–46 ms |
| 0 | 16 | 23.0, 22.2 | — | 2–4 | 1.10–1.12 | 49–50 ms |
| 0 | 64 | 18.3, 22.8 | — | 5 | 1.05–1.17 | 51–58 ms |
| 0.25 s | 64 | 21.8, 19.1 | 256 | 18 | 1.07–1.17 | 54–56 ms |
| 1 s | 16 | 11.8, 11.6 | 16 | 16 | 0.60–0.63 | 51–54 ms |
| 1 s | 64 | 13.6, 14.0 | 64 | 38–45 | 0.81–0.87 | 58–64 ms |

- **Column definitions.**
  - "Latency-bound" is what the application's latency alone would allow at that
    concurrency.
  - "Engine CPU" is the process tree's CPU time, minus a separately measured
    `aibench --version` start-up, divided by the engine's elapsed time.
  - "Harness CPU per case" is that same net CPU (user plus kernel time) divided by the
    number of cases.
  - Both still include the run's fixed work outside the engine's elapsed time (compile,
    migrations, run creation). At 300 cases, they overstate the marginal cost by a few
    ms and by roughly 0.05–0.1 core.
  - The marginal cost between the 300- and 3,000-case runs at concurrency 16 is 54 ms
    per case. That is 162.7 minus 16.4–16.7 CPU-seconds, over 2,700 cases, from the two
    JSON files.
- **Per case.** Peak memory (peak private commit, in MiB) was 61–64 MiB in every
  scenario. The workspace grew by about 16 kB of database and 0.6 kB of artifacts per
  case.
- **Start-up.** CLI start-up (`aibench --version`) took 7.5 s wall and 1.8 s CPU on this
  machine, and is outside the engine's elapsed time.
- **Profile.** A profile of one concurrency-64 run (main thread only;
  `docs/engineering/evidence/20/profile-300-c64.txt`) shows:
  - **SQLite:** 20,138 statements, about 67 per case. Their 7.6 s is about 28% of the
    27.5 s elapsed time under profiling. The count and time include migrations and run
    creation, and the time includes waiting for disk flushes, not only CPU.
  - **Transactions:** each logical commit is its own WAL transaction under SQLite's
    default `synchronous=FULL`. From the call counts, a case flushes about 12 times. That
    is an estimate; transactions were not counted directly.
  - **HTTP client:** 4.5 s (11%) of own time.
  - **The rest** is mostly one-time imports and TLS set-up.
- **Scale point.** One 3,000-case run at concurrency 16, with no delay
  (`capacity-3000.json`, `--cases 3000 --scenario 0:16`), completed all 3,000 executions
  and evaluations at 23.5 cases/s. That is the same rate as at 300 cases. It used 54 ms
  of harness CPU per case, and the workspace reached 43.9 MB of database (14.6 kB per
  case) and 1.7 MB of artifacts.
  - **Memory grew.** Peak memory was 184.6 MiB, against about 62 MiB at 300 cases. That
    is about 46 KiB more per case, consistent with (not measured to be caused by) the
    engine holding every work item in memory.
  - **Extrapolation, not measured.** A straight line from these two points gives about
    44 GiB of memory and 15 GB of database for one million cases. That is more than this
    machine's 31.5 GB of RAM.

## Findings

1. **There is a real local ceiling.** At concurrency 16–64, with no delay or 0.25 s of
   application delay, one harness process on this machine completes about 18–23 cases/s,
   using about one core.
   - At concurrency 1 it completes about 8 cases/s, and is bound by per-case latency.
   - For an application answering in 1 s at concurrency 64, it delivers 14 cases/s of the
     64 the application could take, and the server never saw more than 45 requests in
     flight.
   - For fast applications at high concurrency, the harness binds, not the application.
2. **The spec's million-case scenario is not achievable on one host today.**
   - The §15 illustration (20 cases/s, one million cases in 13.9 hours) is met only by an
     instant application at concurrency 16 or more. With 1 s application latency, the
     harness delivers 12–14 cases/s, below 20.
   - A million-case run would also need about 44 GiB of memory (extrapolated), beyond
     this machine.
   - Stated plainly: the local harness falls short of that illustration.
3. **Distribution would parallelize this cost, not reduce it, and is not the cheapest way
   to parallelize it.**
   - The cost per case is about 50–64 ms of CPU. Part of it is durable bookkeeping:
     SQLite statements and flushes, events, and artifact writes and verification. Part is
     the HTTP client.
   - Distributed workers would run that cost in parallel across hosts. Each worker would
     still pay the same cost, plus a network round trip to PostgreSQL for each fenced
     commit, which has not been measured.
   - Three levers are cheaper and keep the SQLite contract, and all are untried:
     - batching transitions and events into fewer transactions (group commit), which
       would cut the flushes per case;
     - loading work items in bounded batches, which §15 already requires ("do not
       materialize a million tasks") and which targets the memory growth;
     - running several engine processes on one host behind the single writer.
4. **No declared workload needs more.**
   - No user has declared a workload (dataset size, application latency and quota,
     evaluators and deadline) that this ceiling misses. Pilot users have not been
     contacted, and no real application's throughput was measured.
   - For applications behind a provider quota, the quota binds first, and distribution
     cannot raise a per-account quota.

## Decision

- **Distributed execution is DEFERRED.** 20-T1's condition for building it, a
  demonstrated distribution need, is not met.
  - **Why deferred.** The measured shortfall (Findings 1 and 2) is real. But no declared
    workload depends on closing it (Finding 4), and cheaper single-host levers that
    target the measured costs have not been tried (Finding 3).
  - **This is not a claim that one host is enough.** It is a claim that distribution is
    not yet the justified next step.
- **What is not built.** No PostgreSQL coordinator, durable queue, object store or remote
  worker was built (20-T2), and their recovery was not exercised (20-T3). None of them is
  claimed.
- **What is delivered.**
  - **20-T1:** the measurement tool, its evidence, and the contracts below.
  - **20-T4 (partial):** single-host scale evidence against a local mock. Real
    application throughput and monetary cost were not measured.
- **Docker for PostgreSQL.** Docker is available locally, so a PostgreSQL container would
  cost nothing. It was still not used: building the coordinator without a need is what
  20-T1 rules out.
- **Reopening.** The conditions under "Target workload" below must be met, with a
  measurement taken after the local levers.

## Target workload, resource budget and failure model

These are the contracts a distributed implementation would have to meet. They are recorded
now, so that reopening this work starts from a stated need rather than a scale claim.

### Target workload that would justify distribution

Distribution is justified only when all three conditions hold:

1. **A real workload.** A user has a declared workload: a dataset size, the application's
   latency and quota, the evaluators and a deadline. Illustrative arithmetic does not count.
2. **The harness is the constraint.** On one host, the harness is what binds. The measured
   local ceiling (cases per second, after the local levers below) is below
   `min(application concurrency / application latency, provider quota)`. Distribution cannot
   raise a provider's per-account quota. If the quota binds, more workers only produce more
   429 responses.
3. **The deadline cannot be met locally.** At the measured local ceiling, the workload
   misses its deadline, with the local levers already applied.

### Resource budget

- **Single host (the current design).** Measured per-case costs: harness CPU, database
  bytes, artifact bytes and peak memory. At N cases, a run needs roughly N × those costs
  on one disk.
- **A distributed deployment** would add a PostgreSQL instance, an S3-compatible object
  store and W worker hosts. Each worker costs the same per-case harness CPU, plus a network
  round trip per commit.
- **No cloud resources are provisioned or priced** without authorization.

### Failure model a distributed coordinator must survive (20-T2/T3 contract)

| Failure | Required behaviour |
|---|---|
| Duplicate delivery of a task | At-least-once computation. The logical result commit is keyed by the stable task key, so a second commit with the same content is a no-op and one with different content is a conflict. It is never a second result (20-G2). |
| Lease expiry while the worker is alive (a GC pause or partition) | The task's lease carries a monotonically increasing fencing token. A commit must present the current token. A late worker holding an older token is refused, even if its computation finished (20-G1). |
| Worker or host death | The lease expires, and the task is re-queued with a new attempt record. The earlier attempt and its cost are kept. An effectful application's in-flight call becomes `unknown_effect`, and is not blindly retried (§15). |
| Partial artifact upload | Bytes are uploaded under a temporary key and verified by digest. Only then does the result transaction reference them. An unreferenced object is an orphan for garbage collection. It is never a half-written result (§14). |
| Coordinator (PostgreSQL) unavailable | Workers stop claiming, and in-flight results stay local until a commit can be fenced. Nothing is reported as committed before it is. |
| Pause or cancel during failures | Run state is authoritative in the coordinator. Workers check it before each claim and each commit. Pause stops claims. Cancel stops claims and records in-flight work as cancelled or unknown-effect. A worker returning after a cancel cannot commit (20-G3). |
| Exactly-once external effects | Never claimed. The harness deduplicates only its own internal result commits. |

## Consequences

- **Gates.**
  - 20-G1, 20-G2 and 20-G3 (fencing, duplicate commits, pause and recovery under
    distributed failure) are **not satisfied and not applicable** while the implementation
    is deferred. They are not reported as passed. The local equivalents stay covered by
    the existing tests: single-session run leases (06), idempotent logical commits, and
    pause, cancel and resume.
  - 20-G4 holds. Every number above names its workload, machine and contention. The
    support notes label these figures as a different workload from the Prompt 12
    figures they sit next to.
- **Local ceiling.** `docs/support.md` records the measured local ceiling and its cause.
- **Recommended next local steps.** Load work items in bounded batches, and group commits
  in the engine's writer. Then re-measure
  with `scripts/measure_capacity.py`. These are not part of Prompt 20. They change the
  engine's scheduling and durability pattern for every run, so they need their own ticket
  and crash-recovery tests.
- **Schema.** No schema change, dependency or service was added.

## Changes after independent review

An adversarial review checked every number against the raw evidence. It confirmed the
job-object struct layouts (48 and 144 bytes on Win64) and the database size accounting.
It found the following, all changed:

- **Overclaims corrected.**
  - "Within the spec's 20 cases/s" is now the plain shortfall in Finding 2.
  - "18–23 cases/s whatever the concurrency" is now stated per scenario.
  - "Distribution does not remove the cost" is now "distribution parallelizes it".
  - 20-T4 is now partial.
- **Comparison removed.** A comparison with Prompt 12's 1,000-case figure was removed. That
  workload was different: in process, a different fixture and metrics, evaluation
  concurrency 16, an interrupt and resume, and `tracemalloc` enabled.
- **Units and hedges.**
  - MiB and KiB are now labelled, and the extrapolation is about 44 GiB.
  - The profile's SQLite share is marked as including set-up and flush waits.
  - The flushes per case are marked as an estimate.
  - The fixed per-run CPU inside the per-case figures is disclosed, and the marginal
    cost is given.
- **Script.**
  - Win32 calls are now checked, so a failure raises instead of reading 0 or hanging.
  - On POSIX, peak memory and CPU come from `os.wait4` for each child. Previously,
    `RUSAGE_CHILDREN` carried the largest earlier peak into later scenarios.
  - Each scenario records what its memory figure measures, and the document records its
    command line.
  - The Windows accounting that produced the evidence is unchanged. The evidence files
    predate the `argv` and `peak_memory_kind` fields, so their commands are stated in
    this ADR.
- **Test.** The test now runs two scenarios and asserts per-scenario memory, and that each
  run's CPU is at least the start-up CPU.
