# ADR 0007: Benchmark sessions, conversation turns and typed actions

Status: Accepted
Date: 2026-09-23
Prompt: 08 — Persistent two-way conversation and typed actions

## Context

§2–5, §8 and §14–16 make the conversation a first-class part of the product:

- a persistent session linked to a project, the user's decisions, draft plan revisions and runs;
- turns that interpret a message into typed outputs validated outside the model: `answer`, `ask_question`, `propose_plan_patch`, `request_action`, `explain_results`;
- expected revisions, so a delayed model response cannot overwrite a newer user decision;
- deduplicated action IDs, so a retried turn cannot start a duplicate run;
- follow-up questions during execution that never interrupt the run;
- scope changes that create a new draft revision instead of rewriting a running run's frozen plan.

Prompts 06 and 07 already provide the run services (`compile_plan`, `create_run`, `execute_run`, `run_status`, `RunController`) and the planning services (`gather_inputs`, the template planner, `validate_draft`). The terminal UI is Prompt 09.

## Decisions

### Records and storage (08-T1)

- **§5 records in `core/sessions.py`** (core, no planning imports; exported as JSON Schema):
  - `BenchmarkSession`
  - `ConversationTurn`
  - `PendingQuestion`
  - `DecisionRecord`
  - `ActionRequest`
  - `SessionChoices`: the user's decisions at one revision (objectives, the concepts chosen for objectives, case selection, repetitions, budgets, metric parameters and rules, dataset)
  - `PlanPatch`: a typed change to those choices

  A conversation never edits a plan directly.
