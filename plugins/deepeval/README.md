# aibench-deepeval

DeepEval metrics for aibench. The adapter runs **only in its own Python environment**, driven
by aibench's evaluation worker; DeepEval is never imported into the aibench process.

Pinned: `deepeval==4.2.5` (the adapter refuses to run on any other version).

## Metrics

Every DeepEval 4.2.5 single-turn, conversation and agent-trace metric that aibench's
recorded data can feed (40, with the head-to-head judge below). Not here: image, audio
and voice metrics (including agent responsiveness, a voice-latency metric) and the MCP
metrics. JSON correctness
is covered by `native.json_schema`: upstream's score is the same pass/fail schema check, with
the judge only writing the explanation. Each is an
evaluator `deepeval.<name>@1`, scores 0 to 1 with higher better (DeepEval normalizes bias,
toxicity and the other safety metrics that way), and is decided by the plan's rule
(default `>= 0.5`), not by DeepEval's own `success` flag.

| Metric | Reads | Needs from the plan |
|---|---|---|
| `faithfulness` | answer, retrieved passages | judge; optional `penalize_ambiguous_claims` (see below) |
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
| `g_eval` | what `evaluation_params` names (default: question, answer) | judge, `name`, `criteria` or `evaluation_steps`; optional `rubric`, `repeats` (1 to 9, default 3: the median of that many judge scores; scores more than 0.3 apart are reported as unstable) |
| `dag` | what its nodes' `evaluation_params` name; beyond question and answer, only fields the plan's `evaluation_params` lists | judge, `dag`; optional `name`, `evaluation_params` |

**DAG: a decision tree as JSON.** The judge walks the tree; the verdict it lands on gives the
score (0 to 10, reported as 0 to 1) or hands over to a G-Eval for the grade. A starting node
names the fields it reads; a node under a task node reads that task's output, plus whatever
its own `evaluation_params` adds. A yes/no node needs one `true` and one `false` verdict:

```json
{"metric": "deepeval.dag", "params": {"name": "states one amount", "dag": {"nodes": {
  "judge": {"type": "BinaryJudgementNode", "criteria": "Does the answer state exactly one amount?",
            "evaluation_params": ["input", "actual_output"], "children": ["yes", "no"]},
  "yes": {"type": "VerdictNode", "verdict": true, "child": {"type": "geval", "name": "clarity",
          "criteria": "The amount is stated clearly.", "evaluation_params": ["input", "actual_output"]}},
  "no": {"type": "VerdictNode", "verdict": false, "score": 0}}}}}
```

Checked before any case runs: only the node types `TaskNode`, `BinaryJudgementNode`,
`NonBinaryJudgementNode` and `VerdictNode` and the keys each needs; a verdict's child is a node
or a `geval`, never a `metric` (upstream builds those with their own default model, not the
plan's judge); at most 40 nodes, 8 levels and 4000 characters per text. A G-Eval child is
graded by the plan's judge (upstream would use DeepEval's default model and need an OpenAI key).

Field mapping: `case.input` -> `input`, `execution.output` -> `actual_output`,
`case.reference.answer` -> `expected_output`, `execution.retrieved_context` ->
`retrieval_context`, `case.reference.context` -> `context`, `execution.tool_events` ->
`tools_called`, `case.reference.tools` -> `expected_tools`. What the application retrieved
and the Golden's reviewed context are never substituted for each other.

Not applicable instead of a score: an empty or non-text answer, a context with no
non-blank passage, no reference tools, no readable tool call for argument correctness, and
any field G-Eval is asked to read that the case or execution does not have. Faithfulness
also treats an answer with no claims as not applicable (upstream scores a vacuous 1.0).

**Faithfulness counts contradictions, not unsupported claims.** A claim the retrieved passages
say nothing about is graded borderline and counts as faithful, so an answer with invented
detail can score 1.0 (on a real LightRAG run: 0.99 by default, 0.72 strict). With
`penalize_ambiguous_claims: true` such claims count as unfaithful. To make that the project's
default, set it beside the judge in `aibench.json`; every planned faithfulness binding then
uses it, and a value given in a plan still wins:

```json
"default_params": {
  "deepeval.*": {"judge": {"kind": "openai_compatible", "...": "..."}},
  "deepeval.faithfulness": {"penalize_ambiguous_claims": true}
}
```

