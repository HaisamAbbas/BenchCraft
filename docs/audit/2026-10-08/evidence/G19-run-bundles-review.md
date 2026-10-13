# G19.3 portable run-bundle review

The requested read-only `review-agent` workflow inspected the full G19.3 working-tree change.
The review was defect-first and covered the bundle archive, catalog validation, artifact import,
CLI commands, tests, and user documentation. Review findings were fixed before delivery:

- Smoke bundles now include exactly the persisted selected cases and execution records, including
  workspaces where the full dataset was already stored. Execution ordering is preserved.
- Frozen application artifacts are parsed and tied to the run's application hash. Manual-plan
  artifacts are parsed as `ExecutablePlan` values and tied to the run's plan hash. Frozen test
  worlds require a non-empty ID and a matching artifact whose bytes decode to a JSON object/list.
- Duplicate smoke case selections are rejected before workspace creation and again at the service
  boundary.
- Artifact discovery uses known run, execution, evaluation, trace, and remote-job reference fields;
  arbitrary case metadata ending in `_artifact_id` is preserved as metadata. Import rejects
  rehashed execution records with dangling artifact references.
- Run-scoped approval records are included and validated so imported reports retain `approved_by`.
- The review-evidence file linked by the implementation note is included here.

The final combined regression batch passed **90 tests** with **10 platform/environment skips**:

```text
uv run pytest tests/test_run_bundle.py tests/test_cli_runs.py tests/test_cli_workspace.py tests/test_cli_app.py -q
90 passed, 10 skipped
```

Ruff lint passed on changed Python modules/tests; Ruff formatting passed on the new bundle module,
bundle tests, app CLI, and execution service. Mypy passed on the four changed source modules, and
`git diff HEAD --check` passed. A final independent review follow-up was requested after this
evidence file was added.
