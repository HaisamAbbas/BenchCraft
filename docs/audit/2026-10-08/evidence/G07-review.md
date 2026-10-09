# G07 independent review

The review checked the direct run overrides and exact-scope dry-run path, including whether
overrides pass through full plan and policy validation before they can affect execution.

The reviewer reproduced an actionable edge case: `--max-wall-seconds inf` was accepted and
could place a non-standard `Infinity` value in preview JSON or a frozen run. The fix adds
`allow_inf_nan=False` to the budget model and regressions for both the override API and CLI
dry-run. After that change, the reviewer reran the reproduction and the remaining G07 review,
reported 13 focused reviewer tests passing, and found no remaining findings.

Primary verification also passed: 44 focused project tests, Ruff (`src tests plugins`), Mypy
(149 source files), CLI help smoke check, and `git diff --check`.
