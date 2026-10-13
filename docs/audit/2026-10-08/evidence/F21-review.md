# F21 — Supplemental defect-first review

Review date: 13 October 2026.

Scope: CLI tests that match human-readable Rich output, the failing long-path regression, and
the two assertion updates in `tests/test_cli_project.py` and `tests/test_judge_calibration.py`.

## Finding

The captured CLI output preserves Rich's display wrapping. A long temporary path caused Rich to
split the displayed phrase `is bound to` and the filename `cases.jsonl` across output lines.
Assertions that searched the raw display for contiguous strings therefore failed even though
the command returned its expected exit code and semantic error.

## Resolution reviewed

The project-run test normalizes whitespace before matching the phrase. The calibration test
checks the filename after removing display whitespace and checks the semantic error phrase
separately. This tolerates Rich wrapping at spaces or inside paths without weakening the exit
code, filename, or error-category checks. No product behavior changed.

## Verification

- `test_run_refuses_a_dataset_its_configured_plan_does_not_use`: 1 passed.
- Both `test_live_calibration_rejects_a_file_resolving_outside_data_roots` variants: 2 passed,
  22 deselected.
- The CI-style full local run reached 841 passed before it stopped at the calibration wrap
  assertion; the exact failure was then covered by the focused passing tests.
- `git diff --check` passed.

## Follow-up: ANSI-styled help output

Hosted run 38371903480 later identified `tests/test_cli_dashboard.py:532` as another raw Rich
output assertion. Reproduction with `TERM=xterm-256color` showed ANSI style sequences between
characters of the displayed `--no-open` option. The test now decodes terminal styling before
checking the visible help text; all 14 dashboard tests passed locally on Python 3.11 and 3.12
under CI-like terminal variables. Hosted run 38376283063 had one separate Windows Python 3.11
failure at `tests/test_cli_run.py:130`; its restricted JUnit report did not expose assertion values.
The exact test, all 18 tests in that module, and the 277-item full-suite prefix passed in a local
Windows Python 3.11.16 replay. The subsequent full hosted matrix run 38377296862 passed all 18 jobs
on source containing the ANSI fix. The earlier one-off failure remains unexplained. The read-only
review found no actionable issues; the detailed review is in
[G21-dashboard-help-ansi-review.md](../implementation/G21-dashboard-help-ansi-review.md).

## Review outcome

No further actionable issues were found in the whitespace or ANSI-aware assertions. Focused local
tests and the latest hosted matrix pass; the earlier hosted Windows Python 3.11 failure remains
unexplained.
