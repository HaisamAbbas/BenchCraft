# ADR 0005: Run engine, execution policy, budgets, retries and recovery

Status: Accepted
Date: 2026-09-23
Prompt: 06 — Deterministic scheduling, policy, budgets, and recovery

## Context

§13 and §15–16 require:

- manual plans that run a local application end to end and can be rescored;
- policy decided by code before any dispatch;
- hard call/token limits alongside soft cost estimates;
- effect-aware retries;
- durable events and a resume that never repeats ambiguous effectful work.

Earlier prompts supplied the parts: runners that never retry (ADR 0002), evaluators and scoring (ADR 0003), worker execution (ADR 0004), and durable storage with compare-and-set-friendly work items (02).

## Decisions

### Plans and compilation

- **`ExecutablePlan`** (`core/plans.py`) lists the dataset, application config, metric bindings, repetitions, case selection, concurrency caps, retry policy, budgets and plugin environments. Paths are relative to the plan file. The JSON Schema is exported as `schemas/1.0.0/ExecutablePlan.json`.
- **`compile_plan`** collects every problem and denial and reports them together. The order is fixed:
  1. plan-level policy (plugin environments and their import paths, secrets, data roots, budget ceilings) and application policy;
  2. only then are plugin environments loaded (which starts a manifest worker);
  3. then binding validation;
  4. then evaluator policy.

  A denied or invalid plan therefore never runs plugin code, never starts the application, and writes nothing to the workspace (06-G2).
- **Soft cost needs estimates.** `max_cost_usd` requires `estimated_cost_per_application_call_usd`. With model-backed evaluators it also requires `estimated_cost_per_evaluation_usd`. Unknown cost is never counted as $0.

### Policy (`security/policy.py`)

- The conservative default denies:
  - trusted-local execution;
  - non-loopback HTTP origins;
  - applications with declared effects;
  - non-`native.*` evaluators and model-backed evaluators (data egress);
  - plugin environments, plugin import paths and secrets.
- **Data scope:** when a policy sets `data_roots`, the plan's dataset and application config must resolve inside them. When unset, local files are not restricted. That fits a local developer tool; a shared policy should set it.
- **Plugin import paths** are allow-listed separately from interpreters. They become the worker's `PYTHONPATH`, so they can run arbitrary code inside an allowed interpreter.
- Relative paths in a policy file resolve against the policy file's directory.
- `--trust-local-app` is the user's explicit per-run grant. It is recorded in the approval as `granted_by`.

### Freezing and identity

- `create_run` stores the canonical plan bytes and the application spec as content-addressed artifacts. The manifest records the plan hash, application hash, dataset hash, policy and policy hash, binding hashes and seed. An `Approval` is scoped to a hash of those identities.
- **Resume verifies, before any dispatch:**
  - the plan artifact (path, size, digest, and manifest hash);
  - the application artifact against its manifest hash;
  - the approval scope;
  - evaluator binding hashes (version drift is refused, with rescoring suggested instead).

  It also re-checks plan and evaluator policy.
- The `applications` table stays a catalog keyed by `application_id`. Editing an app config never conflicts with past runs or changes them, because each run reads its own frozen spec.

### Engine (`engine/engine.py`)

- **Work graph.**
  - One execution item per (case, repetition): `exec:{case}:r{rep}`.
  - One evaluation item per (execution, binding): `eval:{case}:r{rep}:{binding_hash[7:23]}`, depending on its execution.
  - Only the loop decides what runs.
  - Queues are bounded by the plan (the selected cases).
  - At most `concurrency.application` executions and `concurrency.evaluation` evaluations are materialized as tasks at once.
- **Single writer.**
  - One SQLite connection, used only on the event-loop thread.
  - Every work-item transition is a compare-and-set (`transition_work_item`), so a stale or duplicate transition cannot overwrite a newer one.
  - One live session per run, enforced by a **run lease** (migration 6, `run_leases`), described below.
- **Crash-consistent order:**
  1. mark the item `running` with its attempt number;
  2. dispatch;
  3. commit the attempt record;
  4. move the item on.

  Attempt ids are never reused.
- **Run control** (`RunController`, thread-safe `request()`; requests made before the engine starts are applied when it does):
  - *pause:* no new dispatch; in-flight work finishes.
  - *cancel:* no dispatch; in-flight work is aborted; unstarted work and waiting retries are recorded `cancelled`.
  - *interrupt:* no dispatch; in-flight outcomes are recorded; unstarted work stays `pending`; the exit code is 130.
  - *second interrupt:* also aborts in-flight work, but the run stays resumable. An aborted effect-free execution returns to `pending`; an ambiguous effectful one is `unknown_effect`; a transient or aborted evaluation returns to `pending`.
