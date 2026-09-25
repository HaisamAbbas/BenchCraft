# DeepEval for the conversational assistant

Goal: the chat assistant can use DeepEval metrics the way Claude Code uses tools: it knows
which metrics exist, proposes installing and allowing the plugin, and once the user approves,
plans and scores runs with them. DeepEval still runs only in its isolated worker environment.

Decisions (user, 2026-09-26): every wrappable metric; the assistant proposes the install and
the user approves. Approval is a deterministic slash command the user types
(`/plugins install deepeval`), never inferred by the model.

## Definition of done

- [x] Every single-turn DeepEval 4.2.5 metric that BenchCraft's recorded data can feed is an
      evaluator in `aibench-deepeval`, with a documented field mapping and N/A policy.
- [x] A built-in `openai_compatible` judge (e.g. GLM on Z.ai) needs no custom code; its calls
      and tokens are reported, cost stays unknown unless measured.
- [x] `aibench plugins install deepeval` and `/plugins install deepeval` create the plugin
      environment, show and apply the policy changes (with a backup), and record the plugin
      environment and judge defaults in the project config.
- [x] Chat sessions load the project's plugin environments; the assistant lists DeepEval
      metrics, and optional plugins that are not installed yet with how to enable them.
- [x] Planning maps objectives to the new concepts (relevancy, safety, retrieval quality,
      custom criteria, ...) and fills judge params from the plugin defaults.
- [x] Real-package tests for every metric (deterministic judges, no live calls); the install
      command and a chat journey are tested end to end.
- [ ] Live check in the demo project with GLM as judge.
- [x] Docs: plugin README, support.md, quickstart; CHANGELOG.

Not in this change (stated to the user): multi-turn conversational metrics (they need a
conversation test case built from text episodes), and metrics needing data BenchCraft does not
record (images, audio, MCP servers, agent traces: step efficiency, plan quality/adherence,
agent loop detection). DAG and arena metrics need non-declarative objects.

## Phases

1. Adapter: generic single-turn metrics + `openai_compatible` judge + real-package tests.
2. Plumbing: manifest-declared concepts; plugin environments and default params in project
   config and chat sessions; template fills defaults.
3. Install + approval: CLI and slash command, policy diff/backup, assistant awareness.
4. End to end: chat journey test, live GLM check in the demo project, docs.

## Log

- Phase 1 done: 21 metrics (`metrics.py`, `judges.py`), real-package tests
  (`test_deepeval_metrics.py`, 18 pass). Found and fixed: the worker proxy projected cases
  to static requirements, so G-Eval never saw a reference answer (`parameter_requirements`);
  tool correctness builds an OpenAI model at init, so it needs a judge.
- Phase 2 done: manifest `concepts`, new planner concepts (narrow keywords; v1 planner
  fixture baseline unchanged), `plugin_environments` in project config, session
  `evaluator_defaults`, `with_default_params`.
- Phase 3 done: `aibench plugins install|status`, `/plugins`, `list_optional_plugins`,
  prompt guidance. End-to-end chat test: install -> plan relevancy -> run scored by the
  real DeepEval worker through an OpenAI-compatible judge (`test_plugins_install.py`).
- Phase 4: docs (plugin README, support.md, quickstart, CHANGELOG, ADR 0020) done; live
  install in the demo project running.
