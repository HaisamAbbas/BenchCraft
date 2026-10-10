# G18 delivery: deterministic grouped dataset splits

`aibench dataset split INPUT OUTPUT_DIR` validates canonical JSONL, requires unique explicit
case IDs, and creates train, validation, and test files plus a versioned manifest. Seeded SHA-256
group ranking and a deterministic greedy allocator make membership reproducible. A non-empty
`group_id` is indivisible; cases without one fall back to their `case_id`. Group integrity takes
priority over exact ratios, and the manifest records requested weights, per-split case/group
counts, content hashes, seed, and normalization warnings.

Case bodies and the assignment index stay in temporary SQLite/staging files. The output target
must be a new directory. Each requested split receives a group when there are enough independent
groups; otherwise the command fails before publication. `manifest.json` is written last in
staging, and a single same-parent directory rename publishes the completed result atomically
without exposing partial files. If staging cleanup fails after an error, the failure reports the
retained staging path. Existing destinations, including empty directories created while generation
is in progress, are preserved. Windows uses its no-replace directory rename; Linux and macOS use
their exclusive rename primitives. Other platforms fail closed if no atomic no-replace primitive
is available.

Examples and limits are in [the dataset splitting guide](../../../datasets/splitting.md).

Validation:

```text
uv run pytest tests/test_cli_dataset.py tests/test_ingest.py tests/test_dataset_finite_numbers.py tests/test_dataset_discovery.py tests/test_episode_contract.py tests/test_episode_scoring.py -q
122 passed, 2 skipped

uv run ruff check src/aibench/datasets/transform.py src/aibench/cli/dataset.py tests/test_cli_dataset.py
All checks passed

uv run ruff format --check src/aibench/datasets/transform.py src/aibench/cli/dataset.py tests/test_cli_dataset.py
3 files already formatted

uv run mypy src/aibench
Success: no issues found in 158 source files

Additional targeted regressions for destination races, cleanup failures, symlink loops, and
non-empty splits passed: 3 passed, 1 skipped (symlink creation unavailable in this environment).
The empty-destination race regression was rerun separately and passed.
```

Code commit `bacd1d029df073ee73f7fbd0bd162bdb4aac9a1c` was pushed to
`origin/fix/plugin-core-match`. The independent review record is in
[G18 split review](../evidence/G18-split-review.md).
