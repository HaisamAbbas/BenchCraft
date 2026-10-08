# Changelog

Each release lists new capabilities separately from changes to metric semantics. A metric
whose meaning changes gets a new semantic version, and is listed under "Metric semantics".
Workspace schema changes are listed under "Workspace". Upgrade steps are in
[docs/release/upgrade-and-recovery.md](docs/release/upgrade-and-recovery.md).

## 0.1.0rc28 — the limits a model that thinks first runs into

Found running a 15-case LightRAG benchmark with GLM 5.3 Flash, a model that always thinks.

- `/cases generate` asks for its own 16,000-token allowance. It used the smaller of that and the
  assistant's setting, so a 12,000 (or the default 4,000) setting made a model that thinks first
  run out before it wrote the cases ("must return exactly one write_candidates tool call").
- An assistant turn may use up to 150,000 tokens (was 60,000): several steps of a model that
  thinks first, over a long plan, reached the old limit and stopped the turn.
- The status counts an evaluation the run never got to (it stopped at its time limit) as
  needing attention; it counted only errored ones, so it said 1 when 2 were unfinished.
- A judge that cannot stop thinking is no longer told to switch thinking off when its output is
  cut off; the message names the options that apply.
- **Faithfulness says what it measures.** Its description claimed the share of claims
  "supported by" the retrieved context. Upstream counts a claim the context says nothing about as
  faithful (`borderline`), so only contradictions lower the score: answers with invented facts
  scored 1.00. The description and limitations now say so, and name `penalize_ambiguous_claims`,
  the existing setting that counts such claims as unfaithful.
- `/cases` no longer says "not word for word in that quote: check it" on nearly every case.
  It shows how much of the answer's wording is in the quote: nothing for a quotation, a quiet
  note for a paraphrase, a warning only when under half is there.

## 0.1.0rc27 — "needs attention" no longer counts a failure the new judge settings replaced

Found re-scoring the LightRAG pilot after changing the judge's model and timeout: every
evaluation was scored (24 of 24, no errors), yet the status said "needs attention 1".

- Changing a judge's settings gives its evaluations a new identity, so the failure the run had
  left under the old settings matched nothing the rescore produced. The status now treats it
  as replaced when the newest scoring pass finished the same metric on the same case. A
  failure of the new settings still shows.

## 0.1.0rc26 — a judge that must think, and a garbled reply, no longer fail an evaluation

Found re-scoring the LightRAG pilot with GLM 5.3 Flash on OpenRouter.

- A model that **refuses to run with thinking switched off** (GLM 5.3 Flash answers 400
  "Reasoning is mandatory") is asked again without the field. The judge sends the field by
  default on Z.ai and OpenRouter, so every call to such a model failed before.
- A compressed reply that is **garbled in transit** (`DecodingError: incorrect header check`,
  seen after 143 s on a long call) is retried like a timeout instead of failing the evaluation.
- A judge call that ran past `timeout_seconds` on the async path reported a bare `TimeoutError`.
  It now says `no complete reply within N s`.
- Known: with thinking mandatory, contextual precision over 15 long passages (about 17,000
  tokens) takes a reasoning judge 106 to 160 s per call. The default `timeout_seconds` of 120
  (180 in the pilot) is marginal there; set the judge's `timeout_seconds` to 400 for such models.

## 0.1.0rc25 — a stalled judge call ends at its deadline

Found re-scoring the LightRAG pilot with DeepSeek on OpenRouter: one answer-relevancy case took
the full 600 s while the same metric took 15 to 24 s on the others.

- `timeout_seconds` on an `openai_compatible` judge is now a **total deadline per call**. The
  HTTP client's own timeout is per read, and OpenRouter keeps a waiting request alive with small
  bytes, so a stalled upstream never tripped it: one call hung for over ten minutes against a
  180 s limit. The same prompt stalled on 2 of 3 tries and took 7 s on the third, so a call that
  runs past its deadline is asked again, and the retry window is three deadlines (it was 200 s).
- A rescore that cannot finish an evaluation now shows in the status line. "needs attention"
  read only the run's own records, so a timed-out rescore result left it at 0; an evaluation
  the latest pass could not finish counts, and clears when a later pass finishes it.
- Plugin `aibench-deepeval` 0.2.0rc8.

## 0.1.0rc24 — judging that survives a slow model, on a second app

Found on a pilot run of LightRAG (a different RAG app) judged by DeepSeek V4 Flash on
OpenRouter: 4 of 24 judged evaluations failed.

- Cases that share a metric's worker take turns. They were evaluated at once through one worker
  process: when the first case hit its time limit the worker was killed, and the two cases
  waiting behind it failed with "worker is not running"; each case's limit also counted the
  time it waited in line. A case's limit now starts at its own turn, and a worker killed by one
  case's timeout is restarted for the next.
- Judged metrics get ten minutes per case by default (was five): faithfulness on a 15-passage
  answer took about two minutes even with thinking off.
- The judge turns thinking off by default on OpenRouter (`reasoning: {"enabled": false}`), as it
  does on Z.ai. On four real answers contextual precision took 123 s instead of 1,240 s, scored
  all four cases (thinking on: one errored), and its scores were within the metric's usual noise
  of the thinking-on ones. Set `"thinking": "enabled"` on the judge to turn it back on.
- A judge reply that is valid JSON of the wrong shape (DeepSeek answered with a document, not
  the verdicts) is asked again, like a reply that is not JSON.
