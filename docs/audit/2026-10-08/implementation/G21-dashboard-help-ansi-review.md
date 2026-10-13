# G21 dashboard help ANSI regression review

The read-only `$review-agent` review inspected the dashboard CLI test, the Rich ANSI decoder call,
and their existing command setup. No `AGENTS.md` files apply, and the review found no actionable
defects.

The hosted failure at `tests/test_cli_dashboard.py:532` was reproducible locally with
`TERM=xterm-256color`: Rich emitted ANSI style sequences between characters of the visible
`--no-open` label, so raw substring matching failed even though the rendered help showed the
option. The test now decodes captured help output before checking visible text, preserving the
user-facing assertion while ignoring terminal styling.

All 14 dashboard tests passed on Windows with Python 3.11 and 3.12 using Typer 0.27.2 and
`CI=true`, `GITHUB_ACTIONS=true`, `TERM=xterm-256color`, and `COLUMNS=80`. Ruff lint and format
checks passed. Hosted run 38376283063 included one Windows Python 3.11 failure at the separate
`tests/test_cli_run.py:130` assertion; a local replay passed that test, the whole 18-test module,
and the full-suite prefix through it (272 passed, 5 skipped). The JUnit details for the hosted
failure were unavailable. Follow-up hosted run 38377296862 passed all 18 jobs, including all six
core OS/Python test suites, on source containing this correction.

**No findings.**
