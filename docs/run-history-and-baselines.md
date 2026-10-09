# Run history and named baselines

`aibench runs list` searches committed runs newest first. Use `--limit` (1–1000) and
`--offset` for stable pagination, `--status` for an exact status, `--tag` for an exact tag,
and `--baseline` for a currently promoted baseline alias. `--query` performs a
case-insensitive literal substring search across run IDs, statuses, frozen run identity,
dataset IDs, notes, tags, and current baseline aliases. Characters such as `%` and `_` in
the query are treated literally. Add `--json` for the versioned CLI output envelope.

```console
aibench runs list --query "release candidate" --limit 50 --offset 0 --json
aibench runs tag RUN_ID release
aibench runs untag RUN_ID release
aibench runs note RUN_ID "release candidate for the API rollout"
aibench runs note RUN_ID --clear
```

Tags are case-insensitive labels that start with a letter or digit and contain only letters,
digits, `.`, `_`, or `-` (up to 64 characters). Notes are sanitized before storage and are
limited to 2,000 characters. `runs show` and `runs list --json` include each run's tags,
note, and any baseline aliases that currently point to it.

## Retry application failures

Create a new child run from a finished parent run for final application errors, or select
particular cases from the parent's planned scope. Resume an interrupted parent before
retrying it:

```console
aibench runs retry RUN_ID --dry-run --json
aibench runs retry RUN_ID --max-cases 25 --repetitions 2
aibench runs retry RUN_ID --case case-17 --case case-42 --dry-run
```

Automatic selection includes final application errors only when every final repetition for
that case is known to have had no effects or was not dispatched. It also skips cases with an
unknown or in-flight execution work item. Selecting a case explicitly requests its rerun and
reports any prior execution that may have caused external effects. `--max-cases` defaults to
100, and `--repetitions` defaults to one. A dry run validates the current application and
evaluator setup and prints the exact child scope without creating a run.

The child inherits the parent's frozen plan and execution policy, revalidates authorization
against the current `--policy` or project policy setting (and `--trust-local-app` grant) as
well as the parent's policy, and revalidates current application/evaluator implementations.
It refuses if the dataset content changed or a case was outside the parent scope. Child run
manifests, reports, and `runs show` output retain the parent run ID.

## Promote and compare a baseline

An operator records explicit quality approval when promoting a baseline. Promotion is
allowed only for a completed run with healthy, complete work and every declared release gate
passing. Execution-policy approval authorizes dispatch and does not count as quality
approval. A baseline alias uses the same label syntax as a tag.

```console
aibench runs baseline promote production RUN_ID --approved-by "Release manager"
aibench runs baseline list
aibench runs baseline show production --json
aibench runs baseline history production --json
aibench runs list --baseline production
aibench compare @production CURRENT_RUN_ID
```

Promoting the same run again with the same approver is an idempotent no-op. Replacing a
baseline appends a promotion-history record with the previous run ID, approver, and time.
`compare @ALIAS CURRENT` always resolves a named baseline. For convenience,
`compare ALIAS CURRENT` also resolves an alias when `ALIAS` is not an existing run ID; an
exact run ID takes precedence unless prefixed with `@`.

Run tags, notes, active aliases, and promotion history are stored in the workspace database
and are included in schema migration 12. Existing run records are preserved when an older
workspace is upgraded.
