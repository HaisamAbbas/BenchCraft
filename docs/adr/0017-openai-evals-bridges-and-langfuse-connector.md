# ADR 0017: OpenAI Evals bridges and the Langfuse connector

Status: Accepted
Date: 2026-09-24
Prompt: 17 — OpenAI evaluation bridges and one platform connector

## Context

§11 names two different "OpenAI Evals":
- the open-source framework (openai/evals), with a registry and a completion-function
  interface;
- the hosted Evals API, with asynchronous eval runs.

It asks for separate plugins. §9 separates evaluators from remote jobs, dataset
connectors, trace importers and result exporters. It also says remote adapters persist
external IDs before polling. §18 asks for one demand-selected platform connector.

**Concurrent work.** Prompt 18 was implemented in parallel by another session. It took
ADR 0016 and migration 9, and recorded a Prompt 17 subset (`17-P18`) that it needed. This
ADR and migration 10 are Prompt 17's own. `17-P18` is unaffected.

## Decisions

### Two plugins, never one flag (17-G1)

| | `aibench-openai-evals-oss` | `aibench-openai-evals-api` |
|---|---|---|
| Upstream | `evals==3.0.1.post1` (the framework) | `openai==3.19.2` (the official SDK) |
| Metrics | `openai_evals_oss.{match,includes,fuzzy_match,json_match}`, `consumes=recorded_outputs` | `openai_evals_api.criterion`, `consumes=remote_job` |
| Network | none | the configured Evals API URL |
| Commands | `aibench openai-evals-oss run` | `aibench openai-evals-api submit/status/resume/cancel/fetch/jobs` |

Each has its own environment. The hosted environment has no `evals` installed.

### openai/evals: an allowlist, run unchanged (17-T1)

- **Allowlist.** Only the basic single-request evals are supported: `Match`, `Includes`,
  `FuzzyMatch` and `JsonMatch`. Their verdict is the upstream `match` event. Model-graded,
  solver, multi-turn and tool evals are refused by name.
- **Minimal dependencies.** `evals` is installed without its declared dependencies
  (TensorFlow, Playwright, Snowflake and more), plus the tested set the allowlisted
  modules import (`requirements-lock.txt`).
- **Import-time client.** `evals.registry` builds an OpenAI client when imported. The
  adapter sets a placeholder key and a closed loopback base URL first, so no call can
  leave the machine.
- **Recorded replay.** The replay completion function answers only when the eval's
  request equals the recorded application input (strings exactly; chat messages as
  canonical JSON), and only once per sample. Anything else fails as
  `unsupported_dynamic_request` or `unsupported_follow_up`. It is never answered with the
  recorded output.
- **Live bridge (delegated execution).**
  - The upstream eval runs in the plugin worker. Its completion function asks the harness
    for each answer, over a JSON-lines protocol.
  - First, the worker reports the exact request each sample will make (a few-shot
    expansion included). Each sample becomes a case with that request as its input.
  - The harness invokes the application once per sample through its runner, and records
    the execution in a run labelled `delegated_suite`.
  - The recorded outputs are then scored by the same replay evaluators, in an ordinary
    scoring pass. Parameters that shape the prompt are left out there, because the case
    input already carries their effect. A disagreement between the live and replay
    verdicts is reported.
  - **Rejected: the worker calling the application itself.** The harness must own the
    runner, the policy and the recording.
- **Upstream quirk.** `blobfile` does not read Windows drive paths. Upstream data files
  are passed as paths relative to the worker's private working directory.

### Hosted Evals API: stored outputs only, reconcile never assume (17-T2, 17-G2, 17-G3)

- **The contract.** The contract was checked against the generated types of the pinned SDK.
  - An eval with a `custom` item schema (`aibench_case_id`, `input`, `output`,
    `reference`).
  - A run whose data source is always `jsonl` with `file_content` items built from
    recorded executions.
  - Graders: `string_check`, `text_similarity`, and `label_model` (a model judge; it
    needs `allow_model_evaluators`). Templates may reference only `{{item.*}}`.
  - Refused before sending: any `{{sample.*}}` template, and a `completions` or
    `responses` data source. These would have the service generate a replacement
    answer. The worker checks again at the moment of sending.
  - **Deferred.** A separate model-generation mode is not implemented, and is reported as
    unsupported.
- **Order.**
  1. Plugin environment, destination (`allowed_egress_origins`) and key reference are
     approved before any plugin code starts.
  2. The requests are built and checked by the plugin, with no network.
  3. They are stored, with a fingerprint, as a restricted artifact, and the job row is
     committed.
  4. Only then is anything sent.

  Poll, resume, cancel and fetch re-check the policy each time, because it may have
  changed since submission.
- **No hidden retries.** SDK retries are off (`max_retries=0`), because a retried POST
  could create a duplicate. Failures are classified:
  - `ambiguous`: timeout, dropped connection or 5xx;
  - `rejected`: 4xx;
  - `rate_limited`: 429;
  - `auth`: 401/403.
- **Ambiguity.** An ambiguous create leaves the job `eval_unknown` or `run_unknown`.
  - `resume` lists the remote evals or runs, and adopts the one carrying this job's ID in
    its metadata.
  - If none is found, nothing is resent. The API documents no idempotency guarantee, so
    resending needs `--resend`, which is recorded in the job's history.
  - A second job with the same fingerprint needs `--allow-duplicate`.
- **Mapping (17-G3).** Output items are fetched page by page (cursor `after`). Each maps
  to a case through its uploaded `aibench_case_id`.
  - A repeated item ID is ignored and counted.
  - An unknown case ID is reported, never attached.
  - Two items for one case make that case's result an error. Neither is trusted.
  - A case with no item (a failed or cancelled run) is recorded as skipped, so the
    coverage loss stays visible.

  Results enter the run as a normal scoring pass (`remote-<job>`), one binding per
  criterion. Each result's raw record carries the eval, run and output-item IDs.
