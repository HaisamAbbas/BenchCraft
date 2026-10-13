# G09 independent review

The read-only review examined gate authorization, metric binding identity across draft
revisions, stable target re-resolution, schema changes, plan construction/execution, and user
documentation.

The review reproduced actionable authorization defects involving overlapping gate IDs,
threshold-only removal, gates that share a metric, negated remove-all wording, remove-all
exceptions and metric scope, shared objective aliases, and separate scoped removal clauses.
The implementation now matches gate IDs as complete labels, scopes each threshold/removal to
its gate and metric, rejects ambiguous aliases and same-metric gate targets, respects
negation/exceptions, and accumulates separate scoped requests. Each reproduction has a
regression test.

Final independent review: no remaining actionable findings. The reviewer confirmed the
scoped remove-all correction and its regression test. Final project verification is recorded
in [`G09-verification.txt`](G09-verification.txt).
