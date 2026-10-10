# Deduplicating datasets

`aibench dataset deduplicate INPUT OUTPUT` validates a JSONL dataset, normalizes its cases,
and writes one case per explicit `case_id`.

```powershell
aibench dataset deduplicate cases.jsonl cases.unique.jsonl
aibench dataset deduplicate cases.jsonl cases.unique.jsonl --on-conflict first
aibench dataset deduplicate cases.jsonl cases.unique.jsonl --on-conflict last --json
```

Identical normalized repeats are removed automatically. If a repeated ID has different
normalized content, the command stops without publishing output by default. Choose
`--on-conflict first` or `--on-conflict last` to keep the respective occurrence. The output
order follows the retained occurrences in the source. The summary reports the number of
records removed and how many repeated records differed from their ID's first occurrence.

Every record must have a non-empty explicit string `case_id`; generated IDs depend on record
position and are unsafe for deduplication. Input is fully validated before publication. The
output is canonical JSONL, is created atomically, and an existing target is never replaced.
If Windows or another process prevents removal of the temporary hard link after publication,
the command still reports success and sets `temporary_cleanup_warning` in JSON (or prints a
warning in human output); the published dataset is complete.
Invalid input or a conflicting ID under the default policy returns exit code 2. `--json`
returns the shared machine-readable error envelope on failure.
