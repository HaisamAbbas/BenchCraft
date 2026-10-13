# Machine-readable CLI output

Commands that support `--json` write one JSON document to standard output. Progress and
human-readable diagnostics remain on standard error so scripts can parse standard output
without filtering terminal text.

Every JSON result uses the `aibench.cli-output/1` envelope metadata:

```json
{
  "run_id": "run-123",
  "status": "failed",
  "_cli": {
    "schema": "aibench.cli-output/1",
    "exit_code": 1
  }
}
```

Object results keep their existing command fields. Array and scalar results are placed in a
`data` field because those JSON values cannot carry metadata themselves:

```json
{
  "_cli": {"schema": "aibench.cli-output/1", "exit_code": 0},
  "data": []
}
```

Expected command errors add a stable error document while retaining the output envelope:

```json
{
  "schema": "aibench.cli-error/1",
  "status": "error",
  "message": "dataset file not found",
  "exit_code": 2,
  "_cli": {"schema": "aibench.cli-output/1", "exit_code": 2}
}
```

`_cli.exit_code` matches the process exit status. Error documents may include a `details`
array when the command has actionable validation or policy findings. The envelope schema
version changes when its field placement or envelope semantics change; command-specific
result fields remain owned by their command. JSON Lines progress events are a separate
streaming interface and are not mixed into this one-document result.

Commands with confirmation prompts require `--yes` when `--json` is enabled. They return a
structured error instead of writing an interactive prompt into the machine-readable stream.
