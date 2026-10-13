# G23 — whole-workflow cost attribution

## Behavior

New session-run reports now include durable planner usage attributed by the source user turn
and the next successful run-start boundary. The report and its content-withheld fact export
keep application, primary evaluator-pass, planner, and whole-workflow views separate. The
whole-workflow view combines application spend, every committed evaluator scoring pass
(including rescoring), and planner usage without changing the primary evaluator row.

Planner events preserve reported prompt and completion tokens. A call is marked as having
unknown token usage when either token counter is absent, when the provider fails, or when the
conversation is canceled while its provider request is in flight. Unknown call counts and
known dollar lower bounds remain visible. CLI-only session runs receive a durable zero-call
marker; legacy session runs without planner events remain explicitly unattributed.

Concurrent duplicate deliveries may both reach the provider. Only one assistant reply is kept
in conversation history, while each losing attempt's usage is attached atomically to that
reply and can be reconciled when the run boundary becomes known. Attempt IDs make the usage
events idempotent. Attribution waits while unresolved turns could change the next boundary.

Planner dollar pricing remains unknown unless a provider reports a price, and this work does
not enforce a combined workflow cost budget. Session `/budget` remains a session-level view.

## Verification

- The final affected batch passed: 149 tests across conversation, reports, TUI, storage
  repositories, and run bundles. After the final nested-immutability hardening, the two focused
  duplicate-attribution tests passed again.
- Ruff passed for all changed Python modules and tests; Mypy passed for six changed source
  modules; `git diff --check` passed.
- Independent review-agent final review found no actionable findings; see
  [G23 review](G23-whole-workflow-cost-review.md).

