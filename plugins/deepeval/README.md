# aibench-deepeval

DeepEval metrics for aibench. The adapter runs **only in its own Python environment**, driven
by aibench's evaluation worker; DeepEval is never imported into the aibench process.

Pinned: `deepeval==4.2.5` (the adapter refuses to run on any other version).

## Metrics

Every DeepEval 4.2.5 single-turn, conversation and agent-trace metric that aibench's
recorded data can feed (37). Each is an
evaluator `deepeval.<name>@1`, scores 0 to 1 with higher better (DeepEval normalizes bias,
toxicity and the other safety metrics that way), and is decided by the plan's rule
(default `>= 0.5`), not by DeepEval's own `success` flag.

| Metric | Reads | Needs from the plan |
|---|---|---|
| `faithfulness` | answer, retrieved passages | judge |
| `answer_relevancy` | question, answer | judge |
| `contextual_precision` | question, reference answer, retrieved passages | judge |
| `contextual_recall` | question, reference answer, retrieved passages | judge |
| `contextual_relevancy` | question, retrieved passages | judge |
| `hallucination` | answer, the case's reviewed reference context | judge |
| `bias`, `toxicity`, `pii_leakage` | question, answer | judge |
| `misuse` | question, answer | judge, `domain` |
| `non_advice` | question, answer | judge, `advice_types` |
| `role_violation` | question, answer | judge, `role` |
| `prompt_alignment` | question, answer | judge, `prompt_instructions` |
| `summarization` | input text, summary | judge; optional `assessment_questions`, `n` |
| `task_completion` | question, answer, reported tool calls | judge; optional `task` |
| `argument_correctness` | question, reported tool calls | judge |
| `tool_correctness` | reported tool calls, the case's reference tools | judge (no calls when comparing names) |
| `tool_permission` | reported tool calls | `allowed_tools` or `denied_tools` |
| `exact_match` | answer, reference answer | nothing |
| `pattern_match` | answer | `pattern` |
| `g_eval` | what `evaluation_params` names (default: question, answer) | judge, `name`, `criteria` or `evaluation_steps`; optional `rubric` |

Field mapping: `case.input` -> `input`, `execution.output` -> `actual_output`,
`case.reference.answer` -> `expected_output`, `execution.retrieved_context` ->
`retrieval_context`, `case.reference.context` -> `context`, `execution.tool_events` ->
`tools_called`, `case.reference.tools` -> `expected_tools`. What the application retrieved
and the Golden's reviewed context are never substituted for each other.

Not applicable instead of a score: an empty or non-text answer, a context with no
non-blank passage, no reference tools, no readable tool call for argument correctness, and
any field G-Eval is asked to read that the case or execution does not have. Faithfulness
also treats an answer with no claims as not applicable (upstream scores a vacuous 1.0).

### Conversation metrics

For multi-turn applications: an episode is the cases sharing a `group_id`, run in order
against a stateful app. Each turn is scored on the conversation **up to and including it**,
built from what the run recorded (each turn's input as the user, its answer as the
assistant), so an episode's last turn carries the whole conversation's score. A case in no
episode is not applicable, and so is a turn whose conversation has an earlier turn that did
not complete.

| Metric | Reads per turn | Needs from the plan |
|---|---|---|
| `conversation_completeness` | question, answer | judge; optional `window_size` |
| `knowledge_retention` | question, answer | judge |
| `role_adherence` | question, answer | judge, `chatbot_role` |
| `goal_accuracy` | question, answer | judge |
| `topic_adherence` | question, answer | judge, `relevant_topics` |
| `tool_use` | question, answer, reported tool calls | judge, `available_tools` |
| `turn_relevancy` | question, answer | judge; optional `window_size` |
| `turn_faithfulness` | answer, retrieved passages | judge |
| `turn_contextual_precision`, `turn_contextual_recall` | retrieved passages; this turn's reference answer as the expected outcome | judge |
| `turn_contextual_relevancy` | retrieved passages | judge |
| `conversational_g_eval` | what `evaluation_params` names (default role and content) | judge, `name`, `criteria` or `evaluation_steps` |

A score outside 0..1 (a judge answer DeepEval did not bound) is an evaluator error, never a
recorded score.

