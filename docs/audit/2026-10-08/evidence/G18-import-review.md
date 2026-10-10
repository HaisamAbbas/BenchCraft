# G18 dataset importer independent review

The requested review-agent workflow performed read-only reviews of the importer, CLI, and
regressions. The final review reported no remaining actionable findings.

Earlier passes found that CSV structured-cell detection used overly broad Unicode whitespace,
JSONL lines were read before enforcing the line limit, CSV logical records lacked an aggregate
bound, and Parquet batches were materialized before row-size checks. The fixes now use JSON's
ASCII whitespace set, bounded binary JSONL reads, an aggregate CSV record/header limit with a
256-column cap, and one-row Parquet batches checked before conversion to Python objects.

PyArrow still decompresses Parquet pages before those row checks. Since its Python reader does
not expose a hard page-decompression cap, the CLI fails closed unless `--trust-parquet` is
explicitly supplied. Help text and the user guide say to use this option only for trusted
files. The reviewer accepted this explicit trust gate and confirmed that default Parquet import
fails before decoding.

Final review verification included `tests/test_cli_dataset.py` (32 passed), Ruff, and the import
command help output. The complete focused regression batch and type/format/lock checks are
recorded in [G18 importer delivery](../implementation/G18-import.md).
