# G11 independent review

The review-agent skill performed a read-only, defect-first review of the G11 diff and its
surrounding run-control, lease, cancellation, and resume behavior. Review passes identified
actionable edge cases that were fixed before delivery:

- Detached process status must match the launch PID and host to the current lease owner; a
  later foreground lease cannot make an exited detached worker appear active. Windows process
  handle APIs now use pointer-sized signatures.
- An offline durable cancel must finalize even if the application source has drifted or
  evaluator plugins cannot initialize. Resume now skips dispatch preparation for cancellation.
- A resume request during the first Ctrl-C drain now clears the reversible interruption. A
  pause can replace that same first interruption. Once a second interrupt aborts in-flight
  work, requests remain durable for the next session and are not recorded as applied by the
  exiting worker.
- A legacy run already stored as `cancelling` without a G11 control row now receives a durable
  cancel request when resumed, preserving its prior recovery contract.

Regression tests cover these cases along with detached launch, pause/resume/cancel from separate
CLI invocations, ordered request/application events, cancellation irreversibility, status
ownership, and final lease release. After the corrections, the final independent review
reported **no remaining actionable findings**. The reviewer did not run tests; validation is
recorded in [`G11-verification.txt`](G11-verification.txt).
