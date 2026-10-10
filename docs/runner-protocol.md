# Connecting an application: runner protocol

aibench talks to your application through one of five transports:
- a command-line program (`cli`);
- an HTTP endpoint (`http`);
- a Python function (`python`);
- a container (`container`);
- an OpenAI-compatible chat endpoint (`openai_compatible`).

You describe it in an application config file (JSON, or YAML if PyYAML is installed).
Working examples live in `examples/apps/`. The runner lifecycle is the same for every
transport:

`describe → prepare → healthcheck → invoke → reset → close`.

Check a config without running anything:

```
aibench app describe examples/apps/http_rag.app.json
```

Invoke a few cases once each and record the results (a developer smoke check: no plan,
retries or scoring):

```
aibench app smoke examples/apps/cli_chatbot.app.json --dataset examples/datasets/chatbot.valid.jsonl --trust-local-app
aibench runs show <run_id>
```

## What your application receives

Only the app-visible part of each case:

```json
{"case_id": "rag-001", "input": "What is your refund policy?", "fixtures": {}}
```

`fixtures` contains only fixtures marked `"app_visible": true`. Reference answers,
reference context and other fixtures are never sent.

To reshape the request, map fields with JSON Pointers into a static template:

```json
"input_binding": {"template": {"top_k": 2}, "fields": {"/question": "/input"}}
```

## CLI applications

```json
{
  "application_id": "my-bot",
  "runner": "cli",
  "target": "bot.py",
  "transport": {"kind": "cli", "argv": ["python", "bot.py"], "timeout_seconds": 30}
}
```

- The request arrives as JSON on **stdin**. Case text is never put into `argv` and no shell
  is used.
- Print a JSON object on **stdout**, e.g. `{"output": "..."}`. For a legacy app that prints
  plain text, set `"output_mode": "text"`. Diagnostics go to **stderr**.
- Exit code 0 means success. Anything else is recorded as a failure with stderr kept.
- `cwd` is relative to the config file (default: its directory).
- The environment is **not** inherited, apart from a short allow-list (`PATH`, `TEMP`,
  `HOME`, ...). Pass values with `"env": {"NAME": "value"}` and secrets with
  `"secret_env": {"API_KEY": "env:MY_APP_KEY"}`. `AIBENCH_CORRELATION_ID` is always set.
- Limits: `max_stdout_bytes` (default 1 MiB; exceeding it stops the app),
  `max_stderr_bytes` (default 64 KiB; the excess is dropped).
- On timeout, cancellation or completion, the app **and every process it started** are
  terminated.
- Local execution needs `--trust-local-app`. A subprocess is not a sandbox; run only code
  you trust.

## HTTP applications

```json
{
  "application_id": "my-rag",
  "runner": "http",
  "target": "https://rag.internal/answer",
  "transport": {
    "kind": "http",
    "url": "https://rag.internal/answer",
    "secret_headers": {"Authorization": {"ref": "env:RAG_TOKEN", "prefix": "Bearer "}},
    "healthcheck_url": "https://rag.internal/health"
  },
  "output_binding": {"output": "/answer"}
}
```

- aibench sends a JSON `POST` (or `PUT`) with an `X-Request-ID` correlation header.
- TLS certificates are verified. For a private CA, set `ca_bundle`.
- Only the origin of `url` is allowed unless you list `allowed_endpoints`. Plain `http://`
  works only for localhost unless you set `allow_plaintext_http: true`.
- Redirects are refused. With `follow_redirects: true`, only 307/308 redirects to allowed
  endpoints are followed.
- A non-2xx status, invalid JSON or a response over `max_response_bytes` is recorded as a
  failure.
- `reset_url` (optional) is called to reset server state between cases.

## Reporting more than the output

aibench records retrieval, tool calls, token usage and cost **only if your app reports them
and you declare where**:

```json
"output_binding": {
  "output": "/answer",
  "retrieved_context": "/retrieved",
  "retrieved_context_item": "/text",
  "tool_events": "/tools",
  "usage": "/usage",
  "cost": "/cost",
  "world_state": "/world_state"
}
```

