# Named dataset suites

Register a validated JSONL dataset as an immutable, workspace-local version:

```powershell
aibench dataset suites register support 1.0.0 datasets/support.jsonl `
  --description "Support benchmark" --workspace .
aibench dataset suites list --workspace .
aibench dataset suites show support@1.0.0 --workspace .
```

Suite names begin with a lowercase letter and contain lowercase letters, digits, underscores, or
hyphens; Windows device names such as `con` and `nul` are reserved. Version labels begin with a
letter or digit and contain letters, digits, `.`, `_`, `+`, or `-`. Every suite record must have a
non-empty, unique explicit `case_id`; records are validated against the dataset case schema before
registration. Version labels are case-sensitive. The description is optional and limited to 500
characters.

Registration copies the validated source under `.aibench/dataset-suites/` using a portable,
collision-safe internal filename and records its fingerprint in the workspace catalog. The default
workspace is the current directory; `--workspace` names the project directory containing
`.aibench/`. Changing or removing the source file after registration does not change the snapshot.
Registering the same name and version again with identical content and metadata is an idempotent
no-op. A different snapshot or description under an existing name and version is rejected; publish
it under a new version.

Pin a registered suite when validating or running a plan:

```powershell
aibench run --plan plans/support.json --dataset-suite support@1.0.0 `
  --workspace . --trust-local-app --dry-run --json
```

`--dataset-suite` replaces the dataset path in the plan. It works with `--plan` or the project
directory form of `aibench run`; it cannot be combined with a positional dataset file. The CLI
checks the stored snapshot fingerprint before compiling the plan and compares it again after
compilation, before any application dispatch. `dataset suites show` also verifies the snapshot.
If a snapshot was modified or removed, restore the exact registered file or register the intended
content under a new version. Normal plan policy, including allowed data roots, still applies to the
snapshot path.

All suite commands support `--json`. `list` accepts `--name` to filter a suite name; `show` takes
one `NAME@VERSION` reference.
