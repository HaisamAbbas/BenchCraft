# G20.2 — Policy-gated live judge calibration

`aibench evaluators calibrate --live` runs model-backed evaluator bindings from a reviewed
calibration set through the configured plugin worker environment. Live mode requires all of:

- at least one case, with `review.status: reviewed` and a nonblank `review.reviewer`;
- an explicit `--policy` approving the plugin interpreter, every requested secret and import
  path, the calibration directory under `data_roots` when configured, and each evaluator
  (including `allow_model_evaluators: true`);
- an explicit `--plugin-env` so third-party code runs through the existing isolated worker
  protocol;
- a bounded `--timeout` for each evaluator run and `--max-model-evaluations` for the total
  number of case-by-repeat model-evaluator invocations. The policy's
  `ceilings.max_evaluator_calls`, when set, also bounds all planned case-by-repeat evaluator
  invocations.

Calibration currently has no application phase, and its runtime does not yet enforce token,
cost or whole-command wall-time ceilings. If a live policy configures `max_judge_tokens`,
`max_cost_usd` or `max_wall_seconds`, the command refuses before plugin discovery instead of
silently ignoring that ceiling. `max_application_calls` does not apply because calibration
does not invoke an application.

For example, after a person has reviewed the labels and the policy contains the required
approvals:

```json
{
  "allowed_plugin_environments": ["../.aibench/plugins/judges/.venv/Scripts/python.exe"],
  "allowed_plugin_paths": [],
  "allowed_secret_refs": ["env:OPENAI_API_KEY"],
  "allowed_evaluators": ["deepeval.faithfulness"],
  "allow_model_evaluators": true
}
```

Policy paths resolve relative to the policy file. The interpreter approval keeps the venv's
own interpreter entrypoint even when it is a symlink, matching the executable path used by
the plugin worker.

```powershell
aibench evaluators calibrate `
  --set benchmarks/judges/reviewed-v2 `
  --live `
  --policy policies/live-judges.json `
  --plugin-env .aibench/plugins/judges/.venv/Scripts/python.exe `
  --plugin-secret OPENAI_API_KEY=env:OPENAI_API_KEY `
  --repetitions 3 `
  --max-model-evaluations 90 `
  --timeout 300 `
  --out artifacts/judge-calibration.json `
  --json
```

Plugin-environment, extra-path and secret approvals are checked before plugin discovery. The
review state is checked before worker startup; evaluator permissions are checked after
manifests are read and before any case evaluation. A denial exits with code 4 and includes
the specific policy reasons. The reviewed case snapshot is the same snapshot evaluated after
plugin discovery, so worker startup cannot replace the file between authorization and use.
Machine-readable failures use the common `aibench.cli-error/1` shape. Plugin options without
`--live` are rejected.

`--max-model-evaluations` bounds evaluator invocations (cases with model-backed bindings
multiplied by repetitions), not upstream provider requests; a plugin may make more than one
provider call per invocation. The report therefore separates evaluator invocations from
plugin-reported provider calls, and keeps unreported cost/call data unknown. Per-evaluation
timeouts do not bound plugin environment startup; that has its separate
`--plugin-startup-timeout` option. All calibration reports identify their mode as `offline`
or `live`. A model judge is removed from `not_measured` only if at least one evidence-ready
case reached its `evaluate` method; skipped cases do not count as a measurement.

The tests use local fake evaluators, including a real isolated worker process, and do not
contact a paid provider. The repository's current `benchmarks/judges/v1` labels remain
unreviewed, so that set is correctly refused by live mode until a human reviews it.
