# G18 delivery: named dataset suite catalog

`aibench dataset suites register NAME VERSION SOURCE.jsonl` validates a dataset, requires
non-empty unique explicit case IDs, and stores a workspace-local snapshot with a catalog
fingerprint. `list` and `show` inspect registered versions; `show` verifies the snapshot before
reporting it. Registration is idempotent for identical content and metadata and rejects changes to
an existing version. Version labels preserve case-sensitive identity while using encoded internal
filenames that remain distinct on case-insensitive filesystems.

`aibench run --dataset-suite NAME@VERSION` pins a run to that snapshot and can replace a plan's
dataset path. The CLI verifies the fingerprint before compilation and checks it again before
dispatching any application work. Suite names, paths, and descriptions are bounded and validated;
human output sanitizes database-derived text. Snapshot publication is atomic, serialized per
version across processes, and recovers only unreferenced orphan files left by interrupted writers.
Lock-file opening rejects symbolic links, Windows reparse points, hard links, and non-regular files.

The user guide is [Named dataset suites](../../../datasets/suites.md).

Validation on Windows:

```text
uv run pytest tests/test_cli_dataset_suites.py -q
11 passed, 1 skipped (the environment denied file-symlink creation)

uv run pytest tests/test_cli_dataset_suites.py tests/test_run_overrides.py tests/test_storage_repositories.py tests/test_storage_migrations.py tests/test_cli_run.py -q
75 passed, 1 skipped

uv run ruff check src/aibench tests
All checks passed

uv run ruff format --check <12 changed Python files>
12 files already formatted

uv run mypy src/aibench
Success: no issues found in 160 source files

git diff --check
Passed
```

The independent review record is in [G18 suite review](../evidence/G18-suites-review.md).
Code commit `cf2efc2f6c8e927d84c66d684793720bec7ac8db` was pushed to
`origin/fix/plugin-core-match` as `Haisam Abbas <HaisamAbbas@outlook.com>`.
