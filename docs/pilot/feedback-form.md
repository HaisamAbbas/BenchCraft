# Pilot feedback form

One form per team and recipe. It measures two things:
- **setup effort:** how long it took to reach a first useful report, and what got in the
  way;
- **value over direct evaluator use:** what aibench gave you that calling an evaluation
  library or writing your own script would not.

Please answer from what you observed, not what you expect. "Don't know" is a valid answer.

Return it to the aibench maintainer who invited you. No answer is collected automatically,
and aibench sends nothing anywhere.

## A. About the pilot

| # | Question | Answer |
|---|---|---|
| A1 | Team / pseudonym | |
| A2 | Recipe used | ☐ A: HTTP RAG service ☐ B: command-line assistant ☐ other: |
| A3 | Application type (RAG, agent, chatbot, extraction, other) | |
| A4 | aibench version (`aibench --version`), OS and Python version | |
| A5 | How your team evaluates this application today | ☐ not at all ☐ manual review ☐ own scripts ☐ an evaluation library (which?) ☐ a hosted platform (which?) |
| A6 | Roughly how many cases in your dataset | |

## B. Setup effort

Record the clock time at each milestone. The facilitator may fill this in while observing
(section E).

| # | Milestone | Clock time | Minutes since start |
|---|---|---|---|
| B1 | Started (installation begins) | | 0 |
| B2 | `aibench --version` works | | |
| B3 | `app describe` / `app smoke` returns an answer from your application | | |
| B4 | Dataset validates | | |
| B5 | First complete run | | |
| B6 | First report you found useful | | |

| # | Question | Answer |
|---|---|---|
| B7 | Files you had to edit, and roughly how many lines | |
| B8 | Where you got stuck, and how long each blocker took | |
| B9 | Error messages that did not tell you what to do (paste them) | |
| B10 | Documentation you needed beyond the recipe | |
| B11 | Did you need help from the maintainer? For what? | |
| B12 | Setup effort compared with what you expected | ☐ much less ☐ less ☐ about the same ☐ more ☐ much more |

## C. Value compared with using an evaluator directly

"Directly" means what you would otherwise do: call an evaluation library (DeepEval, Ragas,
OpenAI Evals, …) from a script, or write your own loop.

| # | Question | Answer |
|---|---|---|
| C1 | Estimate the time to get the same first report by direct use (hours) | |
| C2 | Did the report show a problem you did not know about? Describe it | |
| C3 | Was the evidence for each failure (answer, reference, retrieved passages) enough to act on without re-running anything? | ☐ yes ☐ partly ☐ no — why: |
| C4 | Did any of these matter in practice? Tick what you used | ☐ resume after an interruption ☐ rescore without calling the app ☐ gaps shown instead of scores when evidence was missing ☐ release gate / exit code in CI ☐ cost shown as unknown instead of $0 ☐ none |
| C5 | Anything that got in the way which direct use would not have imposed (policy, trust flags, bindings, …) | |
| C6 | How likely is your team to keep using it for this application? (0 = not at all, 10 = certainly) | |
| C7 | What would make that a 10? | |

## D. Trust and safety

| # | Question | Answer |
|---|---|---|
| D1 | Did anything reach your application or leave your machine that you did not expect? | |
| D2 | Did any number in a report look wrong or unexplained? Which one? | |
| D3 | Did you share credentials in any file? (They should only be `env:` references.) | ☐ no ☐ yes — where: |

## E. Facilitator observation log (optional)

For observed sessions. Record facts, not interpretations.

| Clock time | What the participant did or said | Blocker? (Y/N) | Resolved how |
|---|---|---|---|
| | | | |

## F. How the answers are used

- **Kept as observed.** Answers are recorded in `docs/engineering/pilot-results.md`
  (created with the first returned form), keyed by pseudonym.
- **What counts as a trial.** A pilot counts as a real-team trial only when a form like
  this has come back from a team that ran a recipe on its own application.
- **Not trials.** Maintainer-run local trials against stand-in apps are reported
  separately and never counted as real-team trials.
