# Installing, upgrading and recovering (aibench 0.1.0rc1)

This covers installing from the release artifacts, what happens to a workspace when you
upgrade, and what to do when a run or session is interrupted. Each recovery path below is
exercised by the automated test named beside it.

## Installing from the release artifacts

aibench is not published to a package index. The Phase 2 working tree builds these six files:

| File | What it is |
|---|---|
| `aibench-0.1.0rc1-py3-none-any.whl` | The CLI and library. This is what you install |
| `aibench-0.1.0rc1.tar.gz` | Source distribution of the same code |
| `aibench_deepeval-0.1.0rc1-py3-none-any.whl` | Optional DeepEval judge adapter, for its own environment |
| `aibench_deepeval-0.1.0rc1.tar.gz` | Its source distribution |
| `aibench_ragas-0.1.0rc1-py3-none-any.whl` | Optional Ragas adapter, for a separate environment |
| `aibench_ragas-0.1.0rc1.tar.gz` | Its source distribution |

Check the files against `SHA256SUMS` before installing. The same sums are recorded in
`docs/engineering/evidence/13/SHA256SUMS`.
In this repository, the four verified files are in `docs/engineering/evidence/13/dist/`;
copy them beside `SHA256SUMS` when preparing a local handoff.

```bash
sha256sum -c SHA256SUMS                         # PowerShell: Get-FileHash FILE -Algorithm SHA256
python -m venv .venv
.venv/Scripts/pip install aibench-0.1.0rc1-py3-none-any.whl     # Linux/macOS: .venv/bin/pip
.venv/Scripts/aibench --version                                  # aibench 0.1.0rc1
```

The dependencies (pydantic, typer, rich, httpx, jsonschema, prompt_toolkit) come from the
package index. Python 3.11 or 3.12 is required; see the
[compatibility matrix](../engineering/platform-matrix.md).

**Building the artifacts yourself.** From a clone:
`python scripts/release_check.py --out DIR` builds all six files into `DIR/dist` and writes
`SHA256SUMS`. It then installs the wheel into a clean environment and runs the documented
demo. Add `--python PATH` once per interpreter to check, and `--plugin` to check both
evaluator adapters in separate clean environments.

**The DeepEval adapter** goes in its own environment, never in the one aibench runs in:

```bash
python -m venv deepeval-env
deepeval-env/Scripts/pip install aibench-0.1.0rc1-py3-none-any.whl aibench_deepeval-0.1.0rc1-py3-none-any.whl
```

