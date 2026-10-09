# Case-result exports

`aibench export RUN_ID` exports one record for every selected `(case_id, repetition)` item
using committed storage only. It does not invoke the application, evaluators or judges.
Export defaults to the report's primary scoring pass: the engine pass when present, otherwise
the latest recorded pass. Select another recorded pass with `--scoring-id`.

```powershell
aibench export RUN_ID --format jsonl --out results.jsonl
aibench export RUN_ID --format csv --out results.csv --json
aibench export RUN_ID --format jsonl --out - --no-content
```

`--out -` writes only the requested JSONL or CSV rows to stdout. A file export defaults to
`.aibench/exports/<safe-run-id>/case-results.jsonl`. With `--json`, file exports report
path, row count, run status, scoring pass, selected-item basis and content mode in the
versioned CLI result envelope.

JSONL has one `aibench.case-result/1` object per line. Each row contains run/scoring
identity, case ID, repetition, work state, the dataset case, the final execution attempt,
and all metric results for the selected pass. A selected metric without a stored result is
shown as `pending` or `not_recorded`; a selected item with no execution still has a row.
CSV has a stable header; nested case data, output, error, context, usage, tool events, world
state, trace references, observation completeness, timing and metric rows are JSON cells, so
the tabular format retains the same stored execution and metric evidence as JSONL.
For legacy runs without a work graph, the export uses recorded final executions and names
that basis in its metadata.

Content is included by default, matching `aibench report`. `--no-content` withholds the case
object, output, error, retrieved context, tool events, world state, metric values and free-text
metric reasons. IDs, decisions, statuses, timings, observed costs and reason codes remain.
Raw artifact contents are never embedded; artifact references remain available for later
inspection.

CSV text cells beginning with a spreadsheet formula marker (`=`, `+`, `-`, or `@`, including
after leading whitespace) are prefixed with an apostrophe to prevent spreadsheet formula
execution. JSONL preserves the stored text exactly. CSV consumers that need original text can
remove that protective apostrophe from affected cells.
