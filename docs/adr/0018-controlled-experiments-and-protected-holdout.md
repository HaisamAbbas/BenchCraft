# ADR 0018: Controlled experiments and protected holdout

- **Status:** Accepted for Prompt 19
- **Date:** 2026-09-25
- **Context:** Specification §§19 and 24 call for optional, bounded optimization over
  exposed parameters and development data, with explicit budgets, lineage and a protected
  holdout. Prompts 14 and 18 supply the comparison and dataset trust boundaries.

## Decisions

1. **Expose finite application settings explicitly.** An app config declares a default,
   description, finite values, and a dedicated `AIBENCH_TUNABLE_` environment key. An
   experiment cannot name arbitrary fields, source paths, evaluator rubrics, hidden labels,
   or unexposed environment variables. The baseline domain must be included in the grid.
2. **Use a deterministic bounded grid.** The fully materialized grid, stable trial IDs,
   exact parameter maps/hashes, fixed seed, run IDs, frozen plan/evaluator/app/code/policy
   identities, constraints and budgets are recorded. Trial runs use the shared durable run
   service and retain its normal partial-failure and resume behavior. Both dataset paths are
   checked against `data_roots` before plan compilation or plugin discovery. An application
   whose source identity cannot be captured must declare a revision or environment digest;
   otherwise the experiment is refused.
3. **Select on development only.** Trial metrics and pairwise comparisons use the development
   set. A candidate must pass coverage/constraint checks, strict compatibility, and a paired
   95% uncertainty interval excluding zero in the objective direction. Budget extension is
   explicit and append-only in the experiment event history.
4. **Lock before opening holdout cases.** Experiment creation reserves the holdout digest;
   development runs receive only the development plan/dataset. After selection is durably
   locked, one baseline and one selected configuration run against all protected cases under
   the same frozen evaluator plan. The final paired result is reported separately and cannot
   resume candidate search or be re-evaluated after completion.
5. **Propose without applying.** CLI and conversation can explain evidence and recommend a
   configuration for review. They do not change application files, deploy, or write
   production settings. Each of those needs separate explicit authorization.
6. **Treat boolean quality checks as rates for paired comparison.** Existing comparison
   statistics already support numeric paired bootstrap estimates. Boolean results map
   `true`/`false` to 1/0 and are labeled as a paired binary-rate difference, preserving
   existing comparison identity, coverage and uncertainty gates.

## Consequences

- Workspaces gain migration 11 experiment, trial, append-only event, and protected digest
  tables.
- Candidate selection and final evaluation have distinct lifecycle states, run manifests,
  plan hashes and report sections.
- The final holdout comparison is an independent confirmation after selection, not an input
  to further optimization. A statistically inconclusive result recommends retaining the
  baseline pending a newly authorized experiment with a fresh holdout.
- The local known-objective fixture demonstrates a binary exact-match objective without a
  provider or live application dependency.

## Verification

See `docs/engineering/reports/19.md`,
`tests/test_experiments.py`, Prompt 14 comparison tests and Prompt 18 candidate/episode tests.
