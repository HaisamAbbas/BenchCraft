# Independent review of F08

Reviewer: `/root/review_f07`, using the requested `review-agent` workflow.
Scope: `ResolvedConfig.root` path resolution, explicit CLI overrides, nested-project chat,
doctor, and run behavior, `data_roots` denial before dispatch, and the controlled environment
identity pins/revisions added to recovery fixtures for F04.

> Final review is clear: no actionable findings. F08 path resolution and policy containment
> remain correct; the updated F04 fixture pins/revisions are scoped to tests that intentionally
> exercise resume/recovery with opaque identities and do not change production behavior.
