# G19.7: absolute-path migration

`benchcraft workspace migrate-paths [--workspace PROJECT] [--json]` previews path updates
for a project whose directory has moved. `--apply` performs those updates; the default is
read-only. The command requires an existing, healthy workspace and refuses to run while a
session has an active run.

The command updates artifact catalog URIs to the current workspace's content-addressed store,
sets session project roots to the selected project, and rebases session policy and decision
application/dataset paths only when they are rooted under each stored project root. It checks
artifact catalog hashes and file bytes, validates frozen-plan identities, and checks workspace
integrity before changing data. Catalog scanning is paged, and all database edits commit as one
transaction; a failed preflight or verification rolls back the complete operation.
The preview scans one SQLite read snapshot. The operation pins project, workspace, artifact-store,
and database identities throughout execution; Windows holds handles that prevent those paths
from being renamed while SQLite is opening or updating them. On POSIX, the command verifies the
opened SQLite database, WAL, SHM, and rollback-journal file descriptors against the pinned
workspace files. On Windows, it inspects SQLite file handles opened by the process and checks
their final paths and file identities against the workspace. The database and SQLite sidecars
must be private single-link regular files.

External absolute paths remain unchanged. Frozen session plan files, run records, and experiment
records are never rewritten. Before applying, the command checks that each frozen plan still
resolves to the same application and dataset references after the move. If a relative or absolute
reference would resolve somewhere else, migration stops with an error because changing the frozen
plan would violate its recorded identity.

```powershell
benchcraft workspace migrate-paths --workspace D:\Benchmarks\MyProject --json
benchcraft workspace migrate-paths --workspace D:\Benchmarks\MyProject --apply
benchcraft workspace check --workspace D:\Benchmarks\MyProject
```

Verification on Windows: the migration and adjacent workspace/storage suites pass (136 passed,
23 skipped). They cover a moved project with an external
absolute dataset and policy, read-only preview, apply, artifact relocation and integrity,
idempotence, a copied project, active-session refusal, rejection of external relative frozen
plan drift, malformed and replaced sidecars, workspace identity changes, a stable preview
snapshot, path guards, rollback when WAL is removed before commit, rejection of hard-linked
databases and sidecars, sidecar handles outside the workspace, and Windows/POSIX path-flavor
handling for project roots and dependencies. Separate POSIX tests cover
opened-file identity across a directory-swap race and descriptor-bound sidecars; those tests are
skipped on this Windows host. Historical
project roots normalize lexically, so symlinks created at the old path after a move do not
redirect migration. (Creating test directory symlinks is also unavailable on this host.) Ruff,
Mypy, and changed-file formatting checks passed.
