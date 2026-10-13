# G20.2 review — Policy-gated live judge calibration

The requested `review-agent` performed a read-only review of the full implementation diff and
related policy, calibration, plugin-worker and CLI paths. The first pass found four issues:

1. Authorization could be checked against one calibration set and evaluation could re-read a
   changed set.
2. `ceilings.max_evaluator_calls` was not applied.
3. Calibration data outside `data_roots` was not refused.
4. The offline CLI accepted `--timeout` but did not pass it to the service.

After those were fixed, the reviewer found two more fail-closed gaps: symlinked input files
could resolve outside `data_roots`, and configured token, cost and wall-time ceilings were
silently ignored. The final implementation resolves both calibration input files before
reading, checks both targets against configured roots, and passes those exact resolved paths
into the loader. It enforces the evaluator-call ceiling and refuses unsupported runtime
ceilings before plugin discovery. The service uses the same parsed case snapshot that passed
policy checks. Offline calibration now forwards its timeout.

Final independent review: **No findings.** The reviewer confirmed the exact validated input
targets reach the loader and offline callers retain compatible defaults.

Verification on Windows with the repository `.venv`:

- Calibration, execution-policy and worker-isolation batch: **55 passed, 1 skipped**. The
  skipped case requires creating an interpreter symlink, which this Windows environment does
  not permit; an escaping-file target is covered with a resolved-path simulation.
- Ruff lint and formatting passed for all changed Python files.
- Mypy passed for `score.py`, `calibration.py` and `policy.py`.
- `git diff --check` passed.
- Live execution used a local fake evaluator in an isolated worker. No paid provider was
  contacted.

Implementation commit: [`59c7fd8`](https://github.com/HaisamAbbas/BenchCraft/commit/59c7fd85e3ffe474aa61cd113ff4e3cf0551d1ec).
