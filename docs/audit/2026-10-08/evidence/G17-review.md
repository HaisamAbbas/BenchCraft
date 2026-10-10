# G17 independent review

The review-agent performed defect-first read-only passes over the G17 implementation and its tests. The initial review found two actionable issues:

- **P1, guessable no-content identifiers:** the original stable digest exposed low-entropy metadata categories. Group identifiers now use a per-report HMAC salt whenever content is withheld; the salt and internal keys are not exported.
- **P2, sanitized label collisions:** distinct values could display identically after control removal or secret redaction. Every displayed category now receives a unique per-field ordinal.

The follow-up review found two more issues, both fixed before delivery:

- A literal metadata value could imitate a generated label suffix. Ordinals are now assigned to every displayed row after category capping, and the public report drops internal grouping keys. A literal-marker regression covers the case.
- JUnit/SARIF accepted `--group-by` but ignored the aggregate. The CLI now returns a handled error for those formats, with parameterized regressions.

A final scale pass buckets executions and metric results once per requested field instead of rescanning every observation for each category. A direct 101-category regression verifies the 99-visible-plus-`other` cap and its counts.

The final reviewer pass found no remaining actionable findings. The complete focused report, report-regression, and TUI batch passed: **59 passed**. Ruff, formatting, mypy, and `git diff --check` passed. The reviewer made no edits.