- Plugin `aibench-deepeval` 0.2.0rc7.

## 0.1.0rc23 — a terminal opened before a key was saved, and `/cases` with a model that thinks

- On Windows, `benchcraft` adopts the API keys and tokens stored for your account (user
  environment variables whose names end in `_API_KEY` or `_TOKEN`) that the terminal does not
  have. A terminal opened before a key was stored never sees it, which showed up as "assistant
  model disabled: secret 'env:OPENROUTER_API_KEY' is not set" right after saving the key. A
  variable the terminal does have always wins; other stored variables are ignored;
  `BENCHCRAFT_NO_USER_ENV=1` turns it off.
- `/cases generate` works with a model that thinks before it answers (DeepSeek V4 Flash). The
  one-call generation had 6,000 output tokens; the model's thinking used them, so its reply was
  cut off mid-list ("2 validation errors ... source_id Field required") or held no tool call
  ("provider must return exactly one write_candidates tool call"). It now has 16,000, a
  ten-minute timeout (the saved chat config's 120 s cut off generations that took 107 to
  202 s), and, when a reply comes back after nearly all of its allowance, an error that says so
  and suggests `--max 6` or a smaller document. On three real runs, 15 cases each, none left
  out.

## 0.1.0rc22 — passages grouped per source

Found when testing a second RAG app (LightRAG), whose answer lists one reference per source
file, each holding a list of chunk texts.

- `retrieved_context` items (or the field `retrieved_context_item` selects from each) may be a
  **list of strings**: they are flattened in order. LightRAG's response is read with
  `"retrieved_context": "/references", "retrieved_context_item": "/content"`; the earlier
  `/references/0/content` read only the first file's chunks (5 of 14 on a real answer), so
  recall, precision and faithfulness were scored on partial evidence. A source with no
  passages adds none; anything that is not a string or a list of strings is still invalid.

## 0.1.0rc21 — long slash commands behave like a message

Reported from a real session: running `/rescore all` made the input box disappear and showed
nothing for twenty minutes, while a message to the assistant keeps the box and a live line.

- Slash commands that can take minutes (`/rescore`, `/report`, `/compare`, `/plugins install`,
  `/cases generate`, `/traces import`) run in the background like an assistant turn: the input
  box stays, a `Working: /rescore all (42s)` line counts seconds, the toolbar says `working:
  /rescore`, and the result appears when it ends. Quick commands (`/status`, `/pause`, `/stop`,
  `/plugins`, `/cases` ...) still answer at once while one runs. A second long command waits
  its turn ("queued: ...") as a message does while the assistant replies; a failure is
  reported; `/exit` ends a running one and drops the queue (what it stored is kept).
- `/rescore` also prints `rescoring: 70 of 90 evaluations` every ten seconds.
- Not changed: Esc interrupts the assistant's reply only, never a running command.

## 0.1.0rc20 — a dataset edited on disk is noticed

- Fixed: a session's draft kept the case counts and estimate of the dataset as it was when the
  draft was made. After a case was removed from the dataset file, reopening the session still
  said "up to 5 cases" (the file had 4), because the plan file names the dataset by path and
  so hashed the same. Reopening a session now compares the dataset's content as well and makes
  a new draft revision when it changed.

## 0.1.0rc19 — `/cases` shows what a quote sits under

Found when a generated case asked what medical exemptions *differently-abled persons* get and
cited the line just above their heading, which belongs to senior citizens. The quote matched,
so the case looked fine and was accepted; a stricter judge later scored the app 0.0 on
faithfulness because the expected answer was wrong.

- `/cases` shows, for each case, the nearest heading above the quote (a guess: a markdown
  heading or numbered section title), the first heading below it ("the quote is above it"),
  and the text on both sides of the quote. The review prompt asks whether the question is
  about what the quote is really about. A quote that matches its text is not enough.

## 0.1.0rc18 — a requested metric is never dropped quietly

Found when a run's report had no G-Eval line: the assistant had saved G-Eval's `criteria`
without a `name`, G-Eval then "needed a name only you can supply", and the metric was left
out of the plan while the plan still read "ready to run". The run had no correctness check.

