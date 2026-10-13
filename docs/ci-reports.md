# CI report adapters

`aibench report RUN_ID --format junit` writes JUnit XML and
`aibench report RUN_ID --format sarif` writes SARIF 2.1.0. Both adapters are generated from
the committed report, selected case/repetition rows, and recorded scoring pass; they never
rerun the application or evaluators.

```powershell
aibench report RUN_ID --format junit --out junit.xml --no-content
aibench report RUN_ID --format sarif --out results.sarif --no-content
aibench report RUN_ID --format junit --out junit.xml --json
```

JUnit creates test cases for application executions, metric results, release gates, and run
completion. A failed metric or release gate becomes a `<failure>`; unavailable application or
evaluation work and undecided gates become `<error>`; not-applicable results and metrics with
no decision rule are reported as skipped. Use a CI test-result publisher to ingest the XML.

SARIF follows [OASIS SARIF 2.1.0 plus Errata 01](https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/sarif-v2.1.0-errata01-os-complete.html).
Failed metrics, release gates, application errors and incomplete work are findings. Each
finding carries run, case/repetition, scoring-pass, metric or gate identity in its properties;
BenchCraft does not invent source-file locations for benchmark cases. Passing checks do not
produce SARIF findings.

Both formats honor `--no-content`. It withholds output, error details, metric values and
free-text reasons while retaining case/run identifiers and machine reason codes. Raw
artifacts and traces are never embedded. The command exit code reports whether the artifact
was generated; CI test-result/SARIF ingestion determines how findings affect pipeline policy.

Use [case-result exports](case-result-export.md) for complete row-oriented JSONL/CSV data.
