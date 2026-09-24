# ADR 0016: reviewed candidate pools and multi-turn text episodes

- **Status:** Accepted for Prompt 18
- **Date:** 2026-09-24
- **Prompt:** 18 — Reviewed dataset generation and advanced episodes

## Context

Prompt 18 is sequenced after Prompt 17, or after an explicit completed prerequisite subset is
recorded in the phase ledger. Prompt 17 as a whole remains unstarted: its OSS Evals bridge,
hosted Evals API bridge, and selected platform connector are optional Phase 2 extensions.
Prompt 18's local candidate workflow and multi-turn text fixture do not consume those
interfaces. They need a bounded model call with approved egress, existing immutable dataset
cases, per-episode runner resets, and an independent final-state evaluator; those contracts
are already present in the repository.

## Decision

Record prerequisite subset **17-P18** as complete, without marking Prompt 17 complete:

1. Candidate generation uses only the existing OpenAI-compatible chat-completions provider
   mode, whose origin and secret references are policy-checked before workspace creation or
   provider construction.
2. The provider receives only explicit UTF-8 development source documents. Candidate pools
   are represented as development-only records. Holdout is not an accepted generation split
   and case JSONL datasets are not accepted as generation sources.
3. The selected text episode modality uses the existing HTTP runner reset contract,
   `per_episode` scheduling and `native.final_state`; it requires no Prompt 17 evaluator
   bridge or trace connector.

The Prompt 17 OSS/API evaluator bridges and Langfuse/Phoenix/Braintrust connector are not
prerequisites for these Prompt 18 tickets. Their omission is recorded as a dependency slice,
not a claim that any Prompt 17 gate passed.

## Consequences

- Candidate generation is one bounded provider request, and content is sent only after
  policy approval. Live generation remains a separate user-invoked action; Prompt 18
  validation uses an injected fake provider.
- Every generated row starts with `synthetic_unverified`, keeps a source digest/span and
  generator/prompt provenance, and stays outside ordinary datasets until an explicit
  promotion command.
- Human verification and an exact-source-answer executable oracle are recorded separately.
  The built-in oracle is intentionally narrow and cannot bless paraphrased answers.
- Multi-turn application state is evaluated through test-world `world_state`, not harness
  chat history. The deterministic fixture proves execution/reset/evaluator integration but
  makes no model-quality claim.

## Evidence

- Provider policy and secret boundary: `src/aibench/planning/openai_provider.py`,
  `tests/test_openai_provider.py`.
- Episode reset/outcome contracts: `src/aibench/engine/engine.py`,
  `tests/test_agent_worlds.py`.
- Prompt 18 specific egress, split, provenance and lifecycle evidence:
  `tests/test_candidate_workflow.py`, `tests/test_episode_contract.py`.
