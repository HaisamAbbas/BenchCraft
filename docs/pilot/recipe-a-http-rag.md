# Pilot recipe A: benchmark a RAG service you already run over HTTP

**For:** a team with a retrieval-augmented assistant behind an HTTP endpoint, and a set of
questions with known good answers.

**You end with:**
- a repeatable benchmark of that endpoint;
- a release gate;
- an evidence report that shows, for every wrong answer, what the service retrieved.

Plan about an hour for the first run. That's the setup effort the
[feedback form](feedback-form.md) asks you to time. Nothing here calls a paid service.

Working files: [`examples/pilot/http-rag/`](../../examples/pilot/http-rag/). The commands
below were run exactly as written against the stand-in service in step 0 (local trial,
`tests/test_pilot_recipes.py::test_recipe_a_benchmarks_a_running_http_rag_service`).

## 0. Install, and (optionally) the stand-in service

Install aibench from the release artifacts (see the [quickstart](../quickstart.md#1-install)):

```bash
python -m venv .venv
.venv/Scripts/pip install aibench-0.1.0rc1-py3-none-any.whl   # Linux/macOS: .venv/bin/pip
```

To try the recipe before touching your own service, start the example RAG service from the
repository. It listens on `http://127.0.0.1:8765`:

```bash
python examples/apps/http_rag_app.py --port 8765
```

## 1. Copy the recipe and describe your endpoint

```bash
cp -r examples/pilot/http-rag rag-bench && cd rag-bench
```

`app.json` tells aibench how to call your service and what to read from its reply. Edit
four things:

| Field | Set it to |
|---|---|
| `transport.url` and `target` | Your answer endpoint |
| `transport.healthcheck_url` | A cheap GET that returns 200 when the service is up, or remove it |
| `input_binding` | How a question goes into your request body. The example sends `{"question": <input>, "top_k": 2}` |
| `output_binding` | JSON pointers into your reply: `output` for the answer, `retrieved_context` for the passages you actually used, `retrieved_context_item` for the text inside each passage |

Report the passages your service **really** used. aibench never substitutes the reference
context from your dataset. If your service can't expose them, remove `retrieved_context`:
checks that need retrieval evidence will then show up as gaps, not as scores.

If your service isn't on this machine:
- add its origin to `allowed_http_origins` in `policy.json`, for example `"https://rag.internal"`;
- if it needs a token, add `"secret_headers": {"Authorization": {"ref": "env:RAG_TOKEN", "prefix": "Bearer "}}` under `transport`, and `"env:RAG_TOKEN"` to the policy's `allowed_secret_refs`.

The token is read from your environment when the call is made. It is never written to a
file.

Check what aibench can and cannot observe, without calling the service:

```bash
aibench app describe app.json
```

`retrieved_context: declared` means the output binding names it. `usage` and `cost` stay
`unknown` unless your reply reports them.

## 2. Bring your questions

`dataset.jsonl` has one JSON object per line:

```json
{"case_id": "refund-policy", "input": "What is the refund policy?", "expected_output": "Refunds may be requested within 30 days of purchase with a receipt."}
```

Replace the six example cases with yours. Case IDs must be unique. `expected_output` is a
reference: it is used only to judge the answer and is never sent to your service. Then:

```bash
aibench dataset validate dataset.jsonl
aibench app smoke app.json --dataset dataset.jsonl --limit 2
```

`smoke` calls your service for two cases and shows what came back. It isn't a benchmark:
nothing is scored.

## 3. Run the benchmark

```bash
aibench run --plan plan.json --policy policy.json
```

`plan.json` scores each answer with a case-insensitive exact match against your reference,
and has one release gate: at least 90% of the selected cases must pass. With the stand-in
service, 5 of 6 pass. That is below the gate, so the run exits with code 1:

```text
gate correct-answers: fail: pass rate 5/6 below 0.9
```

Exit codes: 0 means every gate passed; 1 means a gate failed; 3 means some work didn't
complete (for example, the service failed). The full table is in the
[quickstart](../quickstart.md#5-or-run-it-headlessly).

## 4. Read the evidence

```bash
aibench report RUN_ID                                  # .aibench/reports/RUN_ID/report.html
aibench report RUN_ID --format markdown --out -
```

For the stand-in, the failing case shows why it failed:

```text
### late-return (repetition 0)
- output: We ship to over 40 countries. International delivery takes 5 to 10 days.
- retrieved (1 of 1): We ship to over 40 countries. International delivery takes 5 to 10 days.
```

The question was about returns, but the service retrieved the shipping passage. It is a
retrieval failure, not a wording problem.

## 5. Go further (optional)

- **Stricter or looser checks.** Exact match rejects correct paraphrases. For meaning rather than wording, add the DeepEval faithfulness judge ([support.md](../support.md#optional-evaluators)). It is a paid model call, needs a key and must be allowed in the policy. It was not part of the local trial.
- **Rescore without calling the service again.** `aibench evaluate RUN_ID --plan other.plan.json`.
- **If a run is interrupted** (Ctrl+C, a crash, a full disk): run `aibench resume RUN_ID`. Only unfinished cases are called again. See [upgrade and recovery](../release/upgrade-and-recovery.md).

When you're done, fill in the [feedback form](feedback-form.md).
