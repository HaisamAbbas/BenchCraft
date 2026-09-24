# Planner fixture set v1

The §23 "benchmark the planner itself" set: 40 annotated planning situations across 27 families.
Run it with:

```bash
aibench plan benchmark --fixtures benchmarks/planner/v1                 # template baseline
aibench plan benchmark --planner model --provider-config P --policy POL # a model planner
```

Each fixture in `fixtures.json` describes an application (its runner and output binding),
the shape of its dataset, the policy, any recorded executions, and the user's objectives.
It then annotates what a correct plan does:
- `select`: concepts to measure with a metric;
- `gaps`: concepts to report as unmeasurable;
- `acceptable`: concepts it may also measure;
- `forbidden`: evaluator IDs it must never choose;
- `executable`: whether the plan must be refused.

Scoring works over concepts, not exact framework names (`src/aibench/planning/benchmark.py`).

- **Held-out families** (`holdout: true`) were written after the template planner existed and are not used to change it. One fixture, `retriever_disabled_runtime`, was written as held out. Its first measurement exposed a planner defect that was then fixed, so it now counts as a development fixture.
- **Catalog.** The `catalog.*` evaluators are manifests only. Planning reads them, and nothing is ever evaluated.
- **Review status.** The annotations were written by the implementing AI agent, not by people. No fixture has been reviewed. §23 asks for at least two human reviewers on a subset, with disagreements adjudicated; until that happens every result from this set is unverified, and reports say so.
- **Versioning.** Change the annotations only in a new version directory, so earlier results remain reproducible.
