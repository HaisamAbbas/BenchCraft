# Read-only review: quickstart numeric-claim fixture

## Findings

No actionable defect in the changed test fixture.

## Review

The quickstart report now includes `calls_with_unknown_cost: 10` and `calls: 24`. The claim checker deliberately tests whether a reply's number can be found in a result, not whether the surrounding sentence is semantically supported. Therefore `42%` was not absent: it rounded from the unrelated 10/24 cost-coverage ratio. The old test expectation contradicted its fixture data.

The fixture now uses `99.73%`, which the deterministic test run did not find in the query results. The assertions continue to cover the intended behavior: warn when a reply contains a number absent from every result queried that turn. Production behavior was not changed.

## Verification

- `uv run pytest tests/test_quickstart.py -q`: **7 passed**.
- Ruff check and format for `tests/test_quickstart.py`: passed.
- `git diff --check`: passed.

## Limit

This number-presence check does not verify whether a sourced number supports the sentence in which it appears. The current UI describes that narrower check; semantic claim verification remains outside this test's contract.
