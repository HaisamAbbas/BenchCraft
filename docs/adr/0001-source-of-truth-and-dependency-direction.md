# ADR 0001: Source of truth, dependency direction, and conversational-first interface

Status: Accepted
Date: 2026-09-21

## Context

Prompt 00 (`00-T3`) requires a recorded decision on (a) which specification file is
authoritative, (b) the core/adapters dependency direction, and (c) that the conversational
interface is a first-class MVP feature rather than a later wrapper.

## Decisions

1. **Source of truth.** `docs/spec/implementation-plan.md` (copied from
   `AI-Application-Evaluation-Harness-Implementation-Plan (1).md`, v1.1, 21 September 2026,
   SHA-256 `ae5aad5bf7b6520019affb085f63d7a11c575e37bca6e47b0b0e5332f57339d8`) is authoritative
   for product/architecture decisions. `docs/spec/prompt-pack.md` orders delivery and defines
   acceptance gates; it must not redefine the specification. The prompt pack's stated source
   hash (`2bbf2119408b2f5f6bdecfb60b97bd4eef5553d8fc7cb8a8b4f4a564f4865d19`, 65 hex chars — not
   a valid SHA-256 length) does not match the actual computed hash of the supplied plan file
   and is treated as a documentation error in the pack, not evidence of a different intended
   source file. See `docs/spec/SOURCE.md`.
2. **Dependency direction.** `src/aibench/core/` (models, schema versions, hashes, errors,
   contracts) must not import evaluator frameworks (DeepEval, Hermes, OpenAI Evals), storage
   engines, or UI packages. Adapters (`plugins/*`, `src/aibench/evaluators/*` framework
   bindings) import `core`; `core` never imports adapters. Enforced by a dependency-boundary
   test (`tests/test_dependency_boundaries.py`) added in Prompt 04 once evaluator adapters
   exist; Prompt 01 keeps `core` free of any non-stdlib/non-pydantic imports as a precondition.
3. **Conversational-first interface.** The persistent two-way terminal conversation
   (`src/aibench/tui/`, `src/aibench/conversation/`, `src/aibench/sessions/`) is scheduled in
   the MVP phase (Prompts 08–10) per specification Amendment v1.1, not deferred to Phase 2.
   Scriptable commands (`src/aibench/cli/`) and the conversational layer share one service
   layer (`src/aibench/services/`) so neither becomes a second, divergent implementation of
   benchmarking behavior.

## Consequences

- Prompt 01 models live in `core` with zero framework dependencies, satisfying 01-G-adjacent
  checks and the later 04-G4 dependency-boundary gate.
- Any future specification-file replacement must update this ADR and `docs/spec/SOURCE.md`
  together.
