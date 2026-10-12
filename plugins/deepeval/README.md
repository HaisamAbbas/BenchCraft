# aibench-deepeval

DeepEval metrics for aibench. The adapter runs **only in its own Python environment**, driven
by aibench's evaluation worker; DeepEval is never imported into the aibench process.

Pinned: `deepeval==4.2.5` (the adapter refuses to run on any other version).

## Metrics

Every DeepEval 4.2.5 single-turn, conversation and agent-trace metric that aibench's
recorded data can feed (40, with the head-to-head judge below). Separate
`deepeval.vision_g_eval`, `deepeval.audio_g_eval`, and `deepeval.video_frames_g_eval`
evaluators provide bounded image-backed, audio-backed, and timestamped sampled-frame task
scoring for text or generated media outputs. The pinned DeepEval voice metrics score stored
timestamped conversation turns; they do not simulate calls or connect to voice agents.
JSON correctness
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

### Image-first vision scoring

`deepeval.vision_g_eval` scores a recorded text or image output using the image supplied to
the application, its task prompt, and the reviewed reference answer. The case image is carried
inside `input`; the reference remains judge-only:

```json
{"case_id":"receipt-01","input":{"prompt":"What is the total?","image":{"mime_type":"image/png","data_base64":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"}},"reference":{"answer":"$18.42"}}
```

Case images must be base64-encoded PNG, JPEG, or WebP, no larger than 4 MiB after decoding.
The MIME type and file signature must agree. A text output remains a bounded string. An image
output is an object with exactly `mime_type` and `data_base64`, using the same image formats
and 4 MiB decoded limit. Configure the application's output selector to return that object.
Remote URLs, local file paths, audio, and video are not supported. This avoids fetching
untrusted URLs or reading arbitrary files in the evaluator. Both images are labeled and sent
as inline data URLs to the configured judge.

Bind the evaluator with a rubric and an OpenAI-compatible vision model:

```json
{"metric":"deepeval.vision_g_eval","params":{"criteria":"Score whether the answer gives the total shown on the receipt.","judge":{"kind":"openai_compatible","base_url":"https://api.example/v1","model":"vision-model","api_key_env":"AIBENCH_VISION_KEY"}}}
```

Use the existing plugin-environment `secret_env` mechanism for the key and allow the evaluator,
model judges, and plugin interpreter in the project's policy. A call sends the image, task
prompt, reference answer, and recorded output to that endpoint. The adapter does not follow
redirects, limits judge replies to 1 MiB, reports provider token counts when present, and
leaves cost unknown unless the provider reports it (no price is guessed). Only a returned
finite 0–1 score with a non-empty reason is accepted; the harness applies the frozen decision
rule. Scores remain judge-dependent.

### Bounded audio task scoring

`deepeval.audio_g_eval` scores a recorded text or audio output against one inline audio input,
the task prompt, and its reviewed reference answer. Case audio lives inside `input`; a generated
audio output uses the same object shape:

```json
{"case_id":"greeting-01","input":{"prompt":"What does the speaker say?","audio":{"mime_type":"audio/wav","data_base64":"UklGR..."}},"reference":{"answer":"Hello there."}}
```

