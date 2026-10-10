# G18 delivery: bounded dataset diff

`aibench dataset diff LEFT RIGHT` validates two canonical JSONL files and compares normalized
cases by unique, non-empty explicit `case_id`. The command reports exact added, removed,
changed, and unchanged counts, bounded sorted ID samples, source content hashes, and a
`--fail-on-change` exit status for CI. A temporary SQLite index keeps case data off the Python
heap. Dataset bodies are never included in the result.

Input record reads and normalized records are capped at the dataset one-megabyte line limit.
ID samples are bounded by both `--limit` (0–1000 per category) and 16 KiB per category; omitted
counts remain exact. Human output JSON-escapes case IDs so control characters cannot alter the
terminal display. Machine errors, including malformed limits, use the shared JSON error
envelope.

The command requires an explicit stable ID on each case and rejects duplicate IDs. It compares
normalized case content independent of record order, JSON formatting, and nested object key
order. The SHA-256 hashes cover stripped nonblank record text in source order: internal JSON
formatting and record order matter, while blank lines and surrounding line whitespace do not.
They provide source-record provenance rather than the normalized-diff identity.

User instructions and examples are in [the dataset diff guide](../../../datasets/diff.md).

Validation:

```text
uv run pytest tests/test_cli_dataset.py tests/test_ingest.py tests/test_dataset_finite_numbers.py tests/test_dataset_discovery.py tests/test_episode_contract.py tests/test_episode_scoring.py
106 passed, 1 skipped

uv run ruff check src/aibench/cli/dataset.py src/aibench/datasets/diff.py src/aibench/datasets/ingest.py tests/test_cli_dataset.py
All checks passed

uv run ruff format --check src/aibench/cli/dataset.py src/aibench/datasets/diff.py src/aibench/datasets/ingest.py tests/test_cli_dataset.py
4 files already formatted

uv run mypy src/aibench
Success: no issues found in 157 source files

git diff --check
Passed
```

The independent review record is in [G18 diff review](../evidence/G18-diff-review.md).

Implementation commit [`777ca5d5`](https://github.com/HaisamAbbas/BenchCraft/commit/777ca5d52312d10925b1218354ebb69a80c44215)
was pushed to `fix/plugin-core-match` under `HaisamAbbas@outlook.com`.