`retrieved_context` points to a list; each item is a string, or `retrieved_context_item`
selects the text from it. If an item (or its selected field) is itself a **list of
strings**, they are flattened in order, which fits an app that groups passages per source
(`"retrieved_context": "/references", "retrieved_context_item": "/content"`).

`world_state` is the state of the application's test world after the case (see
[Test worlds](#test-worlds)).

Anything not declared is tagged **unknown** in `observation_completeness`. Retrieval, usage
and cost values remain null. The existing `tool_events` field is a non-null collection for
schema compatibility, so its empty value must be ignored unless its completeness entry says
`observed` with detail `empty`. `aibench app describe` shows which capabilities are
observable before you run anything.

## Side effects

If your app changes anything outside itself (bookings, emails, database writes), set
`"effects": "reversible"` or `"irreversible"`. Every attempt then records whether the effect
is known:

| `effect_state` | Meaning |
|---|---|
| `none_declared` | The app declares no side effects |
| `not_dispatched` | The request never reached the app, so retrying is safe |
| `completed` | The app finished or responded |
| `unknown` | The request was sent, then timed out or was cancelled. It may have taken effect |

aibench never retries automatically. An `unknown` result on an effectful app needs you to
check the app's state before trying again.

## Python functions

A function `module:function`, or `path/to/file.py:function`, relative to the config file:

```json
"runner": "python",
"transport": {"kind": "python", "callable": "python_chatbot.py:respond",
              "python": "python", "paths": ["."], "timeout_seconds": 30}
```

- **How it runs.** The function runs in a fresh process of the interpreter you name
  (`python`: your application's environment) through a small standard-library shim. So the
  CLI protocol's guarantees apply: JSON in and out, bounded output, a timeout that kills the
  whole process tree, and an allow-listed environment. The application's environment does
  not need aibench installed.
- **Input and output.** The function receives the bound input. It may be `async`. If it
  returns an object, that is the response document the output binding reads. Any other
  value becomes `{"output": value}`.
- **Failures.** An exception is an application failure (`nonzero_exit`), with its
  traceback in the stderr capture.
- **Encoding and output.** Input and output are UTF-8 whatever the platform's locale.
  Anything the function prints goes to the stderr capture, never into the result.
- **Trust.** Like a CLI app, a Python function needs trusted-local mode
  (`--trust-local-app` or `allow_trusted_local`). It runs with your permissions.

Example: `examples/apps/python_chatbot.app.json`.

## OpenAI-compatible endpoints

A chat-completions endpoint as the application under test:

```json
"runner": "openai_compatible",
"transport": {"kind": "openai_compatible", "base_url": "https://api.example.com/v1",
              "model": "my-model", "api_key": "env:MY_KEY",
              "system_prompt": "You answer support questions.",
              "parameters": {"temperature": 0}, "tools": []}
```

- **Requests.** Each case is one `POST {base_url}/chat/completions`. A string input is the
  user message. An object input with `messages` is the conversation.
- **What is observed.** The message content (the output), the `usage` the endpoint
  reports, and the `tool_calls` it asks for (as `tool_events`). A tool call is a request by
  the model, never an executed effect. Cost and the application's internals are unknown.
- **Streaming.** Set `parameters.stream` to `true` to consume server-sent events. The runner
  requests reported usage by default with `stream_options.include_usage`; set it to `false`
  for providers that do not support that option. Reports expose TTFT, inter-content-delta
  timing, output tokens per second when usage is available, and stream completion integrity.
  Incomplete streams are application failures and are excluded from evaluation.
- **Policy.** The endpoint's origin must be loopback or listed in `allowed_http_origins`,
  and the key reference in `allowed_secret_refs`.
- **Not an evaluator.** This transport is unrelated to any OpenAI evaluator plugin.

For a local stand-in with no paid calls, see `examples/apps/openai_stub.py`.

## Containers

One container per case, from an image pinned by digest:

```json
"runner": "container",
"transport": {"kind": "container",
              "image": "python@sha256:2f17fc04...06a9",
              "argv": ["python", "/app/app.py"],
              "mounts": [{"source": "container_app", "target": "/app"}],
              "memory_mb": 256, "cpus": 1, "pids_limit": 64, "network": "none"}
```

Every container runs with:
- a non-root user and group (`65534:65534` by default; uid or gid 0 is refused, however
  it is written);
- a read-only root filesystem, and read-only bind mounts (writable space is `tmpfs`,
  `/tmp` by default);
- `--cap-drop ALL` and `no-new-privileges`;
- memory (with no extra swap), CPU and process limits;
- no network unless `network` is `bridge`, which the policy must allow
  (`allow_container_network`). Bridge network access is unrestricted egress.

Some things are refused or never done:
- the host-installed `docker` client is fixed; app config cannot select a host executable
  or override host-sensitive engine variables such as `PATH`, `LD_PRELOAD` or `DOCKER_HOST`;
- each invocation uses `--pull=never`, so a missing image is refused even if it disappears
  after the preflight check;
- mounting the container engine's socket is refused. The check covers the socket, its
  directory, and any mount whose resolved path exposes it;
- container paths (mount targets, `workdir`, `tmpfs`) must be plain absolute paths:
  letters, digits, `_ . -` and `/`, with no `.` or `..` segments. A host path containing
  a comma is quoted, not split;
- nothing is pulled automatically: run `docker pull IMAGE@sha256:...` first.

The policy must list the image in `allowed_container_images`, for example
`"python@sha256:*"`. A container is a configured sandbox, so it does not need
trusted-local mode.

Two limits:
- **Timeouts.** On a timeout the container is removed (`docker rm -f`), not just its client
  process.
- **Isolation.** This is stronger environment control, not hostile multi-tenant isolation:
  containers share the host kernel.

Example: `examples/apps/container.app.json`.

## State between cases

Applications that keep state (a session store, a database, a test world) declare how it is
reset:

| `reset_policy` | What aibench does |
|---|---|
| `per_case` (default) | Resets before every case, if the application has a reset hook. Without one, a process-per-case transport starts fresh, but state kept outside the process is not reset |
| `per_episode` | Cases sharing a `group_id` form an episode. The app is reset before the episode's first turn, and the turns run in dataset order, sharing state |
| `shared` | Never resets. The report says the state is shared |

**Reset hooks:**
- `http`: `reset_url` (POSTed the test world's seed, or `{}`);
- `cli`: `reset_argv` (the seed as JSON on stdin);
- `python`: `reset_callable` (called with the seed).

A reset that fails blocks the case: it is never run on unknown state. Each reset is
recorded as an `app_reset` run event. A reset is bounded by the runner's 10-second
lifecycle timeout. Stopping or cancelling the run abandons a reset in progress, and the
case is cancelled.

**Rules for a stateful application.** An application with a reset hook keeps state its
cases share, so its plan must use `concurrency.application: 1`. In an episode:
- a turn is never retried, because the earlier attempt changed the state
  (`retry.max_attempts: 1`);
- a turn that fails blocks the rest of its episode;
- after an interruption, the episode's remaining turns are blocked on resume, because
  the state its earlier turns built cannot be restored;
- a case selection must keep each episode's turns from the first one: selecting a later
  turn without the turns before it is refused.

Containers do not support `per_episode`.

## Test worlds

A test world is a named starting state the application's reset hook accepts: a test double
or an ephemeral environment, never production. The application declares its worlds:

```json
"test_worlds": {
  "two-seats": {"seed_file": "worlds/two-seats.json", "description": "BA117 with 2 seats"}
}
```

A seed must be a JSON object or list. A `seed_file` must be inside the application
config's directory. A `shared` application is never reset, so it cannot select a world.

A plan selects one with `"test_world": "two-seats"`. In a conversation, use
`/world two-seats`, or ask the assistant. The policy must approve it
(`allowed_test_worlds: ["my-app:two-seats"]`). The seed is frozen with the run, so later
edits to the file change nothing a run uses. `aibench app describe --policy FILE` (or
`/app` in chat) lists the worlds and which are approved.

## Tool calls and outcomes

Report tool events as a list. These shapes are read:

```json
{"name": "book_flight", "arguments": {"flight": "BA117"}, "status": "ok", "result": "B1"}
{"type": "function", "function": {"name": "book_flight", "arguments": "{\"flight\": \"BA117\"}"}}
```

`status` is `ok`, `error`, `denied` or `requested`. The OpenAI tool-call shape is always
`requested`: asked for, not executed. Three separate native metrics judge an agent, so a
correct tool name can never stand in for a failed effect:

| Metric | Question | Case fields |
|---|---|---|
| `native.tool_calls` | Were the expected tools called, by name? | `reference.tools` |
| `native.tool_outcomes` | Did each required call happen with valid arguments, succeed, and stay within the allowed tools? Categories: `succeeded`, `not_called`, `argument_violation`, `not_executed`, `denied`, `failed`, `unauthorized_attempt` | `expectations.tool_calls`, `expectations.allowed_tools` |
| `native.final_state` | Does the world's state after the case satisfy every assertion? | `expectations.final_state` |

Argument constraints and state assertions use:
- `equals` (or a bare value);
- `not_equals`, `in`, `matches` (a regular expression);
- `min`, `max`, `length`;
- `present`, `absent`.

A worked example with a deliberately faulty agent is `examples/agent_world/`
(`aibench run --plan examples/agent_world/plan.json --policy examples/agent_world/policy.json`,
with `python examples/apps/booking_world.py` running).

## Traces

If your application exports OpenTelemetry traces, aibench can attach them to a run's
executions:

```
aibench traces import RUN_ID traces.json     # OTLP/JSON: one document or JSON Lines
aibench traces show RUN_ID
```

A trace is matched to an execution by a span attribute `aibench.correlation_id`, or by
`http.request.header.x-request-id`. The HTTP runner sends the execution's ID in
`X-Request-ID` by default (`transport.correlation_header`; a renamed header must still be
exported as the `aibench.correlation_id` attribute to match). Unmatched traces are kept and counted, and the raw file is stored as a restricted
artifact. Usage is read from `gen_ai.usage.input_tokens` / `output_tokens` on the lowest
spans that report it. A parent span that repeats its children's totals is not counted
twice. A trace that is sampled out, has dropped spans, or is missing its root or a
parent is marked **partial**, and its usage is reported as a lower bound. So is a trace
with two different spans under one ID, or with looping parent links.

Importing a file again, or a file that has grown, never counts a trace twice. A trace
split across several files is merged and normalized again as one.
`examples/apps/traced_app.py` is a worked example.

## Caching and rate limits

Caching is off by default. A plan can opt in:

```json
{"cache": {"executions": true, "evaluations": true}}
```

- A hit requires everything that could change the result to be unchanged:
  - **executions:** the case input, the repetition, the application config (including
    `revision`), source files under the configured working directory and Python import paths,
    the values of inherited and explicitly referenced secret environment variables (hashed,
    never stored), the aibench version, the test world's seed and the policy;
  - **evaluations:** the output, the whole case (references too), the evaluator, its
    version and parameters, the plugin version, the policy and the repetition.
- **Code aibench can't see.** For an HTTP endpoint, an entry point that isn't a local
  source file, or a container with host bind mounts, declare `revision` (or
  `environment_digest`) in the application config and change it whenever the application or
  mounted code changes. Execution caching is refused until you do.
- **Where caching is refused.** Execution caching is refused for applications with effects
  and no test world, for per-episode state and for shared state.
- **What a hit is.** A hit is a new record that names the run it came from. It never counts
  toward latency or as an independent repetition. `aibench compare` refuses to compare a
  run whose executions came from the cache.
- `aibench cache list` shows the entries, and `aibench cache clear [--kind execution|evaluation]`
  empties them.

Quotas tell aibench about your provider's limits. They apply to the application, or to
evaluators matched by a glob:

```json
{"quotas": [
  {"name": "app", "applies_to": "application", "max_in_flight": 4,
   "requests_per_second": 10, "burst": 2},
  {"name": "judge-provider", "applies_to": "evaluator:deepeval.*", "max_in_flight": 2}
]}
```

- **Before dispatch.** Work over a quota waits in the queue.
- **"Slow down" answers.** An HTTP 429 or 503 from the application pauses everything under
  that quota for the response's `Retry-After` (at most `max_backpressure_seconds`, default
  60 s), or the quota's `backoff_seconds` (default 1 s). The call is then retried under the
  plan's retry policy.
- **Evaluator quotas.** They limit work in flight and the start rate. They don't react to
  a judge provider's 429, because evaluators retry internally.
- **Controls.** Pause and cancel still work while work is throttled.
