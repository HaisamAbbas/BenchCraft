# G18 deterministic split review

The requested independent review-agent performed a read-only review of the implementation and
regression tests. Its final result was **no findings**.

The review found and the implementation resolved these issues before final approval:

- The original greedy score could leave an active split empty even when enough independent groups
  existed. Assignment now reserves enough remaining groups to populate each empty active split,
  and rejects inputs with fewer independent groups than active splits.
- Sequential file links exposed partial output and could leave an incomplete directory on rollback
  failure or interruption. The implementation stages the complete result and publishes it with
  one atomic no-replace directory rename.
- POSIX `rename` can replace an existing empty directory. Linux uses `renameat2` with
  `RENAME_NOREPLACE`, macOS uses `renamex_np` with `RENAME_EXCL`, and Windows uses its no-replace
  `os.rename` behavior. Unsupported platforms fail closed. A regression creates an empty target
  at publication time and verifies it remains empty.
- If cleanup fails after another error, the CLI now includes the retained staging directory path in
  its JSON error. Path-resolution loops are normalized into the shared JSON error envelope.

Validation on Windows:

- Dataset, ingest, finite-number, discovery, and episode suite: 122 passed, 2 skipped.
- Targeted destination-race, cleanup-failure, symlink-loop, and split-population regressions:
  3 passed, 1 skipped. Symlink creation was unavailable for the loop test.
- Ruff, Ruff formatting, full source Mypy (158 files), and `git diff --check` passed.

Linux and macOS native rename branches were reviewed but were not executed in this Windows
environment. The code change is commit `bacd1d029df073ee73f7fbd0bd162bdb4aac9a1c`, pushed to
`origin/fix/plugin-core-match`.
