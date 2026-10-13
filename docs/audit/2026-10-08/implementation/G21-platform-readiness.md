# G21 — Platform and release readiness

## Change

CI now covers GitHub-hosted Windows, Ubuntu Linux, and macOS runners. Core and clean-wheel
release checks cover Python 3.11 and 3.12; DeepEval and Ragas contract jobs use Python 3.12.
Static typing targets the Windows APIs used by the application on every runner, while tests
execute natively on each operating system. CI upgrades setuptools past the current runner-image
advisory. An earlier hosted run for source commit `af918898` passed all 18 jobs. The latest run
for the current rc35 source has six failing core test jobs, so platform readiness is not green.

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
jobs failed at `Tests (pytest)`, while the plugin and clean-wheel jobs passed. Failure annotations
still contain only the generic exit code 1, so the hosted failing test and root cause are not
established.

A CI-style Windows 11 / Python 3.11.16 full-suite attempt passed 355 tests and skipped 13 before
once failing in `test_workspace_repair_previews_then_recovers_and_retains_previous_workspace` with
`Access is denied` while opening the preview `.aibench` directory as an archive. That test passed
alone, in the 55-pass workspace module, and in 12 consecutive isolated repeats; a traced full-suite
replay also passed it before being interrupted at 24%. This local result is inconclusive and does
not establish the hosted root cause. The full suite on this source has not completed; do not carry
the earlier platform pass forward as current rc35 readiness.

## Scope boundary

No live model provider was called, no external pilot was run, and no release was authorized or
published. The earlier passing matrix is platform and packaging evidence for its source commit,
not an external release decision or evidence of live-provider behavior.
