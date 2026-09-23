# ADR 0002: Runner transports, observation honesty and effect semantics

Status: Accepted
Date: 2026-09-23
Prompt: 03 — Application runners and observation capture

## Context

Prompt 03 introduces the first code that executes user applications (§7, §15, §16). Several
choices affect later prompts (engine retries in 06, reports in 11) or add dependencies, so
they are recorded here rather than only in the phase report.

## Decisions

1. **HTTP client: `httpx` 0.28.x (pinned `>=0.28.1,<0.29`).** Chosen over stdlib
   `http.client` because asyncio cancellation closes the connection (an abandoned request
   does not keep a thread alive), per-phase timeouts, decoded streaming reads (so the
   response cap also bounds decompression) and the `trace` extension used for dispatch
   tracking. The provider SDKs expected in later prompts depend on httpx already. The
   client is created with `trust_env=False` (environment proxies cannot reroute traffic)
   and `follow_redirects=False` (redirects are handled by our policy code).
   Adds six packages to the lock: httpx, httpcore, h11, anyio, certifi, idna.
2. **Transport configuration lives on `ApplicationSpec.transport`** as a discriminated union
   (`CliTransport` | `HttpTransport`) in `core/models.py`. It is optional so application
   records committed before Prompt 03 still load; a runner cannot be built without it.
   Relative paths (`cwd`, `ca_bundle`) are resolved against the config file's directory at
   runner construction and are not rewritten into the spec, so the application hash stays
   portable (§6: "An absolute path alone is not portable identity").
3. **`ExecutionResult` gains `error_kind`, `effect_state`, `correlation_id`** (all
   optional). Schema version stays `1.0.0`: the change is additive, historical records
   validate unchanged and nothing is reinterpreted. The first breaking change must bump the
   version with a migration.
4. **Effect semantics (`EffectState`).** Runners never retry. Each attempt records
   `none_declared` (app declares no effects), `not_dispatched` (provably never reached the
   app: binding/size/policy failure, spawn failure, connect failure, TLS handshake
   failure), `completed` (the app finished or responded) or `unknown` (dispatched, then
   timed out, cancelled or lost). HTTP dispatch is detected from httpcore's
   `send_request_headers.started` trace event, not inferred from the exception type. For a
   CLI app any kill after spawn is `unknown`. The engine (Prompt 06) must treat `unknown`
   on an effectful app as requiring intervention or reconciliation, never a blind retry.
5. **Observation honesty.** Only capabilities declared in `output_binding` are read. Each
   capability records `state` (`observed`/`declared`/`unknown`) and `detail` (`present`,
   `empty`, `missing`, `invalid`, `truncated`, `not_bound`). Values the application reports
   are labelled "self-reported by the application". Missing retrieval, usage and cost stay
   `None`, never `0`. `ExecutionResult.tool_events` is a legacy non-null collection and
   remains `[]` when unknown for schema compatibility; consumers must use its completeness
   entry and only treat `[]` as an observed empty value when its state is `observed` and its
   detail is `empty`.
6. **Leakage boundary is structural.** Runners receive only `AppInputEnvelope`
   (`BenchmarkCase.application_input_projection()`); input bindings can only address that
   envelope. The CLI child environment is an allow-list (`inherit_env`) plus explicit `env`
   and `secret_env`, so harness/evaluator credentials are never inherited. Resolved secret
   values are redacted from every capture before parsing and persistence.
7. **Process-tree cleanup.** POSIX: new session + `killpg(SIGKILL)`. Windows: Job Object
   with `KILL_ON_JOB_CLOSE`. The tree is killed at the end of every invocation, including
   successful ones, so no descendant outlives an invocation. Child exit is detected via
   `returncode`, because `asyncio`'s `Process.wait()` blocks until every pipe closes (a
   descendant that inherited stdout would otherwise make a finished app look hung). Pipes
   are always read to EOF (excess discarded) so asyncio releases subprocess transports.
8. **Trusted-local mode is explicit.** Executing a CLI app requires `trusted_local=True`
   (`--trust-local-app`). A subprocess is not a sandbox; hostile-code isolation is
   unsupported in MVP (§16).
9. **Endpoint policy.** Default allowed endpoint is the origin of `url`. Plain `http://` is
   loopback-only unless `allow_plaintext_http`. URLs with credentials or dot segments are
   refused. Only 307/308 redirects are ever followed (and only when enabled), each hop
   re-checked against the policy.

## Consequences

- Prompt 06 consumes `effect_state` and `error_kind` for its retry policy; it must not
  re-derive them from error strings.
- Known limits: on Windows a descendant spawned in the instant between process creation and
  job assignment is not contained; on POSIX a descendant calling `setsid()` escapes the
  group. The POSIX path is exercised by CI on `ubuntu-latest`, not on the Windows
  development machine used for Prompt 03.
- Redaction matches the raw and JSON-escaped secret, plus a trailing partial secret in a
  capture cut by a size cap. A secret the application re-encodes (e.g. base64), or one
  shorter than 4 characters, is not detected.

## Changes after independent review (2026-09-23)

An adversarial review of the Prompt 03 diff led to these changes, each with a regression
test:

- Application JSON is parsed by one hardened function (`runners.bindings.parse_app_json`):
  deep nesting, >4300-digit integers, NaN/Infinity, lone surrogates and nesting beyond 64
  levels become `invalid_output` instead of crashing the invocation or the result commit.
- Stdout already read is kept when a descendant outside containment holds the pipe; pipes
  and the subprocess transport are then closed explicitly.
- 502/503/504 on an effectful app record `effect_state=unknown` (a gateway answered, not
  the application).
- Secret headers are not sent to a different origin on a followed redirect.
- JSON validation uses `allow_nan=False` on its validation round-trip because Python parses
  a legal exponent such as `1e999` as infinity; explicit NaN/Infinity rejection alone did
  not catch that overflow.
- CLI request artifacts are redacted separately from the original stdin bytes, in case
  app-visible input happens to equal a resolved application secret. HTTP response content
  type metadata is redacted along with the captured response headers and body.

Accepted limits (documented, not changed):
- POSIX `killpg` after a normal exit targets the group by number; if the group is already
  empty, a new group could reuse the ID in that brief window. Revisit when Prompt 06 runs
  invocations concurrently (e.g. by killing the group before the child is reaped).
- The response-size cap applies to decoded chunks as they arrive, so a single compressed
  chunk can expand beyond the cap in memory before being rejected.
- A malformed redirect `Location` is rejected inside httpx and recorded as
  `transport`/`unknown` (conservative) with the status missing.