### Head-to-head: which run's answers are better

`deepeval.arena_g_eval` (DeepEval's ArenaGEval) judges two runs against each other rather than
one run, so it is not a plan metric. It runs from the comparison of two stored runs:

```
/compare BASELINE_RUN CURRENT_RUN --judge "the more accurate and complete answer"
```

For every case both runs answered, the project's judge says which answer is better by the
criteria you state: `current`, `baseline` or `tie`, beside the comparison's usual statistics.
Each case is judged twice, with the answers in both orders; the same winner twice decides it,
and a split verdict is a tie, so a judge that favours the first or second answer cannot decide
a case. (Upstream shuffles the answers into a random order on every call, which does not
guarantee both orders; the arena holds that shuffle and gives the two orders itself.) The
application is never called; the verdicts belong to the comparison, not to either run.

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
| `conversational_dag` | what its nodes name: `role`, `content`, and `retrieval_context` or `tools_called` when the plan's `evaluation_params` lists them | judge, `dag`; optional `name`, `evaluation_params` |

The conversation DAG is the DAG above over the conversation, with the same checks. A node may
look at a `turn_window` [first, last] of **messages** (0 is the first user message, 1 the first
answer, and so on; first < last). A turn whose conversation does not reach a window yet is not
applicable (`turn_window_beyond_conversation:<messages>`). Nodes read what each message carries,
so the expected outcome is not available to them (upstream reads node fields per message).

A score outside 0..1 (a judge answer DeepEval did not bound) is an evaluator error, never a
recorded score.

### Agent-trace metrics

For agents that export OpenTelemetry spans: run the app, attach its traces with `aibench
traces import RUN_ID FILE`, then score. In the chat: `/run`, then `/traces import FILE`
(the latest run by default) and `/rescore`. The harness turns the execution's trace into a span
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
  environment's `secret_env`. Calls and tokens are counted. Cost is the provider's own figure
  when it gives one (OpenRouter does), else tokens at `price_per_million_tokens`
  (`{"input": 0.15, "output": 0.5}`, US dollars) when the config states them, else unknown
  (never zero).
  Optional: `timeout_seconds` (a total deadline per call), `max_output_tokens` (default 8000: a
  reasoning model spends some of it thinking), `thinking` (`default`, `disabled` or `enabled`;
  disabled on Z.ai and OpenRouter, where a thinking model is slow and judging does not need it;
  a model that cannot stop thinking is asked again without it), `json_mode` (default true),
  `retry_wait_seconds` (default 2). A rate limit (429), a server error (5xx) or a dropped
  connection is retried up to five times with a doubling wait (the server's `Retry-After`
  when given, at most 30 s) inside 200 s per case; a wrong key or a bad request is not. Free
  endpoints answer 429 under load, and a case would otherwise fail on the first one.
- `{"kind": "deepeval_model", "model": "<name>"}`: DeepEval's native model support. Pass the
  provider key explicitly (`--plugin-secret`); the worker inherits nothing else.
- `{"kind": "python_factory", "factory": "module:function"}`: a function returning a
  `deepeval.models.DeepEvalBaseLLM`. **This runs that code in the worker**; trusted code only.

Scores from different judges are not comparable; the judge and G-Eval criteria are part of
each score's recorded identity.

**A judge that does not think suits the retrieval metrics.** On a real 16-passage LightRAG case,
contextual precision with `glm-4.6` (thinking off) scored 0.14, 0.20 and 0.14 in 26 s each;
with GLM 5.3 Flash, which always thinks, it ran past 32,768 tokens and failed, and a thinking
DeepSeek scored one case 0.00 and then 0.84. A metric can have its own judge in `aibench.json`;
the most specific pattern wins, whatever the order:

```json
"default_params": {
  "deepeval.*": {"judge": {"kind": "openai_compatible", "model": "glm-5.3-flash", "...": "..."}},
  "deepeval.contextual_precision": {"judge": {"kind": "openai_compatible",
    "base_url": "https://api.z.ai/api/paas/v4", "model": "glm-4.6",
    "api_key_env": "AIBENCH_JUDGE_KEY", "thinking": "disabled"}}
}
```

A low contextual precision is not always the judge: it scores where the useful passages come in
the retrieved list. In that case the passage that answers the question was 7th of 16, because
LightRAG lists its references in document order, not by relevance.

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
