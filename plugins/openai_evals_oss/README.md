# aibench-openai-evals-oss

A bridge from aibench to the open-source [openai/evals](https://github.com/openai/evals)
framework, pinned to `evals==3.0.1.post1`. It is a separate plugin from
`aibench-openai-evals-api` (the hosted Evals API): separate package, dependencies,
identity and capabilities.

## What it supports

An explicit allowlist of upstream eval classes, run unchanged:

| Eval type | Upstream class | Verdict |
|---|---|---|
| `match` | `evals.elsuite.basic.match:Match` | the output starts with a reference answer |
| `includes` | `evals.elsuite.basic.includes:Includes` | the output contains a reference answer |
| `fuzzy_match` | `evals.elsuite.basic.fuzzy_match:FuzzyMatch` | normalized containment either way |
| `json_match` | `evals.elsuite.basic.json_match:JsonMatch` | the output parses as equal JSON |

Model-graded, solver, multi-turn and tool evals are refused by name.

## Two modes

- **Recorded replay** (metrics `openai_evals_oss.<type>`, which consume recorded outputs).
  Bind them in a plan with this plugin's environment. The upstream eval asks its
  completion function for an answer, and the recorded output is returned only when the
  request is exactly the input the application was given, and only once. A different
  request (for example a few-shot expansion) fails as `unsupported_dynamic_request`, and
  a second request fails as `unsupported_follow_up`.
- **Live bridge** (`aibench openai-evals-oss run APP --eval TYPE --samples FILE
  --plugin-env PY --policy POLICY`). The upstream eval runs in this environment, and its
  completion function asks the harness for each answer. The harness invokes the
  application once per sample through its runner, records the execution, and then
  replay-scores the recorded outputs. The run is labelled `delegated_suite`.

## Installing

The full `evals` dependency list is very large (TensorFlow, Playwright, Snowflake and
more), and the allowlisted evals never import most of it. Install without it, then add
the tested set:

```
python -m venv plugins/openai_evals_oss/.venv
plugins/openai_evals_oss/.venv/bin/pip install -e . --no-deps -e plugins/openai_evals_oss
plugins/openai_evals_oss/.venv/bin/pip install -r plugins/openai_evals_oss/requirements-lock.txt
```

Two notes on how it runs:
- **Import-time client.** `evals.registry` constructs an OpenAI client when imported. The
  adapter sets a placeholder key and points `OPENAI_BASE_URL` at a closed loopback port
  first, so no call can leave the machine. No OpenAI completion function is ever used.
- **Network.** None: the plugin never calls a model or a remote service.
