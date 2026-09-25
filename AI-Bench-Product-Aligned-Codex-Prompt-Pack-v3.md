# AI-Bench — Product-Aligned Codex Implementation Prompt Pack v2

**Purpose:** Product-aligned replacement/overlay for the existing AI-Bench sequential Codex prompt pack.

## Product vision

AI-Bench is a **codebase-aware, conversational evaluation agent**: think **Claude Code for evaluating AI applications**.

The user opens `aibench` inside an AI application's repository and talks to it naturally:

```text
$ aibench

You: Benchmark my RAG application.

AI-Bench:
I inspected the repository and found:
- RAG pipeline: src/rag/pipeline.py
- Retriever: src/rag/retriever.py
- Embeddings: src/rag/embeddings.py
- Existing dataset: datasets/questions.jsonl
- App entry point: src/api.py

I can evaluate:
- Faithfulness
- Answer relevancy
- Contextual precision
- Contextual recall

I also found 200 existing test cases.

Would you like me to use them, or generate a new evaluation set?
```

The agent then:

1. understands the repository and AI application;
2. identifies how the application can be invoked;
3. proposes an evaluation plan;
4. asks only materially necessary questions;
5. executes the benchmark;
6. observes failures and evidence;
7. lets the user ask questions during execution;
8. analyzes failures and experiments;
9. compares runs when requested;
10. preserves reproducible run/evaluation history.

### Architectural principle

**AI-Bench owns orchestration, conversation, codebase understanding, experiment identity, execution state, evidence, and result analysis.**

Evaluation frameworks remain adapters:

```text
AI-Bench
 ├── Core evaluation contracts
 ├── Codebase intelligence
 ├── Eval Agent
 ├── Dataset/run management
 ├── Application runners
 ├── Experiment engine
 └── Conversation/session layer
       │
       ├── DeepEval adapter
       ├── Ragas adapter
       ├── OpenAI Evals adapter
       └── Custom evaluator adapter
```

Do **not** make DeepEval the product abstraction.

---

# Product requirement — AI-Bench must support non-technical evaluation

AI-Bench is not only for developers who can provide a repository.

A user should be able to evaluate an AI application **without understanding its
implementation, evaluation frameworks, datasets, tracing, or Python APIs**.

The product therefore has two first-class entry paths:

```text
1. Black-box evaluation
   URL / API / deployed application
   ↓
   AI-Bench discovers how to interact with it
   ↓
   Generate or import evaluation cases
   ↓
   Execute application
   ↓
   Capture observable outputs
   ↓
   Evaluate

2. Codebase-aware evaluation
   Repository + optionally running application
   ↓
   Inspect architecture and invocation paths
   ↓
   Discover evaluation opportunities and available evidence
   ↓
   Execute
   ↓
   Evaluate at end-to-end and, when evidence permits, component level
```

The user-facing abstraction is:

> **"Evaluate my AI application."**

It must not require the user to know whether the underlying evaluator is
DeepEval, Ragas, OpenAI Evals, or a custom evaluator.

## Black-box mode

For a non-technical user, the minimum viable interaction should look like:

```text
$ aibench

You:
Evaluate my RAG chatbot.

AI-Bench:
Sure. How can I access it?

1. Website
2. API
3. Existing integration

You:
https://example.com/my-chatbot

AI-Bench:
I can evaluate the chatbot end-to-end.

I recommend:
- Answer correctness
- Answer relevancy
- Faithfulness / hallucination
- Retrieval quality, if retrieval evidence is available
- Response latency

Do you already have evaluation questions?

1. Upload them
2. Use historical conversations
3. Generate an evaluation set
```

The agent should handle the technical translation internally:

```text
User intent
   ↓
Evaluation specification
   ↓
Dataset / test-case generation
   ↓
Application invocation
   ↓
Observation capture
   ↓
Evaluator adapter
   ↓
Evidence-backed analysis
```

The user should not be required to manually construct framework-specific
objects such as `LLMTestCase`.

## Black-box limitations must remain explicit

Black-box evaluation can only evaluate what AI-Bench can observe.

For example:

```text
End-to-end answer quality
        ✓

Latency
        ✓

Final response correctness
        ✓

Internal retriever precision
        ?
```

If retrieval context, traces, or component outputs are unavailable, AI-Bench
must say so rather than pretending it can evaluate an internal component.

Example:

> "I can evaluate the chatbot's end-to-end answers, but I cannot directly
> measure contextual recall because the application does not expose its
> retrieved documents."

If the application later provides traces or the repository is supplied, the
same evaluation can become deeper.

## Hybrid progression

A user should be able to start with almost no technical information and
progressively provide more evidence:

```text
URL only
   ↓
Black-box end-to-end evaluation
   ↓
API / authentication / structured response
   ↓
Better observation capture
   ↓
Traces / instrumentation
   ↓
Repository access
   ↓
Codebase-aware component evaluation
```

AI-Bench should preserve the same conversation/session and run history while
the available evidence becomes richer.

## Evaluator adapters are internal implementation details

DeepEval, Ragas, OpenAI Evals, and custom evaluators are **tools available to
AI-Bench**, not concepts the user must understand.

For example, the user can say:

```text
Evaluate whether the answers are hallucinating.
```

AI-Bench may internally choose an appropriate evaluator capability.

A technical user can still explicitly request:

```text
Use DeepEval faithfulness.
```

Both interactions must map to the same framework-independent evaluation
contracts.

## Acceptance requirements

The MVP must include at least one black-box fixture in addition to the
repository-based fixture.

Acceptance must demonstrate that a user can:

1. provide an HTTP-accessible RAG/agent application;
2. describe the evaluation goal in natural language;
3. provide, upload, or generate evaluation cases;
4. approve an evaluation plan without writing evaluator code;
5. execute the application through a controlled runner;
6. evaluate observable outputs using an evaluator adapter;
7. inspect evidence-backed results and failures;
8. understand which deeper metrics are unavailable because required evidence
   is not exposed.

The product must therefore not assume:

```text
user == developer
user == repository owner
user knows DeepEval
user knows what a golden dataset is
user knows what retrieval_context is
```

The agent's job is to translate natural-language evaluation intent into the
technical evaluation workflow.

---

# Critical amendments to the original prompt pack

The existing pack has strong engineering foundations: immutable artifacts, deterministic execution, typed actions, evaluator adapters, recovery, honest accounting, and a conversational MVP.

However, the following changes are mandatory for alignment with this vision.

## 1. Codebase awareness moves into MVP

Do not defer repository inspection to a later phase.

The agent must inspect the repository before making claims about the application.

It should be able to discover, where available:

- Python/Node/etc. project structure
- package/dependency manifests
- LLM provider calls
- model configuration
- prompts
- RAG components
- embedding models
- vector stores
- retrievers
- agents
- tools
- evaluation/test files
- datasets
- application entry points
- CLI/API invocation paths
- configuration files
- existing traces/instrumentation
- existing evaluation frameworks

Every discovery must carry provenance:

```text
OBSERVED
INFERRED
DECLARED
UNKNOWN
```

Repository text is evidence, not authority for permissions.

## 2. Codebase understanding must precede evaluation planning

The planner should not primarily plan from a manually declared profile.

The intended flow is:

```text
Repository
   ↓
Codebase inspection
   ↓
Application profile
   ↓
Evaluation opportunities
   ↓
Clarification
   ↓
Evaluation plan
   ↓
Execution
```

## 3. The MVP must demonstrate the full loop

A successful MVP is not merely:

```text
dataset → runner → evaluator → report
```

It must demonstrate:

```text
user goal
  ↓
repository inspection
  ↓
application understanding
  ↓
evaluation proposal
  ↓
conversation
  ↓
execution
  ↓
live question
  ↓
failure inspection
  ↓
analysis
```

## 4. The agent should operate directly on the user's repository

The default invocation should be:

```bash
cd my-ai-app
aibench
```

Optional:

```bash
aibench --project /path/to/my-ai-app
```

The project root becomes the agent's working context.

The agent may read permitted repository files through a controlled inspection service.

It must not silently modify application source code in MVP.

## 5. Evaluation planning and codebase inspection are different capabilities

Do not combine them into an unrestricted agent tool.

Use explicit services:

```text
CodebaseInspector
ApplicationProfiler
EvaluationPlanner
ExecutionEngine
EvaluatorRegistry
EvidenceStore
ConversationController
```

The LLM chooses among typed capabilities; it does not receive unrestricted shell access.

## 6. Avoid overbuilding the MVP

The original pack contains useful Phase 2/3 ideas, but MVP should prioritize the product's defining loop.

Do not require:

- PostgreSQL
- distributed workers
- dashboard
- plugin marketplace
- OpenTelemetry ingestion
- multiple hosted platforms
- optimization/autonomous code rewriting

before the core conversational/codebase-aware evaluation loop works.

---

# Product-aligned implementation sequence

## Prompt 00 — Product contract and scaffold

### Goal

Create the project foundation without prematurely implementing infrastructure.

### Tasks

- Create installable `aibench` CLI.
- Establish Python package structure.
- Establish core/adapters dependency boundary.
- Add SQLite persistence.
- Add engineering ledger and test infrastructure.
- Record this product vision in `docs/product-vision.md`.
- Record the adapter architecture in an ADR.
- Make `aibench --help` work.
- Support `aibench --version`.

### Acceptance

- Clean installation works.
- CLI starts.
- Core does not depend on DeepEval/Ragas/OpenAI Evals.
- Product vision is persisted.
- No fake benchmark functionality is exposed.

---

# Prompt 01 — Codebase Intelligence MVP

## Goal

Build the feature that differentiates AI-Bench from a conventional evaluation CLI.

### Required service

```python
CodebaseInspector
```

It must support:

```text
scan_project()
inspect_file()
find_candidates()
summarize_architecture()
find_entrypoints()
find_llm_calls()
find_retrieval_components()
find_agent_tools()
find_datasets()
find_existing_evals()
```

### Inspection behavior

Start with deterministic repository inspection.

Read:

- directory structure
- supported source files
- package manifests
- configuration
- relevant source files
- tests
- dataset metadata

Do not read every file blindly.

Use bounded discovery and relevance ranking.

### Output

Produce an `ApplicationProfile`:

```yaml
project:
  root: ...

language:
  primary: python

architecture:
  type: rag

entrypoints:
  - ...

llm:
  providers:
    - ...
  models:
    - ...

retrieval:
  detected: true
  retriever: ...
  embeddings: ...
  vector_store: ...

agents:
  detected: false

datasets:
  - ...

existing_evaluations:
  - ...

evidence:
  - path: src/rag/pipeline.py
    lines: ...
    status: observed
```

### Critical rule

Never say:

> "Your application uses Pinecone."

unless the repository actually provides evidence.

Say:

> "I found a Pinecone client reference in `src/...`."

or:

> "The repository suggests Pinecone is used, but runtime confirmation is unavailable."

### Acceptance

Given a fixture RAG repository, AI-Bench discovers:

- entry point
- LLM call
- retriever
- embeddings
- vector store
- dataset
- existing tests

without executing arbitrary application code.

---

# Prompt 02 — Application profile and evaluation opportunity discovery

### Goal

Turn codebase evidence into an actionable evaluation profile.

Build:

```text
ApplicationProfiler
EvaluationOpportunityDetector
```

Example:

```text
Detected application: RAG QA

Potential evaluation dimensions:

Retrieval:
  • contextual precision
  • contextual recall

Generation:
  • faithfulness
  • answer relevancy

Operational:
  • latency
  • token usage
  • cost, if observable
```

For agents:

```text
Agent:
  • task completion
  • tool selection
  • tool arguments
  • trajectory
  • final outcome
```

For conversational applications:

```text
Conversation:
  • multi-turn consistency
  • task completion
  • response quality
```

Do not invent metrics simply because they exist in an evaluator library.

The metric must be applicable to the observed application and available evidence.

---

# Prompt 03 — Dataset and evaluation contract

### Goal

Create canonical, framework-independent evaluation contracts.

Core entities:

```text
Dataset
Case
ApplicationProfile
EvaluationPlan
MetricSpec
Run
Observation
EvaluationResult
Evidence
Experiment
Session
```

Separate:

```text
application input
```

from:

```text
judge-only reference / golden data
```

Prevent golden references from accidentally reaching the application.

Support JSONL initially.

Dataset sources may include:

```text
existing repository dataset
existing tests
user-provided dataset
generated candidate dataset
```

Generation is candidate-only until explicitly promoted.

---

# Prompt 04 — Application runners

### Goal

Allow AI-Bench to actually execute the discovered application.

Support MVP:

1. local CLI/process runner
2. HTTP runner

Runner contract:

```text
discover
prepare
invoke
observe
reset
close
```

The codebase inspector should help the agent determine how the application is invoked.

Example:

```text
I found:

python -m app.main

and an HTTP endpoint:

POST /query
```