- G-Eval's `name` is optional (a label; the criteria are what the judge reads).
- A plan change that configures a metric which would not make it into the plan is refused,
  with what is missing ("deepeval.misuse would not be in the plan with these settings ...
  needs domain"), and nothing is saved. The assistant or the user fixes it before the plan
  can read "ready to run" without a metric they asked for.
- G-Eval criteria that speak of the expected (or reference) answer now send it to the judge.
  With no `evaluation_params` the judge saw only the question and the answer, so criteria such
  as "states the same facts as the expected answer" made it reply that the expected answer was
  missing and score every case 0 (a real run's whole G-Eval line was 0). A manifest can now
  declare `parameter_patterns`: a regex over a parameter's text that adds a required field, so
  the harness and the plugin agree on what the judge is shown. A case with no expected answer
  is then not applicable, as when the field is named explicitly. Naming `evaluation_params`
  yourself still decides the fields.
- Plugin `aibench-deepeval` 0.2.0rc6.

## 0.1.0rc17 — G-Eval scores you can trust

From a real run where the same answer, criteria and judge scored 0.2 once and 1.0 when scored
again: a small judge's single G-Eval score is not reliable.

### Metric semantics

- `deepeval.g_eval` is now version 1.1.0: each case is scored `repeats` times (default 3,
  1 to 9; `repeats: 1` is the old single score) and the median is the score. On a real
  document question the five calls were 0.70, 0.70, 0.70, 0.30, 0.70, and the median ignores
  the 0.30 outlier a single call could have returned. Scores further than 0.3 apart are
  flagged: the result's reason starts with `unstable:` and lists the scores. The reason also
  carries the judge's own explanation, which G-Eval results did not show before.
- Reports count them: the terminal report, the markdown/HTML report and the report facts say
  how many of a metric's results are unstable. They are still recorded and still decide pass
  or fail by their median; the count says which numbers not to trust alone.
- G-Eval costs `repeats` times the judge calls (about 6 s each on `glm-4.5-air`). Runs scored
  with 1.0.0 are not comparable with 1.1.0 runs; `/rescore all` re-scores an old run.
- Plugin `aibench-deepeval` 0.2.0rc5.

## 0.1.0rc16 — the assistant stops looping on empty settings

- A plan change from the assistant that carries settings for a metric but sets nothing
  (`params: {"native.exact_match@1.0.0": {}}`) no longer fails. Empty settings blocks are
  ignored and `id@version` is read as `id`; real settings are still checked. The assistant
  used to be refused three ways in a row and ran out of tokens on "use the dataset FILE and
  check that the answers are correct".

## 0.1.0rc15 — status after a rescore, and /cases that survives a bad quote

- Fixed: `needs attention` (and the evaluation counts) in a run's status line stayed at the
  number of evaluations the run itself left failed, even after `/rescore` had scored them all.
  A failed evaluation that a later scoring pass finished no longer counts, for a finished run;
  failures nothing has settled still do.
- Fixed: `/cases generate` (and `benchcraft candidates generate`) failed completely when any
  one case's source quote was not in the document, discarding the good cases ("source_quote
  ... is not an exact substring"). A quote that differs only in spacing or line breaks is now
  matched to the document's own text, so the evidence stays verbatim; a case whose quote is not
  in the document is left out and the chat says how many; only a reply with no usable case is
  an error. On a real document and model, 0 or 1 of 6 cases are left out and generation no
  longer fails.

## 0.1.0rc14 — faithfulness with glm-4.5-air

- The `openai_compatible` judge repairs the two near-misses at JSON that models make: a
  comma before a closing bracket, and doubled braces (`{{ ... }}`) copied from DeepEval's
  prompt examples. `glm-4.5-air` did the second on every faithfulness verdict, so
  faithfulness failed on 11 of 15 cases while every other metric worked. Replies that
  are still not JSON are asked again (up to the judge's five attempts) and then fail
  saying so. Plugin `aibench-deepeval` 0.2.0rc4.

## 0.1.0rc13 — assistant stops over-asking for G-Eval settings

- The assistant's tool description no longer tells it to add G-Eval's `evaluation_params` to every
  check. They are not the user's words, so BenchCraft refused them and the assistant used its six
  calls retrying. A rejection for settings the user never stated now names them and says to drop
  them.

## 0.1.0rc12 — quick judges, test cases from documents, an assistant that recovers

Judge and install changes come from a real 15-case run with five DeepEval metrics that took
an hour and lost 13 of 90 results to the judge.

- The `openai_compatible` judge has a `thinking` setting (`default`, `disabled`,
  `enabled`). On Z.ai it is `disabled` unless set: judging is classification against a
  rubric and DeepEval makes dozens of small calls per case, but a thinking model took 76
  to 197 s for one call and sometimes answered nothing. Other endpoints keep the
  provider's default. With `glm-4.5-air` a call takes about 6 s.
- A judge reply cut off by its output allowance is asked again with twice the room (up to
  32768 tokens) and the metric keeps the room that worked. Only past that ceiling is it an
  error, and the error says what to change.
- Opening a connection to the judge is abandoned after 15 s and retried (a stalled
  handshake held one case for 260 s); a reply still has `timeout_seconds`.
- Plans with a model-judged metric score three cases at a time, not one (an hour for 15
  cases before). Native-only plans are unchanged.
- `/plugins install` (and `aibench plugins install`) keeps the judge and key reference
  the project already has for that plugin. Reinstalling to upgrade used to replace it
  with the assistant's model without saying so (a paid judge became the free one). The
  preview says the judge is kept. To change the judge, edit `plugin_environments` in the
  project config and start a new session.
- The installers (`install.ps1`, `install.sh`) install the most recently published release.
  They took the first release GitHub listed, and that list is not in publish order (`rc10`
  came below `rc5`), so after 0.1.0rc10 they kept installing 0.1.0rc9.
- `/cases`: test cases from documents, in the chat. `/cases generate FILE_OR_FOLDER`
  has the assistant's model write candidate cases from `.txt`/`.md` documents (the policy
  must allow the model's provider to receive them) and shows each beside the exact quote it
  cites, flagging an answer that is not word for word in that quote. `/cases accept N...`
  (or `all`) and `/cases reject N...` are the user's review; `/cases save [FILE.jsonl]`
  writes the accepted cases to a new dataset file, never over an existing one. Generated
  references stay unverified until accepted, exactly as with `benchcraft candidates`.
  The assistant is told it cannot generate, accept or save cases itself.
- Fixed: a dataset written by `benchcraft candidates promote` (and `/cases save`) could
  not be read back. The file carried `duplicate_of_line`, `source_line` and a null
  `repository`, which the dataset reader refuses. Unset fields are now left out.
- A plan change the assistant proposes and BenchCraft rejects now comes back with what to do
  differently. A quote or objective that is not the user's words returns the user's message to
  copy from ("keep \"traffic correctness\", do not turn it into \"correctness\""), and settings
  keyed by something that is not a metric id (an objective's name) list the metric ids that
  exist instead of "not available in this session". The tool description says settings go under
  a metric id and objectives keep the user's exact words. In a real session each of these took
  the assistant several tries.
- Plugin `aibench-deepeval` 0.2.0rc3.

## 0.1.0rc10 — judges that think, budgets that fit

Found on a real RAG project, where the two retrieval metrics failed on every case.

- The `openai_compatible` judge now allows 8000 output tokens by default (was 2000). A
  reasoning model such as `glm-4.7-flashx` spends part of that on thinking before it
  writes its JSON; at 2000 it used all of it and returned nothing, which showed as a JSON
  decode error on every faithfulness and contextual precision case. A reply cut off by the
  allowance is now reported as that, and names `max_output_tokens`.
- `benchcraft connect http` defaults to 100 application calls and allows 20 judge calls
  per application call (it was 20 of each). Judged metrics make several calls per case, so
  a 15-case run with five of them ended `budget_exhausted` at 97 of 100 judge calls. The
  wall-clock limit is one hour (was 30 minutes). Existing projects keep their
  `policy.json`; raise `ceilings.max_evaluator_calls` there.

## 0.1.0rc9 — retry only what failed

- `/rescore` keeps the results the run already finished and evaluates only what failed or is
  missing: 2 judge calls that hit a rate limit are retried, not all 15 (about 45 s each on a
  free endpoint). `/rescore all` (and `aibench evaluate` without `--only-unfinished`) still
  evaluates everything again. A carried result is an earlier pass's result for the same stored
  answer and metric settings; the new pass stays complete, marks each carried result in its
  provenance (`carried_forward`), attributes no calls or cost to it and decides pass or fail
  under the pass's own rule. Results that depend on an imported trace are always evaluated
  again. On a real run: 28 results carried, 2 evaluated, 229 s instead of about 12 minutes.
- `aibench evaluate` gains `--only-unfinished`; stored metric results are now read in the
  order they were committed.

## 0.1.0rc8 — runs that finish

Found on a real project, where three benchmark runs in a row ended incomplete.

- Judge and assistant model calls wait out a rate limit and try again: 429, 5xx and dropped
  connections are retried with a growing wait (the server's `Retry-After` when given). The
  judge makes up to 5 attempts within 200 s a case; the assistant 4, and only while nothing
  has streamed, so a reply is never repeated. A wrong key or a bad request is never retried.
  Before this, 7 of 10 judge calls on a free endpoint failed on their first `429`. The
  provider config gains `retry_wait_seconds`, and so does the judge's.
- Reopening a session refreshes its plan under the project's current policy when that
  changes it. A draft records the limits of the policy it was made under, so raising a limit
  in the policy never reached an existing session: every run stopped at the old 20-call
  limit (`budget_exhausted`) although the policy allowed 100. Nothing changes, and no
  revision is added, when the plan is already current.
- The report warns when answers look like the application failing while reporting success
  (`Error: ... Invalid API Key`, `Error code: 429`, a traceback), counted per run with the
  first case IDs, since every metric would score the error text. A run whose 15 answers were
  all such errors is now flagged at once.

## 0.1.0rc7 — choosing a session

Found when an empty session was opened by mistake among six that all looked alike.

- The session chooser lists sessions newest first with what each checks (its objectives)
  and whether it has run, instead of an ID and a revision number.
- Sessions with no objective and no run are hidden from the chooser and reused: when none
  is worth resuming the newest empty one opens without a prompt, and choosing "new session"
  reuses one, so abandoned sessions no longer pile up. `--new` (and `--objective`) still
  create a session; `--send` needs exactly one session with work in it, as before.

## 0.1.0rc6 — sessions and plugins

Found on a real project: a session created before `plugins install deepeval` was resumed
afterwards, and a G-Eval change was "applied" but planned nothing, while the assistant said
it had added a G-Eval metric.

- Reopening a session loads the project's current plugin environments and their defaults
  (as the next draft revision; a session's own plugins are kept and a run in progress is
  left alone). A session keeps the plugins it was created with, so it never saw plugins
  installed later.
- A plan change with settings for a metric the session does not have is rejected with a
  reason ("deepeval.g_eval is not available in this session ... /plugins") instead of being
  saved and reported as applied, so the assistant cannot claim a metric that is not there.

## 0.1.0rc5 — plan changes with a real model

Found by running the assistant against a real model (`glm-4.5-flash`) on a G-Eval request.

- Plan changes are refused when the user takes the request back ("don't add it", "not yet",
  "hold off", "never mind", a later "actually, don't"), not on any "not" in the message:
  objectives and criteria are full of them. Runs, rescores and experiments keep the strict
  any-negation rule. A quote may span several sentences (the model quoted the whole message
  and was refused with no refusal wording found).
- Fixed: recording a plan change with `params` (metric settings such as G-Eval criteria)
  failed with `Unable to serialize unknown type: mappingproxy`, so the assistant could not
  configure a metric. Settings now save, plan and survive reopening a session.
- A metric whose settings the user gave is planned even when no objective's wording names
  it (project defaults such as the judge select nothing).

## 0.1.0rc4 — G-Eval criteria in conversation

- Negations inside any text copied into the plan (G-Eval criteria such as "says the
  information is not available") no longer read as the user refusing the change.
- The assistant's patch tool now says how metrics are configured: no field adds a metric;
  metrics follow from objectives, and settings go in `params` keyed by evaluator ID.

## 0.1.0rc3 — plan changes in conversation

- An objective worded with a negation ("never invent fines", "no hallucinated penalties")
  was read as the user refusing the change. Negations inside the objective being added no
  longer count; refusals around it ("don't add that", "actually, don't") still do.
- A bare "yes"/"yeah" can confirm values the assistant's previous message asked about
  (that message must end with a question); anything not offered is still refused.
- Planning recognises "invent", "make up", "fabricate" (groundedness) and "expected /
  reference answers" (correctness).

## 0.1.0rc2 — first-run fixes

- `benchcraft` in a folder that is not a project (the home folder, a system folder) explains
  how to connect an app and creates nothing there; a folder it cannot write to gets a clear
  message instead of a traceback.
- Setup no longer asks for the model when the choice has a known one (Z.ai GLM-4.7-Flash),
  and says that Z.ai needs a free API key.

## Unreleased — install like any terminal tool

- One-line installers (`install.ps1`, `install.sh`) install BenchCraft from a GitHub Release
  with uv, verifying the wheel against the release's `SHA256SUMS`. The command is now
  `benchcraft`; `aibench` remains as an alias. A tag push (`.github/workflows/release.yml`)
  runs the release check and publishes the wheels, sdists and checksums only if it passes.
- First-run setup (`benchcraft setup`, or asked on the first chat) saves the assistant's
  model for the user in `~/.benchcraft/config.json` (`BENCHCRAFT_HOME`), with the key's
  environment variable name only; on Windows it can save the key as a user environment
  variable. Projects without a policy use that model; `connect http` allows it in the
  policy it writes. A project policy still decides.
- `/plugins install` and `aibench plugins install` work from an installed package: without a
  source checkout they install the release's adapter wheel (verified against `SHA256SUMS`,
  never looked up by name on a public index); `BENCHCRAFT_RELEASES` points at another
  release folder or URL.
- `connect http` gains `--context-path` and `--context-text-path` for the retrieved
  documents, so RAG metrics apply to connected endpoints. A repo with no connected app now
  explains how to connect one instead of failing on a missing config.

## Unreleased — traces and rescoring in the chat

- `/traces import FILE [RUN_ID]` attaches an OpenTelemetry (OTLP/JSON) export to a run the
  session started (the latest by default), as `aibench traces import` does; the file must
  be inside the project or a policy data root. `/traces [RUN_ID]` shows what the run's
  traces add. `/rescore [RUN_ID]` scores the stored outputs with the current draft (the
  application is not called), so run, import and agent-trace scoring happen in one
  conversation. The assistant points users to these commands for agent-trace metrics.
- Fixed: drafting failed when a concept had many ineligible candidates (a plugin such as
  DeepEval adds dozens): the gap's explanation exceeded its 1000-character bound. It now
  lists the candidates that fit and counts the rest.

## Unreleased — DeepEval metrics for the assistant

- `aibench-deepeval` 0.2.0rc1 wraps every DeepEval 4.2.5 single-turn metric aibench's
  recorded data can feed (21, including G-Eval with plan-stated criteria), each with a
  documented field mapping and not-applicable policy. A built-in `openai_compatible` judge
  runs them on any Chat Completions endpoint (e.g. GLM on Z.ai) and counts its calls.
- `aibench plugins install NAME` / `/plugins install NAME` create a plugin environment for
  the project, record it and its judge default in `aibench.json`, and apply the policy
  lines it needs after showing them (the old policy is kept as `.bak`). `aibench plugins
  status` and `/plugins` show each optional plugin's state. The assistant's new
  `list_optional_plugins` tool lets it explain a plugin and suggest the command; it cannot
  install anything.
- Chat sessions load the project's plugin environments. Planning knows new concepts
  (relevancy, retrieval quality, bias, toxicity, privacy, misuse, advice, role adherence,
  instruction following, summarization, task completion, tool arguments and permissions,
  patterns, custom criteria) and fills project defaults such as the judge.
- Evaluator manifests may declare `concepts` and `parameter_requirements` (fields a
  parameter adds); a worker-run evaluator now receives the fields its parameters name.
- Metric semantics: `deepeval.faithfulness@1` is unchanged; its plugin version moved to
  0.2.0rc1, so strict comparisons against earlier faithfulness runs report a plugin
  identity difference. Workspace: sessions gain `evaluator_defaults` (optional).

## Unreleased — DeepEval agent-trace metrics

- `aibench-deepeval` adds DeepEval 4.2.5's agent metrics: step efficiency, plan quality and
  plan adherence (judged), and agent loop detection (deterministic). They read the trace
  imported for each execution (`aibench traces import`), which the harness turns into a
  span tree (`observations.otel.span_tree`: agent, llm, tool and retriever spans with inputs,
  outputs, model and errors, from `gen_ai.*` and OpenInference attributes, text cut to 4000
  characters) and the adapter converts to DeepEval's trace.
- A trace root without recorded input or output takes the case's input (the agent's task)
  and the recorded answer. `plan_quality` and `plan_adherence` report `no_plan_in_trace`
  (not applicable) where DeepEval would score a trace with no plan 1. Found in a live check
  with GLM-4.6: both metrics scored 1 on traces with no plan, and step efficiency judged
  the agent "with the task unknown".
- The evaluation view gains `execution.trace`. A metric requiring it is not applicable
  without an imported trace, with a partial trace, or with more than one trace for the
  execution, and bypasses the evaluation cache. Planning does not rule such metrics out up
  front, since traces are imported after a run.
- Plans gain `model_evaluation_timeout_seconds` (default 300): the per-case limit for
  model-judged metrics, which `aibench run`, rescoring and `aibench score` apply;
  `evaluation_timeout_seconds` (60) still governs local checks. A live check with GLM-4.6
  judging DeepEval faithfulness took 61 s per case on average (69 s at worst), past the old
  shared 60 s limit.

## Unreleased — DeepEval conversation metrics

- `aibench-deepeval` adds DeepEval 4.2.5's 12 conversational metrics (conversation
  completeness, knowledge retention, role adherence, goal accuracy, topic adherence, tool
  use, turn relevancy, turn faithfulness, turn contextual precision/recall/relevancy and
  conversational G-Eval). Each turn of an episode (cases sharing a `group_id`) is scored on
  the conversation up to and including it, built from the run's recorded executions of the
  same repetition; the episode's last turn carries the whole conversation's score.
- The evaluation view gains `episode.turns` and `case.group_id`. A metric requiring
  `episode.turns` gets the conversation assembled by the scorer (retrieved passages and tool
  calls only if it requires them), is not applicable outside an episode or after an earlier
  turn that did not complete, and bypasses the evaluation cache (earlier turns change it).
- DeepEval scores outside 0..1 are evaluator errors, never recorded scores.
- Dataset summaries list `case.group_id` only for datasets with episodes. No metric
  semantics of existing evaluators changed.

## Unreleased — chat input box

- The message being typed is now drawn as a box across the bottom of the terminal: a tinted
  band in the theme's colour, a marker on its left edge and the hint `Ask BenchCraft to do
  anything` while it is empty. It stays on the last rows, just above the status bar, with spare
  rows above it instead of under it. The box follows the terminal's width, and every further line of
  a multiline message (or a line that wraps) keeps the same left edge. It is drawn by the
  prompt itself, so streamed replies and run progress still print above it, and it takes its
  colour from the active theme (`/themes`). A terminal that cannot print the marker's block
  character gets a plain ASCII marker on unbanded text. No metric semantics or workspace
  schema changed.

## Unreleased — terminal welcome screen and themes

- `aibench chat` opens with a BENCHCRAFT block logo over a bordered panel showing the
  project, session, assistant model and grouped slash commands. Consoles that cannot encode
  block characters get an ASCII logo; terminals narrower than the logo skip it.
- New `/themes` command lists colour themes (`crimson` red by default, plus `ember`, `gold`,
  `ocean`, `forest`, `violet`, `mono`, and `paper` for light backgrounds). `/themes NAME`
  switches immediately and saves the choice in `.aibench/ui.json`. No metric semantics or
  workspace schema changed.

## Unreleased v4 local-scope work — Prompts 30/31

- Fresh conversational planning can include the existing bounded, policy-approved repository
  profile. Findings retain evidence and unknown states; inspection does not execute project
  code.
- Added `aibench connect http` for bounded no-repository setup of the configured JSON HTTP
  runner. Setup validates a selected local JSONL dataset and writes typed config and policy
  without contacting the endpoint.
- Conversations can read imported trace summaries alongside approved repository findings for
  one stored run and can start/resume bounded experiments through the existing experiment
  service. Protected holdout evaluation remains a separate explicitly requested action.
- The clean-installed deterministic suite now includes E2E-08 for a fresh repository-aware
  conversation. No metric semantics or workspace schema changed.

## Unreleased Phase 3 working tree — Prompt 20

- Added `scripts/measure_capacity.py`. It runs the real CLI against the local rate-limited
  fixture, and records throughput, process-tree CPU, peak memory, system load, server-side
  concurrency and workspace growth per case.
- Measured the local single-process ceiling, and recorded the distributed-execution
  decision in ADR 0019. Distributed workers are deferred, because no distribution need was
  demonstrated. No coordinator, queue, object store or worker service was added.

## Unreleased Phase 3 working tree — Prompt 19

- Added optional controlled parameter experiments over finite, app-exposed environment
  settings. Trial contracts freeze the development plan, evaluator identities, budget, seed,
  constraints, exact parameter values and run lineage.
- Added a durable budgeted grid runner with explicit resume, coverage/constraint checks and
  paired uncertainty comparisons. A development candidate is selected only when its
  95% paired interval supports an improvement over baseline.
- Added a protected final evaluation that locks development selection first, then compares
  baseline and selected settings on all holdout cases under one frozen evaluator plan.
  Reports separate selection and final holdout results; adoption is proposed for review and
  never applied automatically.
- Added the synthetic known-objective app, CLI experiment commands and conversation report
  and adoption tools.

### Workspace

Schema version 11 adds experiment contracts, trial lineage, append-only events and reserved
holdout digests. Existing workspaces migrate forward on first use.

## Unreleased Phase 3 working tree — Prompt 18

- Added bounded, policy-checked question/answer candidate generation from explicit
  development text sources. Candidate rows remain separate from runnable datasets and
  record source hashes/spans, model and prompt identity, exact duplicate source documents,
  and every review/verification/promotion action.
- Added human source review, expert review, a strict exact-source-answer oracle, and an
  explicit promotion command that writes a new ordinary JSONL dataset. Synthetic
  unreviewed references cannot be promoted.
- Added a typed multi-turn text episode manifest and a validation command for ordered turns,
  simulator provenance, resettable test worlds, and independent final-state checks.
- Added an executable local support-conversation fixture using the existing per-episode
  engine reset behavior and `native.final_state` evidence.

### Workspace

Schema version 9 adds the candidate-pool, candidate-case, and append-only candidate-event
tables. Older workspaces migrate forward on first use.

## Unreleased Phase 2 working tree — Prompt 17

- **openai/evals bridge** (plugin `aibench-openai-evals-oss`, `evals==3.0.1.post1`):
  - the `match`, `includes`, `fuzzy_match` and `json_match` evals, run by the upstream code;
  - recorded replay needs the eval's request to equal the recorded input exactly, once;
  - `aibench openai-evals-oss run` bridges the upstream completion function to the
    application's runner as a recorded `delegated_suite` run.
- **Hosted OpenAI Evals API bridge** (plugin `aibench-openai-evals-api`, `openai==3.19.2`):
  - `aibench openai-evals-api submit/status/resume/cancel/fetch/jobs` grade recorded
    outputs as remote jobs;
  - requests are stored before sending;
  - ambiguous submissions are reconciled, never resent without `--resend`;
  - results map one-to-one to cases;
  - generating data sources and `{{sample.*}}` templates are refused.
- **Langfuse connector:** `aibench langfuse import-dataset/import-traces/export-scores/status`
  import datasets and traces, and export recorded results as scores, with provenance on
  both sides.
- **Integrations:** `aibench integrations list`, `/integrations` in chat and the
  assistant's `list_integrations` tool show modes, data destinations and availability.
- **Policy:** new `allowed_egress_origins`. Every destination that receives benchmark
  data or credentials must be listed, loopback included.
- **Behaviour change:** a `remote_job` metric cannot be bound in plans or rescoring.

### Workspace

Schema version 10 adds `remote_jobs` (after Prompt 18's migration 9).

## Unreleased Phase 2 working tree — Prompt 16

- **Evidence-backed inspection.**
  - `aibench inspect APP --source DIR --policy P` reads manifests and Python/JS imports
    from a tree inside the policy's new `inspection_roots`.
  - Findings are *inferred*, with file, line and context (code, tests, `TYPE_CHECKING`,
    guarded, commented out). They never satisfy an evaluator's applicability check.
  - Secret-like files are never read.
  - `--probe N` runs N dataset cases through the runner under the policy (effects must be
    `none`), turning declarations into observations.
- **Trace import.**
  - `aibench traces import RUN FILE` and `aibench traces show RUN` read OTLP/JSON,
    preserve the raw export as a restricted artifact, and match traces by correlation ID.
  - Partial traces are marked with reasons.
  - Token usage is summed over the lowest reporting spans only.
  - Reports gain an "Imported traces" row.
- **Opt-in caches.**
  - Plan `cache: {executions, evaluations}`, with version-complete keys. The execution
    key includes the application's source files and inherited environment. Endpoints need
    a declared `revision`.
  - Comparisons against cached executions are blocked.
  - Every hit carries provenance.
  - Execution caching is refused for effectful apps without a test world, for
    per-episode state and for shared state.
  - `aibench cache list/clear`.
  - Reports gain a "Cache" row. Hits are excluded from latency.
- **Provider-aware quotas.** Plan `quotas` (`application` or `evaluator:<glob>`) with
  `max_in_flight`, `requests_per_second`/`burst`, and backpressure on HTTP 429/503
  (`Retry-After` or `backoff_seconds`). Quota summaries and `backpressure` events are
  recorded in the run events.
- **Responsiveness fix.**
  - HTTP client creation and capture-file writes no longer block the event loop. The loop
    stalled for up to 1.7 s under load; it now stays below 0.25 s.
  - This affects the live terminal, which shares the loop.
  - Concurrent writes of identical artifacts are safe on Windows.

### Workspace

Schema version 8 adds the tables `trace_observations` and `cache_entries`. Older
workspaces migrate forward on first use.

## Unreleased Phase 2 working tree — Prompt 15

- **New application transports** behind the same runner contract:
  - `python`: a callable, run in a fresh interpreter through a standard-library shim;
  - `openai_compatible`: a chat-completions endpoint. Usage is observed; tool calls are
    recorded as requests, not effects;
  - `container`: an image pinned by digest, non-root, read-only, capability-free,
    resource-limited and offline by default. The container is removed on timeout, and
    nothing is pulled (including between preflight and run). App config cannot select a
    host executable or set host-sensitive environment variables on the engine client. The
    policy approves images and network.
- **State between cases.** The engine now honours `reset_policy`:
  - it resets through the application's hook (`reset_url`, `reset_argv`,
    `reset_callable`) before every case, or before each episode (cases sharing a
    `group_id`);
  - a failed reset blocks the case;
  - a failed or interrupted episode turn blocks the rest of that episode.
  - **Behaviour change:** an application whose config declared `reset_url` is now actually
    reset, and its plans need `concurrency.application: 1`.
- **Test worlds.** Named seeds declared by the application, selected by a plan
  (`test_world`) or in chat (`/world`), approved by the policy (`allowed_test_worlds`),
  and frozen with the run. A new `world_state` observation. Reports record the reset mode,
  the world and its seed hash, and the reset counts.
- **Agent outcome metrics:** `native.tool_calls@1.0.0` (names), `native.tool_outcomes@1.0.0`
  (arguments, success, authorization) and `native.final_state@1.0.0` (assertions on the
  world state). They are separate, so a correct tool name never masks a failed outcome.
- **Chat.** `/app` and the assistant's `describe_application` explain what the runner
  observes, what evidence is missing, and how state is reset.
- **Known limits.**
  - Containers were exercised with Docker Engine 29.7.2 (Docker Desktop, Windows) only, and
    are not a hostile multi-tenant sandbox.
  - The OpenAI-compatible transport was exercised against a local stub only.

## Unreleased Phase 2 working tree — Prompt 14

- Added the separately packaged `ragas.faithfulness@1` adapter, pinned to `ragas==0.4.3`
  and restricted to text stored outputs in an isolated worker.
- Added strict/exploratory stored-run comparison with case/repetition pairing, compatibility
  identities, coverage gates, case-level macro differences, grouped bootstrap intervals and
  stored-pass stability summaries.
- Added `aibench compare` plus the session-owned `compare_runs` tool and `/compare`; neither
  workflow invokes an application, evaluator or judge.
- Cross-framework results retain separate score semantics. Disagreement is diagnostic and
  is never averaged into a combined quality score.
- Known limitation: Ragas 0.4.3 has an open multi-modal SSRF advisory. The adapter exposes
  only text faithfulness; ADR 0013 records the compensating control.

## 0.1.0rc1 — MVP release candidate (not published)

The first release candidate of the MVP. It is a local technical candidate. **No real team
has piloted it yet** (see [docs/pilot/](docs/pilot/README.md)), and it is not published to
a package index. The readiness decision, with everything that is still open, is in
[docs/engineering/release-readiness.md](docs/engineering/release-readiness.md).

### Capabilities

- **Benchmark conversation.**
  - Bare `aibench` in a terminal opens a persistent two-way session: state a goal, answer clarifications, revise the draft plan, and run it.
  - While it runs you can ask questions and use `/status`, `/pause`, `/resume` and `/stop`.
  - A session is restored after exit or a crash without repeating any action.
  - Failures are discussed with evidence, and every number is traced to a query.
- **Headless commands** for everything the conversation does:
  - `init`, `doctor`;
  - `dataset validate`, `inspect`, `app describe` / `smoke`;
  - `plan`, `plan validate`, `plan benchmark`;
  - `run`, `resume`, `evaluate`, `score`;
  - `runs`, `report`, `benchmark`, `sessions`, `evaluators`, `plugins`.
- **Applications.** CLI and HTTP runners with explicit input and output bindings. Reference answers never reach the application. Retrieval, tools, usage and cost are recorded only when the application reports them.
- **Evaluators.**
  - Native exact match and JSON schema checks.
  - Trusted custom Python evaluators.
  - The DeepEval faithfulness adapter (`aibench-deepeval`, pinned to `deepeval==4.2.5`), run in its own environment.
  - An evaluator that lacks the evidence it needs reports a gap, not a score.
- **Planning.** Evidence-aware plans from declared capabilities and installed evaluators, within a policy. A bounded model planner with a deterministic template fallback. Plans are frozen and hashed before execution.
- **Execution.** Bounded concurrency, timeouts, retries, budgets, cancellation, and conservative resume after interruption or a crash. Effectful calls are never repeated automatically.
- **Reports.** JSON, Markdown and HTML, rebuilt from stored facts, with:
  - full denominators;
  - release gates;
  - latency definitions;
  - cost completeness (unknown is never $0);
  - case evidence.
- **Validation tools.** `aibench plan benchmark` (a planner fixture set) and `aibench evaluators calibrate` (judge calibration).

### Changes in this candidate (after the Prompt 12 acceptance audit)

- **A full disk no longer fails cases.** A workspace storage failure (disk full, quota, I/O error, read-only filesystem, database full) stops dispatching and leaves the run resumable, exit code 130, with the reason shown.
  - Before this, every remaining case was still sent to the application and marked `failed`, and the calls that couldn't be recorded were left out of the accounting.
- **Recovery can't lose a call from the accounting.** Recovery now commits its settlements together with the record of calls that may have reached the application. A crash in between used to lose that record.
- **Why a run stopped early** is shown in `run` and `resume` output, in `runs status` (`warnings`), and in the chat.
- **A newer workspace is refused.** A workspace upgraded by a newer aibench is refused with exit code 2, instead of being written by software that doesn't know its schema.
- **Version metadata.** The version is `0.1.0rc1` for both packages, from one source each. `aibench-deepeval` requires `aibench>=0.1.0rc1,<0.2`.

### Metric semantics

First release: `native.exact_match@1.0.0`, `native.json_schema@1.0.0` and
`deepeval.faithfulness@1.0.0`. Their limitations are listed in `aibench evaluators describe`.

### Workspace

Schema version 7, created on first use. See the upgrade notes for the forward-only
migration rule and the refusal of newer workspaces.

### Known limitations

- **Tested platforms.**
  - Tested on Windows 11 with Python 3.12 and 3.11.
  - Linux runs in CI, but no result has been observed.
  - macOS is not run anywhere.
- **No live model or judge tested.** Nothing in the conversation, model planner or DeepEval judge has been exercised against a live model or judge.
- **Planner recall.** The template planner's selection recall on the v1 fixture set is 17/21, below the 0.85 engineering target. Fixtures and calibration labels haven't been reviewed by people.
- **Throughput.** About 7 cases/s against an instant local service. Durable capture is the bottleneck.
- **Disk-full testing.** Handling is tested by making the real write path fail. A real full volume wasn't used.
