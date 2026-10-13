# Run reproducibility and provenance

Set `--run-seed` to make the engine's seeded randomness repeatable:

```console
aibench run --plan benchmark.plan.json --run-seed 1729
aibench runs show RUN_ID --json
```

The run seed is independent of `--selection-seed`: the selection seed chooses a sample of
cases, while the run seed controls engine randomness such as retry backoff. If `--run-seed`
is omitted, BenchCraft assigns a random seed. The completed `run` output and `runs show`
report the actual value. `--dry-run` reports an explicitly supplied seed, or states that a
seed will be assigned when the run is created.

Run records include the application source-content digest, available interpreter and
dependency fingerprints, BenchCraft version, evaluator dependency/plugin identities, and the
benchmark host runtime. When a local Python or CLI application is inside a Git worktree, the
record also includes the application repository's commit and digests for its tracked
working-tree diff and non-ignored untracked files under the local application root. The
`aibench runs show RUN_ID --json` command exposes source, application
environment, and Git
metadata under `application_identity`, with evaluator and host provenance at the top level.
Git metadata is marked unavailable when it cannot be read or the application is not in a Git
repository. The source digest also covers untracked application code that participates in
the application identity. Tracked diffs are hashed incrementally with a 64 MiB cap. The
untracked-file name list is capped at 8 MiB and 2,000 files, and file contents are streamed
with a 64 MiB combined cap. If Git cannot produce either bounded fingerprint, run creation
fails before dispatch unless the plan's application declares an owner-supplied revision or
environment digest. Ignored files, files outside the local application root, and external
runtime resources are not enumerated; pin them with `revision` or `environment_digest`.

The content and environment digests remain identity checks for resume; a changed source,
runtime, dependency set, Git commit, tracked diff, or untracked local file causes resume to refuse and
directs the operator to restore the original identity or create a new run.

These records make local inputs inspectable, but a seed does not control randomness inside
remote applications, model providers, or evaluators. For those, use a pinned application
revision or environment digest and provider/model versions where available.
