# ADR 0014: Richer runners and agent outcome contracts

Status: Accepted
Date: 2026-09-24
Prompt: 15 — Richer runners and agent outcome contracts

## Context

§7 lists Phase 2 transports: a Python callable, a container and an OpenAI-compatible
endpoint. For tool-using applications it asks for "tool attempts, outcomes, world state if
supplied". It also says multi-turn episodes keep state within the episode and reset
between episodes. §16 asks containers to run:
- non-root;
- with read-only source mounts and restricted writable directories;
- with resource limits and bounded network egress;
- without the host Docker socket.

It also warns that this is not hostile multi-tenant isolation.

Before this prompt, `ApplicationSpec` already had a `reset_policy` field and HTTP had a
`reset_url`. But the engine never reset anything, and nothing evaluated tool events.

Prompt 14 (a second evaluator ecosystem and comparisons) is Prompt 15's nominal
prerequisite, and is being implemented in parallel by another agent. Nothing here consumes
Prompt 14's contracts, and Prompt 14's files were not changed.

## Decisions

### Transports reuse the two existing protocols (15-T1)

- **Python callable** (`runner: python`). The callable runs in a fresh process of the
  application's own interpreter, through a standard-library shim
  (`runners/python_shim.py`) run by path. `PythonRunner` is a `CliRunner` with a derived
  CLI transport, so it inherits the tested guarantees:
  - JSON in and out, and bounded output;
  - a timeout that kills the process tree;
  - an allow-listed environment and redaction.

  **Rejected: in-process calls.** A synchronous function can't be timed out or cancelled,
  and it would share the harness's memory and credentials. It needs trusted-local mode.
- **OpenAI-compatible endpoint** (`runner: openai_compatible`). An `HttpRunner` with a
  derived HTTP transport (`{base_url}/chat/completions`, a bearer secret header) and a
  chat-payload builder. It inherits the endpoint policy, size bounds, dispatch tracking and
  redaction.
  - **Default bindings.** `ApplicationSpec.effective_output_binding()` reads the answer,
    `usage` and `tool_calls` by default, so the registry's applicability check and the
    runner agree.
  - **Tool calls are requests.** A model's tool calls are recorded as `requested`, never
    as executed effects. Cost stays unknown.
