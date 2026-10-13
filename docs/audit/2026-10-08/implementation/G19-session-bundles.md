# G19.4: Portable session bundles

`aibench sessions export SESSION_ID DEST --workspace PROJECT` writes a versioned, checksummed
archive containing the session, its decisions and draft history, conversation turns, pending
questions, action requests, and every frozen plan file. `aibench sessions import BUNDLE
--workspace PROJECT` validates the archive and database schema before creating the destination
workspace, rebases project-relative application, dataset, policy, and project-root paths, and
inserts session rows and plan files transactionally. Reimporting matching data is idempotent;
conflicting rows or plan files fail without a partial database import.

The importer verifies archive paths and digests, typed records, revision and conversation links,
decision lineage, frozen plan hashes, plan-to-project paths, and the target project's required
application, dataset, and policy files. It refuses a session with an active run. Because the
session's local trust grant is specific to its original project context, an imported session has
`trusted_local` cleared; normal review and execution policy checks still apply. Import does not
copy project files or plugin environments: the target project must contain the referenced files,
and runtime plugin environments must be available to that installation.

Runs are independent benchmark records rather than session-owned rows. The bundle reports their
IDs but does not copy run catalogs or artifacts. Move each needed terminal run separately with
`aibench runs export` and `aibench runs import`. This keeps run and conversation transfer
independently idempotent.

Validation: `tests/test_session_bundle.py` covers cross-workspace import, Windows path rebasing,
decision history, tamper detection, active-session refusal, conflict-safe repeat import, and both
CLI commands. Ruff and Mypy checks are recorded with the delivery review.
