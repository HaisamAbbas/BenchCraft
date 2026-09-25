# ADR 0020: Optional plugins the assistant can use, installed by the user

Status: Accepted
Date: 2026-09-26

## Context

- **What users asked for.** The conversational assistant should use DeepEval the way a
  coding agent uses its tools: know which metrics exist, and plan and score with them.
- **What existed.** One DeepEval metric (faithfulness), usable only headlessly through
  `aibench score --plugin-env`. Chat sessions never loaded plugin environments, so the
  assistant's catalog was always native; asked about DeepEval, it said DeepEval was absent.
  The assistant had no install or policy action (by design: "never change production
  settings"), and the only judges were an OpenAI model or custom code.

## Decision

1. **Every wrappable DeepEval metric, from one table.** `aibench-deepeval` 0.2.0rc1 builds
   one evaluator per DeepEval 4.2.5 single-turn metric from a declarative spec: the fields
   it reads, the parameters the plan supplies, and what it measures. A metric reads only
   recorded data; missing or empty evidence is `not_applicable`, never a score. Multi-turn,
   image, audio, MCP and agent-trace metrics are left out, because aibench records no data
   for them.
2. **A built-in OpenAI-compatible judge.** Any Chat Completions endpoint (such as GLM on
   Z.ai) judges without custom code. Its key reaches the worker only through
   `secret_env`. It counts its calls and tokens, and cost stays unknown.
3. **The user installs and approves; the assistant only suggests.** `aibench plugins
   install NAME` and `/plugins install NAME` show the environment, config and policy changes
   first. The chat applies them only when the user adds `--yes`, and keeps the old policy
   as `.bak`. The approval is a deterministic command the user types. It is never inferred
   from a model turn, so text injected into a tool result cannot widen the policy. The
   assistant gets one read-only tool, `list_optional_plugins`, to explain what a plugin
   offers and give the command.
4. **Plugin environments belong to the project.** `aibench.json` declares
   `plugin_environments` (interpreter, secrets, default parameters by evaluator pattern).
   New chat sessions load them. An open session adopts them after an install and redrafts,
   as a new revision. The policy still decides whether each one may load.
5. **Defaults fill only what a metric accepts.** A project default such as the judge for
   `deepeval.*` becomes a user-supplied parameter only where the evaluator's schema declares
   it, and the user's own parameters win. Bindings still carry the judge explicitly, so it
   remains part of each score's identity.
6. **Manifests may declare `concepts` and `parameter_requirements`.** A judged metric reads
   only the question and answer, so its requirements do not say what it judges; it
   declares its concept (relevancy, bias, ...) instead. G-Eval reads the fields its
   parameters name; `parameter_requirements` makes those requirements visible to planning
   and to the worker proxy. Before this, the proxy projected a case to the manifest's
   static fields, so G-Eval never received a reference answer.

## Consequences

- The assistant can plan "answers must be relevant, no bias, polite tone" with DeepEval
  once the user has installed it, and a run is scored by the pinned DeepEval in its worker.
- Installing needs a clone of the aibench repository (the adapter is not published) and
  network access for about 70 packages; `--use-env` adopts an existing environment.
- New planner concepts could change which metrics a template draft picks. Keywords are kept
  narrow; the v1 planner fixture baseline is unchanged.
- `deepeval.faithfulness@1` keeps its semantics, but its plugin version changes, so strict
  comparisons with earlier faithfulness runs report a plugin identity difference.
