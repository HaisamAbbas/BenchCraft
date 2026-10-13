# G19.7 independent review

The requested read-only review used the local `review-agent` skill. The final review reported
no findings.

Review iterations identified these defects before the final pass:

- SQLite sidecar handles whose target path differed from the workspace path were ignored. The
  migration now fails closed on same-name handles outside the workspace. POSIX descriptor checks
  and Windows process-handle checks verify final paths and file identities. Regression coverage
  includes both platform paths, including an empty initialized Windows handle snapshot.
- Frozen-plan references were initially normalized using the host platform's path rules. Plan
  references and historical project roots now use the stored Windows or POSIX flavor, preserving
  cross-flavor absolute external paths and safely rebasing paths rooted beneath the old project.
- Hard-linked database and sidecar files were accepted. The migration now requires private
  single-link regular files, with regression tests for the database and WAL sidecar.
- Earlier passes found a workspace-root parent error, missing preview snapshot isolation, and
  database/sidecar replacement races. The final code pins the correct project and workspace
  identities, scans the preview within one read transaction, checks SQLite's opened files, and
  rolls back apply failures.

The final reviewer did not run tests. The implementation verification was run separately on
Windows: 136 tests passed and 23 were skipped in the affected workspace/storage suites. Ruff,
Mypy, formatting, and `git diff --check` passed. POSIX-only descriptor and directory-swap runtime
tests remain skipped on this Windows host.
