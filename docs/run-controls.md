# Direct run controls and dry-run

`aibench run` accepts common scalar controls without editing the plan file. The CLI applies
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
Advanced retry backoff, quotas, plugin settings, and release gates remain plan fields.

`--run-seed` sets the engine seed separately from `--selection-seed`, which only controls case
sampling. It affects seeded engine randomness such as retry backoff; it cannot make an
external application or evaluator deterministic. See
[run reproducibility and provenance](reproducibility.md) for what the run records.

Add `--dry-run` to validate all overrides and print the exact effective plan, its frozen
hashes, selected case IDs, metric bindings, and planned execution/evaluation counts without
creating a run or dispatching application/evaluation work. `--json` wraps this preview in the
versioned CLI output envelope. A dry-run has no run ID or run seed because neither is created
until execution begins.
