# G18 delivery: safe dataset deduplication

`aibench dataset deduplicate INPUT OUTPUT` validates and normalizes each JSONL record, then
writes one case per explicit `case_id`. The SQLite index keeps case bodies and duplicate
tracking off the Python heap. The output is canonical JSONL, is created atomically, and cannot
replace an existing path.

Identical normalized repeats are removed automatically. A repeated ID with different normalized
content fails before publishing output unless the user explicitly selects `--on-conflict first`
or `--on-conflict last`. Output order follows the retained first or last occurrence. Every
record needs an explicit non-empty string ID because generated IDs depend on record position.
Legacy normalization warnings are returned with a 1,000-entry cap and truncation indicator so
transformations remain visible to users.

Examples and behavior are in [the dataset deduplication guide](../../../datasets/deduplicate.md).

Validation:

```text
uv run pytest tests/test_cli_dataset.py tests/test_ingest.py tests/test_dataset_finite_numbers.py tests/test_dataset_discovery.py tests/test_episode_contract.py tests/test_episode_scoring.py -q
114 passed, 1 skipped

uv run ruff check src/aibench/datasets/transform.py src/aibench/cli/dataset.py tests/test_cli_dataset.py
All checks passed

uv run ruff format --check src/aibench/datasets/transform.py src/aibench/cli/dataset.py tests/test_cli_dataset.py
3 files already formatted

uv run mypy src/aibench
Success: no issues found in 158 source files

git diff --check
Passed
```

The independent review record is in [G18 deduplication review](../evidence/G18-dedup-review.md).

Implementation commit [`0ce569f6`](https://github.com/HaisamAbbas/BenchCraft/commit/0ce569f6e8d2692b537965e0528b641123aa8f5d)
was pushed to `fix/plugin-core-match` under `HaisamAbbas@outlook.com`.