- **Migration 7** adds `sessions`, `conversation_turns`, `decision_records`, `pending_questions` and `action_requests` (§14's list). It enforces:
  - `UNIQUE (session_id, revision)` on decisions, and compare-and-set on `sessions.revision`: a stale change is rejected, never merged;
  - a unique user turn per (session, delivery ID), and one reply per user turn;
  - `action_id` as the primary key: a redelivered action returns the stored record.

  Runs are not children of sessions: deleting a conversation cannot delete run results (§14).
- **Linking.**
  - A decision records its source turn, the revision it produced, the decision it supersedes, the structured change, the resulting choices, the plan file and hash, and the validated draft document.
  - A turn records its decision and action references.
  - An action records its source turn, expected revision, authorization quote and run ID.
- `PendingQuestion` is defined once in `core/sessions.py` and reused by the planning draft document, avoiding a duplicate schema and conversion.

### Drafts (08-T2, 08-T4)

- **`sessions/drafting.apply_patch`** checks a patch against the current choices. It rejects an unknown objective to remove, an unknown concept, a missing dataset, or budgets that don't validate. A patch that changes nothing makes no revision.
- **`build_draft`** derives the draft from choices with the same services as `aibench plan`: `gather_inputs`, the deterministic template planner and `validate_draft`. Its findings separate missing information from missing permission. Conversational drafts are therefore deterministic and equal to headless drafts made from the same choices.

  The model interprets messages into patches; it does not write plans. The 07 model planner remains available headlessly.
- **Answer reuse.** `template_proposal` gained an optional `concepts` mapping (objective text → the concepts the user chose), unioned with keyword matches, so answering "Which concept does … mean?" becomes part of the choices and later drafts don't ask again. The 07 anti-relabel rule still applies.
- **Immutable plan files.** Each draft's plan is written once to `.aibench/sessions/<id>/plan-<hash>.json`, named by content, so a run started from a revision uses exactly what was reviewed. Two turns racing for one revision cannot overwrite each other's file; the loser's commit is rejected and its file, if different, is an unreferenced orphan.
- **Sample seeds.** Without a seed, the current seed is kept, or a stable per-session seed is used, so "use 20 cases" is always a recorded, reproducible sample.
- **Questions.** A decision's draft questions become the open ones. Earlier open questions it no longer asks become `stale`, and so do the ones a patch answered once the draft moves on. Answering a question requires that it be open and asked against the expected revision. So a dataset change invalidates outstanding choices (§8).

### Controller and shared services (08-T3)

- **`SessionController`** calls the services the headless commands call:
  - `compile_plan`, the execution gate, run again at start;
  - `create_run`, which freezes and approves;
  - `execute_run`, the same engine and lease;
  - `run_status`;
  - stored metric results and execution attempts, for failures and case evidence.

  There are no simulated successes. The approval records which session and action granted the run.
- **Concurrency.** Runs execute as asyncio tasks on the caller's loop, and the model call goes to a worker thread (`asyncio.to_thread`), so input and questions stay responsive while the run executes (§15). The single SQLite writer is only used on the loop thread.
- **Controls.**
  - Pause, resume and cancel go through `RunController` for a run executing in this process.
  - For a run that isn't executing here (e.g. after reopening), resume re-enters `execute_run` under the frozen identities, and cancel re-enters it with a pre-set cancel, so unstarted work is recorded as cancelled.
  - Each control request is an `ActionRequest` plus a `control_requested` run event, not a plan change.
  - Reopening a session never restarts a run (§13), and `close()` interrupts live runs so they stay resumable.
- **Start outcome.** `execute_run` takes the lease and verifies the frozen identities before its first suspension. After one scheduler step, the controller reports a refused start or resume (e.g. another session holds the lease) as not done, instead of claiming it ran.
- **Results.** `run_status` answers with a timestamped snapshot, marked `provisional` while the run is active (§15). `run_events(after=…)` replays committed events.
- **Data shown to the model.**
  - The user's `case_evidence` includes the Golden.
  - The assistant's view never includes reference answers or other judge-only fields.
  - It includes case inputs and application outputs only when the policy's new `share_case_content_with_assistant` is true (default false). Otherwise it gets IDs, decisions, scores and reason codes.
  - A reason code is only a leading `snake_case` token (`reason_code`), so free-text judge explanations, application errors and `session_error` never pass when case sharing is disabled. This covers failures, case evidence, and `run_status`'s attention details in `get_run_status` and the session state sent every turn.

### Action boundaries (08-T4)

- **Start.** `start_run` names the reviewed revision it runs, and is refused if that isn't the current revision. The draft's blocking findings decide the outcome:
  - missing permission → `denied`, with the policy's words;
  - missing information → `blocked`, listing what is needed;
  - the execution gate is run again, and the compiled plan hash must equal the reviewed revision's.
- **One active run per session** (§15). The run slot is taken by compare-and-set (`starting:<action>` → run ID) before anything is created, so two processes with different action IDs cannot both start a run. A crashed start's marker expires after 10 minutes.
- **Scope changes while a run is active** create a new revision and report the unchanged active run. The run's frozen plan is never touched.
- **Permissions are never granted in conversation.** The policy file and the explicit trusted-local grant made when the session was opened decide. A denial explains what is missing.

### Conversation loop (08-T2, `conversation/agent.py`)

- **Turns are idempotent.** A user turn is stored once per delivery ID. A redelivered message returns the stored outcome without calling the model. A turn that crashed before replying is re-run, and its action IDs are derived from the user turn, so an action it already took is recognized, not repeated.
- **Tools** (all narrow, validated outside the model; no terminal, file, network or process tool; unknown names are refused):
  - `get_session_state`, `show_plan`, `read_profile`, `summarize_dataset`, `list_evaluators`, `describe_evaluator`, `explain_metric`
  - `propose_plan_patch`, `ask_user`
  - `get_run_status`, `list_failures`, `get_case_evidence`
  - `request_action`
- **Grounding** (`patch_problems`).
  - A model patch must quote the user's words asking for it, and the quoted sentence or any later sentence in that message must not hold back or refuse the change (no, not, never, n't, without, hold off, wait, later, instead). A later refusal overrides an earlier request.
  - Every field it sets must be stated in the user's message, matched as whole words or phrases:
    - objective text, and objectives to remove;
    - chosen concepts;
    - numbers (with percent forms);
    - parameter values; a boolean or null flag counts through its parameter name;
    - thresholds, categories and paths;
    - a comparator without a threshold, which needs "true";
    - `all_cases`, which needs "all", "every", "entire" or "full".
  - It names its expected revision; stale patches come back as `stale` with the current revision.
- **Authorization** (`authorization_problem`). A model action must quote the user's words, and the quoted sentence must ask for that action:
  - any refusal or deferral in the quoted sentence or a later sentence overrides the request;
  - it must not be a question and must contain no negation or deferral;
  - it must use the action's verb, either on its own ("pause", "please stop") or applied to the run ("run it", "cancel the benchmark"). So "Stop explaining…" or "Continue with the explanation" is not a run control.
  - A bare affirmation counts only when the whole message is the affirmation ("yes", "Yes, please.", "ok go ahead") and the previous reply offered exactly that revision. An offer requires a shown, executable draft, no question asked, and a reply whose last sentence proposes running it without negation.
  - A model start also needs a revision that was shown to the user, or one this turn created at the user's request.
  - The rules are deliberately conservative: a refusal costs one clarifying exchange, while a false acceptance acts without consent. A polite request without a question mark ("Could you run it") is accepted as a request.
