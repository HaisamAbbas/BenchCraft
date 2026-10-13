# G21 — Platform and release readiness

## Change

CI now covers GitHub-hosted Windows, Ubuntu Linux, and macOS runners. Core and clean-wheel
release checks cover Python 3.11 and 3.12; DeepEval and Ragas contract jobs use Python 3.12.
Static typing targets the Windows APIs used by the application on every runner, while tests
execute natively on each operating system. CI upgrades setuptools past the current runner-image
advisory. An earlier hosted run for source commit `af918898` passed all 18 jobs. The latest run
for the current rc35 source has six failing core test jobs, so platform readiness is not green.

The core matrix stores a per-platform JUnit report for seven days. A follow-up check step emits up
to 20 content-withheld annotations containing each failed test ID, test source location, and
recognized exception type; assertion values stay in the report artifact. Missing or malformed
reports produce a warning without replacing the pytest result.

The first hosted run exposed stale platform assumptions in path handling, SQLite restore
publication, CLI output assertions, plugin isolation, and session-recovery fixtures. The changes
in this batch correct those assumptions, preserve pinned workspace handles through database
restore, and exercise replacement races at the final publication boundary. Existing runtime
identity protections remain enabled; test fixtures now declare stable environment digests where
their purpose is to test resumption rather than working-tree changes.

## Local verification

- `ruff check src tests` passed.
- `mypy --platform win32 src` passed for 168 source files.
- The focused workspace, workspace-integrity and quickstart regression batch passed: 95 passed,
  22 skipped on Windows 11 / Python 3.12.10.
- The DeepEval metric module passed: 45 passed, 1 skipped; the judge-calibration module passed:
  23 passed, 1 skipped, using the editable development environment and isolated plugin workers.
- The pytest report annotator passed ten focused tests, Ruff lint/format, Windows-target Mypy,
  workflow YAML parsing, and a failing-report serialization check. See the
  [annotation review](G21-pytest-failure-annotations-review.md).
- The independent read-only review found no findings.

## Hosted verification

[Hosted run 38145775882](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38145775882)
completed successfully on `af918898e8a7d14bff2b861c6b3140d3185844d9`: all 18 of 18 jobs passed.
The six full suites passed on Windows, Ubuntu and macOS with Python 3.11 and 3.12 (1,742–1,745
passed and 187–190 skipped per platform/version, with platform-specific skips). All six clean-wheel
release checks passed across the same OS/Python matrix. All six real-package DeepEval and Ragas
contract jobs passed across the three operating systems on Python 3.12. Earlier runs exposed
SQLite descriptor/sidecar races and a fractional-latency claim-check regression; the final run
includes the fixes and their regression coverage.

The first rc35 follow-up [hosted run 38308978546](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38308978546)
on `d8fda15389e1ddb58a5cdc4c4f9ca6e150dafd65` completed with 12 of 18 jobs passing. All six
DeepEval/Ragas contract jobs and all six clean-wheel release checks passed. All six core jobs
passed setup, dependency audits, Ruff, and Mypy, then failed at `Tests (pytest)`; their CLI smoke
and build steps were skipped.

The later [hosted run 38351148266](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38351148266)
on `e3df04568ffc08d082b7283c2cc190bb20576ab6` repeated that 12/18 result: each of the six core
jobs failed at `Tests (pytest)`, while the plugin and clean-wheel jobs passed. The latest completed
[hosted run 38355637698](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38355637698) on
`2f586c7bd710d35dc73b0d959db6a1c6c334a291` again completed 12/18: all six core jobs failed at
`Tests (pytest)`, and all six plugin and six clean-wheel jobs passed. Failure annotations still
contain only the generic exit code 1, so the hosted failing test and root cause are not established.

The subsequent [hosted run 38357086081](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38357086081)
on `21fad75f` also completed 12/18 with the same job split. It predates the F02 orphaned-metadata
fix documented in [its follow-up](F02-orphaned-metadata.md), so it does not validate that change.
The public annotations still omit failing test names.

The latest completed [hosted run 38365229012](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38365229012)
on `a686cf9b5a7b0011a695698913526633fd300ba2` again completed 12/18: all six core `pytest` jobs
failed, and all six plugin and six clean-wheel jobs passed. Its annotations report only exit code 1;
that workflow version did not publish test reports. The CI follow-up now uploads per-platform
JUnit artifacts for seven days.

The next [hosted run 38367136426](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38367136426)
on `9611ede768725abf968d1c4914ce911f4f20e719` published all six per-platform JUnit artifacts; all
six core `pytest` jobs again failed with generic exit-code annotations. The artifact contents were
not accessible through this unauthenticated review session, so their test details remain unknown.
The subsequent source adds content-withheld test-ID and exception-type annotations. Hosted run
38369979240 failed across all six core jobs and identified
`tests.test_cli_dashboard::test_dashboard_cli_requires_existing_workspace_and_exposes_help` with
`AssertionError`. Follow-up run
[38371903480](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38371903480) verified source
locations on all six failures and pinpointed `tests/test_cli_dashboard.py:532`, the assertion that
expects `--no-open` in captured help output. Reproduction with `TERM=xterm-256color` showed Rich
style escape sequences splitting the visible option label in raw captured text. The test now runs
the help output through Rich's ANSI decoder before checking visible text. All 14 dashboard tests
pass locally on Windows Python 3.11 and 3.12 with CI-like terminal variables; the next hosted run
will verify the correction. No application runtime change was needed. See the
[dashboard help review](G21-dashboard-help-ansi-review.md).

A CI-style Windows 11 / Python 3.11.16 full-suite attempt once failed in
`test_workspace_repair_previews_then_recovers_and_retains_previous_workspace` with `Access is
denied` while opening the preview `.aibench` directory as an archive. That test passed alone, in the
55-pass workspace module, and in 12 consecutive isolated repeats. A later clean CI-style Python
3.11.16 full-suite run on the source immediately before the F02 follow-up passed 1,850 tests and
skipped 222. After the F02 fix, a fresh Windows 11 / Python 3.12.10 CI-style full suite passed
1,853 tests and skipped 222; its four focused regression tests also passed under both Python
3.11.16 and 3.12.10. Windows-target Mypy passed for 175 source files and Ruff lint passed on the
changed files. These local runs give current source-level evidence, but they do not explain or
clear the hosted core-job failures. The failing test names and root cause remain unknown.

## Scope boundary

No live model provider was called, no external pilot was run, and no release was authorized or
published. The earlier passing matrix is platform and packaging evidence for its source commit,
not an external release decision or evidence of live-provider behavior.
