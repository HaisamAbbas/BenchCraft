# Prompt 14 local evidence

This directory records local, non-published evidence for Prompt 14. It is not a release
artifact and contains no credentials, provider responses, or user data.

- `summary.json` — environment, package pins, test counts, and explicit pending checks.
- `comparison-contract.json` — the identities and safeguards exercised by the comparison
  service; it is a test-evidence index, not a benchmark score.
- The recorded RunEngine check uses a real instrumented application and verifies that
  stored-output DeepEval/Ragas scoring adds no application calls; the combined diagnostic
  also verifies unchanged stored execution identities. The built-wheel result predates the
  final comparison/identity hardening; a clean post-audit wheel rebuild remains pending while
  concurrent Prompt 17/18 work shares the worktree.

The real-package checks used the repository's ignored `plugins/deepeval/.venv` and
`plugins/ragas/.venv` environments. The cross-ecosystem test uses deterministic local
judges; no OpenAI-compatible hosted judge was called.
