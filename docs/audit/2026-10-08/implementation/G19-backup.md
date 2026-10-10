# G19 delivery: verified workspace backup and restore

`aibench workspace backup DEST.zip` creates a versioned ZIP snapshot of `.aibench/` while
the workspace is in use. SQLite is copied through its online backup API from a temporary
hard-link snapshot of the database and live WAL/SHM sidecars. The archive contains the
catalog-referenced content-addressed artifacts and other durable regular files; it excludes
unreferenced blobs, SQLite sidecars, lock files, credentials, project sources, and recognized
interrupted staging directories. The destination must be new and outside `.aibench/`.

`aibench workspace restore ARCHIVE` validates the archive manifest and file hashes, rejects
unsafe, colliding, linked, encrypted, unsupported, oversized, or malformed entries, then
checks the staged SQLite file against its manifest size and digest before SQLite opens it,
then checks database integrity, foreign keys, artifact digests, and catalog records. Catalog
artifact URIs are relocated to the new workspace. The validated database is protected from
writes through publication and its identity and final digest are rechecked immediately after
the atomic directory move. Restore extracts into a private staging directory and publishes
`.aibench/` only when that path is absent. It never overwrites an existing workspace. POSIX
restores use owner-only file and directory permissions. Windows pins directory handles,
temporarily marks the validated database read-only, and uses a no-replacement directory rename.

The format is bounded to one million entries, 1 TiB of uncompressed files, a 256 MiB ZIP
central directory, a 64 MiB manifest, and 250,000 aggregate path components. Restore copies
the opened archive into a bounded private temporary snapshot, verifies the source did not
change during that copy, and preflights ZIP end records before `ZipFile` materializes entry
metadata. The temporary snapshot can require disk space up to the archive's size. On Windows,
manual copies still require all aibench processes to stop and must include the database with
its live WAL/SHM sidecars.

Commands:

```console
aibench workspace backup ../workspace-backup.zip --workspace .
aibench workspace restore ../workspace-backup.zip --workspace .
```

Implementation is in `src/aibench/storage/workspace_backup.py`; CLI handling and user
instructions are in `src/aibench/cli/workspace.py` and
`docs/release/upgrade-and-recovery.md`. Independent review and final verification are recorded
in [G19 backup review](../evidence/G19-backup-review.md).
