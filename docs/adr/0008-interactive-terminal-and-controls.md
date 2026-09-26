# ADR 0008: Interactive terminal and direct run controls

- Status: accepted
- Date: 2026-09-23
- Prompt: 09

## Context

Prompt 09 makes the terminal the primary conversational interface. It needs editable input
while model and application work continue, multiline entry, history, completion, streamed
replies and controls that do not depend on a provider. The project has a synchronous Typer
CLI and asynchronous session/run services. A full-screen UI or daemon is not required.

## Decision

- Use `prompt_toolkit`'s asynchronous `PromptSession` for the REPL, with `FileHistory`, a
  small slash-command completer and `Esc` then `Enter` for a newline. `Enter` submits the
  message. The REPL remains line-oriented and can be replaced without changing session or
  run services.
- Draw the input being typed as a box across the bottom, the way a chat terminal is expected
  to look, without giving up a prompt that stays editable under live output. The box is part
  of the prompt's own rendering — its left edge is the prompt's line prefix (repeated for
  every line of the input) and one input processor tints the line and fills it to the right
  edge — so nothing is printed, nothing is erased, and the conversation above it is untouched.
  Its colours come from the active theme, and a terminal that cannot print the marker's glyph
  falls back to an unbanded ASCII prompt.
- Keep the terminal input loop, assistant task and run scheduler on the existing asyncio
  loop. Blocking provider calls stay in worker threads. Deterministic slash controls call
  `SessionController` directly; they never use model intent inference.
- Stream provider text into the prompt display. Poll durable run events and coalesce
  progress output. Label a live snapshot provisional and an incomplete terminal run partial.
- Ctrl+C during a reply cancels that assistant task only. `/stop` sends the engine's
  explicit cancel control. `/exit`, Ctrl+D and graceful shutdown interrupt dispatch, drain
  in-flight work and leave pending work resumable. Reopening a session does not auto-run it.
- Keep `aibench chat --send TEXT --json` as the non-TTY interface. It waits for a run
  started by that exchange, while status and control commands return without waiting on a
  run they did not start.

## Consequences

The chosen library supplies editing, multiline bindings, history and completion without a
full-screen UI. Its tested local version is recorded in `pyproject.toml` and the dependency
lock. Windows ConPTY is the real-terminal smoke target available in this environment; this
does not establish terminal support on Linux or macOS. Partial run reports are explicit so
cancelled or budget-exhausted metric snapshots cannot look complete.
