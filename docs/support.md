# Support and limitations

This covers what aibench 0.1.0rc1 (the MVP release candidate) supports, what has actually
been tested, and what isn't available yet. The release decision and its open items are in
`docs/engineering/release-readiness.md`. The authoritative design is `docs/spec/implementation-plan.md`, and
per-prompt evidence is in `docs/engineering/reports/`.

## Platforms

| | Supported | Tested |
|---|---|---|
| Python | 3.11, 3.12 | Full suite on 3.12.10 (Prompt 12); clean wheel install, quickstart and 100-case acceptance on 3.12.10 and 3.11.16 (Prompt 13) |
| OS | Windows, Linux, macOS | Windows 11. Linux is configured in CI (`ubuntu-latest`), but no result has been observed. macOS is not run anywhere |
| Terminal chat | Any terminal prompt_toolkit supports | Windows ConPTY (automated tests, including resizing) |
| Model providers | OpenAI-compatible endpoints | Scripted and local test providers only. No live model has been exercised |

Details are in `docs/engineering/platform-matrix.md`. `aibench doctor` warns on a platform
outside the tested matrix.

## Optional evaluators

The core install ships the native evaluators: `native.exact_match`, `native.json_schema`
and custom Python evaluators you trust. Evaluator frameworks are never imported into the
aibench process. They run in their own environment, driven by a worker.

**DeepEval faithfulness** (`deepeval.faithfulness@1`, pinned to `deepeval==4.2.5`). From
the repository root:

```bash
python -m venv plugins/deepeval/.venv
plugins/deepeval/.venv/Scripts/pip install -e . -e plugins/deepeval    # Windows
plugins/deepeval/.venv/bin/pip install -e . -e plugins/deepeval        # Linux/macOS
```

A plan uses it through `plugin_environments`, and the policy must allow each of these:

- `allowed_plugin_environments`: that interpreter;
- `allowed_evaluators`: `deepeval.*`;
- `allow_model_evaluators`: `true`;
- `allowed_secret_refs`: the judge's key reference.

The judge model is a paid external call. The adapter's behaviour is described in
`plugins/deepeval/README.md`. Faithfulness needs the app to report the passages it
actually retrieved (`output_binding.retrieved_context`). The quickstart app does, so its
dataset works with faithfulness once a judge is configured.

Other frameworks (Ragas, OpenAI Evals, promptfoo) are not integrated yet.

## Credentials

Credentials are never written into project files, datasets, plans or chat. Every
credential is a secret reference, `env:NAME`, resolved from the environment when it is
used. It must also be listed in the policy's `allowed_secret_refs`:

| Where | Field |
|---|---|
| CLI app environment | `transport.secret_env: {"TOKEN": "env:APP_TOKEN"}` |
| HTTP app header | `transport.secret_headers: {"Authorization": {"ref": "env:APP_TOKEN", "prefix": "Bearer "}}` |
| Evaluator plugin | `plugin_environments[].secret_env` or `--plugin-secret NAME=env:VAR` |
| Assistant model | `api_key: "env:OPENAI_API_KEY"` in the provider config |

Set the variables in your shell or your CI's secret store. Don't commit a `.env` file: aibench
doesn't read one; the DeepEval adapter disables its own `.env` loading.
`aibench doctor` reports whether each reference is set, without printing its value. Secrets
are redacted from captured output, stored conversation turns and reports.

## Supported now

- **Conversational benchmarking:** in a terminal, `aibench`/`aibench chat` handles planning, runs, live status, pause/resume/stop, failures, case evidence and reports. Sessions survive exit and crashes, and are never restarted automatically.
- **Headless commands:**
  - `init`, `doctor`;
  - `dataset validate`, `inspect`;
  - `plan`, `plan validate`, `plan benchmark` (planner fixture set);
  - `run`, `resume`, `evaluate` (rescore stored outputs);
  - `runs list/show/status`, `report`, `benchmark`;
  - `sessions list/show/delete`, `evaluators list/describe/plugin/calibrate`, `plugins list`, `score`, `app describe/smoke`.
- **Reports:** JSON, Markdown and static HTML, from stored facts. They include typed metric profiles, full denominators, release gates, latency definitions, cost completeness and case evidence.
- **Release gates** in plans (`gates`): a minimum pass rate or minimum completed coverage for one metric binding, always over selected cases.

## Not available yet

| Feature | Status |
|---|---|
| `aibench compare BASELINE CURRENT` | Not implemented. It says so and exits 2. Planned: paired, uncertainty-aware comparison (Prompt 14) |
| Dashboard or web app | Not planned for the MVP. Reports are static files |
| Release gates in conversation drafts | Gates are authored in plan files. A session's draft can't declare them yet |
| Planning and conversation cost per run | Tracked per session (`/budget`), not attributed to a run's report |
| Source-code inspection | `inspect` reads declared configuration and static metadata only |
| Execution caching, distributed workers, detached runs | Not in the MVP. A run stops dispatching when its terminal exits and resumes on request |
| Session export | Not available. Sessions can be deleted (`aibench sessions delete`), and deletion keeps run records |
| Live model checks | The conversation and the DeepEval adapter are tested against scripted or offline providers. No live provider run is part of this release's evidence |

## Known limits

- **Engine throughput** is low. On the development machine (Windows 11, 14 CPUs), 1,000 cases took between 152 and 233 s at concurrency 16 against an instant local service. Each call waits on a fresh HTTP connection and on its capture artifacts being durably written and verified. Profiles are in `docs/engineering/evidence/12/`.
- **Planner and judge measurements** (`aibench plan benchmark`, `aibench evaluators calibrate`) use fixture annotations and labels that no person has reviewed yet. The template planner misses objectives phrased without its keywords: 17/21 recall on the v1 fixture set, below the 0.85 target.
- **Claim checking** links every number in an assistant reply to a result queried in that turn, and flags numbers it can't trace. It shows where a number could have come from, not that the sentence around it is right. An explanation of why cases failed is a hypothesis unless a stored result states it.
- **Redaction** of credentials is pattern-based, as a safety net. Use secret references rather than relying on it.
- **Latency** is runner-measured wall time per request under the plan's concurrency. It is not a load test.
- **A full disk or failing workspace** (disk full, quota, I/O error, read-only filesystem, database full) stops the run's dispatching and leaves it resumable (exit code 130), with the reason in the output and in `aibench runs status`. No case is marked failed for it. This is tested by making the real write path fail, not on a real full volume. Recovery steps: `docs/release/upgrade-and-recovery.md`.