### Agent-trace metrics

For agents that export OpenTelemetry spans: run the app, attach its traces with `aibench
traces import RUN_ID FILE`, then score. The harness turns the execution's trace into a span
tree (agent, llm, tool and retriever spans with their inputs, outputs, model and errors, from
the `gen_ai.*` and OpenInference attributes the spans carry; each input and output cut to
4000 characters), and the adapter hands it to DeepEval as its trace.

| Metric | Reads | Needs from the plan |
|---|---|---|
| `step_efficiency` | question, answer, trace | judge |
| `plan_quality` | question, answer, trace | judge |
| `plan_adherence` | question, answer, trace | judge |
| `agent_loop_detection` | trace (no judge: repeated tool calls, stalled reasoning, call cycles) | optional `check_*` switches and thresholds |

An execution with no imported trace, a partial trace (a missing parent or root, unsampled or
dropped spans) or more than one trace is not applicable. The trace's span contents are sent
to the judge. A trace whose root records no input or output (OpenTelemetry often leaves
message content out) takes the case's input as the agent's task and the recorded answer.
`plan_quality` and `plan_adherence` are not applicable (`no_plan_in_trace`) when the judge
finds no plan in the trace: DeepEval would score that 1, which is no evidence of a good plan.

Not included: metrics that need data aibench does not record: images, audio, MCP servers.
DAG and arena metrics need objects a plan cannot declare.

## Judges

Every judged metric takes a `judge` parameter:

- `{"kind": "openai_compatible", "base_url": "...", "model": "...", "api_key_env": "NAME"}`:
  any Chat Completions endpoint, such as GLM on Z.ai. Built in: no code needed. The key is
  read from the worker's `NAME` variable, which the harness sets from the plugin
  environment's `secret_env`. Calls and tokens are counted; cost is unknown (never zero).
  Optional: `timeout_seconds`, `max_output_tokens`, `json_mode` (default true).
- `{"kind": "deepeval_model", "model": "<name>"}`: DeepEval's native model support. Pass the
  provider key explicitly (`--plugin-secret`); the worker inherits nothing else.
- `{"kind": "python_factory", "factory": "module:function"}`: a function returning a
  `deepeval.models.DeepEvalBaseLLM`. **This runs that code in the worker**; trusted code only.

Scores from different judges are not comparable; the judge and G-Eval criteria are part of
each score's recorded identity.

## Install

For a project (recommended): `aibench plugins install deepeval --judge-provider
zai.provider.json`, or type `/plugins install deepeval` in the chat, which uses the
assistant's model as judge. It shows what will change first: the environment under
`.aibench/plugins/deepeval/`, the `plugin_environments` entry in `aibench.json` (with the
judge as the default for `deepeval.*`), and the policy lines (`allowed_plugin_environments`,
`allowed_evaluators: deepeval.*`, `allow_model_evaluators`, the judge's key reference). The
old policy is kept as `policy.json.bak`. `--use-env PYTHON` adopts an existing environment.

By hand, from the repository root:

```
python -m venv plugins/deepeval/.venv
plugins/deepeval/.venv/Scripts/pip install -e . -e plugins/deepeval     # Windows
plugins/deepeval/.venv/bin/pip install -e . -e plugins/deepeval         # Linux/macOS
aibench score RUN_ID --metrics metrics.json \
  --plugin-env plugins/deepeval/.venv/Scripts/python.exe \
  --plugin-secret JUDGE_KEY=env:ZAI_API_KEY
```

## Why a separate environment

DeepEval brings ~70 packages, including pytest plugins that auto-load (`pytest-xdist`,
`pytest-rerunfailures`, `pytest-repeat`, `pytest-asyncio`) and telemetry clients. Keeping it
out of the core environment keeps aibench's own dependencies and test runs unaffected.

## Safety

Telemetry off, `.env` loading off, legacy `~/.deepeval` key file off, DeepEval's own retries
off (aibench owns retries), a private temporary working directory and HOME per worker,
nothing published to Confident AI. A new metric and judge per case, so no state is shared
between cases. A judge that hangs past the scoring timeout has its worker killed
(`error: timeout`) and a fresh worker starts for the next case.
