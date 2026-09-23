# Connecting an application: runner protocol

aibench talks to your application through one of two transports. You describe it in an
application config file (JSON, or YAML if PyYAML is installed). Working examples live in
`examples/apps/`.

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
  "cost": "/cost"
}
```

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