- **Container** (`runner: container`). A `CliRunner` that builds a hardened `docker run`
  per invocation, from the frozen `ContainerTransport` only:
  - an image pinned by digest (validated), which must already be present (nothing is
    pulled, including if it disappears after the preflight check (`--pull=never`);
  - the engine client is the host-installed `docker` command; app config cannot select a
    host executable or override engine-sensitive host environment variables;
  - uid and gid 0 refused (default `65534:65534`), `--read-only` and `--cap-drop ALL`;
  - `no-new-privileges`, tmpfs-only writable space, and read-only bind mounts only;
  - a refusal to mount the engine socket, and strict container-path syntax (see
    Changes after independent review);
  - memory without extra swap, CPU and pids limits;
  - `--network none` unless `bridge` is asked for and the policy allows it;
  - secrets passed as `-e NAME`, never as values on the command line.

  Every invocation names its container and removes it afterwards (`rm -f`), because
  killing the engine client does not stop a container.
  - **Policy.** It is a configured sandbox, so it doesn't need trusted-local mode. Instead
    the policy approves images (`allowed_container_images`) and network
    (`allow_container_network`).
  - **Limits.** Per-episode state isn't supported (a new container per invocation).
  - **Rejected: `docker exec` into a long-lived container.** It is more state to reconcile,
    and not needed by any gate.

### State between cases (15-T2, 15-G2)

- **Reset hooks.** HTTP `reset_url` (existing), CLI `reset_argv` and Python
  `reset_callable` (new). Each receives the selected test world's seed. A fresh process or
  container per invocation is *not* a reset hook: it does not reset external state.
- **Reset modes.** The engine resets according to a mode frozen in the run manifest
  (`services/runs.reset_mode`):
  - **`per_case`:** before every attempt, when there is a hook or a selected world;
  - **`per_episode`:** before an episode's first turn;
  - **`none`:** for a shared app, or no hook and no world.

  Each reset is an `app_reset` event. A failed reset raises `ResetFailed`. The case is then
  `blocked` (`reset_failed: ...`) without being invoked, and settles as not dispatched in
  the ledger.
- **Episodes.** An episode is the cases sharing a `group_id`, in dataset order. Each
  (episode, repetition) runs contiguously.
  - A turn runs only if the previous turn succeeded *in this session*. Otherwise it is
    blocked: `episode_broken` if the previous turn failed, `episode_interrupted` if the
    run was interrupted between turns (the built state can't be restored). Replaying into
    unknown state would produce meaningless results.
  - Turn retries are refused at compile time (`retry.max_attempts` must be 1): a retry
    would replay into mutated state.
- **Concurrency.** An application with a reset hook keeps state its cases share, so
  application concurrency must be 1. Compile refuses anything else, rather than silently
  running cases on top of each other's resets.
- **Test worlds.** The application declares `test_worlds` (a named `seed` or
  `seed_file`), and a plan selects one with `test_world`. Compile requires three things:
  - the world is declared;
  - the policy approves it (`allowed_test_worlds`, as `application_id:world` globs);
  - there is a reset hook to load it.

  The seed is canonicalized, hashed and frozen as a run artifact. Resume verifies it, like
  the plan. A later edit of the seed file changes nothing a run uses, and tampering with the
  artifact is refused.
- **World state.** A new optional observation, `world_state` (output binding, and
  `ExecutionResult.world_state`), holds the test world's state after the invocation, as
  the application or its test double reports it. Additive: older records load unchanged.
- **Reports.** They carry `application.state` (reset policy, hook and mode, the world ID
  and seed hash, and reset counts by status). This is part of the §7 reproducibility
  manifest.

### Outcome contracts (15-T2, 15-G3)

There are three separate native metrics, so a correct tool name can't mask a failed
effect:
- **`native.tool_calls`** (boolean): names only, using the existing `ToolExpectation`
  match modes.
- **`native.tool_outcomes`** (category): whether each `expectations.tool_calls` entry
  happened with arguments satisfying its constraints and succeeded, and whether any tool
  outside `expectations.allowed_tools` was attempted. The categories distinguish:
  - `argument_violation`;
  - `not_executed` (only requested);
  - `denied` (the world refused it);
  - `failed`;
  - `unauthorized_attempt`;
  - `not_called`.
- **`native.final_state`** (boolean): assertions over `world_state`, reporting each
  assertion.

One small constraint language serves both argument constraints and state assertions. An
unknown assertion key is an evaluator error, not a silent equality check. Tool events are
normalized from common shapes, and the OpenAI shape is always `requested`.

Rejected: a single combined "agent success" score. It would hide which part failed and
invite averaging.

### Chat (15-T3)

- **Describing the application.** `services/applications.describe_application` is pure:
  nothing starts. It is shared by `aibench app describe`, `/app` and the assistant's
  `describe_application` tool, and returns:
  - the runner and its isolation;
  - the reset mode, in plain words;
  - what is observed, and what each missing observation means for checks;
  - the declared worlds, with their policy approval.
- **Selecting a world.** A validated plan change: `PlanPatch.test_world` or
  `clear_test_world`, then `SessionChoices`, then the draft, then the plan. The model-free
  `/world NAME|none` and the assistant's `propose_plan_patch` use the same path:
  - an undeclared world is rejected when the patch is applied;
  - an unapproved one produces a draft the execution gate won't run
    (`missing_permission`);
  - the assistant may select only a world named in the user's message (the existing
    grounding rule, extended).

  The plan card shows the selected world.
- **Stateful drafts.** A draft for a stateful app gets application concurrency 1, and no
  retries for episodes (`planning/service.state_limits`), so drafts meet the compile rules.
- **Planner concept.** A new concept, `task_outcome`, lets a planner map outcome
  objectives to `tool_outcomes` and `final_state`.

## Changes after independent review

An adversarial review ran reproductions against every change, including a real container
on the Docker engine. It confirmed three major and five minor findings, plus one test that
Prompt 15 had broken. All are fixed. A later commit review found two additional container
gaps in host-engine selection/environment and image-pull behavior; those also have regression
coverage. Tests are in `tests/test_runner_review_regressions.py`.

- **Major:**
  1. **Container hardening could be bypassed.**
     - uid `"00"` passed a string comparison and ran as root, and a root group was
       accepted.
     - A mount target containing `,source=/var/run/docker.sock` added a second field to
       the engine's `--mount` value. The reviewer reached the engine API from inside the
       container.
     - The socket guard was a substring test.
     - A comma in a host path broke the mount.

     Fixes: uid and gid are parsed as numbers and 0 is refused; container paths have a
     strict syntax; the socket check normalizes paths, covers the socket's directory, and
     runs again on the resolved path in `prepare`; the source field is quoted as CSV.
  2. **The application config could affect the host engine client.** It could choose an
     arbitrary executable for `engine`, or set variables such as `PATH`, `LD_PRELOAD` or
     `DOCKER_HOST` in the client's environment. The engine is now fixed to the host's
     `docker` command, and engine-sensitive names are refused in both plain and secret app
     environment maps.
  3. **A preflight/remove race could pull an image.** Docker's default run policy may pull
     a missing image after `image inspect` succeeded. Every invocation now passes
     `--pull=never`.
  4. **The Python shim used the locale encoding.** On Windows that is cp1252, so
     non-ASCII input was garbled and non-ASCII output failed. The shim now reads and writes
     UTF-8 bytes. Separately, the callable's prints go to stderr, and the shim's own
     directory is dropped from `sys.path`.
  5. **A shared application could select a test world.** The world was never loaded, yet
     the report named its seed. Compile now refuses it.
- **Minor:**
  - A selection that drops earlier episode turns is refused at compile time; before, the
    later turns silently ran from the seed.
  - A seed must be a JSON object or list, and a `seed_file` must be inside the
    application's directory.
  - `present: false` now means absent. Booleans never equal numbers (`same`).
  - Cancelling during a reset abandons it, and the case is recorded as cancelled.
  - `tests/test_conversation.py`'s expected tool set now includes `describe_application`.
- **Recorded, not changed:** a reset is bounded by the fixed 10-second lifecycle timeout.
  A per-transport reset timeout is left until an application needs one.

## Consequences

- **Schema, additive.** `RunnerKind` gains three values. The transport union gains three
  models. `ApplicationSpec` gains `test_worlds`. `ExecutionResult` gains `world_state`.
  `ExecutablePlan` gains `test_world`. `SessionChoices` and `PlanPatch` gain world fields.
  `ExecutionPolicy` gains three approval fields, all denying by default. Existing records,
  plans and policies load unchanged.
- **Behaviour change.** An HTTP app that declared a `reset_url` is now actually reset
  before every case, and its plans need application concurrency 1. Before this, the URL
  was silently unused. One example app (`effect_counter.app.json`) declares one; no
  shipped plan used it with concurrency above 1.
- **`BaseRunner.reset`** takes an optional seed. `CliRunner` and `HttpRunner` accept a
  derived transport. `CliRunner` gains an `_argv_for(ctx)` hook.
- **Evidence limits.**
  - The container runner was exercised with Docker Engine 29.7.2 (Docker Desktop, linux/amd64) on
    Windows 11 only.
  - The OpenAI-compatible runner was exercised against a local stub only; no live provider
    was called.
  - Containers are not a hostile-code boundary.
