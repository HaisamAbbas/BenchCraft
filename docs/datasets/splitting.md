# Splitting datasets

`aibench dataset split INPUT OUTPUT_DIR` validates a canonical JSONL dataset and creates
`train.jsonl`, `validation.jsonl`, `test.jsonl`, and `manifest.json` in a new directory.

```powershell
aibench dataset split cases.jsonl build/splits --seed 42
aibench dataset split cases.jsonl build/splits --train 0.7 --validation 0.15 --test 0.15 --seed 42 --json
```

The ratios must be between 0 and 1 and sum to exactly 1; each ratio accepts up to 12 decimal
places. At least two ratios must be non-zero. Each requested split receives at least one group
when enough independent groups exist; otherwise the command fails and asks for fewer active
splits or more groups. The default is 0.8/0.1/0.1. Splits are
deterministic for a given input, seed, and BenchCraft version. All cases sharing a non-empty
`group_id` stay together; cases without one are grouped by their `case_id`. Group integrity
takes priority over exact case-count ratios, so small datasets or large groups can produce
different realized counts. The summary and manifest show the requested ratios and actual case
and group counts.

Every record must have a unique, non-empty explicit string `case_id`. Deduplicate conflicting
IDs before splitting. Empty input is rejected. Normalization warnings are included in the
summary and manifest. `manifest.json` is written last in staging, and a single same-parent
directory rename publishes the completed result atomically without exposing partial files. An
existing output directory is never replaced. If staging cleanup fails after another error, the
error reports the retained staging path.
