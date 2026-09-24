# External integrations

`aibench integrations list --policy POLICY` shows, for each integration below:
- what it supports;
- where it sends data;
- whether your policy and credentials let it run now, and if not, exactly why.

It starts no plugin code and contacts no service. In chat, use `/integrations`.

None of these has been verified against the live service; each was tested against a
local stand-in of the documented contract. The OSS bridge involves no service at all.

## openai/evals (open-source framework)

Plugin `aibench-openai-evals-oss`, pinned to `evals==3.0.1.post1`. It sends no data
anywhere. Supported eval types: `match`, `includes`, `fuzzy_match` and `json_match`, run
by the upstream code unchanged.

- **Recorded replay.** Bind `openai_evals_oss.<type>` metrics in a plan, with the plugin
  environment under `plugin_environments`. The eval's request must be exactly the input
  your application received, and only one request per sample is answered. Otherwise the
  result is an error (`unsupported_dynamic_request` or `unsupported_follow_up`).
- **Live bridge.**

  ```
  aibench openai-evals-oss run app.json --eval match --samples samples.jsonl \
      --plugin-env plugins/openai_evals_oss/.venv/bin/python --policy policy.json
  ```

  The eval asks, and aibench invokes your application once per sample and records it. The
  outputs are then replay-scored. Few-shot `match` parameters work here, because the case
  records the expanded prompt. Samples use the openai/evals format:
  `{"input": ..., "ideal": ...}`.

The policy needs the plugin environment (`allowed_plugin_environments`),
`openai_evals_oss.*` in `allowed_evaluators`, and your application's usual approvals.

## OpenAI Evals API (hosted)

Plugin `aibench-openai-evals-api`, pinned to `openai==3.19.2`. It grades recorded outputs
remotely; nothing is regenerated.

```
aibench openai-evals-api submit RUN --criteria criteria.json --plugin-env PY --policy POLICY
aibench openai-evals-api status JOB --policy POLICY
aibench openai-evals-api fetch  JOB --policy POLICY
```

- **What leaves the machine.** Each case's input, recorded output and reference answer,
  sent to the API origin (default `https://api.openai.com/v1`, or `--base-url`).
- **The policy must approve:**
  - that origin (`allowed_egress_origins`);
  - the key reference (`allowed_secret_refs`, default `env:OPENAI_API_KEY`);
  - the plugin environment;
  - `openai_evals_api.*` evaluators;
  - `allow_model_evaluators` if you use `label_model`.
- **Criteria.** A JSON list of `string_check`, `text_similarity` or `label_model` graders,
  reading `{{item.input}}`, `{{item.output}}` and `{{item.reference}}` only.
- **Failures.** If a submission's outcome is unknown (timeout, dropped connection or 5xx),
  `resume` finds it remotely by the job's ID. It is resent only with `--resend`.
- **Results.** Fetch imports each case's result once:
  - a case the service did not grade is recorded as skipped;
  - a case the service graded twice is recorded as an error.
- **Plans.** These metrics cannot be bound in plans; they run only as remote jobs.

## Langfuse

A connector for moving data in and out; it never computes a metric. It uses the Langfuse
public API v4 endpoints, with the project keys as secret references (default
`env:LANGFUSE_PUBLIC_KEY`, `env:LANGFUSE_SECRET_KEY`). The host must be in
`allowed_egress_origins`.

```
aibench langfuse import-dataset NAME --out cases.jsonl --host HOST --policy POLICY
aibench langfuse import-traces RUN --host HOST --policy POLICY
aibench langfuse export-scores RUN --host HOST --policy POLICY [--include-reasons]
aibench langfuse status
```

- **Datasets.** Active items become cases that keep the item's ID, dataset and version in
  `extensions["langfuse.dataset_item"]` and their provenance.
- **Traces.** Your application must use the `X-Request-ID` aibench sends as its Langfuse
  trace ID. Usage is counted from the lowest observations only, and partial traces stay
  partial.
- **Scores.** Only recorded `ok` results for imported cases with an imported trace are
  exported, so a missing result is never sent as a zero. Each score carries the harness
  run, result, case, metric and dataset item IDs.
  - **Re-exporting.** Each score is read back first: an identical one is skipped, and a
    different one is reported as a conflict, never overwritten.
  - **What is sent.** Metric values and IDs; evaluator reasons only with
    `--include-reasons`.
