"""Redaction for conversation history and terminal rendering (§14, §16, 10-T3).

Two hazards, both from untrusted text (application outputs, dataset content, evaluator
reasons, model replies, pasted messages):

- credentials pasted into chat must not be stored or sent to a model: obvious key shapes
  are replaced (pattern-based, not a guarantee — credentials belong in secret references);
- terminal control content must not reach the user's terminal: escape sequences (CSI such
  as clear-screen or cursor moves, OSC such as window titles or hyperlinks) and other
  control characters are removed, keeping newlines and tabs. A value that clears the
  screen or rewrites earlier lines could otherwise forge what the harness displayed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_SECRETS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
    # key = value / key: value, also as a quoted JSON member or an environment variable
    # (OPENAI_API_KEY=..., "api_key": "...").
    re.compile(
        r"(?i)((?:[A-Z0-9]+_)*(?:api[_-]?key|token|secret|password)(?:_[A-Z0-9]+)*\"?)"
        r"(\s*[:=]\s*)(\"[^\"]*\"|\S+)"
    ),
)

# ESC-introduced sequences: CSI (ESC [ ... final), OSC (ESC ] ... BEL or ST), and other
# two-byte escapes; then the 8-bit C1 introducers and every remaining C0/C1 control except
# tab and newline.
_TERMINAL_SEQUENCES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"  # CSI
    # String sequences end at their terminator; an unterminated one ends at the line end
    # (bounded), so it can remove at most its own line, never the text after it.
    r"|\x1b\][^\x07\x1b\n]{0,4096}(?:\x07|\x1b\\)?"  # OSC, terminated by BEL or ST
    r"|\x1b[PX^_][^\x1b\n]{0,4096}(?:\x1b\\)?"  # DCS, SOS, PM, APC
    r"|\x1b[@-Z\\-_]"  # other two-byte escapes
    r"|\x9b[0-?]*[ -/]*[@-~]"  # 8-bit CSI
    r"|\x9d[^\x07\x9c\n]{0,4096}[\x07\x9c]?"  # 8-bit OSC
)
# C0/C1 controls except tab and newline, plus invisible formatting characters that can
# forge what is displayed: zero-width characters and bidirectional overrides/isolates.
_CONTROLS = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


def redact_secrets(text: str) -> str:
    for pattern in _SECRETS:
        text = pattern.sub(
            lambda m: f"{m.group(1)}{m.group(2)}[redacted]" if m.lastindex else "[redacted]",
            text,
        )
    return text


def strip_terminal_controls(text: str) -> str:
    """Remove escape sequences and control characters, keeping newlines and tabs. A lone
    carriage return is dropped, so it cannot overwrite the current line."""
    return _CONTROLS.sub("", _TERMINAL_SEQUENCES.sub("", text))


def sanitize(text: str) -> str:
    """Safe to store in conversation history, send to a model, or print. Redaction runs
    both before and after controls are removed: before, for a key set off by control
    characters (removing them could glue it to a preceding word); after, for a key that
    an escape or invisible character had split."""
    return redact_secrets(strip_terminal_controls(redact_secrets(text)))


def sanitize_value(value: Any) -> Any:
    """Sanitize every string in a JSON-shaped value before showing structured output.

    JSON formatting escapes terminal controls, but the original value would still be
    visible to anyone reading the output and credentials would remain exposed. Sanitize
    recursively so structured views follow the same policy as plain terminal text.
    """
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, Mapping):
        return {sanitize(str(key)): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    return value
