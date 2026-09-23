# aibench-deepeval

DeepEval metrics for aibench. The adapter runs **only in its own Python environment**, driven
by aibench's evaluation worker; DeepEval is never imported into the aibench process.

Pinned: `deepeval==4.2.5` (the adapter refuses to run on any other version).
Metric: `deepeval.faithfulness@1` — DeepEval's FaithfulnessMetric over *recorded* outputs and
the retrieval the application actually reported.

## Why a separate environment

DeepEval brings ~70 packages, including pytest plugins that auto-load (`pytest-xdist`,
`pytest-rerunfailures`, `pytest-repeat`, `pytest-asyncio`) and telemetry clients. Keeping it
out of the core environment keeps aibench's own dependencies and test runs unaffected.

## Install (from the repository root)

```
python -m venv plugins/deepeval/.venv
plugins/deepeval/.venv/Scripts/pip install -e . -e plugins/deepeval     # Windows
plugins/deepeval/.venv/bin/pip install -e . -e plugins/deepeval         # Linux/macOS
```

## Use

```
aibench score RUN_ID --metrics metrics.json \
  --plugin-env plugins/deepeval/.venv/Scripts/python.exe \
  --plugin-secret OPENAI_API_KEY=env:OPENAI_API_KEY
```

with a binding such as:

```json
{"metrics": [{"metric": "deepeval.faithfulness@1",
              "params": {"judge": {"kind": "deepeval_model", "model": "gpt-4.1-mini"}},
              "rule": {"comparator": ">=", "threshold": 0.8}}]}
```

The run's application must declare `output_binding.retrieved_context`, or the metric is
refused before scoring.

Judges:
- `{"kind": "deepeval_model", "model": "<name>"}` — DeepEval's native model support. The
  provider key must be passed explicitly with `--plugin-secret`; the worker inherits nothing
  else from your environment. Calls go to that provider (paid).
- `{"kind": "python_factory", "factory": "module:function"}` — a function returning a
  `deepeval.models.DeepEvalBaseLLM` instance, importable in the plugin environment or from a
  `--plugin-path` directory. **This runs that code in the worker**; use only trusted code.

## Behaviour

| Situation | Result |
|---|---|
| Retrieval not observed | `not_applicable` (`missing:execution.retrieved_context`); the Golden's reference context is never substituted |
| Retrieval observed but empty | `not_applicable` (`empty:execution.retrieved_context`) — `empty_context_policy: not_applicable` |
| Answer empty or not text | `not_applicable` (`unscorable_output:...`) instead of a vacuous 1.0 |
| Judge extracts no claims | `not_applicable` (`no_claims`) instead of upstream's vacuous 1.0 |
| Only blank retrieved chunks | treated as empty context |
| Judge error | `error`, no score |
| Judge hangs past the scoring timeout | worker killed, `error: timeout`, a fresh worker is started for the next case outside its time budget |
| Judge with no cost reporting | cost recorded as unknown, never 0 |

Safety: telemetry off, `.env` loading off, legacy `~/.deepeval` key file off, DeepEval's own
retries off (aibench owns retries), private temporary working directory and HOME per worker,
nothing published to Confident AI. The upstream `success` flag is kept in the raw artifact;
pass/fail uses the binding's rule.
