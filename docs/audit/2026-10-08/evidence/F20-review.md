# F20 — Supplemental defect-first review

Review date: 13 October 2026.

Scope: synchronous DeepEval judge request handling in
`plugins/deepeval/src/aibench_deepeval/judges.py`, the dispatched transport regression, and
the existing asynchronous retry/accounting paths.

## Finding

Before the fix, `_OpenAICompatible.generate()` accounted only for exceptions in `_TRANSIENT`.
An unlisted `httpx.RequestError` such as `WriteError` escaped with `cost_known=True`,
`cost()==0.0`, and no uncertain-call count. The request can fail during writing after the
provider has accepted it, so that state understated possible spend.

## Resolution reviewed

The synchronous path now catches other `httpx.RequestError` values after the existing transient
branch, marks cost unknown, increments `uncertain_calls` and `calls_without_tokens` once, and
re-raises without retry. The retryable exception tuple and its wait/retry behavior are unchanged.

The new `MockTransport` test raises `WriteError` from its handler after recording the request.
It verifies one dispatch, propagated `WriteError`, unknown cost, one uncertain call, one call
without tokens, zero completed responses, and no replay. The paired asynchronous dispatched
write-error and gate-teardown regressions also passed.

## Review outcome

No further actionable defects were found in the changed exception ordering, accounting, or
retry boundary. Ruff passed for the changed source and test files; `git diff --check` passed.

Residual limit: the regression establishes local accounting for an ambiguous transport failure;
it cannot establish whether a real provider accepted a request before a network error.
