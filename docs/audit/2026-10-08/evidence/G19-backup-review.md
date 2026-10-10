# G19 workspace backup and restore review

The requested independent review-agent completed a read-only review of the backup and restore
implementation and its regression tests. Its final review reported **no actionable findings**.
The final pass confirmed manifest size/SHA verification of the staged SQLite database before
SQLite opens it, protection and identity/content checks through publication, the Windows stage
identity check, and non-destructive cleanup when a staging directory is moved concurrently.

The implementation also validates archive path and size bounds, checks SQLite integrity,
foreign keys, artifact digests and catalog metadata, relocates artifact URIs, rejects existing
restore destinations, snapshots the opened archive before parsing it, and atomically publishes
only a verified workspace. The regression suite includes a same-size copied valid SQLite file
swapped after extraction; restore rejects it against the archive manifest.

Final focused validation on Windows:

```text
uv run pytest tests/test_cli_workspace.py -q
35 passed, 10 skipped

uv run mypy src
Success: no issues found in 162 source files

uv run ruff check src tests
All checks passed

git diff --check
Passed
```

The skipped tests include POSIX-only publication and link-handling paths; those paths were not
executed on this Windows host. The adjacent storage regression batch (excluding the process-lock
test that timed out under the combined run) produced 45 passed, 2 skipped, and 1 failed. The
failure was `test_a_full_disk_stops_the_run_resumably_and_resume_finishes_it`, which reported that
the test run's frozen application Git identity changed before resume. The process-lock test
passed when run alone (`1 passed in 12.69s`). After the feature commits, the isolated resume
test passed (`1 passed in 15.02s`), so the combined-batch identity-drift result did not reproduce
in isolation. The full repository test suite was started but stopped at 4% after several
minutes; it was not completed.

Code commit `c55fa221` is pushed to `origin/fix/plugin-core-match`.