The agent proposes the invocation rather than silently guessing.

### Safety

- no unrestricted shell from the planner
- explicit command/argument structure
- timeouts
- output limits
- environment policy
- secret references
- cancellation
- clear effect classification

Trusted local execution is not a hostile-code sandbox.

---

# Prompt 05 — Evaluator abstraction and DeepEval adapter

### Goal

Create framework-independent evaluation contracts.

Implement:

```text
Evaluator
EvaluatorRegistry
EvaluatorResult
EvaluatorCapability
```

Then implement:

```text
DeepEvalAdapter
```

DeepEval-specific types and APIs must stay inside the adapter.

The user should be able to say:

```text
Use DeepEval faithfulness.
```

without the rest of AI-Bench depending on DeepEval.

### Important

AI-Bench must preserve the distinction between:

```text
application execution
evaluation execution
evaluation error
low score
not applicable
missing evidence
```

A low score is not an evaluator failure.

---

# Prompt 06 — Evaluation agent and planning loop

### Goal

Build the actual AI-Bench reasoning layer.

The agent receives:

```text
user request
+
ApplicationProfile
+
available datasets
+
available evaluator capabilities
+
execution capabilities
+
policy
```

It produces a typed `EvaluationPlan`.

Example conversation:

```text
User:
Benchmark my RAG system.

Agent:
I found a RAG pipeline and a 200-case dataset.

I recommend:
- Faithfulness
- Answer relevancy
- Contextual precision
- Contextual recall

I can run the existing 200 cases.

The current code exposes retrieved context, so those
retrieval metrics are observable.

Run this plan?
```

The agent must ask a question when a missing decision materially affects correctness.

It should not ask unnecessary questions.

### Plan must contain

```text
application
dataset
cases/sample
metrics
evaluator adapters
runner
configuration
budget
policy
expected observations
aggregation
```

Plans are validated outside the LLM.

---

# Prompt 07 — Conversational session

### Goal

Make conversation the primary interface.

Support:

```text
clarify
→ inspect
→ propose
→ revise
→ approve
→ execute
→ discuss
→ continue
```

Example:

```text
User:
Can we just use 50 examples?

Agent:
Yes. I'll change the sample from 200 to 50.

User:
Why did you choose faithfulness?

Agent:
Because the repository exposes retrieved context and the
application produces grounded answers. Faithfulness checks
whether the answer is supported by that context.

User:
Run it.
```

Questions during execution must not cancel the run.

The user can ask:

```text
How many cases finished?
Why is recall low?
Show me the worst failures.
What happened on case 37?
How much did this run cost?
```

All answers must come from stored evidence or clearly labeled hypotheses.

---

# Prompt 08 — Interactive terminal

### Goal

Make the conversational experience feel like a coding agent.

Bare command:

```bash
aibench
```

should open the interactive session.

Support:

```text
aibench --project PATH
aibench --resume SESSION_ID
```

Useful commands:

```text
/help
/inspect
/plan
/run
/status
/failures
/case 37
/pause
/resume
/stop
/report
/sessions
```

Slash commands are deterministic controls, not LLM requests.

The primary experience remains natural language.

---

# Prompt 09 — Evidence analysis and failure investigation

### Goal

Make AI-Bench useful after the benchmark finishes.

The agent should answer:

```text
Why did the benchmark fail?

What are the worst 10 cases?

What pattern do these failures share?

Is this retrieval or generation?

Did the model hallucinate?

Which tool calls failed?

Which cases have missing evidence?
```

Build typed queries over stored results.

The LLM may summarize evidence, but quantitative claims must be generated from actual stored data.

Example:

```text
I found 31 low-faithfulness cases.

23 of them contain relevant retrieved context,
but the final answer introduced unsupported claims.

This pattern appears in ...
```

Do not invent causal claims.

Label hypotheses:

```text
Observed:
...

Possible explanation:
...
```

---

# Prompt 10 — Experiments and comparisons

### Goal

Allow conversational benchmark experimentation.

Examples:

```text
Compare the current prompt with the previous one.

Compare model A and model B.

What happens if top_k changes from 5 to 10?

Run the same benchmark with 50 cases instead of 200.

Compare this run against the baseline.
```

An experiment must freeze:

```text
dataset identity/version
application revision
prompt/configuration
model
evaluator
metric configuration
runner configuration
```

Never compare incompatible runs silently.

