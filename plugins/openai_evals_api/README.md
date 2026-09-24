# aibench-openai-evals-api

A bridge from aibench to the hosted OpenAI Evals API, through the official `openai` SDK
(pinned to `3.19.2`). It is a separate plugin from `aibench-openai-evals-oss` (the
open-source framework): separate package, dependencies, identity and capabilities.

## What it does

It grades a run's **recorded** outputs as a remote job. Each case's input, recorded output
and reference answer are uploaded as a `jsonl` item. Testing criteria read only
`{{item.input}}`, `{{item.output}}` and `{{item.reference}}`.

- Graders: `string_check`, `text_similarity`, and `label_model` (a model judge, which the
  policy must allow with `allow_model_evaluators`).
- Refused: `{{sample.*}}` templates and `completions`/`responses` data sources, which
  would have the service generate a replacement answer.

```
aibench openai-evals-api submit RUN --criteria criteria.json --plugin-env PY --policy POLICY
aibench openai-evals-api status JOB --policy POLICY
aibench openai-evals-api fetch  JOB --policy POLICY      # results enter the run once
aibench openai-evals-api cancel JOB --policy POLICY      # best effort
aibench openai-evals-api resume JOB --policy POLICY [--resend]
```

The policy must approve all of these; nothing is sent otherwise:
- the plugin environment (`allowed_plugin_environments`);
- the API origin (`allowed_egress_origins`, e.g. `https://api.openai.com`);
- the key's secret reference (`allowed_secret_refs`, default `env:OPENAI_API_KEY`);
- `openai_evals_api.*` evaluators.

The job and its request are stored before sending. An ambiguous failure (timeout, dropped
connection, 5xx) is reconciled by listing remote objects that carry the job's ID. It is
resent only with `--resend`.

## Installing

```
python -m venv plugins/openai_evals_api/.venv
plugins/openai_evals_api/.venv/bin/pip install -e . -e plugins/openai_evals_api
```

## Verification status

Tested against a local stand-in of the API contract
(`examples/openai_evals/evals_api_stub.py`), with the real pinned SDK. It has not been
verified against the live service: that needs an authorized key and budget.
