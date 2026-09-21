# Specification source of truth

- **File:** `docs/spec/implementation-plan.md`
- **Document version:** v1.1, 21 September 2026
- **Working name:** BenchCraft (package/CLI name `aibench`)
- **SHA-256 (actual, computed on copy):** `ae5aad5bf7b6520019affb085f63d7a11c575e37bca6e47b0b0e5332f57339d8`
- **Copied from:** `AI-Application-Evaluation-Harness-Implementation-Plan (1).md` (repository root)

## Discrepancy: prompt pack source hash

`docs/spec/prompt-pack.md` (copied from `AI-Bench-Codex-Implementation-Prompt-Pack.md`) states:

> Source SHA-256: `2bbf2119408b2f5f6bdecfb60b97bd4eef5553d8fc7cb8a8b4f4a564f4865d19`

That string is 65 hex characters (a valid SHA-256 hex digest is 64) and does not match the
actual SHA-256 of the supplied specification file computed above. Treated as a documentation
error in the supplied prompt pack, not a signal that a different specification file exists.
Decision: use the actual computed hash above as the canonical content identity for this
specification; recorded in `docs/adr/0001-source-of-truth-and-dependency-direction.md`.
