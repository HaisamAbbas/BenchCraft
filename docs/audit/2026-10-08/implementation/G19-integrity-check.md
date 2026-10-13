# G19.5 workspace integrity and verified-backup repair

`aibench workspace check --workspace PROJECT` performs a read-only inspection of an existing
workspace. It checks SQLite integrity and foreign keys, migration history and the physical schema,
artifact catalog records and object digests, frozen session plans, and registered dataset-suite
snapshots. Dataset snapshots are parsed and normalized again so their stored content hash and case
count are checked, not just the file checksum.

The command returns exit code 0 for a healthy workspace, 1 when it finds integrity issues, and 2
when it cannot safely inspect the workspace. `--json` returns bounded diagnostics and preserves
those exit codes for CI. Checking never creates a missing workspace, applies migrations, or rewrites
catalog or file data. Diagnostics are capped at 100 issue records; catalog scans and individual
file reads also have explicit size limits.

`aibench workspace repair BACKUP --workspace PROJECT` verifies the selected archive by copying it
to a private snapshot, restoring it into a temporary workspace, and running the integrity checker.
This is a preview and leaves `.aibench/` unchanged. `--apply` repeats the verified restore into the
project after retaining its previous `.aibench/` directory as `.aibench-recovery-*`. It runs the
checker after publication and rolls back when that check fails. Rollback tracks the identity of the
directory being published, so a concurrent `.aibench/` that wins the no-replace race is preserved;
if that prevents restoring the old path, repair reports the recovery directory. Stop BenchCraft
processes using the workspace before applying repair. Repair replaces workspace storage from a
verified backup; it does not salvage or merge damaged catalog contents.

Verification on Windows: 86 passed and 13 skipped in the combined workspace, integrity, and
artifact-storage batch. The skipped cases require POSIX-specific link, race, or rename behavior.
Ruff lint/format, Mypy for the changed storage and CLI modules, and `git diff --check` passed. The
independent review found and led to a fix for a concurrent-publication rollback race, then reported
no remaining findings; see [`G19-integrity-check-review.md`](../evidence/G19-integrity-check-review.md).
