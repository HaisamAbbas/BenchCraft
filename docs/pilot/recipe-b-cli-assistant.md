# Pilot recipe B: score a command-line assistant with your own domain check

**For:** a team whose assistant or agent can be run as a program that reads JSON on stdin
and writes JSON on stdout. This is often a wrapper script around the real thing. The team
also knows a domain rule that a generic metric can't check.

**You end with:**
- a benchmark of the assistant;
- your own oracle, applied to the stored answers without running the assistant again;
- one report with both scoring passes.

Plan about an hour for the first run. That's the setup effort the
[feedback form](feedback-form.md) asks you to time. Nothing here calls a paid service.

Working files: [`examples/pilot/cli-assistant/`](../../examples/pilot/cli-assistant/). The
commands below were run as written against the example assistant (local trial,
`tests/test_pilot_recipes.py::test_recipe_b_scores_a_cli_assistant_with_the_teams_own_oracle`).

The commands run from `examples/pilot/cli-assistant/`. If you copy the directory
elsewhere, adjust the two relative paths: `transport.argv` in `app.json`, and
`--custom-evaluator` in step 4.

## 1. Wrap your assistant

aibench starts your program once per case, writes the case to its stdin, and reads one
JSON object from its stdout:

```text
stdin:  {"case_id": "refund-policy", "input": "What is your refund policy?"}
stdout: {"output": "Refunds are available within 30 days of purchase."}
```

Exit with a non-zero code on failure. That is recorded as an application failure, never as
a low score. [`examples/apps/cli_chatbot.py`](../../examples/apps/cli_chatbot.py) is a
complete 40-line example. The protocol is in [runner-protocol.md](../runner-protocol.md).

In `app.json`:
- point `transport.argv` at your program. It runs in the directory of `app.json`, so
  relative paths are resolved from there;
- set `output_binding.output` to the JSON pointer of the answer in your reply.

If your program has side effects (it books, sends or writes something), set `effects` to
`reversible` or `irreversible`. Interrupted calls are then never repeated automatically.

## 2. Bring your cases and your rule

In `dataset.jsonl`, besides `input` and a reference `expected_output`, a case can carry
**expectations**: the facts your rule needs. The example's rule is "the answer must state
the refund window in days", so refund questions carry `"expectations": {"refund_days": 30}`.
Cases without it are reported as *not applicable* for that check, not as passes.

Your rule is a Python evaluator. [`examples/evaluators/refund_window.py`](../../examples/evaluators/refund_window.py)
is a complete one. It classifies each answer as `correct`, `wrong_window`,
`no_window_stated` or `unusable_output`, instead of inventing a 0–10 score. Copy it and
change the classification. `oracle.metrics.json` names the evaluator to apply.

## 3. Check and run

```bash
aibench app smoke app.json --dataset dataset.jsonl --limit 1 --trust-local-app
aibench run --plan plan.json --policy policy.json
```

`--trust-local-app` (or `allow_trusted_local` in `policy.json`) is needed because a local
program runs with your permissions. A subprocess is not a sandbox. The plan scores every
answer with a case-insensitive exact match. With the example assistant, all 6 cases
complete.

## 4. Apply your rule to the stored answers

```bash
aibench score RUN_ID --metrics oracle.metrics.json \
  --custom-evaluator ../../evaluators/refund_window.py --trust-local-code
```

Loading an evaluator file runs its code, so `--trust-local-code` is required. The
assistant is not called again. With the example:

```text
acme.refund_window@1.0.0 (category)
  selected=6 eligible=4 completed=4 errors=0 not_applicable=2 unavailable=0
  values: {"counts": {"correct": 3, "no_window_stated": 1}, ...}
```

"How long do I have to send something back?" got the fallback answer, which states no
window. The two questions without a refund expectation are not applicable.

## 5. Read the evidence

```bash
aibench report RUN_ID --format markdown --out -
```

The report lists the engine's exact-match pass and your rescore pass as separate scoring
passes, each with its own denominators.

To score the same stored answers with a changed rule, edit the evaluator and run
`aibench score` again. Give the changed rule a new `version` in its manifest, so the two
passes can't be confused.

When you're done, fill in the [feedback form](feedback-form.md).