Comparison should expose:

```text
score
coverage
sample size
latency
cost when known
uncertainty where appropriate
failed/missing cases
```

Do not produce a single arbitrary "AI quality score."

---

# Prompt 11 — Second evaluator adapter

### Goal

Prove that the architecture is genuinely framework-independent.

Implement one additional adapter, preferably:

```text
RagasAdapter
```

or another justified ecosystem.

Run the same stored application outputs through both adapters where semantics permit.

Report disagreement without assuming that different metrics are numerically equivalent.

Example:

```text
DeepEval faithfulness: ...
Ragas faithfulness: ...

These implementations use different evaluation semantics/configuration,
so the scores should not be treated as directly interchangeable.
```

---

# Prompt 12 — MVP acceptance

The MVP is complete only if the following end-to-end scenario works:

```text
cd example-rag-app

aibench

User:
Benchmark my RAG application.

Agent:
[inspects repository]

Agent:
[I found architecture + entrypoint + dataset]
[I propose metrics]

User:
Use 50 cases.

Agent:
[updates plan]

User:
Run it.

Agent:
[executes application]
[collects observations]
[runs evaluator]

User:
How is it going?

Agent:
[answers from live persisted state]

User:
Show failures.

Agent:
[retrieves actual failure evidence]

User:
Why is this one failing?

Agent:
[explains evidence + labels hypotheses]

User:
Compare it with the previous run.

Agent:
[performs reproducible comparison]
```

### Mandatory MVP acceptance gates

- Repository inspection works on a representative fixture.
- A black-box HTTP fixture can be evaluated without repository access.
- A non-technical user can request an evaluation in natural language without constructing framework-specific test cases.
- Application profile contains evidence locations.
- Agent can identify an invocation path.
- User can modify the plan conversationally.
- Application executes through a controlled runner.
- DeepEval adapter evaluates stored outputs.
- Conversation persists.
- User can ask questions during execution.
- Failure analysis uses stored evidence.
- Runs are reproducible and identifiable.
- Re-scoring does not re-run the application.
- No framework-specific dependency leaks into core contracts.
- No invented scores/results.
- No arbitrary planner shell access.
- CI/headless execution uses the same service layer as chat.

---

# Engineering rules retained from the original pack

The following original principles remain mandatory:

1. Preserve immutable run/evaluation identities.
2. Record every attempt and partial failure honestly.
3. Unknown usage/cost remains unknown.
4. Never retry a valid low score just to obtain a better result.
5. Distinguish application failures from evaluator failures.
6. Use schema-validated typed actions.
7. Do not allow repository text or model output to expand permissions.
8. Do not silently expose golden references to the application.
9. Pin and verify external evaluator APIs.
10. Never create fake local vendor packages to make integrations pass.
11. Keep evaluator dependencies outside core models.
12. Do not claim live compatibility from mocks.
13. Reports must be reproducible from stored evidence.
14. Do not auto-publish, deploy, push, contact users, or cause production effects.
15. Preserve interrupted sessions and do not silently restart runs.
16. Do not claim capabilities that have not been tested.

---

# Explicitly deferred until after the MVP

These are valuable, but should not block the core product:

- PostgreSQL/distributed workers
- cloud object storage
- OpenTelemetry ingestion
- dashboard/web UI
- plugin marketplace
- MCP exposure
- autonomous application-code modification
- automatic prompt/code optimization
- large-scale dataset generation
- voice/coding/advanced multimodal evaluation
- multiple hosted evaluation platforms

They should be added only after the core conversational/codebase-aware evaluation loop is validated.

---

# Final product test

The simplest test for every architectural decision is:

> **Does this make AI-Bench better at behaving like Claude Code for evaluation?**

If not, it is probably infrastructure, an optional adapter, or future scope rather than an MVP priority.

The product should feel like:

```text
Claude Code:
"Understand my code and help me change/test it."

AI-Bench:
"Understand my AI application and help me evaluate/benchmark it."
```

The key distinction is:

```text
Claude Code
    ↓
Codebase understanding
    ↓
Reasoning
    ↓
Tools
    ↓
Execution
    ↓
Observation
    ↓
Iteration

AI-Bench
    ↓
AI application understanding
    ↓
Evaluation reasoning
    ↓
Evaluation tools/adapters
    ↓
Application execution
    ↓
Evaluation
    ↓
Evidence analysis
    ↓
Iteration
```
