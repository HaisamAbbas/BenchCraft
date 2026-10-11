# G21 — Platform and release readiness

## Change

CI now covers GitHub-hosted Windows, Ubuntu Linux, and macOS runners. Core and clean-wheel
release checks cover Python 3.11 and 3.12; DeepEval and Ragas contract jobs use Python 3.12.
Static typing targets the Windows APIs used by the application on every runner, while tests
execute natively on each operating system. CI upgrades setuptools past the current runner-image
advisory. The final hosted run for source commit `af918898` passed all 18 jobs.

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

## Scope boundary

No live model provider was called, no external pilot was run, and no release was authorized or
published. The passing matrix is platform and packaging evidence for this source commit, not an
external release decision or evidence of live-provider behavior.