- **Presentation.** Only `show_plan`, or an applied patch, puts the draft card in the turn outcome and marks the revision as shown. `get_session_state` is reading, not showing. The terminal (Prompt 09) must render `presented_draft`, and typed commands such as `/plan` call `mark_presented`.
- **Questions.** At most two per turn, and only about benchmark fields (`objectives`, `selection`, `repetitions`, `budgets`, `params.*`, `rule.*`, `dataset`).
- **Outcome.** Each reply stores a structured `TurnOutcome`, computed by the harness rather than claimed by the model: explanations, result snapshots, questions, decisions, rejected attempts, actions, the presented draft, the offer, the active run, usage and why it stopped. Its `status_line` states whether the turn explained, changed the draft or acted, and that an active run continues (§3).
- **Bounds and failures.** Default per-turn bounds are 6 model calls, 12 tool calls and 60k tokens. A limit or a provider failure ends the turn, not the session, and a tool's internal error goes back to the model as an error. Without a provider, messages get guidance, and every typed command still works (§14).
- **Redaction.** Obvious credentials (`sk-…`, `AKIA…`, bearer tokens, `api_key=…`-style assignments) are removed before a message is stored or sent (§14). This is pattern-based, not a guarantee.
- **The provider** is the planning-role model (§2), built through the same `provider_denials` gate as `aibench plan`. The terminal that builds it is Prompt 09.

### Commands

- `aibench sessions list` and `aibench sessions show ID [--json]` are read-only views of the conversation, decisions, questions and actions.
- The interactive terminal (`aibench`, `chat`, slash commands) is Prompt 09. `main.py` still says so.

## Changes after independent review

An adversarial review, run with executed reproductions, found 6 confirmed major and 6 minor or plausible issues. All were fixed, with regression tests (`tests/test_session_review_regressions.py`). The review's scripts reproduced the defects on the reviewed code; the new tests exercise the corrected behaviour (before the fix they failed at import, since `patch_problems` and `reason_code` did not exist).

- **Major:**
  1. The negation check allowed at most 2 words between the negation and the verb ("I don't want you to run the benchmark" started a run). It now scans the quoted sentence and later corrections.
  2. A bare affirmation matched anywhere ("I'm not sure…" with quote "sure"). It must now be the whole message.
  3. "Shown" meant "the model read the state". Now only `show_plan` or an applied patch presents a revision.
  4. Control verbs were too broad ("Stop explaining…" cancelled a run). The verb must now be about the run.
  5. Patch fields were unchecked: removals, concepts, `all_cases`, flag names, comparators, substrings. All are now grounded, with whole-word matching.
  6. A patch was applied despite "don't". The negation check now covers patches and later corrections.
- **Minor:**
  - Free-text reasons reached the assistant through `run_status`, and colon-less reasons passed `_reason_code`.
  - Repeated pause or resume reported `done`; it is now `rejected`, "already paused" or "already running".
  - A non-`AibenchError` from `create_run` or `execute_run` was lost or left the run slot stuck.
  - Offers were detected loosely.
- **Additional completion review:** two more issues were reproduced with failing regression tests and fixed:
  - A later refusal in the same message did not override an earlier run request or patch. Refusal checks now include the quoted sentence and all following sentences.
  - `run_status.session_error` could expose an uncaught exception string to the assistant when case sharing was disabled. It is now reduced to a reason code or omitted.
- Not changed: a `message_id` reused with different text still raises `ConflictError`. That is a client bug and is reported, not turned into a stored turn.

## Consequences and limits

- **Authorization checks are deterministic heuristics** over the user's words, on top of the model's judgment. They block the spec's examples and invented values, but they don't understand language. Adversarial hardening is Prompt 10.
- **Conversational drafts use the template planner.** Free-text objectives it cannot map become a question the user answers with a concept.
- **No conversation spend cap per session yet.** Per-turn usage is recorded in each outcome, and per-turn limits are enforced.
- **Model-asked questions** become stale on the next draft change like any other open question.
- Planning drafts and session storage share the single `PendingQuestion` model from `core/sessions.py`.