- **Events.** `run_events` (migration 5, moved forward from Prompt 08's session tables) records per-run sequenced events: created, session started/ended/aborted/lost, item state changes, retries scheduled, budget exhausted, recovered.

### Budgets (`engine/budget.py`)

- **Separate roles:** application, evaluator and planner. A manual-plan run makes no planner calls, so planner is measured at zero.
- **Reserve before dispatch, reconcile after.** A reservation that never reached the application (binding, spawn or policy errors) is refunded. Every dispatched call counts, failed or not.
- **Hard limits:**
  - `max_application_calls` and `max_evaluator_calls` count in-flight reservations, so concurrency cannot exceed them.
  - `max_wall_seconds`.
  - `max_judge_tokens` is enforced against tokens already reported. In-flight evaluations can overshoot it by at most what `concurrency.evaluation` evaluations use; set evaluation concurrency to 1 for a strict bound. Evaluations that report no tokens are listed as `unenforced`, never counted as zero.
- **Soft limit:** `max_cost_usd` projects known cost plus the plan's estimates for unknown or in-flight calls, and is labelled an estimate everywhere. Model-free evaluators with complete accounting cost a measured zero.
- **Across sessions,** resume replays every committed attempt (calls, known costs, tokens), plus:
  - calls that were dispatched but never committed, counted as spent with unknown cost (recorded in the `recovered` event);
  - per-session wall time (`session_elapsed_seconds` on session ended/aborted events, and on `run_session_lost` for a session that died, taken from its lease heartbeat).

### Retries (`engine/retry.py`)

- **Executions** are retried only on transient failures: timeout, transport, spawn failure, or HTTP 408/425/429/500/502/503/504. Even then, only when nothing could have happened (`none_declared` or `not_dispatched`).
  - An unknown effect is final (`unknown_effect`, "reconcile before retrying").
  - HTTP errors from an application that reports completed effects are not retried.
- **Evaluations** are retried only on `timeout:`, `worker_failed:` and `evaluator_restart_failed:`, and never when the evaluator declares `internal_retries > 0` (no multiplication).
  - Low scores, `not_applicable` and conformance errors are final.
- **Backoff** is exponential, capped and jittered from the run's seed. It honours `Retry-After` (delta-seconds), within the cap.
- `retry.max_attempts` bounds attempts, including across resume.

### Recovery (`services/runs.py`)

- **The lease.**
  - `execute_run` takes the lease (owner token, host, pid, heartbeat) before recovery and re-checks the run status once it holds it. A second session is refused (`LeaseHeld`).
  - The engine heartbeats every 2 s. A lease is stale when its heartbeat is over 60 s old, or its pid is not running on this host.
  - A session that finds its lease taken over stops dispatching, as if interrupted.
  - The lease is released on every exit path that runs Python code.
- **Items left `running` by a dead session:**
  - An execution with a committed attempt is settled from it (retried only while attempts remain).
  - An execution without one counts as a spent call. It is re-dispatched only if the application declares no effects (at-least-once, recorded in the `recovered` event); otherwise it becomes `unknown_effect` and is never repeated automatically.
  - An evaluation with a committed final result is settled; otherwise it runs again. Evaluations cannot affect the application; their earlier spend stays recorded.
- **Planned but never executed.** Cases that were planned but never executed (budget, cancellation) are recorded as `skipped` metric results, `not_executed:*` or `not_evaluated:*`. They stay visible as lost coverage, never silently dropped.

### CLI (`cli/run.py`)

- **Commands:** `plan validate`, `run --plan`, `resume`, `evaluate`, `runs status`.
- **Exit codes:**

  | Code | Meaning |
  |---|---|
  | 0 | complete |
  | 2 | invalid, or a precondition was refused |
  | 3 | incomplete (failed, blocked, cancelled or unknown-effect work) |
  | 4 | policy denied |
  | 130 | interrupted |

- Only `run` creates a workspace. `evaluate` rescores saved executions and never invokes the application; rescores are never new latency samples.

## Consequences and limits

- **No exactly-once guarantee for external effects** (in scope for this prompt). Effect-free work may run more than once after a crash (at-least-once, and every attempt is recorded). Effectful ambiguous work stops at `unknown_effect` for a human to reconcile.
- **Staleness detection is local.** It relies on pid liveness on the same host or a 60 s TTL. Workspaces on network filesystems shared between hosts are not supported (SQLite locking is not reliable there either).
- **Sessions, conversation turns, decision records and pending questions** remain Prompt 08's; only `run_events` and `run_leases` moved forward.
