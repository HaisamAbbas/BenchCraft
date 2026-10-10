# G19.3: Portable terminal-run bundles

`aibench runs export RUN_ID DEST --workspace PATH` writes a versioned, checksummed ZIP bundle
containing one terminal run, its frozen dataset/application/plan dependencies, approval
provenance, associated run records, and referenced artifact bytes. The command
`aibench runs import BUNDLE --workspace PATH` validates the complete bundle before creating a
destination workspace, relocates artifact references to the destination artifact store, and writes
catalog records transactionally. Reimporting identical records is idempotent; a conflicting run or
artifact ID fails without partial database changes.

The importer validates archive limits and paths, every file digest, artifact metadata and bytes,
typed model data, database schema columns, foreign references, run-manifest identities, and plan
dependencies. Frozen application and manual-plan artifacts are parsed and matched to the run's
identity; a frozen test-world ID and seed are validated against the run manifest and artifact.
Artifact closure follows defined BenchCraft reference fields, so arbitrary case metadata that
happens to end in `_artifact_id` remains ordinary case data. Run approval records preserve
`approved_by` provenance in imported reports. Only terminal runs are portable. Active, resumable,
and unknown run states are refused. The archive does not copy
project source, external datasets, credentials, or chat/session state; the frozen application spec
preserves provenance but is not executable source code.

Developer smoke runs persist only the selected case records while their dataset manifest records
the source dataset's full case count. A smoke bundle therefore contains those selected cases and
their executions, not the full source dataset. Other run modes require a complete case dependency.

Validation: `tests/test_run_bundle.py`, `tests/test_cli_runs.py`, `tests/test_cli_workspace.py`,
and smoke selection checks in `tests/test_cli_app.py`; Ruff and Mypy on the changed Python
modules. Smoke selection refuses repeated case IDs. Independent
review/fix history is recorded in [G19 run bundle review evidence](../evidence/G19-run-bundles-review.md).
