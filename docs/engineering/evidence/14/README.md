# Prompt 14 local evidence

This directory records local, non-published evidence for Prompt 14. It is not a release
artifact and contains no credentials, provider responses, or user data.

- `summary.json` — environment, package pins, test counts, and explicit pending checks.
- `comparison-contract.json` — the identities and safeguards exercised by the comparison
  service; it is a test-evidence index, not a benchmark score.

The real-package checks used the repository's ignored `plugins/deepeval/.venv` and
`plugins/ragas/.venv` environments. The cross-ecosystem test uses deterministic local
judges; no OpenAI-compatible hosted judge was called.
