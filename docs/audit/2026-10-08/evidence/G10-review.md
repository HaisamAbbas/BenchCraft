# G10 independent review

The review-agent skill performed a read-only, defect-first review of the full G10 diff and
the surrounding retry, run recovery, policy, and test-world code. Earlier review passes
identified several actionable retry-safety and provenance issues:

- A retry is a new dispatch and must satisfy both the current project/global or explicit
  policy and the parent's frozen policy. Retry now resolves current project settings and
  preserves the parent's policy as a ceiling.
- Case-level retry selection must account for every final repetition, including completed
  effectful and cancelled/unknown outcomes, plus unknown, running, failed-without-result,
  and attempt-mismatched execution work items. Automatic selection now fails closed; explicit
  case selection reports its prior effect risk.
- Retrying an interrupted parent can race with `resume` and select from a changing work
  graph. Retry now rejects all resumable parents at both the CLI and service boundary.
- Recompiling a test world from a changed seed file could give the child a different state.
  Retry now verifies the parent's frozen seed artifact and checks the current world ID/hash
  before creating the child.

Regression tests cover each finding. After these corrections, the final independent review
reported **no findings**. The reviewer did not run tests; final validation is recorded in
[`G10-verification.txt`](G10-verification.txt).
