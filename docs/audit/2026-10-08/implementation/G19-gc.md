# G19 delivery: safe workspace artifact garbage collection

`aibench workspace gc` inventories artifact files that are not referenced by the workspace
catalog. Preview is the default. Applying deletion requires `--apply` and a grace period of at
least one hour. Human and JSON output report bounded candidate/deletion counts and byte totals;
catalog or filesystem failures use the standard CLI error behavior.

The implementation rejects unsafe workspace/database/artifact paths, validates the content
address layout, protects concurrent artifact commits with cross-process digest locks, and checks
catalog identity before deletion. It pins catalog reads to a temporary hard-link snapshot that
includes live SQLite WAL/SHM sidecars, then removes the snapshot on normal completion. Deletion
rechecks eligibility and parent identity to avoid acting on swapped paths. A crash may leave a
`.aibench-gc-*` snapshot, which is harmless and accepted by later runs.

Validation on Windows:

```text
uv run pytest tests/test_storage_artifacts.py tests/test_storage_recovery.py tests/test_storage_failures.py tests/test_cli_workspace.py -q
59 passed, 5 skipped

uv run mypy src/aibench
Success: no issues found in 161 source files

uv run ruff check src tests
All checks passed

uv run ruff format --check <changed Python files>
Passed

git diff --check
Passed
```

The five skips are link-creation cases unavailable in this Windows environment. The independent
review record is in [G19 GC review](../evidence/G19-gc-review.md). Code commit
`739abfe1a41f2d76313b4d475b830f1791941f1c` was pushed to `origin/fix/plugin-core-match` as
`Haisam Abbas <HaisamAbbas@outlook.com>`.
