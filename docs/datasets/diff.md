# Comparing datasets

`aibench dataset diff LEFT RIGHT` validates and compares two canonical JSONL datasets by
`case_id`. It reports added, removed, changed, and unchanged case counts without retaining all
cases in memory or printing case content.

```powershell
aibench dataset diff datasets/release-1.jsonl datasets/release-2.jsonl
aibench dataset diff datasets/release-1.jsonl datasets/release-2.jsonl --limit 10 --json
aibench dataset diff datasets/release-1.jsonl datasets/release-2.jsonl --fail-on-change
```

Both files must contain a unique, non-empty string `case_id` on every record. A missing or
duplicate ID fails the comparison because records cannot be paired reliably. Cases are
normalized with the BenchCraft case schema before comparison. JSON object key order and input
record order do not affect the change counts. Source line numbers and duplicate-tracking
metadata are not compared. The SHA-256 hashes cover stripped nonblank record text in source
order: internal JSON formatting and record order affect the hash, while blank lines and
surrounding line whitespace do not. A source hash can therefore differ while the normalized
case diff remains empty.

The JSON summary contains exact counts and `has_changes`. The `details` object includes sorted
case-ID samples for each changed category. `--limit` controls the maximum IDs read per category
(0–1000); a separate 16 KiB output budget per category can truncate samples further. The
`*_omitted` fields report how many IDs were not included. Case bodies are never returned.

Use `--fail-on-change` in CI to return exit code 1 when any case was added, removed, or changed.
Invalid files and invalid limits return exit code 2. With `--json`, both success and error
responses use the CLI's machine-readable envelope.
