# aibench-ragas

An isolated [Ragas](https://docs.ragas.io/) adapter for aibench. It scores **stored**
application executions and never invokes the application again. Ragas is imported only in
an aibench evaluation worker; it is not a dependency of the core environment.

## Pin and installation

The adapter is pinned to `ragas==0.4.3`. The field mapping and the modern collection API
were checked against that exact release:

```python
from ragas.metrics.collections import Faithfulness

metric = Faithfulness(llm=llm)
result = await metric.ascore(
    user_input=user_input,
    response=response,
    retrieved_contexts=retrieved_contexts,
)
score = result.value
```

The adapter refuses a different installed Ragas version rather than silently changing the
meaning of a score. Ragas 0.4.3 also uses `InstructorBaseRagasLLM` structured-output
response models; the deterministic test judges implement that real contract.

From the repository root, create a separate environment (the `.venv` directory is ignored):

```text
python -m venv plugins/ragas/.venv
plugins/ragas/.venv/Scripts/pip install -e . -e plugins/ragas  # Windows
plugins/ragas/.venv/bin/pip install -e . -e plugins/ragas      # Linux/macOS
```

Ragas has a substantial dependency tree. Keep it out of the core environment. The plugin
also keeps the Ragas 0.4.3-era LangChain/Instructor client APIs in their tested major
ranges, because an unconstrained newer community package can remove imports Ragas still
uses. The plugin worker receives a minimal environment, a private working directory and
HOME, and only credentials explicitly passed with `--plugin-secret`.

Ragas 0.4.3 eagerly imports its full metric catalogue. On a cold, loaded Windows host that
import can take longer than aibench's default 180-second worker-startup bound; the adapter
starts the import during worker preparation, but deployment should measure that startup and
pass a bounded `startup_timeout_seconds` override appropriate for the host. The real-package
contract tests use a bounded 600-second startup allowance for that measurement.

## Use

A binding uses a semantic metric reference and a judge configuration, for example:

```json
{
  "metrics": [
    {
      "metric": "ragas.faithfulness",
      "params": {
        "judge": {
          "kind": "llm_factory",
          "model": "gpt-4o-mini"
        }
      },
      "rule": {"comparator": ">=", "threshold": 0.8}
    }
  ]
}
```

Score it with the plugin interpreter and pass the provider credential explicitly:

```text
aibench score RUN_ID --metrics metrics.json \
  --plugin-env plugins/ragas/.venv/Scripts/python.exe \
  --plugin-secret OPENAI_API_KEY=env:OPENAI_API_KEY
```

The built-in `llm_factory` path is intentionally narrow and safe: it uses Ragas'
`llm_factory` with an `AsyncOpenAI` client created inside the worker. Credentials are read
from the worker environment, never from metric parameters, source files, or the harness
process. `provider` may be omitted (the default is `openai`) or set to `openai`; other
provider clients should use a trusted `python_factory` until an explicit adapter path is
added.

For deterministic local tests, use a trusted factory instead:

```json
{
  "judge": {
    "kind": "python_factory",
    "factory": "aibench_test_ragas_judges:token_judge"
  }
}
```

A `python_factory` is executable code in the worker. The examples under
`tests/fixtures/ragas_judges/` are test-only judges; do not use them as a production
provider. A factory must return an actual `InstructorBaseRagasLLM` implementing both
`generate` and `agenerate`.

## Recorded-field and applicability contract

| Harness field | Ragas argument | Policy |
|---|---|---|
| `case.input` | `user_input` | Text is passed unchanged; non-text Golden input is JSON-encoded as text. |
| `execution.output` | `response` | Must be text. Empty/non-text output is `not_applicable`, never a vacuous score. |
| `execution.retrieved_context` | `retrieved_contexts` | Only observed runtime context is used. Blank chunks are dropped. |
| `case.reference.context` | *(never mapped)* | Golden reference context is never substituted for runtime retrieval. |

Missing or empty required retrieval is rejected by the manifest/scorer before a judge is
called. A context containing only blank chunks is also `not_applicable`. No statements
extracted by Ragas produce a non-finite `MetricResult.value`; the adapter records that as
`not_applicable: no_statements` and never maps it to `0.0` or `1.0`. The Ragas score,
reason, traces, and pinned version remain in the raw artifact; the harness's frozen rule
makes the canonical pass/fail decision.

A fresh Ragas metric and a fresh judge are created for each case. The adapter performs no
internal retries and uses one judge call path at a time (`internal_concurrency=1`). Unknown
provider usage and cost remain unknown rather than being reported as zero.

## Security and compatibility notes

`RAGAS_DO_NOT_TRACK=true` is set before Ragas is imported or used. No application runner,
reference context, URL/file loader, or multi-modal metric is exposed by this adapter.

Ragas 0.4.3 is affected by **GHSA-95ww-475f-pr4f / CVE-2026-6587** in multi-modal
faithfulness URL/file processing. The vulnerable path is not reachable through this
text-only adapter: it validates `list[str]` context and calls only the text
`Faithfulness` collection. The required `ragas.metrics.collections` import may load module
definitions as a side effect, but this adapter never selects or calls
`MultiModalFaithfulness` or its URL/file helpers. The package remains a third-party
executable dependency in a worker, so this is a compensating exposure control, not a claim
that the pinned package is free of vulnerabilities. Reassess the pin when a patched release
exists or if the adapter's input surface changes. The worker environment is isolated but is
not a general security sandbox; only install trusted packages and factories.

The manifest declares `ragas.faithfulness@1`, `aibench-ragas`, semantic evaluator version
`1.0.0`, `consumes="recorded_outputs"`, `uses_models=True`, credential/network
requirements, no internal retries, concurrency one, and `requires_worker=True`.
