# Direct run controls and dry-run

`aibench run` accepts common run controls without editing the plan file. The CLI applies
them to a copy of the plan, validates the full effective plan, and freezes that exact plan
into the run. Policy checks still run after overrides, so CLI values cannot bypass the
workspace's execution policy.

```console
aibench run --plan benchmark.plan.json --limit 100 --repetitions 3
aibench run --plan benchmark.plan.json --sample-size 100 --selection-seed 42
aibench run --plan benchmark.plan.json --application-concurrency 4 --evaluation-concurrency 8
aibench run --plan benchmark.plan.json --max-attempts 2 --max-app-calls 300 `
  --max-evaluator-calls 600 --max-wall-seconds 900
aibench run --plan benchmark.plan.json --max-cost-usd 2.00 `
  --estimated-cost-per-app-call-usd 0.002
aibench run --plan benchmark.plan.json --no-cache-executions --cache-evaluations
aibench run --plan benchmark.plan.json --run-seed 1729
aibench run --plan benchmark.plan.json --open-loop-rps 20
aibench run --plan benchmark.plan.json --ramp-start-rps 2 `
  --ramp-end-rps 40 --ramp-duration-seconds 60
```

`--limit` and `--sample-size` are mutually exclusive. Both operate after the plan's case IDs
and predicates: a limit keeps the first matching cases, while a sample uses the stable seeded
selection algorithm. `--sample-size` needs `--selection-seed` unless the plan already has a
sample seed. Choosing either direct selector replaces the plan's limit/sample setting while
preserving its case IDs and predicates.

Concurrency, repetitions, attempts, timeouts, budgets, and cache flags replace the matching
plan field. Existing policy ceilings and plan validators apply to their effective values.
`--max-cost-usd` is a soft projected-cost limit and requires a declared or directly supplied
application-call estimate; model-backed evaluations also need an evaluator-call estimate.
Advanced retry backoff, quotas, plugin settings, and release gates remain plan fields for
headless execution. Conversation drafts can also declare release gates; see
[support](support.md#conversation-release-gates).

`--run-seed` sets the engine seed separately from `--selection-seed`, which only controls case
sampling. It affects seeded engine randomness such as retry backoff; it cannot make an
external application or evaluator deterministic. See
[run reproducibility and provenance](reproducibility.md) for what the run records.

Add `--dry-run` to validate all overrides and print the exact effective plan, its frozen
hashes, selected case IDs, metric bindings, and planned execution/evaluation counts without
creating a run or dispatching application/evaluation work. `--json` wraps this preview in the
versioned CLI output envelope. A dry-run has no run ID or run seed because neither is created
until execution begins.

Reports include p99 and dispersion, throughput, retry-inclusive elapsed latency, and separate
warmup measurements. Their exact denominators and completeness rules are in
[performance measurements](performance-measurements.md).

The plan's `load_profile` defaults to `{"mode":"closed_loop"}`: a measured request starts
when an application-concurrency slot is free. Set
`{"mode":"open_loop","requests_per_second":20}` for a fixed offered rate, or use
`{"mode":"ramp","start_requests_per_second":2,"end_requests_per_second":40,"ramp_duration_seconds":60}`
to increase the rate linearly and then hold 40 requests per second. The direct CLI flags shown
above override the plan; use `--closed-loop` to restore closed-loop dispatch for one run.
Ramp options must be supplied together and cannot be combined with fixed-rate or closed-loop
options. Open-loop and ramp profiles reject execution caching because a cache hit is not a
fresh application request. The engine keeps its configured concurrency and bounded work queue;
when those limits cannot keep up, later arrivals wait and the report records their dispatch
lag. Warmups finish before the profile clock starts, and retries keep their normal retry
backoff without consuming arrival slots.
