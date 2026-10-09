# Regression policies for CI

`aibench compare BASELINE CURRENT --regression-policy policy.json` applies tolerances
that should be reviewed and committed before a candidate run. It operates on the same
strict, storage-only comparison as `compare`; it never invokes the application or evaluator.
Exploratory comparisons cannot apply a release policy.

Policy files are JSON or safe YAML mappings. A policy declares one or more native-unit
metric degradation limits, an optional p95 latency increase in milliseconds, and an
optional total observed application cost increase in USD:

```json
{
  "schema": "aibench.regression-policy/1",
  "metric_rules": [
    {"metric_id": "native.exact_match", "max_degradation": 0.02}
  ],
  "max_latency_p95_increase_ms": 100,
  "max_application_cost_increase_usd": 0.05
}
```

For higher-is-better metrics, degradation is baseline minus current; for lower-is-better
metrics, it is current minus baseline. Negative degradation is an improvement. The policy
compares the case-macro point estimate against the predeclared maximum, and includes the
comparison's bootstrap interval as context in the result. Equality with the maximum passes.
Unknown, target-oriented, and non-directional metrics cannot be assigned a numeric
degradation tolerance.

Latency uses the nearest-rank p95 of final successful uncached request times, and every such
request must have a valid non-negative timing measurement. Cost includes all committed
dispatched attempts; a total is measurable only when at least one dispatch exists and every
dispatch has a known cost. Interrupted uncommitted dispatches count as unknown-cost calls.
Missing, ambiguous, incompatible, under-covered, or incomplete data produce an
`undetermined` rule instead of a pass.

The JSON result contains `regression_gate` with the policy content hash, each observed
difference, limit, status, and any reason a rule could not be evaluated. Exit codes are:

| Code | Meaning |
|---:|---|
| 0 | All requested comparison and regression rules pass |
| 1 | Paired coverage or a regression tolerance fails; paired-coverage failure takes precedence over an undetermined rule |
| 2 | Strict comparison is blocked, or the policy input is invalid |
| 3 | Required policy measurements are incomplete or indeterminate |

Without `--regression-policy`, the existing comparison exit behavior is unchanged.