It pins `deepeval==4.2.5` and refuses to run with another version. How a plan uses it is
described in [support.md](../support.md#optional-evaluators).

**The Ragas adapter** also goes in its own environment. Do not co-install the two adapter
packages merely to save disk space: their upstream dependency trees are independently
controlled and the release verifier keeps them isolated.

```bash
python -m venv ragas-env
ragas-env/Scripts/pip install aibench-0.1.0rc1-py3-none-any.whl aibench_ragas-0.1.0rc1-py3-none-any.whl
```

It pins `ragas==0.4.3`, exposes only text faithfulness, and refuses another upstream
version. Ragas 0.4.3 has an open multi-modal SSRF advisory; this adapter never exposes the
affected metric and accepts only stored text. See ADR 0013 for the compensating control and
`plugins/ragas/README.md` for judge configuration.

## Upgrading

### Updating a plugin

After upgrading BenchCraft, update each plugin with `/plugins install NAME` in the chat (for
example `/plugins install deepeval`). It installs the new adapter and this version's aibench
together in the plugin's environment; the project's judge settings are kept.

Do not install only the adapter's wheel into that environment (`pip install --no-deps`): the
environment keeps its older aibench, and an adapter that needs the newer one does not load. A
plugin that fails to load says so when a session is reopened, and names the version mismatch.

### What happens to a workspace

A project's workspace is its `.aibench/` directory:
- `aibench.db`: runs, attempts, results, sessions, events;
- `artifacts/`: captured requests, responses and evaluator outputs, content-addressed;
- `reports/`: rendered reports, which can be rebuilt.

- **Newer aibench, older workspace.** The workspace database is migrated forward the first time a command opens it. Each migration is one transaction: a crash mid-migration leaves the previous schema intact, and the next open retries it. Nothing is migrated backwards. (`tests/test_storage_migrations.py`)
- **Prompt 18 workspace schema 9.** The migration adds candidate pools, candidate cases, and append-only candidate review/verification events. Candidate records are kept separately from ordinary datasets. Existing workspace contents remain available after the forward migration.
- **Prompt 17 workspace schema 10.** The migration adds durable remote evaluation job state for the hosted Evals API bridge.
- **Prompt 19 workspace schema 11.** Migration 11 adds experiment contracts and trial lineage, append-only lifecycle events, and protected holdout digest reservations. Existing runs and datasets remain available after the forward migration.
- **Older aibench, newer workspace.** Refused with exit code 2: "this workspace was upgraded by a newer aibench (schema version N; this aibench knows up to M)". Nothing is written. Install the newer version again to use it. (`tests/test_release_candidate.py`)

0.1.0rc1 is the first release, so there is nothing to migrate from yet. A workspace created
by an unreleased development build before this candidate is migrated forward like any
other.

### Runs that span an upgrade

A run freezes the identity of each metric it uses: the evaluator ID, its semantic version,
and its parameters. After an upgrade, `aibench resume`:

- **resumes** if every metric identity is unchanged. Each result records the aibench
  version that produced it (`plugin_version` in its provenance), so a run that spans an
  upgrade shows it;
- **refuses** if the upgrade changed a metric's semantic version, and calls nothing:
  "evaluator identities changed since the run started (version drift)". A run never mixes
  two meanings of a metric. (`tests/test_release_candidate.py::test_resume_after_an_upgrade_refuses_only_a_changed_metric`)

The release notes ([CHANGELOG.md](../../CHANGELOG.md)) list every metric whose semantic
version changed. The simplest rule is to finish runs before upgrading:

1. Finish or cancel running runs: `aibench runs list`, then `aibench resume RUN_ID`, or `/stop` in chat.
2. **Optional:** back up the workspace, as described below.
3. Install the new wheel.

On a run that can't be resumed, these still work:
- `aibench report RUN_ID` (reports are rebuilt from stored facts);
- `aibench evaluate RUN_ID --plan PLAN` (rescores the stored outputs with the new version's evaluators, as a separate scoring pass).

To re-execute its unfinished cases, start a new run.

### Backing up a workspace

Create an online, verified snapshot with:

```console
aibench workspace backup ../workspace-backup.zip --workspace .
```

The command snapshots SQLite consistently while the app is running, includes catalogued
artifacts and other durable regular workspace files, and records checksums for restore-time
verification. It omits unreferenced artifact blobs, SQLite sidecars, lock files, and interrupted
temporary staging directories. Snapshotting temporarily hard-links the database and live WAL
sidecars inside `.aibench/`, so backup needs write permission and filesystem hard-link support.
The archive contains `.aibench/` data only; it excludes project source, external datasets,
credentials, and files outside the workspace. Backup refuses to overwrite an existing archive.
Restore into a project with no `.aibench/` directory:

```console
aibench workspace restore ../workspace-backup.zip --workspace .
```

Restore verifies a bounded private snapshot of the archive and database before atomically
publishing the workspace, and refuses to replace existing data. The temporary snapshot can
require disk space up to the archive's size. The restored database stays protected from writes
until publication and is checked again after the move. On POSIX systems, restored files and
directories are created with owner-only permissions. Archives support up to one million files
and 1 TiB of uncompressed data. Manual copies must be made while **no aibench process is using the
workspace** and must include `aibench.db`, `aibench.db-wal`, and `aibench.db-shm` together.

Check a workspace without changing it:

```console
aibench workspace check --workspace .
aibench workspace check --workspace . --json
```

The check validates SQLite integrity and references to artifact objects, session plans, and
dataset-suite snapshots. It exits 0 when healthy, 1 when it finds data-integrity issues, and 2 if
it cannot safely inspect the workspace. JSON output keeps these exit codes, so CI can fail on
detected corruption.

Recover a workspace from a verified backup with a preview first:

```console
aibench workspace repair ../workspace-backup.zip --workspace .
aibench workspace repair ../workspace-backup.zip --workspace . --apply
```

Repair validates the backup by restoring it into a private workspace and running the integrity
checks before it changes the selected workspace. Without `--apply`, it reports the verified
backup and leaves the workspace untouched. With `--apply`, it retains the existing `.aibench/`
directory as `.aibench-recovery-*`, publishes the verified restore, and checks it again. If the
published workspace fails its final check, repair moves it aside and restores the retained
workspace. The operation needs temporary disk space for a private archive copy and a full staged
restore alongside the existing workspace. Stop all BenchCraft processes that use the workspace
before applying repair; open database handles can prevent the directory swap.

### Moving one completed run to another workspace

Run bundles move one terminal run and its database dependencies and artifact bytes. They do not
include project source files, external datasets, credentials, or conversation sessions. The
frozen application specification is retained for provenance, but the application source still
needs to be available separately if you want to run new benchmarks in the destination project.

```console
aibench runs export RUN_ID ../run-bundle.zip --workspace .
aibench runs import ../run-bundle.zip --workspace ../other-project
```

Import creates `.aibench/` in the destination project if needed. It verifies the archive and
artifact digests before writing catalog rows, relocates artifact references to the destination,
and refuses conflicting run or artifact IDs. Importing the same bundle again is idempotent when
the stored records match. Run approval records are preserved so imported reports retain their
`approved_by` provenance. Only completed, cancelled, budget-exhausted, failed, and legacy
`succeeded` runs can be bundled; active or unknown run states are refused. Evidence pointers in
the results are preserved as pointers and are not treated as artifact IDs. Developer smoke runs
include only their selected case records; the dataset manifest retains the original total case
count, so a smoke bundle is a portable record of that sample rather than a complete dataset copy.

### Moving a conversation to another workspace

Session bundles preserve a conversation's decisions, turns, questions, actions, and frozen draft
plans. They do not include application or dataset files, plugin environments, or benchmark run
records. Import into a project copy with the same relative application, dataset, and policy files;
the importer rebases those paths and clears the session's prior local-trust grant. Export refuses
an active session run. Import matching terminal runs separately with `aibench runs import`.

```console
aibench sessions export SESSION_ID ../session-bundle.zip --workspace .
aibench sessions import ../session-bundle.zip --workspace ../other-project
```

Conflicting session rows or plan files are refused, and importing the same bundle again is
idempotent. Conversation records contain user and assistant messages, so treat the archive as
project data with the same access controls as the workspace.

### Cleaning stale artifacts

Preview storage cleanup before deleting anything:

```console
aibench workspace gc --workspace .
aibench workspace gc --workspace . --json
```

The command lists unreferenced content-addressed artifacts and abandoned write temporaries.
It keeps catalog-referenced objects and files within the grace period. Deletion requires the
explicit `--apply` flag and at least the default one-hour grace period:

```console
aibench workspace gc --workspace . --apply
```

Only recognized artifact shards and object names are considered. Symbolic links, reparse points,
hard-linked objects, and files that change during cleanup are skipped; the command reports how
many files and bytes it removed.
It does not create a workspace when none exists.
To keep its catalog read pinned while cleanup runs, it briefly creates a temporary hard-link
snapshot of `aibench.db` and any live WAL sidecars under `.aibench/`. A preview therefore needs
temporary write permission and filesystem hard-link support there. The snapshot is removed on
normal exit; an interrupted command may leave a `.aibench-gc-*` directory, which does not prevent
later cleanup.

## Recovering

| What happened | What you see | What to do | Tested by |
|---|---|---|---|
| Ctrl+C during a run | The run stops dispatching; exit code 130; "resume with: aibench resume RUN_ID" | `aibench resume RUN_ID` runs only unfinished work | `tests/test_engine.py::test_interrupt_then_resume_completes_without_duplicates`, `tests/test_cli_run.py::test_interrupted_run_resumes_through_the_cli_and_rescoring_never_invokes` |
| The process was killed, or the machine lost power | `aibench runs status RUN_ID` shows the run as `running` or `interrupted`; a chat reopened later shows it interrupted | `aibench resume RUN_ID`. Calls that were in flight are counted as possibly made, with unknown cost. They are repeated only if the application declares no effects | `tests/test_engine_faults.py`, `tests/test_mvp_acceptance.py` (a killed process) |
| The disk filled up, or the workspace failed (I/O error, read-only, database full) | Exit code 130; "warning: workspace storage failed (...); stopped dispatching"; no case is marked failed for it | Free space or fix the disk, then `aibench resume RUN_ID` | `tests/test_storage_failures.py` |
| An effectful application was interrupted mid-call | Those cases end as `unknown_effect` with "reconcile the application state before repeating"; they are never repeated automatically | Check in the application whether each call took effect. To call only the cases that didn't, start a new run with those IDs in the plan's `selection.case_ids` | `tests/test_engine_faults.py::test_ambiguous_effectful_crash_is_unknown_effect_and_never_auto_repeated` |
| Another terminal is running the run | "run RUN_ID is being run by another session (host H, pid P)" | Wait, or use that terminal. If that process is gone, the next resume takes over its lease | `tests/test_engine_review_regressions.py::test_a_second_session_cannot_resume_a_live_run`, `::test_a_dead_sessions_lease_is_taken_over_and_its_time_recorded` |
| A stored plan, dataset or artifact no longer matches its hash | Resume refuses, for example "the run's frozen plan artifact failed verification", and dispatches nothing | Don't edit files under `.aibench/`. Restore them from a backup, or start a new run | `tests/test_engine_faults.py::test_resume_verifies_frozen_identities` |
| The chat was closed or crashed during a run | Reopening with `aibench` shows the run's real state and the events you missed. Nothing restarts on its own | `/resume` in the chat, or `aibench resume RUN_ID` | `tests/test_session_recovery.py` |
| The assistant model is down | Messages can't be interpreted | Slash commands still work: `/status`, `/pause`, `/stop`, `/report` | `tests/test_tui.py::test_provider_failure_does_not_disable_terminal_controls` |

A resumed run keeps its identities, plan, budget and approval. What was spent before the
interruption counts against the budget. That includes calls that may have happened but
left no record.

## Uninstalling

`pip uninstall aibench aibench-deepeval aibench-ragas` removes the code. Workspaces (`.aibench/`) and
reports stay where they are until you delete them.
