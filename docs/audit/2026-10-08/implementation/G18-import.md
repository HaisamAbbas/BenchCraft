# G18 delivery: schema-aware dataset import

`aibench dataset import SOURCE OUTPUT` converts CSV, top-level JSON arrays, JSONL/NDJSON,
and optional Parquet into canonical BenchCraft JSONL. It normalizes each case against the
public `BenchmarkCase` contract, reports compatibility warnings and duplicate IDs, and
publishes the output only after the complete file validates. Existing targets are never
replaced.

Repeat `--map CASE_FIELD=SOURCE_FIELD` to accept external column or key names. CSV structured
cells can contain JSON for reference, context, tools, metadata, fixtures, and related fields.
JSONL and CSV enforce bounded record reads; CSV also caps input at 256 columns. Parquet is an
optional dependency (`aibench[parquet]`) and requires `--trust-parquet`: PyArrow decompresses a
page before the importer can inspect row size, so only trusted files should use this path. The
importer then checks each decoded row against the one-megabyte dataset-record limit.

Examples and format details are in [dataset import guide](../../../datasets/import.md).

Validation on the delivered importer:

```text
uv run --extra parquet pytest tests/test_cli_dataset.py tests/test_dataset_finite_numbers.py tests/test_dataset_discovery.py tests/test_episode_contract.py tests/test_episode_scoring.py -q
81 passed, 1 skipped

uv run ruff check src/aibench/datasets/importers.py src/aibench/cli/dataset.py tests/test_cli_dataset.py
All checks passed

uv run ruff format --check src/aibench/datasets/importers.py src/aibench/cli/dataset.py tests/test_cli_dataset.py
3 files already formatted

uv run mypy src/aibench
Success: no issues found in 156 source files

uv lock --check
Resolved 62 packages

git diff --check
Passed
```

The requested review-agent workflow found and resolved structured-cell whitespace handling,
non-JSON Unicode whitespace acceptance, CSV field and aggregate-record bounds, JSONL line
bounds, and Parquet batch materialization concerns. The final review found no actionable
findings after the explicit trusted-Parquet gate was added. Review details are in
[G18 importer review](../evidence/G18-import-review.md).

Implementation commit [`24809896`](https://github.com/HaisamAbbas/BenchCraft/commit/248098964c286e1e2413a3afbb173a132f7e9901)
was pushed to `fix/plugin-core-match` under `HaisamAbbas@outlook.com`.

G18 remains partial. Dataset diff, deduplication/splitting tools, and a named suite catalog
are the next outstanding subfeatures.
