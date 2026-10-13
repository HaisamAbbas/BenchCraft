# Importing datasets

`aibench dataset import` converts CSV, JSON arrays, JSONL/NDJSON, and optional Parquet files
into canonical BenchCraft JSONL. Each source record is normalized and checked against the
versioned `BenchmarkCase` schema before the output becomes visible. Import does not execute an
application or call a provider.

```powershell
aibench dataset import source.csv cases.jsonl --dataset-id support-v2 --split development
aibench dataset import source.json cases.jsonl --format json --json
aibench dataset import exported.csv cases.jsonl --map input=prompt --map expected_output=answer
aibench dataset import source.parquet cases.jsonl --trust-parquet
```

JSON files must contain a top-level array of case objects. JSONL and NDJSON contain one case
object per nonblank line. CSV headers use the canonical case field names such as `case_id`,
`input`, `reference`, `fixtures`, and `metadata`. Structured CSV cells (`input`, `reference`,
`expectations`, `fixtures`, `repository`, `metadata`, `extensions`, `provenance`, `context`,
and `expected_tools`) may be encoded as JSON objects or arrays; ordinary text remains text.
Unknown fields, malformed
records, duplicate or blank CSV headers, and rows whose field count differs from the header
are rejected with the source record or line number. Repeat `--map CASE_FIELD=SOURCE_FIELD` to
adapt external column names; mappings apply to JSON and Parquet records too. Unmapped unknown
fields are rejected rather than silently discarded.

Parquet support is optional. Install it with `pip install 'aibench[parquet]'`. PyArrow must
decompress Parquet pages before the importer can inspect row sizes, so importing Parquet
requires the explicit `--trust-parquet` acknowledgement and should be limited to trusted files.
Rows larger than one megabyte are rejected after decoding. CSV and Parquet inputs allow at most
256 columns. JSONL and CSV enforce the one-megabyte record bound while reading, and JSON arrays
are decoded incrementally with a one-megabyte record limit.

The output path must be new and its parent directory must already exist. The importer writes
and validates a same-directory temporary file, then publishes it without replacing an
existing target. The summary includes the canonical content hash and any duplicate case IDs;
deduplicating those records remains an explicit dataset operation.
