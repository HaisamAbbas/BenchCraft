# ADR 0009: Conversation recovery and adversarial interaction

Status: Accepted
Date: 2026-09-23
Prompt: 10 — Conversation recovery and adversarial interaction

## Context

§8, §13–16 and §23 require:

- a session that can be reopened after a clean exit, a disconnect or a killed process, showing actual project and run state;
- no work restarted without a new action;
- conversation interruption and run cancellation kept separate;
- long conversations bounded without summaries becoming authoritative;
- stale answers, delayed model mutations and injected tool text that cannot change the plan or widen permissions;
- history and rendering free of secrets and terminal control content.

Prompts 06–09 provide the run lease, recovery and events (ADR 0005), session records and typed actions (ADR 0007) and the terminal (ADR 0008).

## Decisions

### Reconciled state on reopening (10-T1)

- **Run conditions.** A run's stored status is not enough: a process killed mid-run leaves its run stored as `running`. `SessionController.run_condition` combines the stored status with the run's lease (`services.runs.lease_state`: live, stale or none, using the engine's own staleness rule of a dead pid on this host or a heartbeat older than 60 s). It reports one of:
  - `running_here` or `paused_here`: executing in this process;
  - `active_elsewhere`: a live lease held by another process;
  - `interrupted`: stored as active but its session is gone, or stored as `interrupted`; resumable;
  - the terminal statuses.

  It also lists work items in `unknown_effect`.
- **Earlier-layer bug fixed (affects 08-T4 and 09-G4).** `active_run()` treated a dead process's `running` run as active, so after a crash the session refused new runs and the terminal said the run was "still active". Only live conditions now occupy the session's single run slot. `run_status` gains `condition` and `resumable`, and `provisional` now follows the live condition rather than the stored status.
- **`reconcile()`** is the reopening report: the draft, open questions, each run's condition, missed events and unknown-effect work, plus plain notes such as "nothing restarted it; /resume continues it". It only reads. An interrupted run stays stopped until a new resume action (10-G1), and unknown-effect work is never repeated automatically.
- **Event cursors.** `BenchmarkSession.event_cursors` records, per run, the last event sequence shown to the user.
  - `missed_events()` returns what came after it, in sequence order.
  - `acknowledge_events()` only moves the cursor forward.
  - The terminal summarizes missed events once on reopening and acknowledges what its progress watcher shows. Replaying events displays history; no action is replayed.

### Crash-safe start_run (10-G2)

- A redelivered, settled action returns its stored record (ADR 0007). The new case is a redelivered action still `requested`: its first delivery is either still starting or crashed midway.
  - If the crash came after the run was created, the run's approval names the action (`granted_by` includes the action ID). The run is adopted: the action is settled `done` with that run, and the slot points to it. No second run is created.
  - If no run exists and the start is older than the starting window (10 minutes), the request is closed as rejected and the slot freed, so the user can start again.
  - Within the window it stays `requested`, since another process may be mid-start.

### Long conversations (10-T2)

- **What the model sees.** The model gets the last 12 turns, each capped at 4,000 characters. Older turns are replaced by a deterministic summary (`sessions/summary.py`), capped at 4,000 characters, built only from structured records:
  - decision IDs, revisions, sources and changes;
  - the user's own corrections;
  - open questions;
  - run IDs.
- **Not authoritative.** The summary is labelled "references only, not authoritative". It holds no run results and no permissions. The live session state, reloaded every turn, stays the authority.
- **Trimming order.** Oldest decision history goes first. Corrections and open questions are what must survive.

### Boundaries (10-T3, 10-G3, 10-G4)

- **Stale answers and delayed mutations.** Answers and patches against an older revision are rejected (ADR 0007). A dataset change makes the open questions stale. A late start against the old revision is refused. Tested with the model held mid-call on a separate thread while the user changes the dataset.
- **Injection is inert.** Tool output is data. The system prompt says so, but the guarantee is structural: an action's quote and every patch value must come from the user's latest message, so an instruction inside tool output cannot authorize a run or supply a threshold. Tested with an injection in dataset metadata keys, which the model does read through `summarize_dataset`. Golden inputs never reach the model.
- **Permissions stay out of the conversation.** Execution permissions come only from the policy file and the trusted-local grant made when the session was opened. Neither conversation text nor summaries are consulted.
- **Redaction** (`security/redaction.py`).
  - `sanitize` removes obvious credentials and terminal control content: CSI, OSC, DCS/SOS/PM/APC (bounded, and ending at the line end if unterminated), other ESC sequences, 8-bit C1 sequences, C0/C1 control characters except newline and tab (including bare carriage returns), and zero-width and bidirectional formatting characters.
  - Redaction runs before and after control stripping.
  - It applies to stored messages and commands, model replies and everything the terminal renders (`render.safe`), including `sessions show` and `chat --send` output.

### Loss and deletion (10-T4)

- **Graceful exit** interrupts live runs so they stay resumable (ADR 0008).
- **A killed process** leaves a stale lease. The next session sees `interrupted` and resumes under the frozen identities. Effect-free in-flight work may be dispatched once more (at-least-once, ADR 0005); effectful in-flight work becomes `unknown_effect`.
- **Model interruption** stores the reply as interrupted and leaves runs alone. `/stop` cancels the run and the conversation continues.
- **A worker crash** (the application process exiting abruptly) is recorded as an application failure.
- **Deleting a session** (`aibench sessions delete ID --yes`, refused while a run is active) removes its turns, questions, decisions, action requests, session row and draft plan files. Runs, work items, attempts, results, artifacts and events are benchmark records and are kept; they stay readable with `aibench runs` and `run_report`.

## Changes after independent review

An adversarial review, run with executed reproductions, found 4 major and 7 minor issues. The full suite also exposed an in-process race in the new start recovery. All are fixed, with regression tests in `tests/test_recovery_review_regressions.py`.

- **Race.** Two concurrent deliveries of one start action in one process: the second adopted the first's run through its approval, and the first then failed on "already settled" (an 08 test hung). A start still in progress in this process is no longer adopted, and settling is idempotent (`_settle` returns the stored outcome).
- **Major:**
  1. Redaction ran before control stripping, so an escape, NUL or zero-width character inside a key hid it. Redaction now runs before and after stripping.
  2. A run left `cancelling` by a dead session was stuck: its note said `/resume`, but both resume and cancel were refused. `cancelling` is now resumable, continuing it always finishes the cancellation (never un-cancels), and the note says `/stop`.
  3. Summaries were unbounded in objectives, questions and runs. A generic trimming order now enforces the cap and counts what it drops.
  4. Corrections made in conversation (assistant patches, which are grounded in the user's quote) were not kept as user corrections. They now are, marked `via: assistant`.
- **Minor:**
  - An unterminated OSC/DCS sequence swallowed the lines after it; string sequences now stop at the line end, bounded.
  - JSON-member, environment-variable and zero-width-split credentials are now redacted.
  - Bidirectional overrides and zero-width characters are now stripped.
  - The slot claim after `create_run` is checked: if another start took the slot, the created run is not launched.
  - A just-created run without a lease is `starting` for 60 s, not `interrupted`.
  - Event cursors advance atomically in the store.
  - Missed events are replayed, the notable ones by sequence number, not only counted.
- **Found by a regression test (affects 09 rendering):** Rich substituted `:name:` emoji codes in data (a task key `exec:b:r0` rendered with an emoji in place of `:b:`). All terminal output from data now prints with emoji codes off (`render.out`), and the chat and sessions consoles are created with `emoji=False`.

## Consequences and limits

- **Lease staleness is local.** A dead pid is detected only on the same host; elsewhere a stale lease is recognized after the 60 s heartbeat TTL.
- **Deterministic defences.** Authorization and grounding remain deterministic checks over the user's words, not language understanding. Injection resistance comes from the harness never acting on text that isn't the user's.
- **Redaction** is pattern-based for credentials; terminal-control stripping is complete for the sequences listed.
- **No session export** (with redaction) yet.
