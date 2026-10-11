# G21 — Platform and release readiness

## Change

CI now covers GitHub-hosted Windows, Ubuntu Linux, and macOS runners. Core and clean-wheel
release checks cover Python 3.11 and 3.12; DeepEval and Ragas contract jobs use Python 3.12.
Static typing targets the Windows APIs used by the application on every runner, while tests
execute natively on each operating system. CI upgrades setuptools past the current runner-image
advisory.

The first hosted run exposed stale platform assumptions in path handling, SQLite restore
publication, CLI output assertions, plugin isolation, and session-recovery fixtures. The changes
in this batch correct those assumptions, preserve pinned workspace handles through database
restore, and exercise replacement races at the final publication boundary. Existing runtime
identity protections remain enabled; test fixtures now declare stable environment digests where
their purpose is to test resumption rather than working-tree changes.

## Local verification

- `ruff check src tests` passed.
- `mypy --platform win32 src` passed for 168 source files.
- 160 affected tests passed and 18 platform-specific tests were skipped on Windows 11 / Python
  3.12.10.
- The staged-database-entry replacement regression is POSIX-only and will execute on the hosted
  macOS job; Windows skips it by design.
- The independent read-only review found no findings.

## Hosted verification

The matrix-enablement commit is `7c57963e` and the initial CI corrections are `dbfdb952`; both
were pushed to `fix/plugin-core-match` as Haisam Abbas. Hosted run
[38134340939](https://github.com/HaisamAbbas/BenchCraft/actions/runs/38134340939) completed with
all 9 release checks and plugin jobs passing, but its six full core platform/version test jobs
failed. Those failures exposed the additional issues corrected in this batch. The current changes
must be pushed and the resulting full matrix must pass before G21 can be marked complete.

## Scope boundary

No live model provider was called, no external pilot was run, and no release was authorized or
published. This work establishes platform checks and records observed results; it does not make an
external release decision.