- **Not plan-bindable.** A `remote_job` metric is refused by `EvaluatorRegistry.validate`
  (plans, rescoring) and marked ineligible in the planning catalog.

### Langfuse: the default connector, data movement only (17-T3, 17-G4)

- **Choice.** No preference was stated, so the prompt's default applies: Langfuse,
  dataset and trace import. This choice is reversible. The connector is core code over
  `httpx`, needing no SDK. It never computes a metric.
- **Endpoints.** It uses only endpoints that remain in Langfuse v4, checked against the
  generated client of `langfuse==4.15.6`:
  - `v2/datasets`;
  - `dataset-items` (page-numbered);
  - `v2/observations` (cursor-paged);
  - `POST scores`;
  - `v3/scores`.

  The v3 trace, dataset-run and score-list endpoints are deprecated on Langfuse Cloud
  (removal announced for 2026-11-16), and are not used.
- **Dataset import.** Active items become cases. Each case keeps the item's ID, dataset,
  version timestamp and source trace in `extensions["langfuse.dataset_item"]` and its
  provenance. A structured expected output is kept, never flattened into an answer.
- **Trace import.** Trace ID = the execution's correlation ID, which the HTTP runner sends
  as `X-Request-ID`; it is a valid 32-hex trace ID. Observations are mapped onto the
  OpenTelemetry rules of ADR 0015: usage comes from the lowest observations, and partial
  traces stay partial. The raw export is kept as a restricted artifact.
- **Score export.**
  - Only `ok` results for cases imported from Langfuse and executions with an imported
    Langfuse trace are exported. A missing result never becomes a zero.
  - Each score gets a deterministic ID, and metadata naming the harness run, result,
    case, metric and dataset item.
  - Creating a score with an existing ID is not documented as idempotent. So each score
    is read back first:
    - absent: create it;
    - identical: skip it;
    - different: report a conflict.
  - A second read-back verifies each created score. A lost reply to a create is resolved
    by read-back, never by resending.
- **Egress.** Every Langfuse call needs its host in `allowed_egress_origins`, and both key
  references allowed. Redirects are not followed, and environment proxies are ignored.

### Exposure (17-T4)

- **Where it's shown.** `aibench integrations list`, `/integrations` in chat and the
  assistant's `list_integrations` tool share one service. For each integration it
  reports:
  - supported and unsupported modes;
  - data destinations and what they receive;
  - credentials;
  - availability: available, or unavailable with the exact reasons;
  - live verification.

  It starts no plugin code (plugins are found from metadata) and contacts no service.
- **Planning.** Catalog entries carry `consumes` and `network_destinations`.
- **New policy field.** `allowed_egress_origins` makes every egress destination explicit,
  loopback included. Plain http is accepted only on loopback.

## Changes after independent review

An adversarial review reproduced three high-severity defects and several smaller ones.
Each fix below has a regression test.

- **Redirects.** The SDK follows redirects by default, so a 307 from the approved origin
  carried the upload to an unapproved one. The worker's HTTP client now refuses redirects
  and ignores environment proxies. A 3xx is rejected.
- **Interrupted creates.** A crash after a create was sent, but before its reply was
  recorded, left the job `prepared`, and resuming sent it again. The job is now saved as
  `*_unknown` before every create, so resuming reconciles.
- **Misread replies.** A success reply the SDK could not read was recorded as rejected.
  That reopened duplicate submission. For creates, only an explicit 4xx (not 408, 409 or
  429) now counts as rejected. Anything else, including a success without an ID, is
  ambiguous.
- **Pinned uploads.** Results attached to whichever execution was final at fetch time. The
  job now stores the uploaded execution ID and output digest for each case, and runs that
  are still running or interrupted are refused.
- **Plan analysis.** Plan analysis now refuses `remote_job` metrics too, so
  `plan validate` and chat drafts can't accept one.
- **Whole-input delivery.** The live bridge refuses an application whose input binding
  does not deliver the whole input unchanged, or whose transport builds its own payload
  (OpenAI-compatible). Otherwise it would answer a different question than the eval
  asked. Replay can't see the application, so this is a documented limitation there.
- **Type-tagged comparison.** A request string can never equal a list of messages.
- **Langfuse fixes.**
  - Export takes, for each case, metric and binding, the latest scoring pass (or
    `--scoring-id`). A rescored run no longer puts two same-named scores on a trace.
  - Export writes only to the host the items and traces came from.
  - A non-JSON response is a connector error.
- **Report usage.** An execution traced by both an OTLP export and Langfuse is counted
  once.
- **Worker cleanup.** Startup handshake errors bypass a context manager's `__aexit__`. Both
  worker wrappers now clean up the process tree, release its Windows Job Object handle and
  remove the private working directory if startup fails, then perform the same resource
  cleanup on every normal or exceptional exit.
- **Recorded, not changed.** The duplicate-fingerprint check is check-then-insert, so two
  truly concurrent `submit` calls could both send. Submitting is a deliberate command, not
  an automatic one.

## Consequences

- **Workspace schema 10** adds `remote_jobs`, after Prompt 18's migration 9.
- **Live status.** No live OpenAI or Langfuse call was made; none was authorized. The
  hosted bridge and the connector were verified against local stand-ins of the documented
  contracts, with the real pinned SDK parsing the hosted responses. This shows the
  harness follows the contract, not that the live services behave the same way. A bounded
  live smoke needs an authorized key and budget.
- **OSS scope.** The OSS bridge covers only the four basic evals. Registry YAML specs and
  other eval classes stay unsupported until each is checked.
