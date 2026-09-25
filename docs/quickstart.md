# Quickstart

Benchmark a small support assistant in about ten minutes: create a project, check it, plan
and run the benchmark in conversation, then read the evidence report. Everything runs
locally. Nothing here calls a paid service unless you add an assistant model yourself
(section 4).

## 1. Install

aibench is not published to a package index. Install the release wheel you were given,
using Python 3.11 or 3.12. Check it against its `SHA256SUMS` first:

```bash
python -m venv .venv
.venv/Scripts/pip install aibench-0.1.0rc1-py3-none-any.whl    # Windows
.venv/bin/pip install aibench-0.1.0rc1-py3-none-any.whl        # Linux/macOS
aibench --version                                               # aibench 0.1.0rc1
```

From a clone of the repository, `pip install .` works too.
`python scripts/release_check.py --out DIR` builds the release files and checks them. See
[upgrade and recovery](release/upgrade-and-recovery.md) for upgrading and for interrupted
runs.

The core install has no evaluator framework in it. Optional evaluators are installed
separately (see [Optional evaluators](support.md#optional-evaluators)).

## 2. Create the quickstart project

```bash
aibench init support-bench
cd support-bench
```

`init` writes six files and nothing else. It installs nothing, runs nothing, and refuses to
overwrite a file that already exists.

| File | What it is |
|---|---|
| `aibench.json` | Project config: which app, dataset, policy and plan the commands use |
| `support_app.py` | The fixture app: a support assistant with keyword retrieval (standard library only) |
| `support.app.json` | How aibench calls it: a CLI app, JSON in and out, with the answer and retrieved passages bound |
| `dataset.jsonl` | 10 support questions with reviewed reference answers |
| `plan.json` | An executable plan: exact-match correctness, a non-empty-answer check, and two release gates |
| `policy.json` | A local policy: this trusted local app may run; only native evaluators; no model judges |

The fixture has two deliberate faults, so the first report has something real to show:

- **`support-004`:** the warranty question retrieves the shipping passage, so the answer is wrong. The report shows what was retrieved.
- **`support-010`:** the order-status question makes the app itself fail. That is an application failure, not a low score.

## 3. Check the environment

```bash
aibench doctor
```

`doctor` checks the following without running the app, and never prints a secret's value:

- Python and platform support;
- the workspace;
- the config and every file it names;
- the CLI interpreter;
- the dataset (10 valid cases);
- the plan (2 metrics, 2 release gates);
- that every secret reference is set.

Exit code 0 means ready to run. 2 means a file is invalid. 3 means a prerequisite is missing.

## 4. Plan and run in conversation

```bash
aibench chat --new --objective "answers are correct"
```

Inside the chat:

| Type | What happens |
|---|---|
| `/plan` | Shows the draft: the chosen metric and why, 10 cases, estimated calls, cost unknown |
| `/run` | Starts the draft you were shown, within `policy.json`. Progress appears while you keep typing |
| `/status` | Committed run state. Needs no model |
| `/failures` | The failed case and the application failure |
| `/case support-004` | The question, the reference answer, the app's answer and what it retrieved |
| `/report` | Report summary in the terminal, plus `report.html` and `report.json` under `.aibench/reports/RUN_ID/` |
| `/exit` | Leave. Reopening with `aibench` restores the session and never restarts a run |

Without an assistant model, messages are not interpreted, but every slash command works.
To converse in natural language, give the chat an OpenAI-compatible model. For example,
[`examples/planner/openai.provider.json`](../examples/planner/openai.provider.json):

```json
{"kind": "openai_compatible", "base_url": "https://api.openai.com/v1",
 "model": "gpt-4.1-mini", "api_key": "env:OPENAI_API_KEY"}
```

Allow it in `policy.json`. The conversation leaves your machine only to origins the policy
lists:

```json
"allowed_planner_origins": ["https://api.openai.com"],
"allowed_secret_refs": ["env:OPENAI_API_KEY"]
```

Set the key in your environment, never in a file. Then run:

```bash
export OPENAI_API_KEY=...        # PowerShell: $env:OPENAI_API_KEY = "..."
aibench --provider-config openai.provider.json
```

Now you can say "check that answers are correct", "use 5 cases first", "run it", "show the
failures" or "export the report". Each reply ends with a status line saying what really
happened: explained, changed the draft, or acted. Numbers in a reply are checked against
the results queried in that turn, and any number that can't be traced is flagged. Calls
to that endpoint are billed by its provider.

## 5. Or run it headlessly

```bash
aibench run                      # the plan from aibench.json
aibench report RUN_ID            # .aibench/reports/RUN_ID/report.html
aibench report RUN_ID --format markdown --out -
```

The run exits with code 3. Both release gate results are recorded in the JSON output:

- **`correct-answers`:** fails, with 8 of 10 selected cases passing, below 0.9.
- **`answers-present`:** passes, with 9 of 10 completed.

The application failure makes the run incomplete, and incompleteness takes precedence over
a gate failure. Exit codes are the same headless and in `chat --send`:

| Code | Meaning |
|---|---|
| 0 | Complete, and every release gate passed |
| 1 | Complete, but a release gate failed |
| 2 | Invalid input or plan |
| 3 | Incomplete: failed, blocked, cancelled or unknown-effect work |
| 4 | Authorization required: the policy denies it, or the run wasn't authorized |
| 130 | Interrupted. Continue with `aibench resume RUN_ID` |

The one-command form drafts, validates and, with `--auto`, runs within an existing policy:

```bash
aibench benchmark support.app.json --dataset dataset.jsonl --policy policy.json \
  --objective "answers are correct" --out bench.plan.json --non-interactive
aibench benchmark support.app.json --dataset dataset.jsonl --policy policy.json \
  --objective "answers are correct" --out bench.plan.json --auto --revise
```

What `benchmark` does when something is missing:

- **No `--objective`:** it stops with exit code 2 and names the missing information. It never invents what to check.
- **No `--auto`:** it stops with exit code 4 (`authorization_required`) and prints the command to run.
- **No `--policy`:** the built-in conservative policy doesn't allow a local CLI app, so it stops with exit code 4, blocked on that permission.

`--auto` grants nothing beyond the policy.

## 6. Read the report

The report is rebuilt from stored records every time. The app, evaluators and judges never
run again. It shows:

- **Provenance:** plan, dataset, app and policy hashes, evaluator versions, seed and approver.
- **Release gates:** each threshold beside the observed passes over selected cases.
- **Metric profiles:** value kind, direction, aggregation and rule. Also every denominator: selected, completed, pass/fail/indeterminate, evaluator errors, not applicable, unavailable (no app output), cancelled and pending.
- **Application:** failures by kind, and successful-request latency p50/p95 with its definition. Failed and timed-out requests are counted separately.
- **Cost:** observed spend and how complete the accounting is. Unknown cost is never shown as $0.
- **Case evidence:** each non-passing case with sanitized, truncated excerpts. Raw evaluator outputs stay in the artifact store, referenced by ID and digest.

`--no-content` withholds case excerpts and per-case metric values while keeping aggregate
summaries. A report of an unfinished run is labelled a partial snapshot.

## 7. Connect a JSON HTTP API without its repository

When you have an API endpoint and a local JSONL benchmark dataset but not the application's
repository, configure the narrow JSON POST runner explicitly:

```bash
aibench connect http --project support-api-bench \
  --url https://api.example.com/v1/answer \
  --authorize-origin https://api.example.com \
  --dataset ./support-cases.jsonl \
  --app-id support-api --effects none \
  --bearer-secret-ref env:SUPPORT_API_TOKEN
cd support-api-bench
aibench doctor
aibench chat --new --objective "answers are correct"
```

Use `--effects none` only if the endpoint is read-only for these requests; choose the
declared effect level that matches the API. The setup validates the dataset and writes
`application.http.json`, `policy.json`, and `aibench.json`; it does not probe or call the
endpoint and refuses to overwrite those files. Remote HTTPS requires authorizing that exact
origin. Plain HTTP is allowed only for loopback fixtures. A bearer token is read from the
named environment variable when a benchmark runs; the token value is never written to project
files.

The bindings default to sending each case's `input` at `/question` and reading `/answer` from
the response. Use `--input-path`, `--input-field`, and `--output-path` when the API uses
different JSON pointers. This command does not discover API schemas, import conversation
history, or automate browser actions. Without a configured assistant model, the slash
commands still work; natural-language conversation uses the provider setup in section 4.

## Next steps

- **Your own app:** copy `support.app.json` and change the transport (`cli` or `http`) and the input and output bindings. See [runner-protocol.md](runner-protocol.md).
- **Your own data:** replace `dataset.jsonl`. `aibench dataset validate FILE` checks it.
- **Rescoring:** `aibench evaluate RUN_ID --plan other.plan.json` scores the stored outputs again with other metrics, without calling the app. The report lists that scoring pass separately.
- **Limits:** see [support.md](support.md) for what is and isn't supported.