Only base64 PCM/IEEE-float WAV (`audio/wav`) and MPEG Layer III MP3 (`audio/mpeg`) are
accepted, up to 4 MiB each after decoding. The parser checks RIFF chunk bounds, WAV rate and
alignment fields, data-frame alignment, and a complete MP3 frame. Configure the application
output selector to return exactly `{"mime_type":"audio/wav","data_base64":"..."}` or
`audio/mpeg`; a bounded text answer is also accepted. URLs, paths, extra object fields, other
media formats, and malformed or oversized payloads are not applicable and stop before the
judge call. Dataset JSONL lines are capped at 1,000,000 bytes, including base64 and JSON
overhead, so case audio loaded through JSONL must stay below roughly 750 KB decoded. HTTP
application request and response limits default to 1 MiB; raise `transport.max_request_bytes`
or `transport.max_response_bytes` when needed. For example, 4 MiB generated audio needs about
5.34 MiB of base64 before its JSON wrapper, so set `max_response_bytes` to at least 8,388,608.
The evaluator sends labeled `input_audio` parts to the configured
[Chat Completions endpoint](https://platform.openai.com/docs/api-reference/chat/create); the
selected provider/model must support audio input. Audio, prompt, reference answer, and output
are sent to that endpoint. This single-request task evaluator does not score timestamps,
diarization, interruptions, or multi-turn voice interaction.

Bind it with a rubric and an audio-capable OpenAI-compatible judge:

```json
{"metric":"deepeval.audio_g_eval","params":{"criteria":"Score whether the output faithfully conveys the greeting, including the words and language.","judge":{"kind":"openai_compatible","base_url":"https://api.example/v1","model":"audio-model","api_key_env":"AIBENCH_AUDIO_KEY"}}}
```

Pass the key using the plugin-environment `secret_env` mechanism and allow the evaluator,
judge model, and plugin interpreter in policy. The evaluator does not follow redirects,
limits judge replies to 1 MiB, records provider token usage when present, and leaves cost
unknown unless the provider reports it.

### Stored voice-conversation metrics

The pinned DeepEval voice metrics run against each case's episode prefix (`group_id`) using
recorded transcripts and inline audio. They make no judge or provider call. The supported
metrics are `deepeval.turn_taking_naturalness`, `deepeval.agent_responsiveness`,
`deepeval.audio_integrity`, `deepeval.voice_naturalness`,
`deepeval.speech_intelligibility`, `deepeval.voice_consistency`, and
`deepeval.voice_reliability`.

Each user case input has a transcript and a bounded WAV clip. Supply `audio_start_ms` when the
timestamp is call-relative and known:

```json
{"case_id":"call-01-turn-01","group_id":"call-01","input":{"transcript":"Please check my delivery.","audio":{"mime_type":"audio/wav","data_base64":"UklGR..."},"audio_start_ms":0}}
```

The application's recorded output is an assistant transcript and, when spoken audio exists,
the matching clip and its call-relative start time:

```json
{"transcript":"It is arriving tomorrow.","audio":{"mime_type":"audio/wav","data_base64":"UklGR..."},"audio_start_ms":860}
```

`audio_start_ms` is optional for audio-quality metrics; turn-taking scoring requires a known
start for every clip and returns not-applicable when timestamps are missing. The metric derives
duration from the WAV frame count, so it does not infer timing from transcript length or a
declared duration. Input accepts only inline 16-bit PCM WAV up to 512 KiB per user clip; the
512 KiB bound leaves room under the built-in 1,000,000-byte dataset row cap. Assistant clips
are limited to 1 MiB each and all audio across a scored episode to 4 MiB. Application responses
larger than the HTTP default need `transport.max_response_bytes` raised; 2,097,152 bytes carries
one maximum-size assistant clip plus JSON/base64 overhead. The host limits an episode to 50
turns and 7 MiB of serialized evidence before building a worker request.

`deepeval.agent_responsiveness` is a narrow proxy over the recorded format: it detects missing
assistant audio. Since each case records exactly one user turn and one assistant turn, it cannot
represent or detect user reprompts, call-ending reasons, interruptions, or live response latency.
The adapter rejects `latency_ms` and `interrupted` fields so they cannot be mistaken for scored
signals. Turn-taking uses only the supplied call-relative audio timestamps and clip durations.

These metrics analyze the stored conversation only. They do not start voice calls, capture
audio, generate/transcribe speech, evaluate MP3 or other WAV encodings, or establish how
listeners perceive naturalness or intelligibility. See
DeepEval's [voice evaluation contract](https://deepeval.com/docs/evaluation-voice) and
[turn-taking metric](https://deepeval.com/docs/metrics-turn-taking-naturalness).

### Timestamped sampled-video task scoring

`deepeval.video_frames_g_eval` evaluates a sequence of supplied video-frame samples against
the task and reviewed reference. It does not decode video files or claim to assess motion
between samples. Case input uses an ordered `video.frames` list with integer millisecond
timestamps and inline images:

```json
{"case_id":"door-01","input":{"prompt":"Does the person enter and then leave?","video":{"frames":[{"timestamp_ms":0,"image":{"mime_type":"image/png","data_base64":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"}},{"timestamp_ms":1500,"image":{"mime_type":"image/png","data_base64":"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC"}}]}},"reference":{"answer":"The person enters, then leaves."}}
```

Sequences require 2 to 8 strictly increasing timestamps within 24 hours. Case frames are
capped at 512 KiB total decoded so their base64 representation fits under the built-in dataset
reader's row limit alongside the prompt, reference, and metadata. Generated output frames allow
1 MiB per frame and 4 MiB total decoded. A generated-frame output uses
`{"frames":[{"timestamp_ms":0,"image":{"mime_type":"image/png","data_base64":"..."}},...]}`;
a bounded text output is also accepted. The evaluator sends each image as an individually
labeled inline image part in chronological order. It refuses URLs, local paths, raw video
containers, audio tracks, extra frame fields, repeated/out-of-order timestamps, malformed
images, and over-limit sequences before contacting the judge.

Built-in JSONL, JSON, CSV, and Parquet dataset readers cap each row at 1,000,000 bytes,
including frame base64 and other case fields. The 512 KiB input budget leaves room for those
other fields; the limit depends on the prompt, reference, and row metadata.
HTTP application request and response limits default to 1 MiB; raise the applicable `transport.max_request_bytes` or
`transport.max_response_bytes` for larger generated sequences. The 4 MiB generated sequence
limit needs about 5.34 MiB in base64 before its JSON wrapper; configure
`max_response_bytes` to at least 8,388,608. Frame images, prompt, reference answer, and output
are sent to the configured [Chat Completions endpoint](https://platform.openai.com/docs/api-reference/chat/create).
The selected provider/model must support multiple image parts. The evaluator bounds judge
replies to 1 MiB, reports token usage when present, and leaves cost unknown unless the provider
reports it. No live provider call is made by its tests.

Bind it with a rubric and an OpenAI-compatible vision judge:

```json
{"metric":"deepeval.video_frames_g_eval","params":{"criteria":"Score whether the answer correctly describes the sampled event order.","judge":{"kind":"openai_compatible","base_url":"https://api.example/v1","model":"vision-model","api_key_env":"AIBENCH_VIDEO_KEY"}}}
```

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

Not included: raw-video decoding or continuous motion scoring, DeepEval's upstream multimodal
image/audio and MCP metric families.
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
plugins/deepeval/.venv/Scripts/python.exe -m pip install --upgrade "pip>=26.2.1,<27"  # Windows
plugins/deepeval/.venv/Scripts/python.exe -m pip install -e . -e plugins/deepeval     # Windows
plugins/deepeval/.venv/bin/python -m pip install --upgrade 'pip>=26.2.1,<27'           # Linux/macOS
plugins/deepeval/.venv/bin/python -m pip install -e . -e plugins/deepeval              # Linux/macOS
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
