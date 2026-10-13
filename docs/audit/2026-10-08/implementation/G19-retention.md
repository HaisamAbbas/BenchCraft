# G19.6 retention policy and workspace usage

`aibench workspace retention --workspace PROJECT` shows the workspace's orphan-artifact grace
period. Before a setting is saved, the effective value is the one-hour safe default. Use
`--grace-seconds SECONDS` to persist a versioned `retention.json` file under `.aibench/`; the
setting is included in workspace backups. GC uses this value unless an explicit per-command value
is supplied. A preview may show the result of a shorter override, but an applied collection cannot
use less than the configured minimum (or less than the one-hour default).

`aibench workspace usage` reports regular-file counts and byte sizes by database, artifacts,
sessions, dataset-suite snapshots, logs, and other files. It is read-only and does not open or
migrate SQLite. The traversal does not follow symlinks/reparse points or cross-device directories,
skips hard-linked and special files, and is capped at one million entries and 64 directory levels.
The JSON response includes skipped unsafe paths and the number of scanned entries. Use
`workspace gc --json` to inspect reclaimable artifact bytes; usage itself does not infer whether
arbitrary workspace files are safe to remove.

Verification on Windows:

```text
uv run pytest tests/test_cli_workspace.py tests/test_workspace_integrity.py tests/test_storage_artifacts.py -q
99 passed, 16 skipped in 214.90s

uv run pytest tests/test_cli_workspace.py -q -k "extended_path_prefix or retention or workspace_usage"
12 passed, 3 skipped, 51 deselected

uv run mypy src/aibench/cli/workspace.py src/aibench/storage/workspace_retention.py src/aibench/storage/_windows_handles.py
Success: no issues found in 3 source files

uv run ruff check src/aibench/cli/workspace.py src/aibench/storage/workspace_retention.py src/aibench/storage/_windows_handles.py tests/test_cli_workspace.py
All checks passed
uv run ruff format --check src/aibench/cli/workspace.py src/aibench/storage/workspace_retention.py src/aibench/storage/_windows_handles.py tests/test_cli_workspace.py
4 files already formatted
git diff --check
passed
```

The skips are platform-specific link, locking, and POSIX filesystem cases. Windows regressions
exercise handle-relative enumeration, policy reads, and atomic publication through the validated
workspace directory. A directory-swap test verifies scans reject a replacement path while a
swap-and-restore test confirms enumeration remains attached to the opened directory. The final
independent review found no code findings after fixing extended-path conversion. POSIX-specific
runtime behavior was not exercised on this Windows host. Review findings and disposition are in
[`G19-retention-review.md`](G19-retention-review.md).
