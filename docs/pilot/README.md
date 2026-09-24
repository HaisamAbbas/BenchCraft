# Pilot package (aibench 0.1.0rc1)

This is what a pilot team needs to try aibench on its own application, and what the
maintainer needs in order to learn from it.

| Item | What it is |
|---|---|
| [Recipe A: HTTP RAG service](recipe-a-http-rag.md) | Benchmark a retrieval-augmented service you already run over HTTP, with a release gate and retrieval evidence |
| [Recipe B: command-line assistant](recipe-b-cli-assistant.md) | Benchmark a JSON-in/JSON-out program, and apply your own domain check to the stored answers |
| [Feedback form](feedback-form.md) | Measures setup effort and value compared with using an evaluator directly |
| [`examples/pilot/`](../../examples/pilot/) | The recipes' working files |
| Release artifacts | `aibench-0.1.0rc1` wheel and sdist, plus separately packaged `aibench-deepeval` and Phase 2 `aibench-ragas` adapters. The Prompt 13 evidence checksums cover the MVP artifacts in `docs/engineering/evidence/13/SHA256SUMS` |

## Status

| | Status | Evidence |
|---|---|---|
| **Local integration trials** (maintainer, stand-in applications, Windows 11, Python 3.12) | Done: both recipes run as written | `tests/test_pilot_recipes.py`, report 13 |
| **Real-team trials** | **Pending**: no team has run a recipe on its own application yet, and no feedback form has come back | None yet |
| **Contacting pilot teams** | Not done. Needs the project owner's go-ahead | None |

The verified Prompt 13 package files are in `../engineering/evidence/13/dist/`; their
checksums are recorded in `../engineering/evidence/13/SHA256SUMS`.

A local trial shows that the recipes and the product work on the maintainer's machine. It
doesn't show that a team can set aibench up without help, or that the reports are worth
more than direct evaluator use. Only the real-team trials can show that, so the MVP's
pilot claim stays unverified until forms come back.

## Running a pilot (for the maintainer)

1. **Choose** a team whose application matches a recipe, and get the project owner's go-ahead to contact them.
2. **Send** the wheel file, its SHA-256 sum, the recipe and the form. Don't send a workspace or someone else's data.
3. **Watch** the session if possible, and fill in section E of the form.
4. **Record** the returned form in `docs/engineering/pilot-results.md` as it was answered.
5. **File** each blocker as a ticket, and link it from the requirements matrix.
